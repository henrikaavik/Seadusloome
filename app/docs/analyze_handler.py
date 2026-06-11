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

from app.annotations.models import update_stale_flags_for_version
from app.annotations.row_keys import collect_row_specs
from app.auth.audit import log_action
from app.db import get_connection
from app.docs.draft_model import fetch_draft, get_draft, update_draft_status
from app.docs.entity_extractor import ExtractedRef
from app.docs.error_mapping import map_failure_to_user_message
from app.docs.graph_builder import build_draft_graph, write_doc_lineage
from app.docs.impact import ImpactAnalyzer, calculate_impact_score
from app.docs.impact.analyzer import analyze_burden_delta, analyze_sanctions_delta
from app.docs.reference_resolver import ResolvedRef
from app.docs.similarity import (
    compute_entity_set_hash,
    find_similar_drafts,
    get_similarity_threshold,
    persist_similarities,
    update_uri_index,
)
from app.docs.version_model import get_latest_version
from app.jobs.worker import register_handler
from app.sync.jena_loader import put_named_graph

logger = logging.getLogger(__name__)


def analyze_impact(
    payload: dict[str, Any],
    *,
    attempt: int = 1,
    max_attempts: int = 3,
    job_id: int | None = None,
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

    # #852 E6: the whole pipeline — INCLUDING the draft/entity load below —
    # runs inside the retry-gated try. The load used to sit before the
    # gated region, so a DB hiccup (or missing draft) there never flipped
    # the draft to ``failed`` even on the final attempt and the pipeline
    # appeared stuck in ``analyzing``. (When the draft row itself is gone,
    # ``_mark_draft_failed`` simply updates zero rows — harmless.)
    try:
        # ------------------------------------------------------------------
        # 1. Load draft + resolved references
        # ------------------------------------------------------------------
        #
        # Step 5 of docs/2026-05-18-bugfix-plan.md: widen the SELECT to
        # include ``partial_match`` (jsonb column from migration 034).
        # Previously the WHERE clause filtered ``entity_uri is not null``
        # which silently dropped every act-level partial match the resolver
        # wrote. Those rows now surface so the graph builder can emit an
        # act-level annotation triple and the impact engine can include the
        # partial-match reference in the report (as an act-level finding,
        # not as a synthetic provision URI).
        with get_connection() as conn:
            draft = get_draft(conn, draft_id)
            if draft is None:
                raise ValueError(f"Draft {draft_id} not found")
            # #815: fetch fully-unresolved EU references BEFORE the resolved
            # SELECT below filters them out. The downstream analyzer never
            # sees these rows (entity_uri IS NULL AND partial_match IS NULL),
            # but the impact-report renderer + .docx export use them to
            # surface a "we detected EU refs but couldn't map them" warning
            # so the user knows the analysis is missing coverage rather
            # than silently treating "no EU findings" as "no EU impact".
            unresolved_eu_rows = conn.execute(
                """
                select ref_text, confidence
                from draft_entities
                where draft_id = %s
                  and ref_type = 'eu_act'
                  and entity_uri is null
                  and partial_match is null
                order by confidence desc
                """,
                (str(draft_id),),
            ).fetchall()
            entity_rows = conn.execute(
                """
                select ref_text, entity_uri, confidence, ref_type, location,
                       partial_match
                from draft_entities
                where draft_id = %s
                  and (entity_uri is not null or partial_match is not null)
                """,
                (str(draft_id),),
            ).fetchall()
            # #844 A3b: resolve the org's owned draft UUIDs on the *same*
            # connection so the conflict pass can mask cross-org draft rows
            # (foreign draft URIs / titles are never persisted into the
            # report). Best-effort — a lookup failure yields an empty set,
            # which masks every cross-draft row (the safe default). Done here,
            # inside the already-open connection, so we add no extra
            # ``get_connection()`` round-trip.
            owned_draft_ids = _owned_draft_ids_on_conn(conn, draft.org_id)

        # Normalise the unresolved rows into the JSON-friendly shape that
        # gets persisted in ``report_data["unresolved_eu_refs"]``.
        # Tolerates both tuple-style rows (psycopg default) and dict-style
        # rows (rare; some test fixtures use DictRow).
        unresolved_eu_refs: list[dict[str, Any]] = []
        for row in unresolved_eu_rows or []:
            if isinstance(row, dict):
                ref_text = row.get("ref_text")
                confidence = row.get("confidence")
            else:
                ref_text = row[0] if len(row) > 0 else None
                confidence = row[1] if len(row) > 1 else None
            ref_text_str = str(ref_text or "").strip()
            if not ref_text_str:
                continue
            try:
                conf = float(confidence) if confidence is not None else 0.0
            except (TypeError, ValueError):
                conf = 0.0
            unresolved_eu_refs.append({"ref_text": ref_text_str, "confidence": conf})

        resolved_refs = [_row_to_resolved_ref(row) for row in entity_rows]
        logger.info(
            "analyze_impact: draft %s has %d resolved references "
            "(%d full URI, %d act-level partial match)",
            draft_id,
            len(resolved_refs),
            sum(1 for r in resolved_refs if r.entity_uri),
            sum(1 for r in resolved_refs if r.partial_match is not None),
        )

        # ------------------------------------------------------------------
        # 2-7. Build graph, load to Jena, analyse, persist, flip status
        # ------------------------------------------------------------------
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
        # #844 A3b: ``owned_draft_ids`` (resolved above on the draft-load
        # connection) lets the conflict pass mask cross-org draft rows so
        # foreign draft URIs / titles are never persisted into the report.
        findings = analyzer.analyze(draft.graph_uri, owned_draft_ids=owned_draft_ids)
        score = calculate_impact_score(findings)

        report_id = uuid4()
        ontology_version = _get_current_ontology_version()
        # C6 (#791): extend the JSONB ``report_data`` blob with
        # sanctions + burden delta sections. Each helper returns an
        # empty/baseline dataclass on Jena failure so the analyze
        # pipeline never fails because of an A1/A2 hiccup. Legacy
        # reports without these keys remain readable via the
        # renderer's ``.get(...)`` fallback.
        affected_uris = [
            str(e.get("uri") or "").strip()
            for e in (findings.affected_entities or [])
            if (e.get("uri") or "").strip()
        ]
        try:
            sanctions_delta = analyze_sanctions_delta(draft.graph_uri, affected_uris)
        except Exception:
            logger.warning(
                "analyze_sanctions_delta failed for draft=%s; continuing without it",
                draft_id,
                exc_info=True,
            )
            sanctions_delta = None
        try:
            burden_delta = analyze_burden_delta(draft.graph_uri)
        except Exception:
            logger.warning(
                "analyze_burden_delta failed for draft=%s; continuing without it",
                draft_id,
                exc_info=True,
            )
            burden_delta = None

        findings_dict_for_report = dataclasses.asdict(findings)
        if sanctions_delta is not None:
            findings_dict_for_report["sanctions_delta"] = dataclasses.asdict(sanctions_delta)
        if burden_delta is not None:
            findings_dict_for_report["burden_delta"] = dataclasses.asdict(burden_delta)
        # #815: persist unresolved-EU-ref list (may be empty). The
        # renderer + .docx export read this key to surface a "EU refs
        # detected but couldn't be mapped" warning when non-empty.
        # Legacy reports lack the key entirely; readers use
        # ``report_data.get("unresolved_eu_refs", [])`` to stay
        # backward-compatible.
        findings_dict_for_report["unresolved_eu_refs"] = unresolved_eu_refs
        report_data = json.dumps(findings_dict_for_report)

        with get_connection() as conn:
            # #618 PR-B: bind the impact report to the latest
            # ``draft_versions`` row so per-version diff/timeline (PR-C)
            # can join against the right report.  The lookup runs
            # inside the same transaction as the INSERT so a v3
            # parallel upload mid-analyze cannot retro-attach this
            # report to the wrong version.  The column is NULLABLE
            # via migration 032 so a missing v1 row (impossible
            # post-migration but defended-against) does not block the
            # insert.
            latest_version = get_latest_version(conn, draft_id)
            draft_version_id = str(latest_version.id) if latest_version is not None else None
            conn.execute(
                """
                insert into impact_reports
                    (id, draft_id, draft_version_id, affected_count,
                     conflict_count, gap_count, impact_score,
                     report_data, ontology_version)
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    str(report_id),
                    str(draft_id),
                    draft_version_id,
                    findings.affected_count,
                    findings.conflict_count,
                    findings.gap_count,
                    score,
                    report_data,
                    ontology_version,
                ),
            )
            # #625 §4.2: route through the SSOT helper. The helper
            # clears ``error_message`` / ``error_debug`` to NULL and
            # stamps ``processing_completed_at = now()`` for terminal
            # transitions (#670). Lives in the SAME transaction as the
            # ``impact_reports`` insert above so observers never see a
            # ``status='ready'`` row without its report.
            update_draft_status(conn, draft_id, "ready")
            conn.commit()

        # ------------------------------------------------------------------
        # #619 PR-C: stale-flag automation.  After a fresh analyze writes
        # the new impact_reports row, walk every annotation on the same
        # draft_version_id and flip ``stale``:
        #   - row reappeared in the new findings → stale=false
        #   - row vanished from the new findings → stale=true
        # Wrapped in its own try/except so a stale-flag glitch never
        # fails the analyze job; this is a UX signal, not a correctness
        # invariant.  Runs in its own transaction (separate connection)
        # so the impact_reports commit above is durable regardless.
        # ------------------------------------------------------------------
        if draft_version_id is not None:
            try:
                findings_dict = dataclasses.asdict(findings)
                row_specs = collect_row_specs(findings_dict)
                current_keys: set[tuple[str, str]] = set(row_specs)
                with get_connection() as conn:
                    changed = update_stale_flags_for_version(conn, draft_version_id, current_keys)
                    conn.commit()
                if changed:
                    logger.info(
                        "analyze_impact: reconciled stale flags for version=%s changed=%d",
                        draft_version_id,
                        changed,
                    )
            except Exception:
                logger.warning(
                    "analyze_impact: stale-flag reconcile failed for draft=%s version=%s",
                    draft_id,
                    draft_version_id,
                    exc_info=True,
                )

        # ------------------------------------------------------------------
        # #621: "sarnased eelnõud" — best-effort similarity compute.
        # Wrapped in its own try/except so a DB hiccup here never fails
        # the analyze job; similarity is supplemental UX, not core analysis.
        # ------------------------------------------------------------------
        try:
            uris = [str(row[1]) for row in entity_rows if row[1]]
            with get_connection() as conn:
                # Rebuild the inverted index for this draft so future
                # analyze runs by OTHER drafts can find this one as a
                # candidate.
                update_uri_index(conn, str(draft_id), uris)
                conn.commit()

            new_hash = compute_entity_set_hash(uris)
            with get_connection() as conn:
                existing_hash_row = conn.execute(
                    "SELECT DISTINCT entity_set_hash FROM draft_similarities"
                    " WHERE draft_id = %s LIMIT 1",
                    (str(draft_id),),
                ).fetchone()

                if existing_hash_row and existing_hash_row[0] == new_hash:
                    logger.info(
                        "similarity: skipping recompute for draft=%s (hash unchanged)",
                        draft_id,
                    )
                else:
                    threshold = get_similarity_threshold()
                    similarities = find_similar_drafts(
                        conn, str(draft_id), uris, threshold=threshold
                    )
                    persist_similarities(conn, str(draft_id), similarities, new_hash)
                    conn.commit()
                    log_action(
                        None,
                        "draft.similar.compute",
                        {
                            "draft_id": str(draft_id),
                            "candidate_count": len(similarities),
                        },
                    )
                    logger.info(
                        "similarity: computed %d similar drafts for draft=%s",
                        len(similarities),
                        draft_id,
                    )
        except Exception:
            logger.warning(
                "similarity: non-critical failure for draft=%s, skipping",
                draft_id,
                exc_info=True,
            )

        # #608: push the terminal status to subscribed WS clients first
        # (cheap; a notify_analysis_done failure shouldn't drop the WS
        # event).
        from app.docs.status_events import emit_threadsafe

        emit_threadsafe(draft_id, type="status", status="ready")

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
    analyzer only reads ``entity_uri``, ``confidence``, and
    ``partial_match``, so a thin reconstruction is sufficient.
    ``location`` and ``partial_match`` are stored as JSONB columns;
    psycopg returns either a parsed dict or the raw JSON string
    depending on type-adaptation config, so both shapes are tolerated.

    The row shape is ``(ref_text, entity_uri, confidence, ref_type,
    location, partial_match)`` — see the SELECT in
    :func:`analyze_impact`. The partial-match column was added by
    migration 034 (Wave 2 Step 2) and is threaded through here so the
    graph builder can emit a distinct act-level annotation triple for
    those rows (Step 5).
    """
    ref_text, entity_uri, confidence, ref_type, location, partial_match = row
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
    # ``partial_match`` is jsonb (migration 034). Tolerate both string
    # and dict shapes so the handler is independent of psycopg's
    # JSONB-loader configuration.
    partial_match_dict: dict[str, Any] | None
    if isinstance(partial_match, str):
        try:
            partial_match_dict = json.loads(partial_match)
        except (TypeError, ValueError):
            partial_match_dict = None
    elif isinstance(partial_match, dict):
        partial_match_dict = partial_match
    else:
        partial_match_dict = None
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
        match_score=1.0 if entity_uri else (0.5 if partial_match_dict else 0.0),
        partial_match=partial_match_dict,
    )


def _owned_draft_ids_on_conn(conn: Any, org_id: UUID | str | None) -> set[str]:
    """Return the draft UUIDs owned by *org_id*, using an open *conn* (#844 A3b).

    Scopes the conflict pass: cross-draft conflict rows pointing at drafts
    outside this set are masked so a freshly-generated report never
    persists another org's draft URI / title. Runs on the caller's already
    open connection (no extra ``get_connection()`` round-trip). Best-effort
    — any error (or a falsy ``org_id``) yields an empty set, which masks
    *every* cross-draft row (the safe default).
    """
    if not org_id:
        return set()
    try:
        from app.docs.impact.masking import fetch_owned_draft_ids

        return fetch_owned_draft_ids(conn, str(org_id))
    except Exception:  # noqa: BLE001 — masking must never break analysis
        logger.warning(
            "analyze_impact: owned-draft lookup failed for org=%s; masking all cross-draft rows",
            org_id,
            exc_info=True,
        )
        return set()


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
            update_draft_status(
                conn,
                draft_id,
                "failed",
                user_msg[:500],
                error_debug=debug_detail,
            )
            conn.commit()
    except Exception:  # noqa: BLE001
        logger.exception("analyze_impact: failed to mark draft %s as failed", draft_id)
        return

    # #608: push the failure transition to WS subscribers.
    from app.docs.status_events import emit_threadsafe

    emit_threadsafe(
        draft_id,
        type="status",
        status="failed",
        error_message=user_msg[:500],
    )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------
#
# Importing this module registers the real handler with the worker's
# dispatch registry. ``app/docs/__init__.py`` imports us at startup so
# the stub registered in ``app.jobs.worker`` is replaced before any
# job is claimed.

register_handler("analyze_impact")(analyze_impact)
