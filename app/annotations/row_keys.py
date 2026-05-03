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
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


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
