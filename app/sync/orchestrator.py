"""Sync pipeline orchestrator: GitHub → RDF → validate → Jena Fuseki."""

import logging
import os
import re
import shutil
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from app.db import get_connection
from app.sync.converter import convert_ontology, serialize_to_turtle
from app.sync.jena_loader import (
    copy_graph_to_default,
    drop_graph,
    graph_triple_count,
    upload_turtle_to_named_graph,
)
from app.sync.validator import load_shapes, validate_graph

_RESULTS_COUNT_RE = re.compile(r"Results \((\d+)\)")

# Phase labels written to sync_log.current_step so the admin UI can
# render a live progress-pill indicator. Keep this list in sync with the
# frontend's `_PROGRESS_PHASES` in app/admin/sync.py.
PHASE_CLONING = "cloning"
PHASE_CONVERTING = "converting"
PHASE_VALIDATING = "validating"
PHASE_UPLOADING = "uploading"
PHASE_REINGESTING = "reingesting"


def _parse_violation_count(results_line: str) -> int:
    """Extract the integer violation count from a pyshacl `Results (N)` line.

    Returns 0 if the line doesn't match the expected format — the caller
    only uses this for logging/UI display, so failing open is acceptable.
    """
    match = _RESULTS_COUNT_RE.search(results_line)
    if match:
        try:
            return int(match.group(1))
        except ValueError:
            return 0
    return 0


# Lazy import to avoid circular dependencies; used for WS notifications.
_notify_sync: object | None = None


def _get_notify_fn():  # type: ignore[no-untyped-def]
    """Lazily import the sync-complete notifier."""
    global _notify_sync  # noqa: PLW0603
    if _notify_sync is None:
        from app.explorer.websocket import notify_sync_complete_sync

        _notify_sync = notify_sync_complete_sync
    return _notify_sync


logger = logging.getLogger(__name__)

ONTOLOGY_REPO = "https://github.com/henrikaavik/estonian-legal-ontology.git"

# #573: staged publish. The sync pipeline uploads fresh data into this
# named graph first, verifies the triple count, and only then swaps it
# into the default graph with SPARQL ``COPY``. A single stable URI is
# safe because ``run_sync`` is serialised by the DB-level lock taken by
# the admin/webhook callers — two syncs can't race for the same slot.
STAGING_GRAPH = "urn:estleg:staging"

# Lower bound for "this sync looks plausible". Default: 1,000,000 triples
# (the enacted-law ontology has ~1.3M today). Overridable per env for
# non-prod fixtures and tests. The effective threshold is the max of
# this value and 80% of the current live-graph count — see
# :func:`_compute_verification_threshold`.
_DEFAULT_MIN_TRIPLES = 1_000_000


def _sync_min_triples() -> int:
    """Return the configured absolute minimum triple count for verification.

    Exposed as a function so tests (and ops overriding ``SYNC_MIN_TRIPLES``
    at runtime) don't need to patch a module-level constant.
    """
    raw = os.environ.get("SYNC_MIN_TRIPLES")
    if raw is None:
        return _DEFAULT_MIN_TRIPLES
    try:
        return max(0, int(raw))
    except ValueError:
        logger.warning(
            "Invalid SYNC_MIN_TRIPLES=%r; falling back to default %d",
            raw,
            _DEFAULT_MIN_TRIPLES,
        )
        return _DEFAULT_MIN_TRIPLES


def _compute_verification_threshold(live_count: int) -> int:
    """Compute the effective staging-verification threshold.

    The rule is: staging must have at least ``max(SYNC_MIN_TRIPLES,
    0.8 * live_count)`` triples. The 80% floor protects against
    regressions (a sudden 50% drop in triple count is almost certainly
    a parse/convert bug). On first-ever load the live graph is empty,
    so the floor collapses to ``SYNC_MIN_TRIPLES`` alone.
    """
    absolute = _sync_min_triples()
    if live_count <= 0:
        return absolute
    relative = int(live_count * 0.8)
    return max(absolute, relative)


def _trigger_rag_ingestion() -> None:
    """Trigger a lightweight RAG re-ingestion after successful sync.

    Runs the async ingestion in a new event loop since the sync
    pipeline is synchronous. Non-critical — failures are logged but
    don't break the sync.
    """
    import asyncio

    from scripts.ingest_rag import ingest_modified_entities

    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # If there's already a running loop (e.g. inside FastHTML),
            # schedule the ingestion as a task. The caller catches
            # exceptions so a fire-and-forget approach is fine.
            loop.create_task(ingest_modified_entities())
            logger.info("RAG re-ingestion scheduled as background task")
        else:
            asyncio.run(ingest_modified_entities())
            logger.info("RAG re-ingestion completed synchronously")
    except RuntimeError:
        # No event loop available — create one
        asyncio.run(ingest_modified_entities())
        logger.info("RAG re-ingestion completed in new event loop")


