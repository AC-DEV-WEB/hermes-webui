"""WebUI sessions must record their workspace as ``sessions.cwd`` in state.db.

The Agent creates the state.db row lazily during ``run_conversation()``, but
``run_agent._launch_cwd_for_session`` only stamps ``cwd`` for CLI-family
sources. WebUI rows therefore kept an empty ``cwd`` and Hermes Desktop, which
groups sessions by ``cwd``/``git_repo_root``, filed them under "Home" instead
of the workspace they were created in.

``_FakeSessionDB`` mirrors the three SessionDB methods the helper uses, with
the Agent's ``update_session_cwd`` semantics (row must exist, the generation
is bumped on every write, git metadata is cleared on a cwd change), so the
behavioural tests run in CI where hermes-agent is not installed. The last
test repeats the core check against the real SessionDB when it is importable.
"""

from __future__ import annotations

import queue
import sys
import types
from unittest import mock
from urllib.parse import urlparse

import pytest

import api.config as config
import api.models as models
import api.state_sync as state_sync
import api.streaming as streaming
from api.models import Session


class _FakeSessionDB:
    """In-memory stand-in for hermes_state.SessionDB (the subset used here)."""

    def __init__(self):
        self.rows: dict[str, dict] = {}
        self.closed = False
        self.cwd_writes = 0

    def create_session(self, session_id, source="webui", cwd=None, **_kw):
        self.rows.setdefault(session_id, {
            "id": session_id, "source": source, "cwd": cwd,
            "git_branch": None, "git_repo_root": None, "git_metadata_generation": 0,
        })

    def get_session(self, session_id):
        row = self.rows.get(session_id)
        return dict(row) if row else None

    def update_session_cwd(self, session_id, cwd, git_branch=None, git_repo_root=None, replace_git_meta=False):
        row = self.rows.get(session_id)
        if not session_id or not cwd or row is None:
            return None
        if row["cwd"] != cwd or replace_git_meta:
            row["git_branch"] = git_branch or None
            row["git_repo_root"] = git_repo_root or None
        row["cwd"] = cwd
        row["git_metadata_generation"] += 1
        self.cwd_writes += 1
        return row["git_metadata_generation"]

    def close(self):
        self.closed = True


@pytest.fixture
def fake_db(monkeypatch):
    db = _FakeSessionDB()
    monkeypatch.setattr(state_sync, "_get_state_db", lambda profile=None: db)
    return db


# ── helper contract ─────────────────────────────────────────────────────────


def test_fills_empty_cwd_on_agent_created_row(fake_db):
    fake_db.create_session("sess-ws", source="webui", cwd=None)
    assert state_sync.sync_session_cwd("sess-ws", "/home/u/workspace", profile="default") is True
    assert fake_db.rows["sess-ws"]["cwd"] == "/home/u/workspace"
    assert fake_db.closed is True  # handle it opened is released


def test_never_creates_a_row(fake_db):
    """A session that never sent a message must stay out of state.db."""
    assert state_sync.sync_session_cwd("sess-empty", "/home/u/workspace") is False
    assert fake_db.rows == {}


def test_workspace_change_moves_row_and_clears_stale_git_meta(fake_db):
    fake_db.create_session("sess-move")
    fake_db.update_session_cwd("sess-move", "/repo/a", git_branch="main", git_repo_root="/repo/a")
    assert state_sync.sync_session_cwd("sess-move", "/home/u/other") is True
    row = fake_db.rows["sess-move"]
    assert row["cwd"] == "/home/u/other"
    assert row["git_repo_root"] is None and row["git_branch"] is None


def test_idempotent_and_trailing_slash_normalised(fake_db):
    fake_db.create_session("sess-slash")
    assert state_sync.sync_session_cwd("sess-slash", "/home/u/customers/acme/") is True
    assert fake_db.rows["sess-slash"]["cwd"] == "/home/u/customers/acme"
    generation = fake_db.rows["sess-slash"]["git_metadata_generation"]
    assert state_sync.sync_session_cwd("sess-slash", "/home/u/customers/acme") is False
    assert state_sync.sync_session_cwd("sess-slash", "/home/u/customers/acme/") is False
    assert fake_db.rows["sess-slash"]["git_metadata_generation"] == generation


def test_root_workspace_is_kept(fake_db):
    fake_db.create_session("sess-root")
    assert state_sync.sync_session_cwd("sess-root", "/") is True
    assert fake_db.rows["sess-root"]["cwd"] == "/"


