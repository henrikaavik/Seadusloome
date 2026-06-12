"""Normi mõjuahel — framework-free service function (#860, Phase-5 reference).

``analyse_normi_mojuahel(sisend, *, org_id)`` resolves a free-text legal
reference and returns a typed result describing one of four outcomes:

* :class:`NormiDraftBackedResult` — the input is a UUID of a draft the org
  owns that has a persisted ``impact_reports`` row; the findings come straight
  from that row (no synthetic graph).
* :class:`NormiAdhocResult` — the input resolved to exactly one ontology
  entity; the impact analyser ran against an ephemeral synthetic named graph
  (always torn down) and produced :class:`ImpactFindings` + a score.
* :class:`NormiDisambiguation` — the input resolved to several plausible
  entities; the caller should offer them as choices.
* :class:`NormiUnresolved` — nothing resolved; the caller should show a
  "no structured reference" hint (optionally with RAG candidates, which the
  route layer fetches — RAG is a rendering-time nicety, not core to the
  typed result).

There are **no** ``fasthtml`` / ``starlette`` imports here. A dead Jena (or
any resolver/analyser crash) degrades to :class:`NormiUnresolved` /
:class:`NormiAdhocResult` with empty findings, exactly as the route expects —
it is never an HTTP error.

The route (``app/analyysikeskus/routes/_normi.py``) wraps this by matching on
the result type and rendering each branch through ``analysis_result_shell``;
a Phase-5 REST endpoint / MCP tool would serialise the dataclass to JSON.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from app.analyysikeskus.adhoc_analysis import run_adhoc_impact_analysis
from app.analyysikeskus.input_parser import parse_user_reference
from app.db import get_connection as _connect
from app.docs.reference_resolver import ReferenceResolver
from app.impact.analyzer import ImpactFindings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Typed results — a small discriminated union over the ``kind`` field
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NormiCandidate:
    """One resolved-reference candidate for the disambiguation outcome.

    ``label`` is the human label; ``ref`` is the search-box text a re-run
    should submit (the original ``ExtractedRef.ref_text`` when available).
    """

    label: str
    ref: str


@dataclass(frozen=True)
class NormiDraftBackedResult:
    """The input was a UUID of an owned draft with a persisted impact report."""

    kind: str = field(default="draft_backed", init=False)
    draft_id: str = ""
    draft_title: str = ""
    findings: ImpactFindings = field(default_factory=ImpactFindings)
    score: int = 0


@dataclass(frozen=True)
class NormiAdhocResult:
    """The input resolved to one entity; ad-hoc impact analysis ran on it."""

    kind: str = field(default="adhoc", init=False)
    entity_uri: str = ""
    label: str = ""
    type_label: str = ""
    findings: ImpactFindings = field(default_factory=ImpactFindings)
    score: int = 0


@dataclass(frozen=True)
class NormiDisambiguation:
    """The input resolved to several plausible entities."""

    kind: str = field(default="disambiguation", init=False)
    candidates: list[NormiCandidate] = field(default_factory=list)


@dataclass(frozen=True)
class NormiUnresolved:
    """Nothing resolved — the caller shows a 'no structured reference' hint."""

    kind: str = field(default="unresolved", init=False)


# The discriminated-union alias the route matches on.
NormiResult = NormiDraftBackedResult | NormiAdhocResult | NormiDisambiguation | NormiUnresolved


# ---------------------------------------------------------------------------
# Internal composition helpers (framework-free)
# ---------------------------------------------------------------------------


def _try_parse_uuid(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(value)
    except (ValueError, TypeError):
        return None


def _resolve_refs(refs: list[Any]) -> list[Any]:
    """Resolve parsed refs to ontology URIs; an unreachable Jena yields ``[]``."""
    if not refs:
        return []
    try:
        return ReferenceResolver().resolve(refs)
    except Exception:
        logger.warning("Normi mõjuahel service: reference resolution failed", exc_info=True)
        return []


def _resolved_label(resolved: Any, fallback: str) -> str:
    """Best human label for a resolved ref."""
    label = getattr(resolved, "matched_label", None)
    if label and str(label).strip():
        return str(label).strip()
    extracted = getattr(resolved, "extracted", None)
    if extracted is not None and getattr(extracted, "ref_text", None):
        return str(extracted.ref_text).strip()
    return fallback


def _resolved_type_label(resolved: Any) -> str:
    """Estonian type label for a resolved ref's ref_type, or "" if unknown."""
    extracted = getattr(resolved, "extracted", None)
    ref_type = getattr(extracted, "ref_type", "") if extracted is not None else ""
    return {
        "law": "seadus",
        "provision": "säte",
        "eu_act": "EL õigusakt",
        "court_decision": "kohtulahend",
        "concept": "õigusmõiste",
    }.get(str(ref_type), "")


