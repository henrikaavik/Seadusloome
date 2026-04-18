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
from app.docs.error_mapping import map_failure_to_user_message
from app.docs.reference_resolver import resolve_refs
from app.jobs import JobQueue
from app.jobs.worker import register_handler
from app.storage import decrypt_text

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
    if draft.parsed_text_encrypted is None:
        raise ValueError(f"Draft {draft_id} has no parsed text — was parse_draft skipped?")
    parsed_text = decrypt_text(draft.parsed_text_encrypted)
    if not parsed_text.strip():
        raise ValueError(f"Draft {draft_id} has no parsed text — was parse_draft skipped?")

    # -- 2. Mark 'extracting' ------------------------------------------
    with get_connection() as conn:
        update_draft_status(conn, draft_id, "extracting")
        conn.commit()

    try:
        # -- 3. Extract refs via LLM -----------------------------------
        extracted = extract_refs_from_text(parsed_text)
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
        #
        # #626: DELETE + INSERT + UPDATE happen in a SINGLE transaction
        # so a crash between any two steps cannot leave
        # ``entity_count != 0`` with zero ``draft_entities`` rows (or
        # the mirror-image, duplicate rows left over from a prior
        # failed attempt).
        #
        # Supersedes the split two-transaction approach from #469. The
        # original #469 rationale ("keep the transaction bounded and
        # idempotent") is still respected — the DELETE runs first
        # inside the same transaction, so the end state is still
        # "exactly the rows this attempt produced". What changed is
        # that a mid-transaction failure now rolls back to the *prior*
        # consistent state (previous attempt's rows, or empty) instead
        # of the empty state, which prevents the entity_count / row
        # count drift #626 describes.
        #
        # The #448 retry-gating pattern still holds: this transaction
        # is inside the try block, so any failure propagates to the
        # except Exception handler below, which only flips the draft
        # to ``failed`` on the final attempt.
        with get_connection() as conn:
            conn.execute(
                "delete from draft_entities where draft_id = %s",
                (str(draft_id),),
            )
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
                    error_message = null,
                    error_debug = null,
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
            user_msg, debug_detail = map_failure_to_user_message(exc, stage="extract")
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
