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


# ---------------------------------------------------------------------------
# Finding #861-A — escape the fallback tool label before innerHTML.
#
# ``toolLabel()`` returns ``TOOL_LABELS[name] || name``; for an unknown tool
# the raw, server-supplied name is returned and then interpolated into the
# tool-activity <summary> via innerHTML. That value MUST be HTML-escaped or a
# crafted tool name injects markup into the chat transcript.
# ---------------------------------------------------------------------------


def _slice_fn(src: str, name: str) -> str:
    """Return the source of one function declaration up to the next one."""
    start = src.index("function " + name)
    rest = src[start + len("function " + name) :]
    nxt = rest.find("\n  function ")
    return rest if nxt < 0 else rest[:nxt]


def test_tool_activity_summary_escapes_fallback_label():
    body = _slice_fn(_source(), "createToolActivity")
    # The innerHTML sink for the label must pass through escapeHtml.
    assert "summary.innerHTML" in body
    assert "escapeHtml(label)" in body
    # And must NOT interpolate the raw label into innerHTML anymore.
    assert "spinner + label" not in body


# ---------------------------------------------------------------------------
# Finding #861-B — clear the stale thinking indicator on WS reconnect.
#
# When the socket dies mid-stream, the elapsed ticker + watchdog timers and
# the animated "Mõtlen..." bubble belong to a turn the server will not
# resume. The reconnect ``open`` handler must call clearThinking() so those
# orphaned timers/visuals don't survive into the recovered session.
# ---------------------------------------------------------------------------


def test_reconnect_clears_thinking_indicator():
    src = _source()
    # Isolate the open handler's broken-mid-stream recovery block.
    open_idx = src.index("ws.addEventListener('open'")
    close_idx = src.index("ws.addEventListener('close'")
    open_block = src[open_idx:close_idx]
    assert "brokenMidStream" in open_block
    # The recovery branch must tear down the thinking timers/visuals.
    recovery = open_block[open_block.index("if (brokenMidStream)") :]
    assert "clearThinking()" in recovery


# ---------------------------------------------------------------------------
# Finding #861-C — gate sendMessage/replayFromPivot on the streaming flag.
#
# Starting a new turn (send, follow-up chip, regenerate, edit) while a reply
# is still streaming interleaves deltas and corrupts the per-message buffers.
# Both entry points must bail when ``streaming`` is true; this composes with
# stop-generation (the user stops the live reply first).
# ---------------------------------------------------------------------------


def test_send_message_gated_on_streaming():
    body = _slice_fn(_source(), "sendMessage")
    # The very first guard inside sendMessage must short-circuit on streaming.
    assert "if (streaming) return;" in body


def test_replay_from_pivot_gated_on_streaming():
    body = _slice_fn(_source(), "replayFromPivot")
    # replayFromPivot trims sibling bubbles + resets buffers, so it must bail
    # BEFORE any DOM mutation when a reply is still streaming.
    assert "if (streaming)" in body
    guard = body[: body.index("nextElementSibling")]
    assert "if (streaming)" in guard


def test_follow_up_chip_gated_on_streaming():
    body = _slice_fn(_source(), "appendFollowUps")
    # The follow-up chip click handler must not fire a send mid-stream.
    assert "if (!streaming) sendMessage()" in body
