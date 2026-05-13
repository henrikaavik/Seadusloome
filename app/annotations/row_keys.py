"""§9.4 row-key formulas for impact-report annotation threads.

Locked-in contract from #619 PR-A:

    target_type = 'impact_report_item'
    target_id   = '{row_kind}:{row_key}'

row_key formulas:
    entity   → entity URI from ontology
    eu       → EU directive URI
    conflict → sha256(canonical_json([sorted_subject_uri,
               sorted_object_uri, predicate_uri]))[:32]
    gap      → sha256(canonical_json([gap_kind, sorted_required_uris]))[:32]

Lives in :mod:`app.annotations` (not :mod:`app.docs`) because the keys
ARE the annotation contract: both the report renderer (UI side) and the
analyze pipeline (stale-flag side) consume them, and pulling them out
of :mod:`app.docs.report_routes` keeps the analyze handler free of the
FastHTML/UI import baggage.

URL + CSS safety (#773):

The ``entity`` and ``eu`` row keys are raw ontology URIs that contain
``/``, ``:``, and ``#`` characters. Embedding them directly into URL
path segments (e.g. ``/annotations/version/<v>/<kind>/<row_key>``) or
CSS id selectors (``#annotation-popover-entity-<row_key>``) breaks
routing and DOM lookups. Three helpers mediate the boundary:

* :func:`safe_row_key` — percent-encode for URL path embedding.
* :func:`decode_row_key` — server-side decode at the handler boundary.
* :func:`target_dom_id` — derive a CSS-safe DOM id from any raw target.

The original URI stays the round-trip identity; only the URL path
segment and the DOM id use the encoded / hashed form.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any
from urllib.parse import quote, unquote


def stable_hash(parts: list[str]) -> str:
    """Return the first 32 hex chars of sha256 over a canonical JSON list.

    ``ensure_ascii=False`` + ``sort_keys=True`` matches the §9.4 contract so
    server + client produce the same digest given the same logical inputs
    even when strings contain non-ASCII Estonian characters.
    """
    raw = json.dumps(parts, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def row_key_for_entity(entity: dict[str, Any]) -> str:
    """row_key for an affected-entities row: the entity URI itself."""
    return str(entity.get("uri") or "")


def row_key_for_eu(eu: dict[str, Any]) -> str:
    """row_key for an EU-compliance row: the EU directive URI.

    Mirrors the docx exporter which also reads ``eu_act`` (the URI) as the
    primary identity field.
    """
    return str(eu.get("eu_act") or "")


def row_key_for_conflict(conflict: dict[str, Any]) -> str:
    """row_key for a conflict row: deterministic sha256-32 over the conflict identity.

    The analyzer emits ``draft_ref`` + ``conflicting_entity`` (sorted), with
    ``reason`` truncated to 64 chars as a tie-breaker so two conflicts on
    the same pair of entities but with different reasons get different
    threads.
    """
    parts = sorted(
        [
            str(conflict.get("conflicting_entity") or ""),
            str(conflict.get("draft_ref") or ""),
        ]
    ) + [str(conflict.get("reason") or "")[:64]]
    return stable_hash(parts)


def row_key_for_gap(gap: dict[str, Any]) -> str:
    """row_key for a gap row: deterministic sha256-32 over the gap identity.

    Currently keyed on ``topic_cluster`` (the cluster URI). The "gap_kind"
    discriminator stays as a static string for now because the analyzer only
    produces one kind of gap; future expansion can add more discriminators
    without invalidating existing keys (the JSON-canonical form keeps the
    sort order stable).
    """
    parts = ["gap_topic_cluster", str(gap.get("topic_cluster") or "")]
    return stable_hash(parts)


def collect_row_specs(findings: dict[str, Any]) -> list[tuple[str, str]]:
    """Walk every section and emit (row_kind, row_key) pairs.

    Used by both the report renderer (to bulk-load badge counts) and the
    analyze handler (to drive stale-flag reconciliation).  Returns rows in
    section order with empty keys filtered out.
    """
    specs: list[tuple[str, str]] = []
    for entity in findings.get("affected_entities") or []:
        key = row_key_for_entity(entity)
        if key:
            specs.append(("entity", key))
    for conflict in findings.get("conflicts") or []:
        key = row_key_for_conflict(conflict)
        if key:
            specs.append(("conflict", key))
    for eu in findings.get("eu_compliance") or []:
        key = row_key_for_eu(eu)
        if key:
            specs.append(("eu", key))
    for gap in findings.get("gaps") or []:
        key = row_key_for_gap(gap)
        if key:
            specs.append(("gap", key))
    return specs


# ---------------------------------------------------------------------------
# URL- and CSS-safety helpers (#773)
# ---------------------------------------------------------------------------


def safe_row_key(raw: str) -> str:
    """Percent-encode a row_key for safe embedding in URL path segments.

    Entity and EU row keys are raw ontology URIs like
    ``https://data.riik.ee/ontology/estleg#KarS`` — they contain ``/``,
    ``:``, and ``#`` which a browser treats as path separators / fragment
    markers if left unescaped, so the path segment never reaches the
    server intact.  ``quote(..., safe="")`` percent-encodes every reserved
    character so the encoded form is a single, opaque path segment that
    round-trips through :func:`decode_row_key`.

    Hashed row_keys (conflict / gap) are sha256-32 hex digests so this is
    a no-op for them — they only contain ``[0-9a-f]`` — but applying the
    helper uniformly keeps the call sites simple.
    """
    return quote(str(raw or ""), safe="")


def decode_row_key(quoted: str) -> str:
    """Reverse :func:`safe_row_key` server-side at the route handler boundary.

    Starlette decodes a path segment once by default, but ``%23`` in the
    raw URI (``#``) survives the first decode step because routing
    matches on the *encoded* segment when the path includes reserved
    characters. The handler should call ``decode_row_key`` to recover
    the original URI before reading the DB / passing to the renderer.
    """
    return unquote(str(quoted or ""))


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
