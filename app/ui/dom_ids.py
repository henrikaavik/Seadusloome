"""CSS-safe DOM id derivation for UI surfaces.

This is a *UI-layer* concern: turning an arbitrary application identifier
(typically a raw ontology URI containing ``/``, ``:``, ``#``, and ``%``)
into a string that is simultaneously a valid ``getElementById`` id, a valid
CSS selector, and a valid HTMX ``hx-target="#..."`` query.

It lives in :mod:`app.ui` (alongside :mod:`app.ui.safe_url`) rather than in
a feature module so that the :mod:`app.ui` primitives that need it —
``AnnotationButton`` (``app.ui.primitives.annotation_button``) and
``AnnotationPopover`` (``app.ui.surfaces.annotation_popover``) — can import
it without reaching back into a feature package (the disallowed ui→feature
direction, #860). The annotations feature re-exports it from
:mod:`app.annotations.row_keys` for its own internal callers (the allowed
feature→ui direction).

The output shape is a locked-in contract: live DOM ids and the
``hx-target`` selectors that pair the button + popover surfaces depend on
it, and its format is pinned by tests. Do not change the hashing length or
the ``annotation-popover-{kind}-{digest}`` template without coordinating a
DOM migration.
"""

from __future__ import annotations

import hashlib


def target_dom_id(target_kind: str, target_id: str) -> str:
    """Return a CSS-safe DOM id for an annotation container.

    The id must be selectable via ``getElementById`` AND via CSS
    selectors / HTMX ``hx-target="#..."`` queries.  Raw ontology URIs
    contain ``/``, ``:``, ``#``, and ``%`` (after percent-encoding) —
    all of which are CSS structural characters that need backslash
    escapes inside a selector.  Hashing the raw ``target_id`` into a
    sha256-truncated hex digest sidesteps the problem entirely: the
    result is ``[0-9a-f]+`` and therefore always a valid CSS identifier.

    The 16-char digest gives ~64 bits of collision resistance which is
    plenty for one report page worth of rows.  Data attributes still
    carry the original URI for round-trip identification — only the
    DOM id is the hashed form.
    """
    raw = f"{target_kind}:{target_id or ''}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    return f"annotation-popover-{target_kind}-{digest}"
