"""Background job handler for ``extract_entities``.

Flow:
    1. Load the draft from Postgres and make sure ``parse_draft`` left
       parsed text behind.
    2. Flip status to ``extracting`` so the UI can show a progress
       indicator.
    3. Run the LLM extractor over the parsed text.
    4. Resolve every extracted reference against the ontology.
    5. Insert one ``draft_entities`` row per resolved ref (matched or
       not — unmatched refs are still useful to the user).
    6. Update ``drafts.status = 'analyzing'`` and enqueue the next
       pipeline step (``analyze_impact``).

Any exception along the way flips the draft to ``failed`` with a
truncated error message and re-raises so the job queue retry loop
picks it up.

The handler registers itself via ``@register_handler`` when this
module is imported; ``app/docs/__init__.py`` pulls it in so the
worker's dispatcher sees it at startup.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from uuid import UUID

from app.db import get_connection
from app.docs.draft_model import get_draft, update_draft_status
from app.docs.entity_extractor import extract_refs_from_text
from app.docs.reference_resolver import resolve_refs
from app.jobs import JobQueue
from app.jobs.worker import register_handler

logger = logging.getLogger(__name__)


@register_handler("extract_entities")
def extract_entities(
    payload: dict[str, Any],
    *,
    attempt: int = 1,
    max_attempts: int = 3,
) -> dict[str, Any]:
    """Real ``extract_entities`` handler.

    Args:
        payload: ``{"draft_id": "<uuid str>"}`` — the only input the
            job queue hands us.
        attempt: 1-based current attempt counter. Used to delay the
            ``status='failed'`` flip until the retry budget is
            exhausted (#448).
        max_attempts: Total retry budget for this job.

    Returns:
        Summary dict persisted to ``background_jobs.result``. Includes
        counts for the admin dashboard and the next job type so log
        readers can trace the pipeline without digging through code.

    Raises:
        ValueError: if the draft is missing or has no parsed text.
            The job queue's retry logic will pick this up like any
            other handler exception.
    """
    draft_id_raw = payload.get("draft_id")
    if not draft_id_raw:
        raise ValueError("extract_entities payload missing draft_id")
    draft_id = UUID(str(draft_id_raw))

    # -- 1. Load draft + preconditions ---------------------------------
    with get_connection() as conn:
        draft = get_draft(conn, draft_id)
    if draft is None:
        raise ValueError(f"Draft {draft_id} not found")
    if draft.parsed_text is None or not draft.parsed_text.strip():
        raise ValueError(f"Draft {draft_id} has no parsed text — was parse_draft skipped?")

    # -- 2. Mark 'extracting' ------------------------------------------
    with get_connection() as conn:
        update_draft_status(conn, draft_id, "extracting")
        conn.commit()

    # #469: clear any draft_entities rows left behind by a previous
    # failed attempt. Without this, a retry that succeeds after a
    # partial insert would double-count entities because the final
    # UPDATE only sets ``entity_count`` from ``len(resolved)`` but the
    # table still holds the orphan rows from the prior attempt. Doing
    # this BEFORE the extractor runs keeps the transaction bounded and
    # idempotent — a retry that never reaches step 5 still leaves the
    # table in a known-clean state.
    with get_connection() as conn:
        conn.execute(
            "DELETE FROM draft_entities WHERE draft_id = %s",
            (str(draft_id),),
        )
        conn.commit()

    try:
        # -- 3. Extract refs via LLM -----------------------------------
        extracted = extract_refs_from_text(draft.parsed_text)
        logger.info(
            "extract_entities: extracted %d refs from draft %s",
            len(extracted),
            draft_id,
        )

        # -- 4. Resolve refs against ontology --------------------------
        resolved = resolve_refs(extracted)
        matched_count = sum(1 for r in resolved if r.entity_uri is not None)
        logger.info(
            "extract_entities: resolved %d / %d refs to ontology URIs for draft %s",
            matched_count,
            len(resolved),
            draft_id,
        )

        # -- 5. Persist draft_entities + status transition -------------
        with get_connection() as conn:
            for r in resolved:
                conn.execute(
                    """
                    insert into draft_entities
                        (draft_id, ref_text, entity_uri, confidence, ref_type, location)
                    values (%s, %s, %s, %s, %s, %s)
                    """,
                    (
                        str(draft_id),
                        r.extracted.ref_text,
                        r.entity_uri,
                        r.extracted.confidence,
                        r.extracted.ref_type,
                        json.dumps(r.extracted.location),
                    ),
                )
            conn.execute(
                """
                update drafts
                set entity_count = %s,
                    status = 'analyzing',
                    updated_at = now()
                where id = %s
                """,
                (len(resolved), str(draft_id)),
            )
            conn.commit()

        # -- 6. Enqueue next step --------------------------------------
        JobQueue().enqueue("analyze_impact", {"draft_id": str(draft_id)}, priority=0)

        return {
            "draft_id": str(draft_id),
            "extracted": len(extracted),
            "resolved": matched_count,
            "next_job": "analyze_impact",
        }

    except Exception as exc:
        # Only flip the draft to ``failed`` on the FINAL attempt (#448);
        # earlier attempts re-raise so the queue can retry without the
        # UI showing a misleading permanent-failure state.
        if attempt >= max_attempts:
            try:
                with get_connection() as conn:
                    update_draft_status(
                        conn,
                        draft_id,
                        "failed",
                        error_message=str(exc)[:500],
                    )
                    conn.commit()
            except Exception:  # noqa: BLE001
                logger.exception(
                    "extract_entities: failed to mark draft %s as failed",
                    draft_id,
                )
        else:
            logger.warning(
                "extract_entities attempt %d/%d failed for draft %s, will retry: %s",
                attempt,
                max_attempts,
                draft_id,
                exc,
            )
        raise