def _insert_running_row(started_at: datetime, step: str) -> int | None:
    """Insert a 'running' sync_log row and return its id.

    Returns None on DB failure — the orchestrator continues without a
    log id and falls back to a single INSERT at the end.
    """
    try:
        with get_connection() as conn:
            row = conn.execute(
                "INSERT INTO sync_log (started_at, status, current_step) "
                "VALUES (%s, 'running', %s) RETURNING id",
                (started_at, step),
            ).fetchone()
            conn.commit()
            return int(row[0]) if row else None
    except Exception:
        logger.exception("Failed to insert running sync_log row")
        return None


def _update_step(log_id: int | None, step: str) -> None:
    """Update the current_step for an in-flight sync_log row."""
    if log_id is None:
        return
    try:
        with get_connection() as conn:
            conn.execute(
                "UPDATE sync_log SET current_step = %s WHERE id = %s",
                (step, log_id),
            )
            conn.commit()
    except Exception:
        logger.exception("Failed to update sync_log step")


def _finalize_row(
    log_id: int | None,
    status: str,
    started_at: datetime,
    entity_count: int | None = None,
    error_message: str | None = None,
) -> None:
    """Finalize a sync_log row.

    If ``log_id`` is not None, updates the existing 'running' row in place
    (clearing current_step and setting finished_at). If the initial INSERT
    failed and ``log_id`` is None, falls back to a terminal-only INSERT so
    operators still see a record.
    """
    finished_at = datetime.now(UTC)
    try:
        with get_connection() as conn:
            if log_id is not None:
                conn.execute(
                    """UPDATE sync_log
                       SET status = %s,
                           finished_at = %s,
                           entity_count = %s,
                           error_message = %s,
                           current_step = NULL
                       WHERE id = %s""",  # type: ignore[arg-type]
                    (status, finished_at, entity_count, error_message, log_id),
                )
            else:
                conn.execute(
                    """INSERT INTO sync_log
                       (started_at, finished_at, status, entity_count, error_message)
                       VALUES (%s, %s, %s, %s, %s)""",  # type: ignore[arg-type]
                    (started_at, finished_at, status, entity_count, error_message),
                )
            conn.commit()
    except Exception:
        logger.exception("Failed to finalize sync_log row")


def mark_stale_running_as_failed() -> int:
    """Mark any lingering 'running' rows as failed.

    Called at app startup (app/main.py) to clean up rows orphaned by a
    process crash or restart. Returns the number of rows updated.
    """
    try:
        with get_connection() as conn:
            cur = conn.execute(
                """UPDATE sync_log
                   SET status = 'failed',
                       finished_at = now(),
                       error_message = 'Process restarted during sync',
                       current_step = NULL
                   WHERE status = 'running'"""
            )
            conn.commit()
            return cur.rowcount or 0
    except Exception:
        logger.exception("Failed to clean stale running rows")
        return 0


def has_recent_running_row(max_age_minutes: int = 30) -> bool:
    """Return True if a sync_log row with status='running' exists and is
    recent enough to still be plausibly alive.

    Used by the webhook to avoid spawning a parallel sync while an
    admin-triggered one is in flight. The age bound protects against a
    phantom 'running' row wedging future syncs if startup cleanup also
    failed.
    """
    try:
        with get_connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM sync_log "
                "WHERE status = 'running' "
                "  AND started_at > now() - (%s::text || ' minutes')::interval "
                "LIMIT 1",
                (str(max_age_minutes),),
            ).fetchone()
            return row is not None
    except Exception:
        logger.exception("Failed to check for running sync row")
        return False


def clone_or_pull(target_dir: Path) -> None:
    """Clone or pull the ontology repo."""
    if (target_dir / ".git").exists():
        logger.info("Pulling latest changes...")
        subprocess.run(
            ["git", "pull", "--ff-only"],
            cwd=target_dir,
            check=True,
            capture_output=True,
        )
    else:
        logger.info("Cloning ontology repo...")
        subprocess.run(
            ["git", "clone", "--depth", "1", ONTOLOGY_REPO, str(target_dir)],
            check=True,
            capture_output=True,
        )


