"""Background job handler for ``analyze_impact``.

Pipeline (spec §7 + §8):

    1. Load the draft + every resolved reference from Postgres.
    2. Build the draft's Turtle graph via :mod:`app.docs.graph_builder`.
    3. PUT the graph into its named Jena graph.
    4. Run :class:`app.docs.impact.ImpactAnalyzer` against the graph.
    5. Compute an impact score.
    6. Persist a new ``impact_reports`` row with the full findings JSON.
    7. Flip the draft row to ``status='ready'``.

Failure modes:

    - Draft missing ⇒ ``ValueError`` (worker retries are pointless
      for a permanently-deleted draft; retries still get consumed so
      the user eventually sees the error).
    - Jena PUT fails ⇒ the whole handler raises ``RuntimeError``
      and flips the draft to ``failed``. The worker's retry loop
      picks it up — useful for transient Fuseki restarts.
    - Analyzer or scoring exception ⇒ handler flips the draft to
      ``failed``, deletes the named graph we just pushed (best
      effort, so stale data doesn't accumulate), and re-raises.

The real handler registers itself at module bottom via
``@register_handler``; importing :mod:`app.docs` wires it into the
worker's dispatch table (replacing the Phase 2 stub).
"""

from __future__ import annotations

import dataclasses
import json
import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from app.db import get_connection
from app.docs.draft_model import fetch_draft, get_draft
from app.docs.entity_extractor import ExtractedRef
from app.docs.error_mapping import map_failure_to_user_message
from app.docs.graph_builder import build_draft_graph, write_doc_lineage
from app.docs.impact import ImpactAnalyzer, calculate_impact_score
from app.docs.reference_resolver import ResolvedRef
from app.jobs.worker import register_handler
from app.sync.jena_loader import put_named_graph

logger = logging.getLogger(__name__)


