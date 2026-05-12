"""Lifecycle mutation handlers for /drafts (#623 partial split).

Extracted from ``app/docs/routes/__init__.py`` so each mutation handler
lives next to its peers and ``__init__.py`` shrinks toward a thin
registration shim. This is the FIRST extraction of the #623 routes
split; ``link_vtk_handler``, ``new_draft_page``, ``create_draft_handler``,
``drafts_list_page``, and ``draft_detail_page`` still live in the
package's ``__init__.py`` and will move in follow-up PRs.

Routes registered:

    POST /drafts/{draft_id}/delete   — delete_draft_handler
    POST /drafts/{draft_id}/keep     — keep_draft_handler

Both routes are owner-only per ``app.auth.policy.can_delete_draft``;
cross-org or non-owner callers receive 404 (never 403) so existence
is not leaked.
"""

from __future__ import annotations

import logging

from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

from app.auth.audit import log_action
from app.auth.helpers import require_auth as _require_auth
from app.auth.policy import can_delete_draft
from app.db import get_connection as _connect
from app.docs._helpers import _not_found_page, _parse_uuid
from app.docs.audit import log_draft_delete
from app.docs.draft_model import (
    delete_draft,
    fetch_draft,
    get_draft_artifact_paths,
    touch_draft_access,
)
from app.jobs.queue import JobQueue
from app.rag.retriever import delete_chunks_for_draft
from app.ui.feedback.flash import push_flash

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# POST /drafts/{draft_id}/delete
# ---------------------------------------------------------------------------


def delete_draft_handler(req: Request, draft_id: str):
    """POST /drafts/{draft_id}/delete — remove the draft + encrypted file.

    Owner-only per NFR §5 matrix (fixed by #568). Any same-org colleague
    used to be able to delete another user's draft because the handler
    authorized on ``org_id`` alone. The helper in ``app.auth.policy``
    enforces the full rule: owner OR system admin.
    """
    auth_or_redirect = _require_auth(req)
    if isinstance(auth_or_redirect, Response):
        return auth_or_redirect
    auth = auth_or_redirect

    parsed = _parse_uuid(draft_id)
    if parsed is None:
        return _not_found_page(req)

    draft = fetch_draft(parsed)
    if draft is None:
        return _not_found_page(req)
    if not can_delete_draft(auth, draft):
        # Return 404 rather than 403 so we don't leak existence of the
        # draft to cross-org or non-owner callers.
        return _not_found_page(req)

    # #628: single transaction for every DB-side deletion. Previously
    # the handler opened two separate ``_connect()`` contexts (row +
    # rag_chunks, then background_jobs cancellation) and sandwiched
    # two external calls (encrypted file + Jena graph) between them
    # with no boundary. A failure partway through left the system in
    # an inconsistent state: row gone but jobs still pending, or jobs
    # cancelled but encrypted file still on disk. The refactor folds
    # every DB mutation into ONE transactional block and hands the
    # slow/flaky external cleanup off to a background ``draft_cleanup``
    # job that can retry independently.
    storage_paths: list[str] = []
    graph_uris: list[str] = []
    try:
        with _connect() as conn:
            # #736: a draft can carry multiple ``draft_versions`` rows,
            # each with its own encrypted file + per-version Jena named
            # graph. Those rows cascade-delete with the parent ``drafts``
            # row, so we MUST snapshot every artifact path BEFORE the
            # delete — otherwise older versions' files/graphs become
            # undiscoverable orphans. ``get_draft_artifact_paths`` reads
            # ``draft_versions`` + the legacy ``drafts`` columns and
            # de-dupes.
            storage_paths, graph_uris = get_draft_artifact_paths(conn, parsed)
            delete_draft(conn, parsed)
            # #576: polymorphic soft reference — clear any rag_chunks rows
            # tied to this draft inside the same transaction as the row
            # delete so either both land or neither does. Today no private
            # draft ingestion exists so this is a no-op, but wiring it now
            # means future ingestion code can't forget.
            try:
                delete_chunks_for_draft(conn, parsed)
            except Exception:
                logger.exception("Failed to delete rag_chunks for draft id=%s", parsed)
            # #454/#478: cancel any pending/claimed/running/retrying
            # background jobs that still reference this draft. Doing
            # this in the SAME transaction as the row delete means a
            # rollback doesn't strand orphaned jobs on the queue.
            # #478 added ``running`` because a worker that picked up
            # the job just before deletion would otherwise leave the
            # row behind and produce a spurious failure.
            conn.execute(
                """
                DELETE FROM background_jobs
                WHERE payload->>'draft_id' = %s
                  AND status IN ('pending', 'claimed', 'running', 'retrying')
                """,
                (str(parsed),),
            )
            conn.commit()
    except Exception:
        logger.exception("Failed to delete draft id=%s", parsed)
        return _not_found_page(req)

    # #628: enqueue an async cleanup job for the external effects that
    # can fail independently of the user-visible delete. The job
    # retries on its own schedule; a flaky Jena instance no longer
    # blocks the user flow or leaves the DB inconsistent. Failure to
    # enqueue is logged but non-fatal — the DB is already clean and
    # the operator can always delete the file/graph manually.
    #
    # #736: the payload carries arrays — one entry per draft version —
    # so the handler purges every file + named graph, not just the
    # latest. ``draft.graph_uri`` (the latest version's URI) is folded
    # into ``graph_uris`` by ``get_draft_artifact_paths`` already, but
    # we union it again defensively in case the snapshot raced an edit.
    # The legacy singular ``storage_path``/``graph_uri`` keys are kept
    # so the older handler shape (if a worker hasn't redeployed yet)
    # still cleans up at least the primary artifact.
    if draft.graph_uri and draft.graph_uri not in graph_uris:
        graph_uris.append(draft.graph_uri)
    primary_path = storage_paths[0] if storage_paths else None
    primary_graph = graph_uris[0] if graph_uris else draft.graph_uri
    cleanup_payload = {
        "draft_id": str(parsed),
        "storage_paths": storage_paths,
        "graph_uris": graph_uris,
        # Backward-compat for an older in-flight handler build:
        "storage_path": primary_path,
        "graph_uri": primary_graph,
    }
    try:
        cleanup_job_id = JobQueue().enqueue("draft_cleanup", cleanup_payload, priority=0)
        logger.info(
            "Orphan cleanup job enqueued draft=%s job_id=%s files=%d graphs=%d",
            parsed,
            cleanup_job_id,
            len(storage_paths),
            len(graph_uris),
        )
    except Exception:
        logger.exception(
            "Failed to enqueue draft_cleanup job for draft id=%s — "
            "external resources may be orphaned",
            parsed,
        )

    log_draft_delete(
        auth.get("id"),
        parsed,
        filename=draft.filename,
    )

    # #598: queue a success toast for the drafts listing page.
    push_flash(req, "Eelnõu kustutatud.", kind="success")

    # #467: when the browser drives the delete via HTMX (the form has
    # ``hx_post`` + ``hx_target='body'`` + ``hx_swap='outerHTML'`` — see
    # ``_draft_detail_body``), returning a plain 303 here makes HTMX
    # follow the redirect as an AJAX GET, fetch the drafts-list partial
    # (whose first element is a ``<title>`` tag from ``PageShell``), and
    # swap that entire partial into ``<body>``. The rendered page ends
    # up with a ``<title>`` inside the body, which browsers treat as
    # invalid HTML and render as visible text — corrupting the layout.
    #
    # The fix is to detect HTMX requests and return an empty 204 with an
    # ``HX-Redirect`` header so HTMX performs a **real** browser
    # navigation to ``/drafts`` instead of swapping. Non-HTMX clients
    # (JS-disabled users hitting the native form action) still get the
    # 303 redirect.
    if req.headers.get("HX-Request") == "true":
        return Response(
            status_code=204,
            headers={"HX-Redirect": "/drafts"},
        )
    return RedirectResponse(url="/drafts", status_code=303)


