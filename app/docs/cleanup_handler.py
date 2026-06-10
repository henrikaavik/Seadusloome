"""``draft_cleanup`` job handler — post-delete orphan cleanup (#628, #736).

When a user deletes a draft we do the fast, transactional work inline
(drop the row, cancel pending background jobs, clear rag_chunks) and
hand the slow, failure-prone external cleanups off to this handler so
a flaky Jena instance or a missing ciphertext file cannot block the
user or leave the DB in an inconsistent state.

Responsibilities
----------------

1. Delete **every** encrypted file on disk that the draft (and all its
   versions) referenced (idempotent — a missing file counts as success;
   one bad path does not abort the rest of the list).
2. Delete **every** Jena named graph the draft (and all its versions)
   referenced (idempotent — Fuseki 404 is also success, reported as
   ``True`` by :func:`delete_named_graph`; a ``False`` return is a real
   transport/HTTP failure and is treated as a cleanup error, #845 B3).
3. Delete **every** rendered export artifact for the draft in
   ``EXPORT_DIR`` — ``<draft_id>-<report_id>.docx`` / ``.pdf`` /
   ``-summary.docx`` (#845 B2). These are plaintext renderings of the
   encrypted draft, so leaving them behind would defeat the delete.

Why a list (#736)
-----------------

A draft can carry multiple ``draft_versions`` rows, each with its own
``storage_path`` and per-version ``graph_uri``. Those rows cascade-delete
with the parent ``drafts`` row, so unless the lifecycle handler collects
them *before* deleting the draft and passes them all here, older versions'
files and named graphs become undiscoverable orphans. The payload now
carries ``storage_paths`` / ``graph_uris`` arrays; the legacy singular
``storage_path`` / ``graph_uri`` keys are still honoured so any job
enqueued by an older app build (in-flight at deploy time) still works.

The handler is intentionally tolerant of partial-failure *within a
run*: each delete runs inside its own ``try`` so a failure in one step
(or on one path) still attempts the rest. But any failure at all makes
the run raise at the end (#845 B3) — every operation here is idempotent
(missing file / absent graph count as success), so the worker's retry
machinery can safely re-run the whole payload until either everything
is cleaned or the bounded retry budget is exhausted and the job lands
in ``failed`` where admins can see the orphaned sensitive artifacts.
The pre-#845 behaviour (return success after partial progress) silently
orphaned Jena graphs and export files with zero retries.

Registration
------------

``app/docs/__init__.py`` imports this module so
``register_handler("draft_cleanup")`` runs at startup and overrides
the worker's fallback stub.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from app.docs.docx_export import get_export_dir
from app.storage import delete_file as delete_encrypted_file
from app.sync.jena_loader import delete_named_graph

logger = logging.getLogger(__name__)


def _export_artifacts_for_draft(draft_id: str) -> list[Any]:
    """Return every rendered export file in EXPORT_DIR for *draft_id*.

    Export writers key report artifacts as ``<draft_id>-<report_id>``
    with ``.docx`` / ``.pdf`` / ``-summary.docx`` suffixes, so a single
    ``<draft_id>-*`` glob covers all of them. The draft id is required
    to parse as a UUID before it is interpolated into the glob — a
    malformed payload value must never be able to widen the pattern
    (defense in depth; the payload is produced by our own delete route).
    """
    try:
        parsed = uuid.UUID(draft_id)
    except (TypeError, ValueError):
        return []
    try:
        export_dir = get_export_dir()
        return sorted(p for p in export_dir.glob(f"{parsed}-*") if p.is_file())
    except OSError:
        logger.exception(
            "draft_cleanup: failed to scan export dir for draft=%s",
            draft_id,
        )
        return []


def _collect(payload: dict[str, Any], plural_key: str, singular_key: str) -> list[str]:
    """Merge a payload's ``plural_key`` list and legacy ``singular_key`` scalar.

    De-duplicates while preserving first-seen order and drops empties.
    Accepts the legacy ``{storage_path: ..., graph_uri: ...}`` shape so a
    ``draft_cleanup`` job enqueued by an older app build still cleans up
    its single file/graph after a deploy (#736 backward-compat).
    """
    out: list[str] = []
    seen: set[str] = set()
    values = list(payload.get(plural_key) or [])
    legacy = payload.get(singular_key)
    if legacy:
        values.append(legacy)
    for value in values:
        if not value:
            continue
        text = str(value)
        if text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


def draft_cleanup(
    payload: dict[str, Any],
    *,
    attempt: int = 1,
    max_attempts: int = 3,
    job_id: int | None = None,
) -> dict[str, Any]:
    """Delete every encrypted file, Jena graph + export artifact for a removed draft.

    Args:
        payload: ``{"draft_id": "<uuid>",
                    "storage_paths": ["<path>", ...],
                    "graph_uris": ["<uri>", ...],
                    # legacy singular keys still accepted:
                    "storage_path": "<path or None>",
                    "graph_uri": "<uri or None>"}``.
        attempt: 1-based retry counter (unused today but accepted so
            the handler signature matches the worker contract).
        max_attempts: Retry budget (unused; reserved for future
            "mark the draft as orphaned" logic on final attempt).

    Returns:
        ``{"draft_id": ..., "storage_deleted": int, "graph_deleted": int,
           "exports_deleted": int, "storage_total": int,
           "graph_total": int, "exports_total": int}``.

    Raises:
        RuntimeError: when any cleanup step failed (#845 B3). Every step
            is idempotent, so the worker retries the whole payload; once
            the budget is exhausted the job lands in ``failed`` instead
            of silently reporting success over orphaned sensitive data.
    """
    draft_id = str(payload.get("draft_id") or "")
    storage_paths = _collect(payload, "storage_paths", "storage_path")
    graph_uris = _collect(payload, "graph_uris", "graph_uri")
    # #845 (B2): rendered report exports (<draft_id>-<report_id>.docx/.pdf/
    # -summary.docx) are plaintext derivatives of the encrypted draft and
    # must die with it.
    export_paths = _export_artifacts_for_draft(draft_id)

    storage_deleted = 0
    graph_deleted = 0
    exports_deleted = 0
    errors: list[str] = []

    for storage_path in storage_paths:
        try:
            delete_encrypted_file(storage_path)
            storage_deleted += 1
        except FileNotFoundError:
            # Already gone — count as success and keep going.
            storage_deleted += 1
        except Exception as exc:
            errors.append(f"storage[{storage_path}]: {exc}")
            logger.exception(
                "draft_cleanup: failed to delete encrypted file draft=%s path=%s",
                draft_id,
                storage_path,
            )

    for graph_uri in graph_uris:
        try:
            # #845 (B3): delete_named_graph reports failure two ways — an
            # exception OR a ``False`` return (transport error / non-2xx;
            # a Fuseki 404 returns True). Both must count as errors, or a
            # flaky Jena silently orphans a politically sensitive graph
            # with zero retries.
            if delete_named_graph(graph_uri):
                graph_deleted += 1
            else:
                errors.append(f"jena[{graph_uri}]: delete_named_graph returned False")
                logger.error(
                    "draft_cleanup: delete_named_graph reported failure draft=%s uri=%s",
                    draft_id,
                    graph_uri,
                )
        except Exception as exc:
            errors.append(f"jena[{graph_uri}]: {exc}")
            logger.exception(
                "draft_cleanup: failed to delete named graph draft=%s uri=%s",
                draft_id,
                graph_uri,
            )

    for export_path in export_paths:
        try:
            export_path.unlink(missing_ok=True)
            exports_deleted += 1
        except Exception as exc:
            errors.append(f"export[{export_path}]: {exc}")
            logger.exception(
                "draft_cleanup: failed to delete export artifact draft=%s path=%s",
                draft_id,
                export_path,
            )

    # #845 (B3): any failure fails the run so the worker's bounded retry
    # budget engages. Every step above is idempotent (missing file or
    # absent graph counts as success), so re-running the full payload is
    # safe and converges; after the final attempt the job is visibly
    # ``failed`` instead of silently succeeding over orphaned sensitive
    # artifacts. An empty payload (no work) remains a success.
    if errors:
        raise RuntimeError("; ".join(errors))

    return {
        "draft_id": draft_id,
        "storage_deleted": storage_deleted,
        "graph_deleted": graph_deleted,
        "exports_deleted": exports_deleted,
        "storage_total": len(storage_paths),
        "graph_total": len(graph_uris),
        "exports_total": len(export_paths),
    }


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

from app.jobs.worker import register_handler  # noqa: E402

register_handler("draft_cleanup")(draft_cleanup)
