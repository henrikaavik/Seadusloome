"""Guard test for the client-side estleg: URI rewrite in chat.js.

The server-side sanitizer (``app/chat/sanitize.py::_rewrite_ontology_uris``)
rewrites bare ``data.riik.ee/ontology/estleg#…`` links to in-app
``/explorer?focus=`` deep links, but live WebSocket replies are rendered
in the browser by ``app/static/js/chat.js`` (marked + DOMPurify +
linkifyCitations). That client path must mirror the same rewrite, or a
streamed reply shows a dead external link until reload.

There is no JS test runner in this repo, so this is a static guard that
reads the bundle and asserts the rewrite logic is present and wired into
the streaming render path. It fails loudly if someone removes it.
"""

from __future__ import annotations

from pathlib import Path

_CHAT_JS = Path(__file__).resolve().parent.parent / "app" / "static" / "js" / "chat.js"


def _source() -> str:
    return _CHAT_JS.read_text(encoding="utf-8")


def test_chat_js_exists():
    assert _CHAT_JS.is_file(), f"missing {_CHAT_JS}"


def test_defines_estleg_uri_matcher():
    src = _source()
    # A regex bound to the estleg namespace host must exist.
    assert "ESTLEG_URI_RE" in src
    assert "data\\.riik\\.ee" in src
    assert "ontology" in src and "estleg" in src


def test_defines_rewrite_function_targeting_explorer_focus():
    src = _source()
    assert "function rewriteOntologyUris" in src
    # Rewrites to the in-app explorer focus link, not an external page.
    assert "/explorer?focus=" in src
    assert "encodeURIComponent(href)" in src
    # Drops the external link attributes for the now same-origin anchor.
    assert "removeAttribute('target')" in src


def test_rewrite_is_wired_into_render_buffer():
    src = _source()
    # The streaming render path (renderBuffer) must invoke the rewrite,
    # after DOMPurify/marked produced the autolinked anchor.
    render = src[src.index("function renderBuffer") :]
    render = render[: render.index("function rewriteOntologyUris")]
    assert "rewriteOntologyUris(" in render
