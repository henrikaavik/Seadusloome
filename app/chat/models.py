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
    # Migration 017 — Vestlus UX polish (pin / archive / custom-title).
    # Defaulted so older DB rows and unit-test fixtures (which may still
    # ship pre-017 column layouts) continue to load cleanly.
    is_pinned: bool = False
    is_archived: bool = False
    pinned_at: datetime | None = None
    title_is_custom: bool = False


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
    # Migration 017 — per-message pin and partial-generation truncation
    # flags. Defaulted for backward compatibility with pre-017 fixtures.
    is_pinned: bool = False
    is_truncated: bool = False


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

# Migration 017 adds ``is_pinned``, ``is_archived``, ``pinned_at`` and
# ``title_is_custom``. They are appended at the end of the SELECT list so
# pre-017 test fixtures (which build 7-tuple rows) still map cleanly via
# :func:`_row_to_conversation`, which indexes defensively.
_CONVERSATION_COLUMNS = (
    "id, user_id, org_id, title, context_draft_id, created_at, updated_at, "
    "is_pinned, is_archived, pinned_at, title_is_custom"
)

# NOTE (#570): SELECT returns both the plaintext and the ``*_encrypted``
# columns. ``_row_to_message`` prefers the encrypted column when set and
# falls back to the plaintext column for rows that predate the backfill
# (migration 014 adds the columns, scripts/migrate_chat_encryption.py
# populates them, a later migration drops the plaintext columns).
_MESSAGE_COLUMNS = (
    "id, conversation_id, role, content, tool_name, tool_input, "
    "tool_output, rag_context, tokens_input, tokens_output, model, created_at, "
    "content_encrypted, tool_input_encrypted, tool_output_encrypted, "
    "rag_context_encrypted, is_pinned, is_truncated"
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
    """Build a ``Conversation`` from a raw cursor row.

    Indexing is defensive: the migration-017 pin / archive / title-is-custom
    columns are read via positional lookup with ``None``/``False`` fallbacks
    so pre-017 test fixtures (7-tuple rows) and freshly-migrated prod rows
    (11-tuple rows) both load cleanly.
    """
    conv_id = row[0]
    user_id = row[1]
    org_id = row[2]
    title = row[3]
    context_draft_id = row[4]
    created_at = row[5]
    updated_at = row[6]

    is_pinned = bool(row[7]) if len(row) > 7 and row[7] is not None else False
    is_archived = bool(row[8]) if len(row) > 8 and row[8] is not None else False
    pinned_at = row[9] if len(row) > 9 else None
    title_is_custom = bool(row[10]) if len(row) > 10 and row[10] is not None else False

    return Conversation(
        id=coerce_uuid(conv_id),
        user_id=coerce_uuid(user_id),
        org_id=coerce_uuid(org_id),
        title=title,
        context_draft_id=coerce_uuid(context_draft_id) if context_draft_id else None,
        created_at=created_at,
        updated_at=updated_at,
        is_pinned=is_pinned,
        is_archived=is_archived,
        pinned_at=pinned_at,
        title_is_custom=title_is_custom,
    )


def _row_to_message(row: tuple[Any, ...]) -> Message:
    """Build a ``Message`` from a raw cursor row.

    Column order matches :data:`_MESSAGE_COLUMNS`. Encrypted columns take
    precedence over their plaintext counterparts; the plaintext fallback
    only matters for rows written before the #570 rollout (see migration
    014 and ``scripts/migrate_chat_encryption.py``).
    """
    msg_id = row[0]
    conversation_id = row[1]
    role = row[2]
    content_plain = row[3]
    tool_name = row[4]
    tool_input_raw = row[5]
    tool_output_raw = row[6]
    rag_context_raw = row[7]
    tokens_input = row[8]
    tokens_output = row[9]
    model = row[10]
    created_at = row[11]
    content_encrypted = row[12]
    tool_input_encrypted = row[13]
    tool_output_encrypted = row[14]
    rag_context_encrypted = row[15]
    # Migration 017 — pin / truncated flags. Defensive indexing so pre-017
    # test fixtures that build 16-tuple rows continue to work.
    is_pinned = bool(row[16]) if len(row) > 16 and row[16] is not None else False
    is_truncated = bool(row[17]) if len(row) > 17 and row[17] is not None else False

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
        is_pinned=is_pinned,
        is_truncated=is_truncated,
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
    include_archived: bool = False,
    pinned_first: bool = True,
    search: str | None = None,
) -> list[Conversation]:
    """Return conversations owned by *user_id*.

    Ordering:
      * ``pinned_first=True`` (default) — pinned conversations float to the
        top, ordered by ``pinned_at DESC NULLS LAST`` then ``updated_at DESC``.
      * ``pinned_first=False`` — order purely by ``updated_at DESC``.

    Filters:
      * ``include_archived=False`` (default) — excludes archived rows; the
        partial index ``idx_conversations_active`` supports this path.
      * ``search`` — case-insensitive substring match on ``title`` via
        ``ILIKE`` (v1; a pgvector-backed semantic search lands later).

    Note: unlike drafting sessions, conversations are scoped by user_id
    only (a user can list their own conversations across orgs they belong
    to). Org-scoped filtering is handled at the route level if needed.
    """
    if limit <= 0:
        return []

    where_clauses = ["user_id = %s"]
    params: list[Any] = [str(user_id)]

    if not include_archived:
        where_clauses.append("is_archived = FALSE")

    if search:
        # ILIKE substring match; escape LIKE metacharacters so user-typed
        # ``%`` / ``_`` / ``\`` match literally instead of acting as
        # wildcards. ``ESCAPE '\'`` tells PostgreSQL to treat the
        # backslash as the escape character for the pattern.
        escaped = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        where_clauses.append("title ILIKE %s ESCAPE '\\'")
        params.append(f"%{escaped}%")

    if pinned_first:
        order_by = "is_pinned DESC, pinned_at DESC NULLS LAST, updated_at DESC"
    else:
        order_by = "updated_at DESC"

    where_sql = " AND ".join(where_clauses)
    params.extend([limit, max(0, offset)])

    try:
        rows = conn.execute(
            f"""
            SELECT {_CONVERSATION_COLUMNS}
            FROM conversations
            WHERE {where_sql}
            ORDER BY {order_by}
            LIMIT %s OFFSET %s
            """,
            tuple(params),
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
    *,
    is_custom: bool = False,
) -> None:
    """Update the title (and bump ``updated_at``) of a conversation.

    ``is_custom=True`` flips ``title_is_custom`` on so the auto-title
    background job will not overwrite a name the user explicitly set.
    ``is_custom=False`` (the default, used by the auto-title job) must
    *not* clobber a previously-set custom flag — otherwise running the
    auto-titler after a user rename would re-open the row to auto-title
    overwrites. The ``CASE`` expression keeps the flag sticky: we only
    ever transition FALSE → TRUE, never TRUE → FALSE.
    """
    conn.execute(
        """
        UPDATE conversations
        SET title = %s,
            title_is_custom = CASE WHEN %s THEN TRUE ELSE title_is_custom END,
            updated_at = now()
        WHERE id = %s
        """,
        (title, bool(is_custom), str(conv_id)),
    )