def run_sync(
    repo_dir: Path | None = None,
    *,
    log_id: int | None = None,
    started_at: datetime | None = None,
) -> bool:
    """Execute the full sync pipeline.

    Steps:
    1. Clone/pull ontology repo
    2. Convert JSON-LD → RDF
    3. Validate with SHACL shapes
    4. Upload new RDF data into a staging named graph
    5. Verify staging triple count meets threshold
    6. Atomically COPY staging → default graph
    7. Log result

    Args:
        repo_dir: Path to existing ontology repo clone. If None, clones to temp dir.
        log_id: Pre-allocated sync_log row id. When ``trigger_sync`` inserts
            the ``running`` row synchronously to avoid a UI race, it passes
            the id here so the orchestrator doesn't create a second row.
        started_at: Paired with ``log_id`` — the timestamp recorded on the
            pre-allocated row. Used for finalize fallback paths.

    Returns:
        True if sync succeeded.
    """
    if started_at is None:
        started_at = datetime.now(UTC)
    logger.info("Starting sync pipeline at %s", started_at.isoformat())

    # Insert 'running' row up front so the admin UI can show live progress.
    # All subsequent step transitions reference this row via log_id. The
    # admin POST handler inserts synchronously before spawning the
    # background thread to close the UI race; webhook-triggered syncs
    # don't render a UI so they fall through to the orchestrator's own
    # insert here.
    if log_id is None:
        log_id = _insert_running_row(started_at, PHASE_CLONING)

    use_temp = repo_dir is None
    if use_temp:
        temp_dir = tempfile.mkdtemp(prefix="ontology-sync-")
        repo_dir = Path(temp_dir)

    try:
        # Step 1: Clone or pull
        _update_step(log_id, PHASE_CLONING)
        clone_or_pull(repo_dir)

        # Step 2: Convert JSON-LD → RDF
        _update_step(log_id, PHASE_CONVERTING)
        logger.info("Converting JSON-LD to RDF...")
        graph = convert_ontology(repo_dir)
        entity_count = len(graph)
        logger.info("Converted %d triples", entity_count)

        # Step 3: Validate with SHACL (if shapes exist).
        #
        # Design note (#440): SHACL violations are reported as WARNINGS,
        # not errors that abort the sync. Phase 1 discovered that a
        # shape-vs-data drift could block every deploy behind a
        # 2,634-violation report even though the data was largely good;
        # simultaneously the ontology repo got a shape fix that cut
        # violations to 213 genuine missing-summary cases (0.4% of
        # provisions). The right long-term policy is "validate, log,
        # keep going" — the admin dashboard surfaces the warning count
        # so cleanup can happen over time without blocking deploys.
        _update_step(log_id, PHASE_VALIDATING)
        shacl_warning: str | None = None
        shapes_dir = repo_dir / "shacl"
        if shapes_dir.exists() and any(shapes_dir.iterdir()):
            logger.info("Validating with SHACL shapes...")
            shapes = load_shapes(shapes_dir)
            if len(shapes) > 0:
                conforms, report = validate_graph(graph, shapes)
                if not conforms:
                    # Extract "Results (N):" line for a clean summary
                    results_line = next(
                        (line for line in report.splitlines() if "Results (" in line),
                        "Results (unknown)",
                    ).strip()
                    violation_count = _parse_violation_count(results_line)
                    logger.warning(
                        "SHACL validation produced %d warnings: %s (continuing with upload)",
                        violation_count,
                        results_line,
                    )
                    shacl_warning = f"WARN: SHACL {results_line} — {report[:900]}"
                else:
                    logger.info("SHACL validation passed with no violations")
        else:
            logger.info("No SHACL shapes found, skipping validation")

        # Step 4: Serialize to Turtle
        logger.info("Serializing to Turtle...")
        turtle = serialize_to_turtle(graph)

        # Step 5: Staged upload + verify + atomic promote (#573).
        # This replaces the old "clear default, then upload" flow which
        # could leave production empty if upload failed after clear.
        # The new flow keeps the live default graph untouched until we
        # have a verified staging graph ready to swap in.
        _update_step(log_id, PHASE_UPLOADING)

        # Clean up any stale staging slot from a previous failed run.
        # drop_graph is SILENT so an already-absent graph is fine.
        logger.info("Dropping stale staging graph (if any)...")
        if not drop_graph(STAGING_GRAPH):
            _finalize_row(
                log_id,
                "failed",
                started_at,
                error_message="Failed to drop stale staging graph — aborting (data intact)",
            )
            return False

        logger.info("Uploading to staging graph %s...", STAGING_GRAPH)
        staged = upload_turtle_to_named_graph(STAGING_GRAPH, turtle)
        if not staged:
            # Live default graph is untouched — staging upload failed
            # in isolation. Best-effort cleanup of any partial staging
            # content so the next run starts clean.
            drop_graph(STAGING_GRAPH)
            _finalize_row(
                log_id,
                "failed",
                started_at,
                error_message="Upload to staging graph failed — live data intact",
            )
            return False

        # Verify staging before promoting. Must clear both the absolute
        # minimum (SYNC_MIN_TRIPLES) and at least 80% of the current
        # live count (regression protection).
        staging_count = graph_triple_count(STAGING_GRAPH)
        live_count = graph_triple_count(None)
        threshold = _compute_verification_threshold(live_count)
        if staging_count < threshold:
            logger.error(
                "Staging verification failed: got %d triples (threshold %d, "
                "live=%d). Leaving live default graph untouched.",
                staging_count,
                threshold,
                live_count,
            )
            drop_graph(STAGING_GRAPH)
            _finalize_row(
                log_id,
                "failed",
                started_at,
                error_message=(
                    f"staging verification failed: got {staging_count} triples "
                    f"(threshold {threshold}, live={live_count}) — live data intact"
                ),
            )
            return False

        logger.info(
            "Staging verified (%d triples, threshold %d, live=%d). "
            "Promoting to default graph via COPY...",
            staging_count,
            threshold,
            live_count,
        )
        promoted = copy_graph_to_default(STAGING_GRAPH)
        if not promoted:
            # COPY failed. Fuseki may have cleared the default graph
            # before the insert step raised, in which case live data
            # is degraded; or it may have rejected the update before
            # touching anything. We can't tell for sure without a
            # follow-up count, which itself may lie. Log loudly and
            # surface via sync_log.
            logger.critical(
                "COPY <%s> TO DEFAULT failed. Live default graph may be "
                "in an inconsistent state — investigate immediately.",
                STAGING_GRAPH,
            )
            # Keep staging around so an operator can replay the COPY
            # manually if needed. A subsequent successful run will
            # drop it on entry.
            _finalize_row(
                log_id,
                "failed",
                started_at,
                error_message=(
                    "Promote (COPY staging TO DEFAULT) failed — "
                    "live graph may be degraded. Staging left in place "
                    "for manual recovery."
                ),
            )
            return False

        # Step 6: Post-promote verification. A successful COPY of a
        # graph with N>0 triples MUST yield N triples in the default
        # graph. Zero here means something went wrong server-side
        # that we didn't catch above.
        final_count = graph_triple_count(None)
        if final_count == 0:
            logger.critical(
                "Post-promote health check: default graph is EMPTY despite "
                "COPY reporting success. Ontology is degraded."
            )
            _finalize_row(
                log_id,
                "failed",
                started_at,
                error_message=(
                    "Post-promote verification failed: default graph has "
                    "zero triples after COPY. Ontology is degraded — re-sync required."
                ),
            )
            return False

        # Cleanup: drop the staging slot now that default holds the
        # promoted data. Best-effort — a failure here just wastes
        # space and will be reclaimed on the next sync's pre-drop.
        if not drop_graph(STAGING_GRAPH):
            logger.warning(
                "Post-promote cleanup: failed to drop staging graph %s "
                "(non-fatal, will be retried on next sync).",
                STAGING_GRAPH,
            )

        logger.info("Sync complete. %d triples in Jena.", final_count)

        # Step 7: RAG re-ingestion runs under its own phase label so the
        # admin UI shows "Taasindekseerimine" while Voyage embeds chunks.
        # Marked before the notify call so clients polling see the shift
        # immediately; finalization to 'success' happens after.
        _update_step(log_id, PHASE_REINGESTING)

        # If SHACL produced warnings, record them in the same log row so
        # the admin dashboard shows both success + warning count in one
        # place. Row remains status=success — this is informational.
        _finalize_row(
            log_id,
            "success",
            started_at,
            entity_count=final_count,
            error_message=shacl_warning,
        )

        # Notify connected explorer WebSocket clients.
        try:
            fn = _get_notify_fn()
            if callable(fn):
                fn()
        except Exception:
            logger.debug("WS notification after sync failed (non-critical)")

        # Trigger RAG re-ingestion for modified entities.
        try:
            _trigger_rag_ingestion()
        except Exception:
            logger.debug("RAG re-ingestion after sync failed (non-critical)")

        return True

    except Exception as e:
        logger.exception("Sync pipeline failed")
        _finalize_row(log_id, "failed", started_at, error_message=str(e)[:1000])

        # Notify all system admins about the sync failure.
        try:
            from app.notifications.wire import notify_sync_failed

            notify_sync_failed(str(e)[:1000])
        except Exception:
            logger.debug("notify_sync_failed failed (non-critical)", exc_info=True)

        return False

    finally:
        if use_temp and repo_dir:
            shutil.rmtree(repo_dir, ignore_errors=True)
