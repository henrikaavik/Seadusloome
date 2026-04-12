"""``conversations`` and ``messages`` table dataclasses + query helpers.

Mirrors ``migrations/008_chat_tables.sql``.

Every query helper follows the same pattern as
``app/drafter/session_model.py``:

    - Explicit ``conn`` parameter from the caller
    - ``conn.commit()`` on writes is the caller's responsibility
    - Exceptions are logged and the function returns a sentinel value
      (``None`` / empty list) rather than raising, so a dead DB never
      takes down the whole request
    - Org scoping: list queries include ``AND org_id = %s`` where appropriate

Single-item lookups return None if the row doesn't exist; callers are
expected to compare ``conversation.org_id`` against the current user's
``org_id`` for access control.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.db_utils import coerce_uuid, parse_jsonb
from app.storage import DecryptionError, decrypt_text, encrypt_text

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class Conversation:
    """Snapshot of a row in the ``conversations`` table."""

    id: uuid.UUID
    user_id: uuid.UUID
    org_id: uuid.UUID
    title: str
    context_draft_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime


@dataclass
class Message:
    """Snapshot of a row in the ``messages`` table."""

    id: uuid.UUID
    conversation_id: uuid.UUID
    role: str
    content: str
    tool_name: str | None
    tool_input: dict | None
    tool_output: dict | None
    rag_context: list[dict] | None
    tokens_input: int | None
    tokens_output: int | None
    model: str | None
    created_at: datetime


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_CONVERSATION_COLUMNS = "id, user_id, org_id, title, context_draft_id, created_at, updated_at"

# NOTE (#570): SELECT returns both the plaintext and the ``*_encrypted``
# columns. ``_row_to_message`` prefers the encrypted column when set and
# falls back to the plaintext column for rows that predate the backfill
# (migration 014 adds the columns, scripts/migrate_chat_encryption.py
# populates them, a later migration drops the plaintext columns).
_MESSAGE_COLUMNS = (
    "id, conversation_id, role, content, tool_name, tool_input, "
    "tool_output, rag_context, tokens_input, tokens_output, model, created_at, "
    "content_encrypted, tool_input_encrypted, tool_output_encrypted, "
    "rag_context_encrypted"
)


def _decode_encrypted_text(ciphertext: bytes | memoryview | None) -> str | None:
    """Decrypt a BYTEA column; return ``None`` on NULL or decrypt failure.

    Fallback semantics: the caller uses ``None`` to signal "fall back to
    plaintext column". We log decrypt failures loudly because they can only
    mean the key rotated or the ciphertext got corrupted — both operator
    problems that a silent fall-through would hide.
    """
    if ciphertext is None:
        return None
    raw = bytes(ciphertext) if isinstance(ciphertext, memoryview) else ciphertext
    try:
        return decrypt_text(raw)
    except DecryptionError:
        logger.exception("Failed to decrypt message column — falling back to plaintext")
        return None


def _decode_encrypted_json(ciphertext: bytes | memoryview | None) -> Any:
    """Decrypt a BYTEA column and JSON-parse the result.

    Returns ``None`` for NULL inputs, decryption failures, and non-JSON
    payloads. Same fallback semantics as :func:`_decode_encrypted_text`.
    """
    plaintext = _decode_encrypted_text(ciphertext)
    if plaintext is None:
        return None
    try:
        return json.loads(plaintext)
    except (ValueError, TypeError):
        logger.exception("Failed to parse decrypted JSON payload")
        return None


def _row_to_conversation(row: tuple[Any, ...]) -> Conversation:
    """Build a ``Conversation`` from a raw cursor row."""
    (
        conv_id,
        user_id,
        org_id,
        title,
        context_draft_id,
        created_at,
        updated_at,
    ) = row

    return Conversation(
        id=coerce_uuid(conv_id),
        user_id=coerce_uuid(user_id),
        org_id=coerce_uuid(org_id),
        title=title,
        context_draft_id=coerce_uuid(context_draft_id) if context_draft_id else None,
        created_at=created_at,
        updated_at=updated_at,
    )


def _row_to_message(row: tuple[Any, ...]) -> Message:
    """Build a ``Message`` from a raw cursor row.

    Column order matches :data:`_MESSAGE_COLUMNS`. Encrypted columns take
    precedence over their plaintext counterparts; the plaintext fallback
    only matters for rows written before the #570 rollout (see migration
    014 and ``scripts/migrate_chat_encryption.py``).
    """
    (
        msg_id,
        conversation_id,
        role,
        content_plain,
        tool_name,
        tool_input_raw,
        tool_output_raw,
        rag_context_raw,
        tokens_input,
        tokens_output,
        model,
        created_at,
        content_encrypted,
        tool_input_encrypted,
        tool_output_encrypted,
        rag_context_encrypted,
    ) = row

    # Content: prefer encrypted blob, fall back to plaintext TEXT column.
    decrypted_content = _decode_encrypted_text(content_encrypted)
    content = decrypted_content if decrypted_content is not None else (content_plain or "")

    # tool_input: prefer encrypted JSON blob; fall back to plaintext JSONB.
    tool_input = _decode_encrypted_json(tool_input_encrypted)
    if tool_input is None:
        tool_input = parse_jsonb(tool_input_raw)
    if tool_input is not None and not isinstance(tool_input, dict):
        tool_input = None

    tool_output = _decode_encrypted_json(tool_output_encrypted)
    if tool_output is None:
        tool_output = parse_jsonb(tool_output_raw)
    if tool_output is not None and not isinstance(tool_output, dict):
        tool_output = None

    rag_context = _decode_encrypted_json(rag_context_encrypted)
    if rag_context is None:
        rag_context = parse_jsonb(rag_context_raw)
    if rag_context is not None and not isinstance(rag_context, list):
        rag_context = None

    return Message(
        id=coerce_uuid(msg_id),
        conversation_id=coerce_uuid(conversation_id),
        role=role,
        content=content,
        tool_name=tool_name,
        tool_input=tool_input,
        tool_output=tool_output,
        rag_context=rag_context,
        tokens_input=int(tokens_input) if tokens_input is not None else None,
        tokens_output=int(tokens_output) if tokens_output is not None else None,
        model=model,
        created_at=created_at,
    )


# ---------------------------------------------------------------------------
# Conversation CRUD
# ---------------------------------------------------------------------------


def create_conversation(
    conn: Any,
    user_id: uuid.UUID | str,
    org_id: uuid.UUID | str,
    title: str = "Uus vestlus",
    context_draft_id: uuid.UUID | str | None = None,
) -> Conversation:
    """Insert a new ``conversations`` row and return the created conversation.

    The caller is responsible for committing the transaction.
    """
    row = conn.execute(
        f"""
        INSERT INTO conversations (user_id, org_id, title, context_draft_id)
        VALUES (%s, %s, %s, %s)
        RETURNING {_CONVERSATION_COLUMNS}
        """,
        (
            str(user_id),
            str(org_id),
            title,
            str(context_draft_id) if context_draft_id else None,
        ),
    ).fetchone()
    if row is None:
        raise RuntimeError("INSERT ... RETURNING conversations produced no row")
    return _row_to_conversation(row)


def get_conversation(
    conn: Any,
    conv_id: uuid.UUID | str,
) -> Conversation | None:
    """Return a single conversation by id, or ``None``."""
    try:
        row = conn.execute(
            f"SELECT {_CONVERSATION_COLUMNS} FROM conversations WHERE id = %s",
            (str(conv_id),),
        ).fetchone()
    except Exception:
        logger.exception("Failed to fetch conversation id=%s", conv_id)
        return None
    return _row_to_conversation(row) if row else None


def list_conversations_for_user(
    conn: Any,
    user_id: uuid.UUID | str,
    *,
    limit: int = 25,
    offset: int = 0,
) -> list[Conversation]:
    """Return conversations owned by *user_id*, newest first.

    Note: unlike drafting sessions, conversations are scoped by user_id
    only (a user can list their own conversations across orgs they belong
    to). Org-scoped filtering is handled at the route level if needed.
    """
    if limit <= 0:
        return []
    try:
        rows = conn.execute(
            f"""
            SELECT {_CONVERSATION_COLUMNS}
            FROM conversations
            WHERE user_id = %s
            ORDER BY updated_at DESC
            LIMIT %s OFFSET %s
            """,
            (str(user_id), limit, max(0, offset)),
        ).fetchall()
    except Exception:
        logger.exception(
            "Failed to list conversations for user=%s",
            user_id,
        )
        return []
    return [_row_to_conversation(row) for row in rows]


def update_conversation_title(
    conn: Any,
    conv_id: uuid.UUID | str,
    title: str,
) -> None:
    """Update the title (and bump ``updated_at``) of a conversation."""
    conn.execute(
        """
        UPDATE conversations
        SET title = %s, updated_at = now()
        WHERE id = %s
        """,
        (title, str(conv_id)),
    )


def delete_conversation(
    conn: Any,
    conv_id: uuid.UUID | str,
) -> None:
    """Delete a conversation. FK cascade removes associated messages."""
    conn.execute(
        "DELETE FROM conversations WHERE id = %s",
        (str(conv_id),),
    )


# ---------------------------------------------------------------------------
# Message CRUD
# ---------------------------------------------------------------------------


VALID_ROLES = ("system", "user", "assistant", "tool")


def create_message(
    conn: Any,
    conversation_id: uuid.UUID | str,
    role: str,
    content: str,
    *,
    tool_name: str | None = None,
    tool_input: dict | None = None,
    tool_output: dict | None = None,
    rag_context: list[dict] | None = None,
    tokens_input: int | None = None,
    tokens_output: int | None = None,
    model: str | None = None,
) -> Message:
    """Insert a new ``messages`` row and return the created message.

    The caller is responsible for committing the transaction.
    """
    if role not in VALID_ROLES:
        raise ValueError(f"Invalid message role: {role!r}")

    # #570: write payload columns to their ``*_encrypted`` BYTEA siblings via
    # Fernet. The legacy plaintext columns stay NULL on new INSERTs — Phase C
    # migration will drop them entirely. Encrypting here (instead of at the
    # DB layer via pgcrypto) keeps the key inside the app process and lets
    # us reuse the same Fernet primitive as drafts.parsed_text_encrypted.
    content_ciphertext = encrypt_text(content) if content else encrypt_text("")
    tool_input_ciphertext = (
        encrypt_text(json.dumps(tool_input, ensure_ascii=False))
        if tool_input is not None
        else None
    )
    tool_output_ciphertext = (
        encrypt_text(json.dumps(tool_output, ensure_ascii=False))
        if tool_output is not None
        else None
    )
    rag_context_ciphertext = (
        encrypt_text(json.dumps(rag_context, ensure_ascii=False))
        if rag_context is not None
        else None
    )

    row = conn.execute(
        f"""
        INSERT INTO messages
            (conversation_id, role, content, tool_name, tool_input,
             tool_output, rag_context, tokens_input, tokens_output, model,
             content_encrypted, tool_input_encrypted, tool_output_encrypted,
             rag_context_encrypted)
        VALUES (%s, %s, NULL, %s, NULL::jsonb, NULL::jsonb, NULL::jsonb, %s, %s, %s,
                %s, %s, %s, %s)
        RETURNING {_MESSAGE_COLUMNS}
        """,
        (
            str(conversation_id),
            role,
            tool_name,
            tokens_input,
            tokens_output,
            model,
            content_ciphertext,
            tool_input_ciphertext,
            tool_output_ciphertext,
            rag_context_ciphertext,
        ),
    ).fetchone()
    if row is None:
        raise RuntimeError("INSERT ... RETURNING messages produced no row")
    return _row_to_message(row)


def list_messages(
    conn: Any,
    conversation_id: uuid.UUID | str,
) -> list[Message]:
    """Return all messages in a conversation, ordered by ``created_at`` ASC."""
    try:
        rows = conn.execute(
            f"""
            SELECT {_MESSAGE_COLUMNS}
            FROM messages
            WHERE conversation_id = %s
            ORDER BY created_at ASC
            """,
            (str(conversation_id),),
        ).fetchall()
    except Exception:
        logger.exception(
            "Failed to list messages for conversation=%s",
            conversation_id,
        )
        return []
    return [_row_to_message(row) for row in rows]
