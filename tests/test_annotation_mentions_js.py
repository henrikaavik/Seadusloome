"""Static guard tests for the @mention typeahead client (annotation_mentions.js).

This repo has no JS test runner, so — like ``tests/test_chat_js_ontology_rewrite.py``
— these tests read the shipped bundle and assert that the load-bearing
behaviour is present and wired up. They fail loudly if a refactor drops it.

Focus: issue #861-D. Each ``attach()`` registers three *global* listeners
(``document`` click, ``window`` resize, ``window`` scroll-capture) plus a
``<ul>`` appended to ``document.body``. When HTMX swaps a popover out the
textarea leaves the DOM but those globals + the list element leak. The file
already exposes an ``instance.destroy()`` that unwinds all of them; this
guards that a destroy path is actually wired to the HTMX swap lifecycle.
"""

from __future__ import annotations

from pathlib import Path

_MENTIONS_JS = (
    Path(__file__).resolve().parent.parent / "app" / "static" / "js" / "annotation_mentions.js"
)


def _source() -> str:
    return _MENTIONS_JS.read_text(encoding="utf-8")


def test_mentions_js_exists():
    assert _MENTIONS_JS.is_file(), f"missing {_MENTIONS_JS}"


def test_destroy_unwinds_all_global_listeners_and_list():
    """The destroy() teardown must remove every global listener + the list."""
    src = _source()
    destroy = src[src.index("destroy()") :]
    destroy = destroy[: destroy.index("};")]
    # All three global listeners registered in attach() must be removed.
    assert 'document.removeEventListener("click", onDocClick)' in destroy
    assert 'window.removeEventListener("resize", onScrollOrResize)' in destroy
    assert 'window.removeEventListener("scroll", onScrollOrResize, true)' in destroy
    # The orphan list element appended to <body> must be removed.
    assert "list.remove()" in destroy
    # And both instance registries must drop the entry.
    assert "INSTANCES.delete(textarea)" in destroy
    assert "LIVE_TEXTAREAS.delete(textarea)" in destroy


def test_live_textareas_registry_is_iterable():
    """A WeakMap can't be walked; an iterable registry must back the sweep."""
    src = _source()
    # An iterable Set tracks live textareas so HTMX cleanup can find them.
    assert "LIVE_TEXTAREAS = new Set()" in src
    # attach() must register the textarea in that iterable set.
    attach_body = src[src.index("function attach(textarea)") :]
    attach_body = attach_body[: attach_body.index("function attachAll")]
    assert "LIVE_TEXTAREAS.add(textarea)" in attach_body


def test_destroy_within_targets_swapped_subtree():
    """destroyWithin(root) must tear down instances inside the outgoing markup."""
    src = _source()
    assert "function destroyWithin(root)" in src
    body = src[src.index("function destroyWithin(root)") :]
    body = body[: body.index("function destroyDetached")]
    # Walks the iterable registry and destroys textareas inside root.
    assert "LIVE_TEXTAREAS" in body
    assert "root.contains(textarea)" in body
    # Snapshots the set first since destroy() mutates it during iteration.
    assert "Array.from(LIVE_TEXTAREAS)" in body


def test_destroy_wired_to_htmx_cleanup_events():
    """The destroy path must be bound to HTMX's swap/cleanup lifecycle."""
    src = _source()
    # Per-element removal hook — the precise HTMX cleanup event.
    assert 'addEventListener("htmx:beforeCleanupElement"' in src
    # Swap-target content replacement hook.
    assert 'addEventListener("htmx:beforeSwap"' in src
    # Both must invoke the targeted teardown.
    cleanup_idx = src.index('addEventListener("htmx:beforeCleanupElement"')
    cleanup_block = src[cleanup_idx : cleanup_idx + 200]
    assert "destroyWithin(ev.target)" in cleanup_block
    beforeswap_idx = src.index('addEventListener("htmx:beforeSwap"')
    beforeswap_block = src[beforeswap_idx : beforeswap_idx + 200]
    assert "destroyWithin(ev.target)" in beforeswap_block


def test_after_swap_sweeps_detached_then_rebinds():
    """afterSwap must sweep detached instances AND re-bind the new markup."""
    src = _source()
    idx = src.index('addEventListener("htmx:afterSwap"')
    block = src[idx : idx + 200]
    # Belt-and-braces detached sweep before the re-scan re-binds incoming nodes.
    assert "destroyDetached()" in block
    assert "attachAll(ev.target" in block


def test_destroy_helpers_exposed_for_debugging():
    """The public surface should expose the teardown helpers (matches attach/attachAll)."""
    src = _source()
    expose = src[src.index("window.AnnotationMentions") :]
    expose = expose[: expose.index("\n")]
    assert "destroyWithin" in expose
    assert "destroyDetached" in expose
