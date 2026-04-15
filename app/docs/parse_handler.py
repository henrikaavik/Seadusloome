"""``parse_draft`` job handler — the real Tika-backed implementation.

This module replaces the placeholder ``_parse_draft_stub`` in
``app.jobs.worker``. It is registered with the worker's handler
registry via a module-bottom ``register_handler("parse_draft")`` call,
so any import of ``app.docs`` (and hence ``app.docs.parse_handler``
from ``app/docs/__init__.py``) wires the real handler into the worker.

Pipeline
--------

1. Load the draft row.
2. Flip status ``uploaded`` → ``parsing``.
3. Read the encrypted file from disk and decrypt it.
4. Send the plaintext bytes to Tika's ``PUT /tika`` endpoint.
5. Guard against an empty result (Tika returns ``""`` for image-only
   PDFs and corrupt files — without this check downstream extraction
   would pointlessly call the LLM with nothing to analyse).
6. Store ``parsed_text`` and flip status to ``extracting``.
7. Enqueue an ``extract_entities`` follow-up job so the parallel
   extractor agent's handler picks up from here.

Failure handling
----------------

Any exception in steps 3-7 is turned into a ``failed`` status with a
500-char truncated error message, then re-raised so the worker loop
also records the job as failed and consumes a retry attempt. We do
*not* update the draft status to ``failed`` before flipping to
``parsing`` in step 2 — if step 1 (``get_draft``) returns ``None`` we
raise a ``ValueError`` and the worker handles it the same way it
handles any other handler exception (no draft row exists to mark).

Security note (#488): ``parsed_text_encrypted`` is a Fernet-encrypted
BYTEA column (migration 006). The plaintext is encrypted via
``encrypt_text`` before the UPDATE so no cleartext legislative content
ever lands in Postgres.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from app.db import get_connection
from app.docs.draft_model import get_draft, update_draft_status
from app.docs.error_mapping import map_failure_to_user_message
from app.docs.tika_client import TikaError, get_default_tika_client
from app.jobs import JobQueue
from app.storage import DecryptionError, encrypt_text, read_file

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


def parse_draft(
    payload: dict[str, Any],
    *,
    attempt: int = 1,
    max_attempts: int = 3,
) -> dict[str, Any]:
    """Parse an uploaded draft's file via Apache Tika.

    Args:
        payload: Must carry ``draft_id`` (a UUID-serialisable string).
        attempt: 1-based index of the current attempt. Passed by the
            worker so we can hold off on flipping the draft row to
            ``failed`` until the retry budget is exhausted (#448).
        max_attempts: Total retry budget for this job.

    Returns:
        ``{"draft_id": ..., "text_length": N, "next_job": "extract_entities"}``
        on success. This value is persisted in ``background_jobs.result``
        by the worker.

    Raises:
        ValueError: The draft row is missing, or Tika returned empty text.
        TikaError: The Tika HTTP call failed.
        FileNotFoundError: The encrypted file is missing on disk.
        DecryptionError: The ciphertext could not be decrypted.

    All of the above are caught by :class:`app.jobs.worker.JobWorker`,
    which flips the job to ``failed`` (or schedules a retry) and logs
    the traceback. The draft row is only flipped to ``status='failed'``
    on the *final* attempt — earlier failures simply re-raise so the
    job queue's exponential backoff loop can retry without misleading
    the UI into showing a permanent failure (#448).
    """
    raw_id = payload.get("draft_id")
    if not raw_id:
        raise ValueError("parse_draft payload missing required 'draft_id'")
    draft_id = UUID(str(raw_id))

    logger.info("Parsing draft %s...", draft_id)

    # Step 1: load the draft row. If it's gone there's nothing to do.
    with get_connection() as conn:
        draft = get_draft(conn, draft_id)
    if draft is None:
        raise ValueError(f"Draft {draft_id} not found")

    # Step 2: mark parsing. This happens in its own transaction so the
    # UI/progress polling sees the state flip even if Tika hangs.
    with get_connection() as conn:
        update_draft_status(conn, draft_id, "parsing")
        conn.commit()

    try:
        # Step 3: read + decrypt.
        file_bytes = read_file(draft.storage_path)
        logger.info("Read %d decrypted bytes for draft %s", len(file_bytes), draft_id)

        # Step 4: send to Tika.
        client = get_default_tika_client()
        parsed_text = client.extract_text(file_bytes, draft.content_type)
        logger.info("Tika returned %d chars for draft %s", len(parsed_text), draft_id)

        # Step 5: guard against empty results. Tika happily returns ""
        # for image-only PDFs and corrupt .docx files; without this
        # check the extract_entities job would chew LLM tokens on
        # nothing and produce a confusing "no entities found" result.
        if not parsed_text.strip():
            raise ValueError("Tika returned empty text — file may be corrupt or an image-only PDF")

        # Step 6: encrypt parsed text and persist alongside status flip.
        # Use a direct UPDATE because ``update_draft_status`` doesn't
        # know about the parsed_text_encrypted column; the two need to
        # land in the same row update so observers never see an
        # inconsistent (parsed_text_encrypted NULL, status=extracting)
        # snapshot. encrypt_text raises RuntimeError if the key is unset
        # in prod — that propagates to the except block below and flips
        # the draft to failed on the final attempt.
        encrypted = encrypt_text(parsed_text)
        with get_connection() as conn:
            conn.execute(
                """
                update drafts
                set parsed_text_encrypted = %s,
                    status = 'extracting',
                    error_message = null,
                    error_debug = null,
                    updated_at = now()
                where id = %s
                """,
                (encrypted, str(draft_id)),
            )
            conn.commit()

        # Step 7: enqueue the next stage. A failure here should not
        # leave the draft in 'extracting' without a pending job, so we
        # let the exception propagate and mark the draft failed below.
        queue = JobQueue()
        queue.enqueue("extract_entities", {"draft_id": str(draft_id)}, priority=0)
        logger.info("Enqueued extract_entities for draft %s", draft_id)

        return {
            "draft_id": str(draft_id),
            "text_length": len(parsed_text),
            "next_job": "extract_entities",
        }

    except (TikaError, FileNotFoundError, DecryptionError, ValueError) as exc:
        # Only flip the draft to ``failed`` on the FINAL attempt (#448).
        # Earlier attempts just re-raise so the worker increments the
        # retry counter and re-schedules the job — keeping the user
        # from seeing "Ebaõnnestus" while a retry is still pending.
        if attempt >= max_attempts:
            logger.error(
                "parse_draft permanently failed for draft %s after %d attempts: %s",
                draft_id,
                attempt,
                exc,
            )
            _mark_draft_failed(draft_id, exc)
        else:
            logger.warning(
                "parse_draft attempt %d/%d failed for draft %s, will retry: %s",
                attempt,
                max_attempts,
                draft_id,
                exc,
            )
        raise
    except Exception as exc:  # noqa: BLE001 — belt-and-braces for handler guarantees
        # Unknown failure modes follow the same retry logic. The
        # worker's outer ``except Exception`` still records the job
        # row's failure regardless.
        if attempt >= max_attempts:
            logger.exception(
                "parse_draft unexpected permanent error for draft %s after %d attempts",
                draft_id,
                attempt,
            )
            _mark_draft_failed(draft_id, exc)
        else:
            logger.warning(
                "parse_draft unexpected error on attempt %d/%d for draft %s, will retry: %s",
                attempt,
                max_attempts,
                draft_id,
                exc,
            )
        raise


def _mark_draft_failed(draft_id: UUID, exc: BaseException) -> None:
    """Best-effort transition of a draft into ``status='failed'``.

    Translates ``exc`` through :func:`map_failure_to_user_message` so
    ``drafts.error_message`` carries an actionable Estonian string (#609)
    and ``drafts.error_debug`` carries the raw technical detail for
    admin triage.

    Swallows DB errors so a secondary failure (e.g. Postgres went away
    mid-handler) never masks the original exception that triggered this
    call. The original exception is still re-raised by the caller.
    """
    user_msg, debug_detail = map_failure_to_user_message(exc, stage="parse")
    try:
        with get_connection() as conn:
            conn.execute(
                """
                update drafts
                set status = 'failed',
                    error_message = %s,
                    error_debug = %s,
                    updated_at = now()
                where id = %s
                """,
                (user_msg[:500], debug_detail, str(draft_id)),
            )
            conn.commit()
    except Exception:  # noqa: BLE001 — best-effort cleanup
        logger.exception("Failed to mark draft %s as failed", draft_id)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------
#
# Importing this module has the side effect of registering
# ``parse_draft`` with the worker's handler registry. ``app/docs/__init__.py``
# imports this module so any code path that touches the docs package
# (route registration, tests, the worker itself via app.main) triggers
# the registration.

from app.jobs.worker import register_handler  # noqa: E402

register_handler("parse_draft")(parse_draft)