def _load_owned_draft_report(draft_uuid: uuid.UUID, org_id: str | None) -> tuple | None:
    """Return the latest ``impact_reports`` row for an owned draft, or ``None``.

    ``(draft_id, draft_title, draft_version_id, report_data, impact_score)``
    only when *draft_uuid* is a draft the org owns that has a report. Any DB
    error / missing report ⇒ ``None`` (the caller falls through to the parse
    path). Best-effort, framework-free.
    """
    if not org_id:
        return None
    try:
        with _connect() as conn:
            row = conn.execute(
                """
                SELECT d.id, d.title, ir.draft_version_id, ir.report_data, ir.impact_score
                FROM drafts d
                JOIN impact_reports ir ON ir.draft_id = d.id
                WHERE d.id = %s AND d.org_id = %s
                ORDER BY ir.generated_at DESC
                LIMIT 1
                """,
                (str(draft_uuid), str(org_id)),
            ).fetchone()
    except Exception:
        logger.warning(
            "Normi mõjuahel service: failed to load owned-draft report for draft=%s",
            draft_uuid,
            exc_info=True,
        )
        return None
    return row


def _parse_report_data(raw: Any) -> dict[str, Any]:
    """Normalise a JSONB ``report_data`` value into a dict."""
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, (bytes, bytearray)):
        try:
            return json.loads(raw.decode())
        except (TypeError, ValueError, UnicodeDecodeError):
            return {}
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return {}
    return {}


def _findings_from_report_data(
    data: dict[str, Any],
    *,
    viewer_org_id: str | None = None,
) -> ImpactFindings:
    """Rebuild an :class:`ImpactFindings` from a persisted ``report_data`` dict.

    #844 data remediation: stored conflict rows are masked against the
    viewer's org (drop adhoc-probe rows, blank cross-org draft identities)
    before anything leaves the service.
    """
    affected = list(data.get("affected_entities") or [])
    conflicts = list(data.get("conflicts") or [])
    gaps = list(data.get("gaps") or [])
    eu = list(data.get("eu_compliance") or [])

    from app.impact.masking import mask_stored_conflict_rows

    conflicts = mask_stored_conflict_rows(conflicts, viewer_org_id=viewer_org_id)

    return ImpactFindings(
        affected_entities=affected,
        conflicts=conflicts,
        gaps=gaps,
        eu_compliance=eu,
        affected_count=int(data.get("affected_count") or len(affected)),
        conflict_count=len(conflicts),
        gap_count=int(data.get("gap_count") or len(gaps)),
    )


# ---------------------------------------------------------------------------
# Public service function
# ---------------------------------------------------------------------------


def analyse_normi_mojuahel(sisend: str, *, org_id: str | None) -> NormiResult:
    """Resolve *sisend* and run the Normi mõjuahel impact analysis.

    Args:
        sisend: The user's free-text legal reference (provision / law / CELEX
            / court-case number / draft UUID / free description). Must be
            non-empty and already ``.strip()``-ed by the caller.
        org_id: The caller's organisation id, used to scope the owned-draft
            short-circuit and to mask stored cross-org conflict rows. ``None``
            for an unauthenticated/serviceless context (the draft path is then
            skipped).

    Returns:
        One of :data:`NormiResult` — a frozen dataclass discriminated by its
        ``kind`` field. Never raises for a dead Jena / resolver / analyser
        crash: those degrade to an unresolved / empty-findings result.
    """
    sisend = (sisend or "").strip()

    # --- 1. UUID → owned-draft report short-circuit ------------------------
    maybe_uuid = _try_parse_uuid(sisend)
    if maybe_uuid is not None:
        report_row = _load_owned_draft_report(maybe_uuid, org_id)
        if report_row is not None:
            findings = _findings_from_report_data(
                _parse_report_data(report_row[3]), viewer_org_id=org_id
            )
            return NormiDraftBackedResult(
                draft_id=str(report_row[0]),
                draft_title=str(report_row[1] or "Pealkirjata eelnõu"),
                findings=findings,
                score=int(report_row[4] or 0),
            )
        # UUID that isn't an owned draft with a report → fall through.

    # --- 2. parse + resolve ------------------------------------------------
    parsed_refs = parse_user_reference(sisend)
    resolved = _resolve_refs(parsed_refs)
    resolved_with_uri = [
        r for r in resolved if getattr(r, "entity_uri", None) and str(r.entity_uri).strip()
    ]
    # Dedupe by entity URI so "AvTS § 35" + its "AvTS" law ref don't double.
    seen: set[str] = set()
    unique_resolved: list[Any] = []
    for r in resolved_with_uri:
        uri = str(r.entity_uri)
        if uri in seen:
            continue
        seen.add(uri)
        unique_resolved.append(r)

    if len(unique_resolved) == 1:
        resolved_one = unique_resolved[0]
        entity_uri = str(resolved_one.entity_uri)
        result = run_adhoc_impact_analysis(entity_uri)
        return NormiAdhocResult(
            entity_uri=entity_uri,
            label=_resolved_label(resolved_one, sisend),
            type_label=_resolved_type_label(resolved_one),
            findings=result.findings,
            score=result.score,
        )

    if len(unique_resolved) > 1:
        candidates: list[NormiCandidate] = []
        for r in unique_resolved:
            label = _resolved_label(r, sisend)
            extracted = getattr(r, "extracted", None)
            ref_text = str(getattr(extracted, "ref_text", "") or label)
            candidates.append(NormiCandidate(label=label, ref=ref_text))
        return NormiDisambiguation(candidates=candidates)

    # Nothing resolved.
    return NormiUnresolved()
