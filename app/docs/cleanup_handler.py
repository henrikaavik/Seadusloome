"""``draft_cleanup`` job handler — post-delete orphan cleanup (#628).

When a user deletes a draft we do the fast, transactional work inline
(drop the row, cancel pending background jobs, clear rag_chunks) and
hand the slow, failure-prone external cleanups off to this handler so
a flaky Jena instance or a missing ciphertext file cannot block the
user or leave the DB in an inconsistent state.

Responsibilities
----------------

1. Delete the encrypted file on disk (idempotent — a missing file
   counts as success).
2. Delete the draft's Jena named graph (idempotent — Fuseki 404 is
   also success).

The handler is intentionally tolerant of partial-failure: we do each
step inside its own ``try`` so a failure in step 2 still leaves step
1 applied, and we only raise when *both* steps fail. The worker's
retry machinery will then re-run us with the same payload until the
retry budget is exhausted.

Registration
------------

``app/docs/__init__.py`` imports this module so
``register_handler("draft_cleanup")`` runs at startup and overrides
the worker's fallback stub.
"""

from __future__ import annotations

import logging
from typing import Any

from app.storage import delete_file as delete_encrypted_file
from app.sync.jena_loader import delete_named_graph

logger = logging.getLogger(__name__)


def draft_cleanup(
    payload: dict[str, Any],
    *,
    attempt: int = 1,
    max_attempts: int = 3,
) -> dict[str, Any]:
    """Delete the encrypted file + Jena named graph for a removed draft.

    Args:
        payload: ``{"draft_id": "<uuid>", "storage_path": "<path or None>",
                    "graph_uri": "<uri or None>"}``.
        attempt: 1-based retry counter (unused today but accepted so
            the handler signature matches the worker contract).
        max_attempts: Retry budget (unused; reserved for future
            "mark the draft as orphaned" logic on final attempt).

    Returns:
        ``{"draft_id": ..., "storage_deleted": bool, "graph_deleted": bool}``.
    """
    draft_id = str(payload.get("draft_id") or "")
    storage_path = payload.get("storage_path")
    graph_uri = payload.get("graph_uri")

    storage_deleted = False
    graph_deleted = False
    errors: list[str] = []

    if storage_path:
        try:
            delete_encrypted_file(str(storage_path))
            storage_deleted = True
        except FileNotFoundError:
            # Already gone — count as success.
            storage_deleted = True
        except Exception as exc:
            errors.append(f"storage: {exc}")
            logger.exception(
                "draft_cleanup: failed to delete encrypted file draft=%s path=%s",
                draft_id,
                storage_path,
            )
    else:
        storage_deleted = True

    if graph_uri:
        try:
            delete_named_graph(str(graph_uri))
            graph_deleted = True
        except Exception as exc:
            errors.append(f"jena: {exc}")
            logger.exception(
                "draft_cleanup: failed to delete named graph draft=%s uri=%s",
                draft_id,
                graph_uri,
            )
    else:
        graph_deleted = True

    if errors and not (storage_deleted or graph_deleted):
        # Both steps failed — re-raise so the worker records the job as
        # failed and consumes a retry attempt. One-of-two partial
        # failures still report success so we don't loop forever on a
        # persistently-missing Jena graph when the file is already gone.
        raise RuntimeError("; ".join(errors))

    return {
        "draft_id": draft_id,
        "storage_deleted": storage_deleted,
        "graph_deleted": graph_deleted,
    }


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

from app.jobs.worker import register_handler  # noqa: E402

register_handler("draft_cleanup")(draft_cleanup)
