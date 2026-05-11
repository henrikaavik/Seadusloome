"""``pending_chat_seed`` table helpers — server-side single-use chat-seed tokens.

Mirrors ``migrations/033_pending_chat_seed.sql``.

Why this exists (#714 PR-J / #724): the "Küsi nõustajalt selle leiu kohta"
affordance on Analüüsikeskus result rows pre-fills the chat input with a
finding phrased as a question. A finding can quote draft content (sensitive
pre-publication text), so the seed text must **not** travel through the URL
as plain text. Instead ``POST /chat/seed`` stashes the Fernet-encrypted seed
here and redirects with an opaque single-use ``token`` UUID; the chat view
consumes the token once and renders the textarea pre-filled.

Every helper follows the same conventions as :mod:`app.chat.models`:

    - Explicit ``conn`` parameter from the caller
    - Writes are committed *inside* the consume helper (single-use semantics
      mean the DELETE must land before the caller renders the seed) — the
      ``create`` helper leaves the commit to the caller, matching the rest of
      the chat model layer
    - Exceptions are logged and the function returns a sentinel value
      (``None``) rather than raising, so a dead DB never takes down the
      whole request
    - The seed plaintext is never persisted — only ``seed_encrypted`` bytes
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from app.storage import DecryptionError, decrypt_text, encrypt_text

logger = logging.getLogger(__name__)

# Tokens older than this are treated as expired by the consume/peek paths and
# opportunistically garbage-collected. Kept short — a seed is meant to be
# consumed within seconds of the redirect.
_TOKEN_TTL_SQL = "interval '1 hour'"


def _coerce_token(token: Any) -> uuid.UUID | None:
    """Return *token* as a ``UUID``, or ``None`` if it isn't a valid UUID string.

    A bad token string (truncated, tampered, garbage) must degrade to "no
    seed" rather than raise — the chat view treats ``None`` as "render the
    normal empty textarea".
    """
    if isinstance(token, uuid.UUID):
        return token
    try:
        return uuid.UUID(str(token))
    except (ValueError, TypeError, AttributeError):
        return None


def create_pending_seed(
    conn: Any,
    *,
    user_id: uuid.UUID | str,
    org_id: uuid.UUID | str | None,
    draft_id: uuid.UUID | str | None,
    seed_text: str,
) -> str | None:
    """Insert a ``pending_chat_seed`` row and return the generated token string.

    ``seed_text`` is Fernet-encrypted via :func:`app.storage.encrypt_text`
    before it touches the DB — the plaintext is never persisted. ``draft_id``
    may be ``None`` (ad-hoc analyses have no backing draft). The caller is
    responsible for committing the transaction.

    Returns the token as a string, or ``None`` on any failure (logged) so the
    caller can fall back to a seedless redirect.
    """
    try:
        seed_ciphertext = encrypt_text(seed_text)
        row = conn.execute(
            """
            INSERT INTO pending_chat_seed (user_id, org_id, draft_id, seed_encrypted)
            VALUES (%s, %s, %s, %s)
            RETURNING token
            """,
            (
                str(user_id),
                str(org_id) if org_id else None,
                str(draft_id) if draft_id else None,
                seed_ciphertext,
            ),
        ).fetchone()
    except Exception:
        logger.exception("Failed to create pending_chat_seed for user=%s", user_id)
        return None
    if row is None or row[0] is None:
        logger.warning("INSERT ... RETURNING pending_chat_seed produced no token")
        return None
    return str(row[0])


def _decode_seed(ciphertext: Any) -> str | None:
    """Decrypt a ``seed_encrypted`` BYTEA value; ``None`` on NULL / decrypt failure."""
    if ciphertext is None:
        return None
    raw = bytes(ciphertext) if isinstance(ciphertext, memoryview) else ciphertext
    try:
        return decrypt_text(raw)
    except DecryptionError:
        logger.exception("Failed to decrypt pending_chat_seed.seed_encrypted")
        return None


def _gc_stale_seeds(conn: Any) -> None:
    """Opportunistically delete seed rows older than the TTL. Best-effort."""
    try:
        conn.execute(f"DELETE FROM pending_chat_seed WHERE created_at < now() - {_TOKEN_TTL_SQL}")
        conn.commit()
    except Exception:
        logger.debug("pending_chat_seed GC failed", exc_info=True)
        try:
            conn.rollback()
        except Exception:
            pass


def peek_pending_seed(
    conn: Any,
    *,
    token: Any,
    user_id: uuid.UUID | str,
) -> tuple[str, uuid.UUID | None] | None:
    """Resolve a seed token **without** consuming it.

    Used by ``GET /chat/new?seed=<token>`` — the conversation-creation step
    needs the token's ``draft_id`` to bind ``context_draft_id``, but the
    subsequent ``GET /chat/{id}?seed=<token>`` page is the one that actually
    consumes the seed and pre-fills the textarea. Same SELECT as
    :func:`consume_pending_seed`, just no DELETE.

    Returns ``(seed_text, draft_id_or_None)`` on a valid, unexpired,
    correctly-owned token; ``None`` otherwise (bad token string, wrong user,
    expired, decrypt failure, or DB error).
    """
    parsed = _coerce_token(token)
    if parsed is None:
        return None
    try:
        row = conn.execute(
            f"""
            SELECT seed_encrypted, draft_id
            FROM pending_chat_seed
            WHERE token = %s AND user_id = %s
              AND created_at > now() - {_TOKEN_TTL_SQL}
            """,
            (str(parsed), str(user_id)),
        ).fetchone()
    except Exception:
        logger.warning("peek_pending_seed lookup failed for token=%s", parsed, exc_info=True)
        return None
    if row is None:
        return None
    seed_text = _decode_seed(row[0])
    if seed_text is None:
        return None
    draft_id: uuid.UUID | None = None
    if row[1] is not None:
        try:
            draft_id = uuid.UUID(str(row[1]))
        except (ValueError, TypeError):
            draft_id = None
    return seed_text, draft_id


def consume_pending_seed(
    conn: Any,
    *,
    token: Any,
    user_id: uuid.UUID | str,
) -> tuple[str, uuid.UUID | None] | None:
    """Resolve **and consume** a seed token (single-use).

    Used by ``GET /chat/{conv_id}?seed=<token>``: the seed is read, the row is
    DELETEd (so a refresh shows an empty textarea), and the transaction is
    committed before returning. Also opportunistically garbage-collects seed
    rows older than the TTL.

    Returns ``(seed_text, draft_id_or_None)`` on a valid, unexpired,
    correctly-owned token; ``None`` otherwise (bad token string, wrong user,
    expired, already-consumed, decrypt failure, or DB error). Never raises —
    the caller treats ``None`` as "render the normal empty textarea".
    """
    parsed = _coerce_token(token)
    if parsed is None:
        # Still worth a GC pass even on a bad token — keeps the table tidy.
        _gc_stale_seeds(conn)
        return None
    try:
        row = conn.execute(
            f"""
            DELETE FROM pending_chat_seed
            WHERE token = %s AND user_id = %s
              AND created_at > now() - {_TOKEN_TTL_SQL}
            RETURNING seed_encrypted, draft_id
            """,
            (str(parsed), str(user_id)),
        ).fetchone()
        conn.commit()
    except Exception:
        logger.warning("consume_pending_seed failed for token=%s", parsed, exc_info=True)
        try:
            conn.rollback()
        except Exception:
            pass
        return None
    # Garbage-collect any stale rows now that we're already in the table.
    _gc_stale_seeds(conn)
    if row is None:
        return None
    seed_text = _decode_seed(row[0])
    if seed_text is None:
        return None
    draft_id: uuid.UUID | None = None
    if row[1] is not None:
        try:
            draft_id = uuid.UUID(str(row[1]))
        except (ValueError, TypeError):
            draft_id = None
    return seed_text, draft_id