def set_conversation_pinned(
    conn: Any,
    conv_id: uuid.UUID | str,
    pinned: bool,
) -> None:
    """Pin or unpin a conversation.

    Sets ``pinned_at`` to ``now()`` on pin and ``NULL`` on unpin so the
    pinned-first ordering reflects the most-recently-pinned conversation
    at the top of the list. Does not touch ``updated_at`` — pinning is a
    UI affordance, not an edit.
    """
    conn.execute(
        """
        UPDATE conversations
        SET is_pinned = %s,
            pinned_at = CASE WHEN %s THEN now() ELSE NULL END
        WHERE id = %s
        """,
        (bool(pinned), bool(pinned), str(conv_id)),
    )


def set_conversation_archived(
    conn: Any,
    conv_id: uuid.UUID | str,
    archived: bool,
) -> None:
    """Archive or unarchive a conversation.

    Archived conversations are hidden from the default list view but are
    not deleted; the user can restore them from the archive panel.
    """
    conn.execute(
        """
        UPDATE conversations
        SET is_archived = %s
        WHERE id = %s
        """,
        (bool(archived), str(conv_id)),
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


# ---------------------------------------------------------------------------
# Message pin / truncation / delete helpers (migration 017)
# ---------------------------------------------------------------------------


def set_message_pinned(
    conn: Any,
    message_id: uuid.UUID | str,
    pinned: bool,
) -> None:
    """Pin or unpin an individual message inside a conversation."""
    conn.execute(
        "UPDATE messages SET is_pinned = %s WHERE id = %s",
        (bool(pinned), str(message_id)),
    )


def update_message_truncated(
    conn: Any,
    message_id: uuid.UUID | str,
    truncated: bool = True,
) -> None:
    """Mark an assistant message as truncated (stop-generation path).

    The orchestrator currently writes this column with raw SQL — this
    helper exists for parity with the other setters and to give tests a
    single entry point to assert against.
    """
    conn.execute(
        "UPDATE messages SET is_truncated = %s WHERE id = %s",
        (bool(truncated), str(message_id)),
    )


def list_pinned_messages(
    conn: Any,
    conv_id: uuid.UUID | str,
) -> list[Message]:
    """Return all pinned messages in a conversation, oldest first."""
    try:
        rows = conn.execute(
            f"""
            SELECT {_MESSAGE_COLUMNS}
            FROM messages
            WHERE conversation_id = %s AND is_pinned = TRUE
            ORDER BY created_at ASC
            """,
            (str(conv_id),),
        ).fetchall()
    except Exception:
        logger.exception(
            "Failed to list pinned messages for conversation=%s",
            conv_id,
        )
        return []
    return [_row_to_message(row) for row in rows]


def delete_messages_after(
    conn: Any,
    conv_id: uuid.UUID | str,
    after_created_at: datetime,
) -> int:
    """Delete all messages strictly after *after_created_at* in the thread.

    Used by the edit-and-resend flow to drop downstream messages when the
    user rewrites an earlier turn. The boundary message itself
    (``created_at == after_created_at``) is kept. Returns the number of
    rows deleted; returns ``0`` on DB error (logged) so callers can treat
    the operation as a no-op.
    """
    try:
        cursor = conn.execute(
            """
            DELETE FROM messages
            WHERE conversation_id = %s AND created_at > %s
            """,
            (str(conv_id), after_created_at),
        )
    except Exception:
        logger.exception(
            "Failed to delete messages after %s for conversation=%s",
            after_created_at,
            conv_id,
        )
        return 0
    # psycopg exposes rowcount on the cursor; the DELETE returns an empty
    # result set, so rowcount is the only signal.
    return int(getattr(cursor, "rowcount", 0) or 0)


# ---------------------------------------------------------------------------
# Fork a conversation (migration 017)
# ---------------------------------------------------------------------------


def fork_conversation(
    conn: Any,
    source_conv_id: uuid.UUID | str,
    up_to_message_id: uuid.UUID | str,
    *,
    user_id: uuid.UUID | str,
    org_id: uuid.UUID | str,
) -> Conversation:
    """Branch a conversation at *up_to_message_id* into a new thread.

    Copies the source conversation's messages up to and including the
    given message into a newly-created conversation owned by *user_id* /
    *org_id*. Encrypted BYTEA columns (``content_encrypted``,
    ``tool_input_encrypted``, ``tool_output_encrypted``,
    ``rag_context_encrypted``) are copied byte-for-byte so the fork
    inherits the exact ciphertexts — no re-encryption, no plaintext
    round-trip.

    The new conversation's title is ``"Jätk: <original title>"`` with
    ``title_is_custom=False`` so the auto-title job can still refine it.

    The caller is responsible for committing the transaction.
    """
    # Look up the fork point so we can bound the message copy by
    # ``created_at``. Looking up the message (rather than filtering by
    # id alone) lets us copy a contiguous time-ordered slice instead of
    # relying on the caller to know the insertion order.
    boundary = conn.execute(
        """
        SELECT created_at, conversation_id
        FROM messages
        WHERE id = %s
        """,
        (str(up_to_message_id),),
    ).fetchone()
    if boundary is None:
        raise ValueError(f"Message {up_to_message_id} not found")
    boundary_created_at, boundary_conv_id = boundary
    if coerce_uuid(boundary_conv_id) != coerce_uuid(source_conv_id):
        raise ValueError(
            f"Message {up_to_message_id} does not belong to conversation {source_conv_id}"
        )

    source = get_conversation(conn, source_conv_id)
    if source is None:
        raise ValueError(f"Source conversation {source_conv_id} not found")

    new_title = f"Jätk: {source.title}" if source.title else "Jätk"
    new_conv = create_conversation(
        conn,
        user_id,
        org_id,
        title=new_title,
        context_draft_id=source.context_draft_id,
    )
    # ``create_conversation`` does not set ``title_is_custom``; migration
    # 017 defaults it to FALSE so we do not need an extra UPDATE.

    # Copy messages byte-for-byte — include the encrypted BYTEA columns
    # directly. ``created_at`` is preserved so the fork reads in the same
    # order as the source up to the boundary.
    conn.execute(
        """
        INSERT INTO messages (
            conversation_id, role, content, tool_name, tool_input,
            tool_output, rag_context, tokens_input, tokens_output, model,
            content_encrypted, tool_input_encrypted, tool_output_encrypted,
            rag_context_encrypted, is_pinned, is_truncated, created_at
        )
        SELECT
            %s, role, content, tool_name, tool_input,
            tool_output, rag_context, tokens_input, tokens_output, model,
            content_encrypted, tool_input_encrypted, tool_output_encrypted,
            rag_context_encrypted, is_pinned, is_truncated, created_at
        FROM messages
        WHERE conversation_id = %s AND created_at <= %s
        ORDER BY created_at ASC
        """,
        (str(new_conv.id), str(source_conv_id), boundary_created_at),
    )
    return new_conv
