"""Backfill script for ticket #570 — encrypt chat message columns at rest.

Migration 014 adds ``content_encrypted``, ``tool_input_encrypted``,
``tool_output_encrypted`` and ``rag_context_encrypted`` BYTEA columns to
the ``messages`` table alongside the legacy plaintext columns. This
script reads every row, encrypts the plaintext payload columns with the
application-wide Fernet key (``STORAGE_ENCRYPTION_KEY``), and writes the
ciphertext into the new ``*_encrypted`` columns.

Safety properties:
    - Idempotent. Rows where ``content_encrypted`` is already set are
      skipped, so re-running the script after a partial run is safe.
    - Does NOT null out the plaintext columns. A separate follow-up
      migration (Phase C — not this ticket) will drop them once the
      backfill has been verified in production.
    - Batched. Processes ``BATCH_SIZE`` rows per transaction so a long
      run can be interrupted without rolling back the whole table.

Usage:
    DATABASE_URL=postgresql://... STORAGE_ENCRYPTION_KEY=... \\
        uv run python -m scripts.migrate_chat_encryption

Environment:
    DATABASE_URL             PostgreSQL DSN (same default as scripts/migrate.py)
    STORAGE_ENCRYPTION_KEY   Fernet key (REQUIRED — will not run without it)
    BATCH_SIZE               Rows per transaction (default 500)
"""

from __future__ import annotations

import json
import logging
import os
import sys
from typing import Any

import psycopg

from app.storage import encrypt_text

logger = logging.getLogger(__name__)


DEFAULT_DATABASE_URL = "postgresql://seadusloome:localdev@localhost:5432/seadusloome"


def _get_database_url() -> str:
    return os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL)


def _get_batch_size() -> int:
    raw = os.environ.get("BATCH_SIZE", "500")
    try:
        value = int(raw)
    except ValueError:
        return 500
    return max(1, value)


def _serialize_json(payload: Any) -> str | None:
    """Convert a JSONB column value back to a canonical JSON string.

    psycopg returns JSONB columns as already-parsed Python objects, so we
    ``json.dumps`` them here before handing the bytes to Fernet. Returns
    ``None`` when the input is NULL.
    """
    if payload is None:
        return None
    if isinstance(payload, str):
        # Some drivers may hand back the raw text; trust it as already JSON.
        return payload
    return json.dumps(payload, ensure_ascii=False)


def _encrypt_row(
    content: str | None,
    tool_input: Any,
    tool_output: Any,
    rag_context: Any,
) -> tuple[bytes, bytes | None, bytes | None, bytes | None]:
    """Return Fernet ciphertexts for each payload column.

    ``content`` is required by the schema today, so we encrypt an empty
    string when it is unexpectedly NULL — mirroring the fallback that
    ``_row_to_message`` does for malformed rows. JSONB columns stay
    NULL-in / NULL-out so the encrypted column matches the plaintext.
    """
    content_ct = encrypt_text(content if content is not None else "")
    tool_input_ct = (
        encrypt_text(_serialize_json(tool_input) or "") if tool_input is not None else None
    )
    tool_output_ct = (
        encrypt_text(_serialize_json(tool_output) or "") if tool_output is not None else None
    )
    rag_context_ct = (
        encrypt_text(_serialize_json(rag_context) or "") if rag_context is not None else None
    )
    return content_ct, tool_input_ct, tool_output_ct, rag_context_ct


def _count_pending(conn: psycopg.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) FROM messages WHERE content_encrypted IS NULL").fetchone()
    return int(row[0]) if row else 0


def _fetch_batch(conn: psycopg.Connection, batch_size: int) -> list[tuple[Any, ...]]:
    return conn.execute(
        """
        SELECT id, content, tool_input, tool_output, rag_context
        FROM messages
        WHERE content_encrypted IS NULL
        ORDER BY created_at ASC
        LIMIT %s
        """,
        (batch_size,),
    ).fetchall()


def _write_batch(conn: psycopg.Connection, batch: list[tuple[Any, ...]]) -> int:
    """Encrypt + UPDATE each row in *batch*; return the number updated.

    Uses a single transaction per batch so a crash in the middle of the
    script leaves the table either fully-batch-encrypted or untouched.
    """
    updated = 0
    with conn.cursor() as cur:
        for msg_id, content, tool_input, tool_output, rag_context in batch:
            content_ct, ti_ct, to_ct, rc_ct = _encrypt_row(
                content, tool_input, tool_output, rag_context
            )
            cur.execute(
                """
                UPDATE messages
                SET content_encrypted = %s,
                    tool_input_encrypted = %s,
                    tool_output_encrypted = %s,
                    rag_context_encrypted = %s
                WHERE id = %s
                  AND content_encrypted IS NULL
                """,
                (content_ct, ti_ct, to_ct, rc_ct, msg_id),
            )
            updated += cur.rowcount or 0
    conn.commit()
    return updated


def migrate() -> int:
    """Run the backfill. Returns the total number of rows encrypted."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if not os.environ.get("STORAGE_ENCRYPTION_KEY") and os.environ.get("APP_ENV") == "production":
        logger.error("STORAGE_ENCRYPTION_KEY must be set in production")
        return 1

    database_url = _get_database_url()
    batch_size = _get_batch_size()
    logger.info("Starting chat encryption backfill (batch_size=%d)", batch_size)

    total_updated = 0
    with psycopg.connect(database_url) as conn:
        pending = _count_pending(conn)
        logger.info("Rows pending encryption: %d", pending)

        while True:
            batch = _fetch_batch(conn, batch_size)
            if not batch:
                break
            updated = _write_batch(conn, batch)
            total_updated += updated
            logger.info(
                "Encrypted batch: %d rows (total=%d / %d)",
                updated,
                total_updated,
                pending,
            )
            if updated == 0:
                # Defensive exit: if a batch returned no rows but the fetch
                # gave us some, something is racing — bail rather than loop.
                logger.warning("Fetched non-empty batch but updated zero rows; stopping.")
                break

        remaining = _count_pending(conn)
        logger.info("Backfill complete. Remaining unencrypted rows: %d", remaining)

    return 0


if __name__ == "__main__":
    sys.exit(migrate())