# ---------------------------------------------------------------------------
# POST /drafts/{draft_id}/keep — reset last_accessed_at (#572)
# ---------------------------------------------------------------------------


def keep_draft_handler(req: Request, draft_id: str):
    """POST /drafts/{draft_id}/keep — reset the 90-day archive clock.

    Owner-only per the same policy as delete — resetting the archive
    clock is a governance action that re-commits the org to retaining
    the draft for another 90 days. Same-org reviewers and admins MUST
    NOT be able to bypass the owner's intent to let a stale draft
    auto-warn.
    """
    auth_or_redirect = _require_auth(req)
    if isinstance(auth_or_redirect, Response):
        return auth_or_redirect
    auth = auth_or_redirect

    parsed = _parse_uuid(draft_id)
    if parsed is None:
        return _not_found_page(req)

    draft = fetch_draft(parsed)
    if draft is None:
        return _not_found_page(req)
    if not can_delete_draft(auth, draft):
        # 404 (not 403) — see delete_draft_handler for the reasoning.
        return _not_found_page(req)

    try:
        with _connect() as conn:
            touch_draft_access(conn, parsed)
            conn.commit()
    except Exception:
        logger.exception("Failed to reset last_accessed_at for draft=%s", parsed)
        return _not_found_page(req)

    log_action(
        auth.get("id"),
        "draft.keep",
        {"draft_id": str(parsed)},
    )

    # #598: queue a success toast for the detail page redirect target.
    push_flash(req, "90-päevane loendur lähtestatud.", kind="success")

    # HTMX-driven submits get an HX-Redirect so the browser performs a
    # real navigation rather than swapping a partial into <body>.
    if req.headers.get("HX-Request") == "true":
        return Response(
            status_code=204,
            headers={"HX-Redirect": f"/drafts/{parsed}"},
        )
    return RedirectResponse(url=f"/drafts/{parsed}", status_code=303)