def analyze_impact(
    payload: dict[str, Any],
    *,
    attempt: int = 1,
    max_attempts: int = 3,
) -> dict[str, Any]:
    """Run the impact analysis pipeline for one draft.

    Args:
        payload: ``{"draft_id": "<uuid-str>"}``. Any other keys are
            ignored. Missing or unparseable ``draft_id`` raises a
            ``ValueError`` that the job worker will convert into a
            ``failed`` status.
        attempt: 1-based current attempt counter. Used to delay the
            ``status='failed'`` transition until the retry budget is
            exhausted (#448).
        max_attempts: Total retry budget for this job.

    Returns:
        Summary dict persisted in ``background_jobs.result`` — kept
        deliberately small so admin-dashboard rows stay cheap to
        serialise.
    """
    raw_id = payload.get("draft_id")
    if not raw_id:
        raise ValueError("analyze_impact payload missing draft_id")
    draft_id = UUID(str(raw_id))

    logger.info("analyze_impact: starting pipeline for draft %s", draft_id)

    # ------------------------------------------------------------------
    # 1. Load draft + resolved references
    # ------------------------------------------------------------------
    with get_connection() as conn:
        draft = get_draft(conn, draft_id)
        if draft is None:
            raise ValueError(f"Draft {draft_id} not found")
        entity_rows = conn.execute(
            """
            select ref_text, entity_uri, confidence, ref_type, location
            from draft_entities
            where draft_id = %s and entity_uri is not null
            """,
            (str(draft_id),),
        ).fetchall()

    resolved_refs = [_row_to_resolved_ref(row) for row in entity_rows]
    logger.info(
        "analyze_impact: draft %s has %d resolved references",
        draft_id,
        len(resolved_refs),
    )

    # ------------------------------------------------------------------
    # 2-7. Build graph, load to Jena, analyse, persist, flip status
    # ------------------------------------------------------------------
    try:
        turtle = build_draft_graph(draft, resolved_refs)
        loaded = put_named_graph(draft.graph_uri, turtle)
        if not loaded:
            raise RuntimeError(f"Failed to load draft graph into Jena: {draft.graph_uri}")

        # #641 — A3 ontology lineage.  Runs right after the Turtle PUT
        # so the optional ``estleg:basedOn`` edge is visible to the
        # ImpactAnalyzer below (and to any B-series SPARQL consumers
        # that come later).  The class assertion is already part of
        # the Turtle PUT above (``build_draft_graph`` picks the right
        # class from ``doc_type``); this helper re-asserts it as a
        # no-op on re-runs and writes / clears the ``basedOn`` edge
        # atomically via SPARQL UPDATE.  Lineage write failures bubble
        # up through the existing retry/fail machinery.
        parent_vtk = fetch_draft(draft.parent_vtk_id) if draft.parent_vtk_id else None
        write_doc_lineage(draft, parent_vtk)

        analyzer = ImpactAnalyzer()
        findings = analyzer.analyze(draft.graph_uri)
        score = calculate_impact_score(findings)

        report_id = uuid4()
        ontology_version = _get_current_ontology_version()
        report_data = json.dumps(dataclasses.asdict(findings))

        with get_connection() as conn:
            conn.execute(
                """
                insert into impact_reports
                    (id, draft_id, affected_count, conflict_count,
                     gap_count, impact_score, report_data, ontology_version)
                values (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    str(report_id),
                    str(draft_id),
                    findings.affected_count,
                    findings.conflict_count,
                    findings.gap_count,
                    score,
                    report_data,
                    ontology_version,
                ),
            )
            conn.execute(
                """
                update drafts
                set status = 'ready',
                    error_message = null,
                    error_debug = null,
                    updated_at = now(),
                    processing_completed_at = now()
                where id = %s
                """,
                (str(draft_id),),
            )
            conn.commit()

        # Notify the draft owner that analysis is complete.
        try:
            from app.notifications.wire import notify_analysis_done

            notify_analysis_done(draft)
        except Exception:
            logger.debug("notify_analysis_done failed (non-critical)", exc_info=True)

        logger.info(
            "analyze_impact: draft %s ready report=%s score=%d",
            draft_id,
            report_id,
            score,
        )
        return {
            "draft_id": str(draft_id),
            "report_id": str(report_id),
            "impact_score": score,
            "affected_count": findings.affected_count,
            "conflict_count": findings.conflict_count,
            "gap_count": findings.gap_count,
        }

    except Exception as exc:
        # #448: only flip the draft to ``failed`` on the FINAL attempt.
        # Earlier attempts re-raise so the queue's retry loop can take
        # another swing without the user seeing a permanent-failure
        # state.
        #
        # #456: we deliberately do NOT delete the named graph here.
        # Graph lifecycle is owned by ``delete_draft_handler`` —
        # cleaning up here masks transient Jena hiccups (the next
        # retry would just have to load the graph again) and risks
        # racing the user's own delete.
        if attempt >= max_attempts:
            logger.exception(
                "analyze_impact permanently failed for draft %s after %d attempts",
                draft_id,
                attempt,
            )
            _mark_draft_failed(draft_id, exc)
        else:
            logger.warning(
                "analyze_impact attempt %d/%d failed for draft %s, will retry: %s",
                attempt,
                max_attempts,
                draft_id,
                exc,
            )
        raise


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _row_to_resolved_ref(row: tuple[Any, ...]) -> ResolvedRef:
    """Reconstruct a :class:`ResolvedRef` from a ``draft_entities`` row.

    We cannot round-trip the full ``ExtractedRef`` (the resolver did
    not persist ``location``/``confidence`` losslessly) but the
    analyzer only reads ``entity_uri`` and ``confidence``, so a thin
    reconstruction is sufficient. ``location`` is stored as a JSONB
    column so we need to JSON-parse it if Postgres handed us a string.
    """
    ref_text, entity_uri, confidence, ref_type, location = row
    try:
        conf = float(confidence) if confidence is not None else 0.0
    except (TypeError, ValueError):
        conf = 0.0
    if isinstance(location, str):
        try:
            location_dict = json.loads(location)
        except (TypeError, ValueError):
            location_dict = {}
    elif isinstance(location, dict):
        location_dict = location
    else:
        location_dict = {}
    extracted = ExtractedRef(
        ref_text=str(ref_text),
        ref_type=str(ref_type or "provision"),
        confidence=conf,
        location=location_dict,
    )
    return ResolvedRef(
        extracted=extracted,
        entity_uri=str(entity_uri) if entity_uri else None,
        matched_label=None,
        match_score=1.0 if entity_uri else 0.0,
    )


def _get_current_ontology_version() -> str:
    """Return a reproducibility tag for the ontology-version column.

    We don't persist the git SHA (yet) but the ``sync_log`` table
    tracks the most recent successful sync — its ``started_at`` +
    ``entity_count`` pair is a reasonable proxy for "which snapshot
    of the ontology did this report run against". Returns ``"unknown"``
    when the sync_log is empty or the query fails.
    """
    try:
        with get_connection() as conn:
            row = conn.execute(
                """
                select started_at, entity_count
                from sync_log
                where status = 'success'
                order by started_at desc
                limit 1
                """
            ).fetchone()
    except Exception:  # noqa: BLE001 — reproducibility tag is best-effort
        logger.warning("analyze_impact: could not read sync_log", exc_info=True)
        return "unknown"
    if row is None:
        return "unknown"
    started_at, entity_count = row
    if started_at is None:
        return "unknown"
    if isinstance(started_at, datetime):
        ts = started_at.astimezone(UTC).isoformat()
    else:
        ts = str(started_at)
    return f"{ts}@{entity_count or 0}"


def _mark_draft_failed(draft_id: UUID, exc: BaseException) -> None:
    """Best-effort transition of the draft to ``status='failed'``.

    Maps ``exc`` through :func:`map_failure_to_user_message` (#609) so
    the UI surfaces a short actionable Estonian message and the raw
    technical detail lands in ``drafts.error_debug`` for admin triage.
    """
    user_msg, debug_detail = map_failure_to_user_message(exc, stage="analyze")
    try:
        with get_connection() as conn:
            conn.execute(
                """
                update drafts
                set status = 'failed',
                    error_message = %s,
                    error_debug = %s,
                    updated_at = now(),
                    processing_completed_at = now()
                where id = %s
                """,
                (user_msg[:500], debug_detail, str(draft_id)),
            )
            conn.commit()
    except Exception:  # noqa: BLE001
        logger.exception("analyze_impact: failed to mark draft %s as failed", draft_id)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------
#
# Importing this module registers the real handler with the worker's
# dispatch registry. ``app/docs/__init__.py`` imports us at startup so
# the stub registered in ``app.jobs.worker`` is replaced before any
# job is claimed.

register_handler("analyze_impact")(analyze_impact)
