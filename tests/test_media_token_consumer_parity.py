"""Cross-consumer regression coverage for MEDIA token boundary parity (#6890)."""

from __future__ import annotations

import re
import urllib.parse
from types import SimpleNamespace
from unittest import mock

import pytest

from tests.test_renderer_js_behaviour import NODE, _DRIVER_SRC, _render

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


@pytest.fixture(scope="module")
def media_parity_driver(tmp_path_factory):
    path = tmp_path_factory.mktemp("media_parity_driver") / "driver.js"
    path.write_text(_DRIVER_SRC, encoding="utf-8")
    return str(path)


def _write_png(path):
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)


def test_wrapped_inner_punctuation_matches_renderer_share_auth_and_snapshot(
    media_parity_driver, tmp_path, monkeypatch
):
    from api import routes, shares
    from api.media_snapshots import annotate_media_snapshots

    image = tmp_path / "ok.png"
    _write_png(image)
    text = f"**MEDIA:{image}.**"
    encoded = urllib.parse.quote(str(image), safe="")

    rendered = _render(media_parity_driver, text)
    assert f"path={encoded}" in rendered
    assert f"path={encoded}." not in rendered
    assert ".</strong>" in rendered

    shared = shares._embed_share_media(text, allowed_roots=(tmp_path,))
    assert "data:image/png;base64," in shared
    assert shares._PLACEHOLDER not in shared
    assert shared.endswith(".**")

    session = SimpleNamespace(messages=[{"role": "assistant", "content": text}])
    with mock.patch.object(routes, "get_session", return_value=session):
        assert routes._session_media_token_allows_image_path(
            "s-media-parity", image, {"image/png"}
        )

    monkeypatch.setenv("MEDIA_ALLOWED_ROOTS", str(tmp_path))
    monkeypatch.setenv(
        "HERMES_WEBUI_MEDIA_SNAPSHOT_DIR", str(tmp_path / "media_snapshots")
    )
    messages = [{"role": "assistant", "content": text}]
    assert annotate_media_snapshots(messages) == 1
    snapshots = messages[0]["_media_snapshots"]
    assert str(image.resolve()) in snapshots
    assert len(snapshots[str(image.resolve())]) == 64


@pytest.mark.parametrize(
    ("entity_quote", "literal_quote"),
    [("&quot;", '"'), ("&#39;", "'")],
)
def test_entity_balanced_local_media_matches_renderer_stream_server_consumers(
    media_parity_driver, tmp_path, monkeypatch, entity_quote, literal_quote
):
    from api import routes, shares
    from api.helpers import split_media_token_ref
    from api.media_snapshots import annotate_media_snapshots

    image = tmp_path / "ok.png"
    _write_png(image)
    text = f"{entity_quote}MEDIA:{image}{entity_quote}."
    encoded = urllib.parse.quote(str(image), safe="")

    match = re.search(r"MEDIA:([^\s\)\]]+)", text)
    assert match is not None
    assert split_media_token_ref(text, match) == (str(image), f"{literal_quote}.")

    rendered = _render(media_parity_driver, text)
    assert f"path={encoded}" in rendered
    assert f"path={encoded}%26" not in rendered

    shared = shares._embed_share_media(text, allowed_roots=(tmp_path,))
    assert "data:image/png;base64," in shared
    assert shares._PLACEHOLDER not in shared

    session = SimpleNamespace(messages=[{"role": "assistant", "content": text}])
    with mock.patch.object(routes, "get_session", return_value=session):
        assert routes._session_media_token_allows_image_path(
            "s-media-entity-parity", image, {"image/png"}
        )

    monkeypatch.setenv("MEDIA_ALLOWED_ROOTS", str(tmp_path))
    monkeypatch.setenv(
        "HERMES_WEBUI_MEDIA_SNAPSHOT_DIR", str(tmp_path / "media_snapshots")
    )
    messages = [{"role": "assistant", "content": text}]
    assert annotate_media_snapshots(messages) == 1
    snapshots = messages[0]["_media_snapshots"]
    assert str(image.resolve()) in snapshots
    assert len(snapshots[str(image.resolve())]) == 64


