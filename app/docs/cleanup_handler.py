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
   referenced (idempotent — Fuseki 404 is also success).

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

The handler is intentionally tolerant of partial-failure: each delete
runs inside its own ``try`` so a failure in one step (or on one path)
still applies the rest, and we only raise when **everything** failed and
nothing was cleaned. The worker's retry machinery will then re-run us
with the same payload until the retry budget is exhausted.

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
    """Delete every encrypted file + Jena named graph for a removed draft.

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
           "storage_total": int, "graph_total": int}``.
    """
    draft_id = str(payload.get("draft_id") or "")
    storage_paths = _collect(payload, "storage_paths", "storage_path")
    graph_uris = _collect(payload, "graph_uris", "graph_uri")

    storage_deleted = 0
    graph_deleted = 0
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
            delete_named_graph(graph_uri)
            graph_deleted += 1
        except Exception as exc:
            errors.append(f"jena[{graph_uri}]: {exc}")
            logger.exception(
                "draft_cleanup: failed to delete named graph draft=%s uri=%s",
                draft_id,
                graph_uri,
            )

    # Re-raise only when *everything* failed — i.e. there was work to do,
    # we attempted it, and not a single file or graph was cleaned. A
    # partial success (some paths/graphs purged) reports success so we
    # don't loop forever on a persistently-missing Jena graph while the
    # files are already gone. An empty payload (no work) is also success.
    attempted = len(storage_paths) + len(graph_uris)
    cleaned = storage_deleted + graph_deleted
    if errors and attempted > 0 and cleaned == 0:
        raise RuntimeError("; ".join(errors))

    return {
        "draft_id": draft_id,
        "storage_deleted": storage_deleted,
        "graph_deleted": graph_deleted,
        "storage_total": len(storage_paths),
        "graph_total": len(graph_uris),
    }


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

from app.jobs.worker import register_handler  # noqa: E402

register_handler("draft_cleanup")(draft_cleanup)