def test_reuses_caller_db_without_closing_it(monkeypatch):
    """The streaming path passes the Agent's own profile-bound SessionDB."""
    monkeypatch.setattr(
        state_sync, "_get_state_db",
        lambda profile=None: pytest.fail("must not open a second handle when db= is given"),
    )
    db = _FakeSessionDB()
    db.create_session("sess-shared")
    assert state_sync.sync_session_cwd("sess-shared", "/home/u/workspace", db=db) is True
    assert db.closed is False


def test_blank_input_missing_db_and_db_errors_are_swallowed(monkeypatch):
    monkeypatch.setattr(state_sync, "_get_state_db", lambda profile=None: None)
    assert state_sync.sync_session_cwd("", "/x") is False
    assert state_sync.sync_session_cwd("sid", "") is False
    assert state_sync.sync_session_cwd("sid", None) is False
    assert state_sync.sync_session_cwd("sid", "/x") is False

    broken = _FakeSessionDB()
    broken.create_session("sid")
    broken.update_session_cwd = mock.Mock(side_effect=RuntimeError("database is locked"))
    assert state_sync.sync_session_cwd("sid", "/x", db=broken) is False


# ── streaming worker: every exit path ───────────────────────────────────────


@pytest.fixture
def stream_env(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(streaming, "_attempt_credential_self_heal", lambda *a, **k: None)
    for registry in (config.STREAMS, config.CANCEL_FLAGS, config.AGENT_INSTANCES,
                     config.STREAM_PARTIAL_TEXT, config.SESSION_AGENT_LOCKS, models.SESSIONS):
        registry.clear()

    fake_runtime = types.ModuleType("hermes_cli.runtime_provider")
    fake_runtime.resolve_runtime_provider = lambda requested=None, **_kw: {
        "provider": requested or "test-provider", "api_key": "synthetic-key", "base_url": None,
    }
    fake_cli = types.ModuleType("hermes_cli")
    fake_cli.runtime_provider = fake_runtime
    fake_state = types.ModuleType("hermes_state")
    fake_state.SessionDB = mock.Mock(return_value=None)
    monkeypatch.setitem(sys.modules, "hermes_cli", fake_cli)
    monkeypatch.setitem(sys.modules, "hermes_cli.runtime_provider", fake_runtime)
    monkeypatch.setitem(sys.modules, "hermes_state", fake_state)

    workspace = tmp_path / "ws" / "project"
    workspace.mkdir(parents=True)
    yield workspace
    for registry in (config.STREAMS, config.CANCEL_FLAGS, config.AGENT_INSTANCES,
                     config.STREAM_PARTIAL_TEXT, config.SESSION_AGENT_LOCKS, models.SESSIONS):
        registry.clear()


def _agent_class(db, outcome):
    """An Agent double that, like the real one, creates the row with cwd=None."""

    class _Agent:
        def __init__(self, **kwargs):
            self.session_id = kwargs.get("session_id")
            self._session_db = db
            self.stream_delta_callback = kwargs.get("stream_delta_callback")
            self.session_prompt_tokens = 0
            self.session_completion_tokens = 0
            self.session_estimated_cost_usd = 0.0
            self.context_compressor = None
            self._last_error = None
            self.ephemeral_system_prompt = None

        def run_conversation(self, **kwargs):
            db.create_session(self.session_id, source="webui", cwd=None)
            history = list(kwargs.get("conversation_history") or [])
            if outcome == "raise":
                raise RuntimeError("provider exploded")
            if outcome == "error":
                return {"messages": history, "error": {"error": {
                    "type": "authentication_error", "status_code": 401,
                    "code": "auth_unavailable", "message": "token invalidated"}}}
            if outcome == "cancel":
                config.CANCEL_FLAGS[self._stream_id].set()
            return {"status": "ok",
                    "messages": history + [{"role": "assistant", "content": "hello"}]}

        def interrupt(self, _message):
            pass

    return _Agent


def _run_stream(db, workspace, outcome, sid):
    stream_id = f"stream-{sid}"
    session = Session(session_id=sid, title="t")
    session.messages, session.context_messages = [], []
    session.pending_user_message = "hi"
    session.pending_started_at = 1.0
    session.active_stream_id = stream_id
    session.save()
    models.SESSIONS[sid] = session
    config.STREAMS[stream_id] = queue.Queue()
    config.STREAM_PARTIAL_TEXT[stream_id] = ""

    agent_cls = _agent_class(db, outcome)
    original_init = agent_cls.__init__

    def _init(self, **kwargs):
        original_init(self, **kwargs)
        self._stream_id = stream_id

    agent_cls.__init__ = _init
    with mock.patch.object(streaming, "get_session", return_value=session), \
         mock.patch.object(streaming, "_get_ai_agent", return_value=agent_cls), \
         mock.patch.object(streaming, "resolve_model_provider", return_value=("test-model", "test-provider", None)), \
         mock.patch("api.config.get_config", return_value={}), \
         mock.patch("api.config._resolve_cli_toolsets", return_value=[]):
        streaming._run_agent_streaming(
            session_id=sid, msg_text="hi", model="test-model",
            workspace=str(workspace), stream_id=stream_id,
        )


@pytest.mark.parametrize("outcome", ["success", "error", "raise", "cancel"])
def test_streaming_turn_records_workspace_on_every_exit(stream_env, monkeypatch, outcome):
    """The row the Agent created must carry the workspace however the turn ends."""
    db = _FakeSessionDB()
    # The worker must use the Agent's own (profile-bound) handle, not open one.
    monkeypatch.setattr(state_sync, "_get_state_db", lambda profile=None: None)
    sid = f"stream-{outcome}"
    _run_stream(db, stream_env, outcome, sid)
    assert sid in db.rows, "the Agent double should have created the row"
    assert db.rows[sid]["cwd"] == str(stream_env)


# ── synchronous /api/chat and workspace change ──────────────────────────────


def test_sync_chat_records_workspace_even_when_the_turn_raises(tmp_path, monkeypatch, fake_db):
    import api.routes as routes

    workspace = tmp_path / "ws"
    workspace.mkdir()
    session = Session(session_id="sync-sid", title="t")
    session.workspace = str(workspace)

    def _agent_created_row_then_failed():
        fake_db.create_session("sync-sid", source="webui", cwd=None)
        raise RuntimeError("agent unavailable")

    monkeypatch.setattr(routes, "_agent_runtime_barrier_response", lambda **_k: None)
    monkeypatch.setattr(routes, "get_session", lambda _sid: session)
    monkeypatch.setattr(routes, "resolve_trusted_workspace", lambda ws, **_k: ws)
    monkeypatch.setattr(routes, "_read_profile_model_config", lambda *_a, **_k: (None, None, None))
    monkeypatch.setattr(
        routes, "_resolve_compatible_session_model_state",
        lambda model, provider, **_k: (model, provider),
    )
    monkeypatch.setattr(routes, "require_ai_agent_class", _agent_created_row_then_failed)

    with pytest.raises(RuntimeError, match="agent unavailable"):
        routes._handle_chat_sync(object(), {"session_id": "sync-sid", "message": "hi"})
    assert fake_db.rows["sync-sid"]["cwd"] == str(workspace)


def test_workspace_update_moves_existing_row(tmp_path, monkeypatch, fake_db):
    import api.routes as routes

    old_ws, new_ws = tmp_path / "a", tmp_path / "b"
    old_ws.mkdir()
    new_ws.mkdir()
    session = Session(session_id="upd-sid", title="t")
    session.workspace = str(old_ws)
    session.save = lambda *a, **k: None
    fake_db.create_session("upd-sid", source="webui", cwd=str(old_ws))

    captured = {}
    monkeypatch.setattr(routes, "_check_csrf", lambda _h: True, raising=False)
    monkeypatch.setattr(routes, "read_body", lambda _h: {"session_id": "upd-sid", "workspace": str(new_ws)})
    monkeypatch.setattr(routes, "_get_or_materialize_session", lambda _sid: session)
    monkeypatch.setattr(routes, "resolve_trusted_workspace", lambda ws, **_k: ws)
    monkeypatch.setattr(routes, "set_last_workspace", lambda *a, **k: None)
    monkeypatch.setattr(routes, "j", lambda _h, obj, *a, **k: captured.setdefault("ok", obj) or True)
    monkeypatch.setattr(routes, "bad", lambda _h, msg, code=400: captured.setdefault("bad", (msg, code)) or True)

    class _Handler:
        headers = {"Content-Type": "application/json"}
        command = "POST"
        path = "/api/session/update"
        client_address = ("127.0.0.1", 0)

    routes.handle_post(_Handler(), urlparse("/api/session/update"))
    assert "bad" not in captured, captured.get("bad")
    assert fake_db.rows["upd-sid"]["cwd"] == str(new_ws)


# ── against the real Agent SessionDB when available ─────────────────────────


@pytest.mark.requires_agent_modules
def test_real_session_db_row_gets_workspace(tmp_path):
    hermes_state = pytest.importorskip("hermes_state")
    db = hermes_state.SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session(session_id="real", source="webui", model="m", cwd=None)
        assert state_sync.sync_session_cwd("real", "/home/u/workspace/", db=db) is True
        row = db.get_session("real")
        assert row["cwd"] == "/home/u/workspace"
        assert state_sync.sync_session_cwd("real", "/home/u/workspace", db=db) is False
        assert state_sync.sync_session_cwd("missing", "/home/u/workspace", db=db) is False
        assert db.get_session("missing") is None
    finally:
        db.close()