@pytest.mark.parametrize("punctuation", [".", ",", "?"])
def test_sentence_punctuation_detaches_from_remote_file_url_while_server_consumers_bypass_remote_refs(
    media_parity_driver, tmp_path, punctuation
):
    from api import routes, shares
    from api.media_snapshots import annotate_media_snapshots

    clean_ref = "https://example.com/a.png"
    text = f"MEDIA:{clean_ref}{punctuation}"

    rendered = _render(media_parity_driver, text)
    assert f'src="{clean_ref}"' in rendered
    assert f'src="{clean_ref}{punctuation}"' not in rendered
    assert punctuation in rendered

    # Public-share embedding intentionally handles only local refs; a remote
    # token must pass through byte-for-byte instead of being normalized as a
    # local path.
    assert shares._embed_share_media(text, allowed_roots=(tmp_path,)) == text

    messages = [{"role": "assistant", "content": text}]
    assert annotate_media_snapshots(messages) == 0
    assert "_media_snapshots" not in messages[0]

    # Session-token authorization is likewise local-path-only. Feeding the same
    # remote transcript token must never authorize an unrelated local file.
    local_image = tmp_path / "a.png"
    _write_png(local_image)
    session = SimpleNamespace(messages=[{"role": "assistant", "content": text}])
    with mock.patch.object(routes, "get_session", return_value=session):
        assert not routes._session_media_token_allows_image_path(
            "s-media-parity", local_image, {"image/png"}
        )


@pytest.mark.parametrize("wrapped", [False, True])
def test_backtick_filename_matches_renderer_auth_and_snapshot(
    media_parity_driver, tmp_path, monkeypatch, wrapped
):
    from api import routes
    from api.media_snapshots import annotate_media_snapshots

    image = tmp_path / ("ok.png" if wrapped else "ok`final.png")
    _write_png(image)
    text = f"`MEDIA:{image}`" if wrapped else f"MEDIA:{image}"
    rendered = _render(media_parity_driver, text)
    encoded = urllib.parse.quote(str(image), safe="")
    assert f"path={encoded}" in rendered
    assert f"path={encoded}%60" not in rendered
    session = SimpleNamespace(messages=[{"role": "assistant", "content": text}])
    with mock.patch.object(routes, "get_session", return_value=session):
        assert routes._session_media_token_allows_image_path("backticks", image, {"image/png"})
    monkeypatch.setenv("MEDIA_ALLOWED_ROOTS", str(tmp_path))
    monkeypatch.setenv("HERMES_WEBUI_MEDIA_SNAPSHOT_DIR", str(tmp_path / "snapshots"))
    messages = [{"role": "assistant", "content": text}]
    assert annotate_media_snapshots(messages) == 1
    assert str(image.resolve()) in messages[0]["_media_snapshots"]


@pytest.mark.parametrize("suffix", ["!", ";", ":", "!!", ".!", ";."])
def test_remote_path_keeps_meaningful_trailing_bytes(media_parity_driver, suffix):
    ref = f"https://example.com/a.png{suffix}"
    assert f'src="{ref}"' in _render(media_parity_driver, f"MEDIA:{ref}")


@pytest.mark.parametrize("ref", ["_", "__", "*", "**", "`", "*_", "___"])
def test_unmatched_delimiter_only_filename_is_a_media_ref(media_parity_driver, ref):
    from api.helpers import split_media_token_ref

    text = f"MEDIA:{ref}"
    match = re.search(r"MEDIA:([^\s\)\]]+)", text)
    assert split_media_token_ref(text, match) == (ref, "")
    encoded = urllib.parse.quote(ref, safe="").replace("%2A", "*")
    assert f"api/media?path={encoded}" in _render(media_parity_driver, text)


@pytest.mark.parametrize("delimiter", ["*", "**", "***", "_", "__", "___", "`"])
def test_matching_empty_wrapper_is_not_a_media_ref(media_parity_driver, delimiter):
    from api.helpers import split_media_token_ref

    text = f"{delimiter}MEDIA:{delimiter}"
    match = re.search(r"MEDIA:([^\s\)\]]+)", text)
    assert split_media_token_ref(text, match) is None
    assert "api/media?path=" not in _render(media_parity_driver, text)


@pytest.mark.parametrize("suffix", ["?signature=!", "?signature=?", "#", "#?"])
def test_remote_query_and_fragment_bytes_stay_intact(media_parity_driver, suffix):
    ref = f"https://example.com/a.png{suffix}"
    assert f'src="{ref}"' in _render(media_parity_driver, f"MEDIA:{ref}")


@pytest.mark.parametrize("closer", [")", "]"])
def test_unwrapped_remote_period_requires_sentence_boundary(media_parity_driver, closer):
    ref = "https://example.com/a.png."
    assert f'src="{ref}"' in _render(media_parity_driver, f"MEDIA:{ref}{closer}")
