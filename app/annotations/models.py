"""``annotations`` and ``annotation_replies`` table dataclasses + query helpers.

Every query helper follows the same pattern as ``app/chat/models.py``:

    - Explicit ``conn`` parameter from the caller
    - ``conn.commit()`` on writes is the caller's responsibility
    - Exceptions are logged and the function returns a sentinel value
      (``None`` / empty list) rather than raising, so a dead DB never
      takes down the whole request
    - Org scoping: list queries include ``AND org_id = %s`` where appropriate

Single-item lookups return None if the row doesn't exist; callers are
expected to compare ``annotation.org_id`` against the current user's
``org_id`` for access control.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.db_utils import coerce_uuid, parse_jsonb
from app.storage import DecryptionError, decrypt_text, encrypt_text

logger = logging.getLogger(__name__)


# §9.4 row_kind whitelist — the four impact-report row types that can host
# annotations. Validated at every write entry point so a malformed value
# cannot poison the (target_type, target_id) composite index.
VALID_ROW_KINDS: tuple[str, ...] = ("entity", "conflict", "eu", "gap")


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class Annotation:
    """Snapshot of a row in the ``annotations`` table."""

    id: uuid.UUID
    user_id: uuid.UUID
    org_id: uuid.UUID
    target_type: str
    target_id: str
    target_metadata: dict | None
    content: str
    resolved: bool
    resolved_by: uuid.UUID | None
    resolved_at: datetime | None
    created_at: datetime
    updated_at: datetime
    # Migration 029 extensions
    draft_version_id: uuid.UUID | None = None
    mentions: list[uuid.UUID] = field(default_factory=list)
    stale: bool = False


@dataclass
class AnnotationReply:
    """Snapshot of a row in the ``annotation_replies`` table."""

    id: uuid.UUID
    annotation_id: uuid.UUID
    user_id: uuid.UUID
    content: str
    created_at: datetime
    # Migration 029 extensions
    mentions: list[uuid.UUID] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_ANNOTATION_COLUMNS = (
    "id, user_id, org_id, target_type, target_id, target_metadata, "
    "content, resolved, resolved_by, resolved_at, created_at, updated_at, "
    "content_encrypted, draft_version_id, mentions, stale"
)

_REPLY_COLUMNS = "id, annotation_id, user_id, content, created_at, content_encrypted, mentions"

# Indices into the _ANNOTATION_COLUMNS tuple (zero-based)
_ANN_IDX_ID = 0
_ANN_IDX_USER_ID = 1
_ANN_IDX_ORG_ID = 2
_ANN_IDX_TARGET_TYPE = 3
_ANN_IDX_TARGET_ID = 4
_ANN_IDX_TARGET_METADATA = 5
_ANN_IDX_CONTENT = 6
_ANN_IDX_RESOLVED = 7
_ANN_IDX_RESOLVED_BY = 8
_ANN_IDX_RESOLVED_AT = 9
_ANN_IDX_CREATED_AT = 10
_ANN_IDX_UPDATED_AT = 11
_ANN_IDX_CONTENT_ENCRYPTED = 12
_ANN_IDX_DRAFT_VERSION_ID = 13
_ANN_IDX_MENTIONS = 14
_ANN_IDX_STALE = 15

# Indices into the _REPLY_COLUMNS tuple (zero-based)
_REPLY_IDX_ID = 0
_REPLY_IDX_ANNOTATION_ID = 1
_REPLY_IDX_USER_ID = 2
_REPLY_IDX_CONTENT = 3
_REPLY_IDX_CREATED_AT = 4
_REPLY_IDX_CONTENT_ENCRYPTED = 5
_REPLY_IDX_MENTIONS = 6


def _decode_encrypted_text(ciphertext: bytes | memoryview | None) -> str | None:
    """Decrypt a BYTEA column; return ``None`` on NULL or decrypt failure.

    Fallback semantics: the caller uses ``None`` to signal "fall back to
    the legacy plaintext column". Decrypt failures are logged loudly because
    they can only mean the key rotated or the ciphertext got corrupted.
    """
    if ciphertext is None:
        return None
    raw = bytes(ciphertext) if isinstance(ciphertext, memoryview) else ciphertext
    try:
        return decrypt_text(raw)
    except DecryptionError:
        logger.exception("Failed to decrypt annotation column — falling back to plaintext")
        return None


def _row_to_annotation(row: tuple[Any, ...]) -> Annotation:
    """Build an ``Annotation`` from a raw cursor row selected with _ANNOTATION_COLUMNS.

    Content is sourced from ``content_encrypted`` when present (migration 029
    onwards), falling back to the legacy plaintext ``content`` column so that
    existing rows created before the encryption rollout continue to decode.
    """
    ann_id = row[_ANN_IDX_ID]
    user_id = row[_ANN_IDX_USER_ID]
    org_id = row[_ANN_IDX_ORG_ID]
    target_type = row[_ANN_IDX_TARGET_TYPE]
    target_id = row[_ANN_IDX_TARGET_ID]
    target_metadata_raw = row[_ANN_IDX_TARGET_METADATA]
    content_plain = row[_ANN_IDX_CONTENT]
    resolved = row[_ANN_IDX_RESOLVED]
    resolved_by = row[_ANN_IDX_RESOLVED_BY]
    resolved_at = row[_ANN_IDX_RESOLVED_AT]
    created_at = row[_ANN_IDX_CREATED_AT]
    updated_at = row[_ANN_IDX_UPDATED_AT]
    content_encrypted = (
        row[_ANN_IDX_CONTENT_ENCRYPTED] if len(row) > _ANN_IDX_CONTENT_ENCRYPTED else None
    )
    draft_version_id_raw = (
        row[_ANN_IDX_DRAFT_VERSION_ID] if len(row) > _ANN_IDX_DRAFT_VERSION_ID else None
    )
    mentions_raw = row[_ANN_IDX_MENTIONS] if len(row) > _ANN_IDX_MENTIONS else []
    stale = row[_ANN_IDX_STALE] if len(row) > _ANN_IDX_STALE else False

    # Prefer decrypted content; fall back to plaintext for legacy rows.
    content = _decode_encrypted_text(content_encrypted) or content_plain or ""

    target_metadata = parse_jsonb(target_metadata_raw)
    if target_metadata is not None and not isinstance(target_metadata, dict):
        target_metadata = None

    mentions: list[uuid.UUID] = (
        [coerce_uuid(m) for m in mentions_raw if m is not None] if mentions_raw else []
    )

    return Annotation(
        id=coerce_uuid(ann_id),
        user_id=coerce_uuid(user_id),
        org_id=coerce_uuid(org_id),
        target_type=target_type,
        target_id=str(target_id),
        target_metadata=target_metadata,
        content=content,
        resolved=bool(resolved),
        resolved_by=coerce_uuid(resolved_by) if resolved_by else None,
        resolved_at=resolved_at,
        created_at=created_at,
        updated_at=updated_at,
        draft_version_id=coerce_uuid(draft_version_id_raw) if draft_version_id_raw else None,
        mentions=mentions,
        stale=bool(stale),
    )


def _row_to_reply(row: tuple[Any, ...]) -> AnnotationReply:
    """Build an ``AnnotationReply`` from a raw cursor row selected with _REPLY_COLUMNS.

    Content is sourced from ``content_encrypted`` when present (migration 029
    onwards), falling back to the legacy plaintext ``content`` column.
    """
    reply_id = row[_REPLY_IDX_ID]
    annotation_id = row[_REPLY_IDX_ANNOTATION_ID]
    user_id = row[_REPLY_IDX_USER_ID]
    content_plain = row[_REPLY_IDX_CONTENT]
    created_at = row[_REPLY_IDX_CREATED_AT]
    content_encrypted = (
        row[_REPLY_IDX_CONTENT_ENCRYPTED] if len(row) > _REPLY_IDX_CONTENT_ENCRYPTED else None
    )
    mentions_raw = row[_REPLY_IDX_MENTIONS] if len(row) > _REPLY_IDX_MENTIONS else []

    content = _decode_encrypted_text(content_encrypted) or content_plain or ""

    mentions: list[uuid.UUID] = (
        [coerce_uuid(m) for m in mentions_raw if m is not None] if mentions_raw else []
    )

    return AnnotationReply(
        id=coerce_uuid(reply_id),
        annotation_id=coerce_uuid(annotation_id),
        user_id=coerce_uuid(user_id),
        content=content,
        created_at=created_at,
        mentions=mentions,
    )


# ---------------------------------------------------------------------------
# Annotation CRUD
# ---------------------------------------------------------------------------


VALID_TARGET_TYPES = ("draft", "provision", "conversation", "entity")


def create_annotation(
    conn: Any,
    user_id: uuid.UUID | str,
    org_id: uuid.UUID | str,
    target_type: str,
    target_id: str,
    content: str,
    target_metadata: dict | None = None,
) -> Annotation:
    """Insert a new ``annotations`` row and return the created annotation.

    The body text is encrypted via Fernet (:func:`encrypt_text`) and written
    to ``content_encrypted``; the legacy plaintext ``content`` column is left
    NULL, matching :func:`create_row_annotation` (#772). Read paths fall back
    to plaintext for rows written before this change.

    The caller is responsible for committing the transaction.
    """
    if target_type not in VALID_TARGET_TYPES:
        raise ValueError(f"Invalid target_type: {target_type!r}")

    ciphertext = encrypt_text(content)

    row = conn.execute(
        f"""
        INSERT INTO annotations
            (user_id, org_id, target_type, target_id, target_metadata,
             content, content_encrypted)
        VALUES (%s, %s, %s, %s, %s::jsonb, NULL, %s)
        RETURNING {_ANNOTATION_COLUMNS}
        """,
        (
            str(user_id),
            str(org_id),
            target_type,
            target_id,
            json.dumps(target_metadata) if target_metadata is not None else None,
            ciphertext,
        ),
    ).fetchone()
    if row is None:
        raise RuntimeError("INSERT ... RETURNING annotations produced no row")
    return _row_to_annotation(row)


def list_annotations_for_target(
    conn: Any,
    target_type: str,
    target_id: str,
    org_id: uuid.UUID | str,
) -> list[Annotation]:
    """Return annotations for a target within an org, newest first.

    Results are always org-scoped to prevent cross-org data leakage.
    """
    try:
        rows = conn.execute(
            f"""
            SELECT {_ANNOTATION_COLUMNS}
            FROM annotations
            WHERE target_type = %s
              AND target_id = %s
              AND org_id = %s
            ORDER BY created_at DESC
            """,
            (target_type, target_id, str(org_id)),
        ).fetchall()
    except Exception:
        logger.exception(
            "Failed to list annotations for target_type=%s target_id=%s org=%s",
            target_type,
            target_id,
            org_id,
        )
        return []
    return [_row_to_annotation(row) for row in rows]


def get_annotation(
    conn: Any,
    annotation_id: uuid.UUID | str,
) -> Annotation | None:
    """Return a single annotation by id, or ``None``."""
    try:
        row = conn.execute(
            f"SELECT {_ANNOTATION_COLUMNS} FROM annotations WHERE id = %s",
            (str(annotation_id),),
        ).fetchone()
    except Exception:
        logger.exception("Failed to fetch annotation id=%s", annotation_id)
        return None
    return _row_to_annotation(row) if row else None


def resolve_annotation(
    conn: Any,
    annotation_id: uuid.UUID | str,
    resolved_by_user_id: uuid.UUID | str,
) -> Annotation | None:
    """Mark an annotation as resolved and return the updated row.

    The caller is responsible for committing the transaction.
    Returns ``None`` if the annotation does not exist or the DB errors.
    """
    try:
        row = conn.execute(
            f"""
            UPDATE annotations
            SET resolved = TRUE,
                resolved_by = %s,
                resolved_at = now(),
                updated_at = now()
            WHERE id = %s
            RETURNING {_ANNOTATION_COLUMNS}
            """,
            (str(resolved_by_user_id), str(annotation_id)),
        ).fetchone()
    except Exception:
        logger.exception("Failed to resolve annotation id=%s", annotation_id)
        return None
    return _row_to_annotation(row) if row else None


def delete_annotation(
    conn: Any,
    annotation_id: uuid.UUID | str,
) -> None:
    """Delete an annotation. FK CASCADE removes associated replies.

    The caller is responsible for committing the transaction.
    """
    conn.execute(
        "DELETE FROM annotations WHERE id = %s",
        (str(annotation_id),),
    )


# ---------------------------------------------------------------------------
# Reply CRUD
# ---------------------------------------------------------------------------


def create_reply(
    conn: Any,
    annotation_id: uuid.UUID | str,
    user_id: uuid.UUID | str,
    content: str,
) -> AnnotationReply:
    """Insert a new ``annotation_replies`` row and return the created reply.

    The body text is encrypted via Fernet (:func:`encrypt_text`) and written
    to ``content_encrypted``; the legacy plaintext ``content`` column is left
    NULL (#772). Read paths fall back to plaintext for rows written before
    this change.

    The caller is responsible for committing the transaction.
    """
    ciphertext = encrypt_text(content)

    row = conn.execute(
        f"""
        INSERT INTO annotation_replies
            (annotation_id, user_id, content, content_encrypted)
        VALUES (%s, %s, NULL, %s)
        RETURNING {_REPLY_COLUMNS}
        """,
        (
            str(annotation_id),
            str(user_id),
            ciphertext,
        ),
    ).fetchone()
    if row is None:
        raise RuntimeError("INSERT ... RETURNING annotation_replies produced no row")
    return _row_to_reply(row)


def list_replies(
    conn: Any,
    annotation_id: uuid.UUID | str,
) -> list[AnnotationReply]:
    """Return all replies for an annotation, ordered by ``created_at`` ASC."""
    try:
        rows = conn.execute(
            f"""
            SELECT {_REPLY_COLUMNS}
            FROM annotation_replies
            WHERE annotation_id = %s
            ORDER BY created_at ASC
            """,
            (str(annotation_id),),
        ).fetchall()
    except Exception:
        logger.exception(
            "Failed to list replies for annotation=%s",
            annotation_id,
        )
        return []
    return [_row_to_reply(row) for row in rows]


# ---------------------------------------------------------------------------
# §9.4 Impact-report row annotations (read-only; writes in PR-B)
# ---------------------------------------------------------------------------

# Regex for @mention tokens.  Estonian usernames / display names may contain
# dots and hyphens; we stop at whitespace and punctuation that cannot be part
# of an identifier.
_MENTION_RE = re.compile(r"@([\w.\-]+)")


def parse_mentions(
    conn: Any,
    content: str,
    org_id: uuid.UUID | str,
) -> list[uuid.UUID]:
    """Extract @token-style mentions and resolve them to user_ids in the same org.

    Security: matches are looked up with ``AND org_id = %s`` so an attacker
    cannot probe for the existence of users in other organisations.  Out-of-org
    matches are silently dropped.  Duplicate mentions of the same user are
    deduplicated in the returned list.

    The token captured by ``_MENTION_RE`` is whitespace-free, matching the
    token the typeahead widget inserts (``@<email-local-part>``).  The lookup
    accepts four forms so both typed and typeahead-inserted mentions resolve:

    1. Full email address (``@peeter@min.ee`` — typed form).
    2. Email local-part prefix (``@peeter`` → matches ``peeter@…``).
    3. Exact ``full_name`` (handles ``@Peeter`` for single-word names,
       and any underscore-joined form like ``@Peeter_Pärn``).
    4. Underscore-to-space variant (``@eesnimi_perenimi`` →
       ``Eesnimi Perenimi``).

    Returns an empty list when *content* contains no ``@`` tokens, or when no
    tokens resolve to users in the given org.
    """
    tokens = _MENTION_RE.findall(content)
    if not tokens:
        return []

    seen: set[uuid.UUID] = set()
    result: list[uuid.UUID] = []

    for token in tokens:
        # Normalise: treat underscores as spaces so @eesnimi_perenimi works too.
        name_variant = token.replace("_", " ")
        # Local-part prefix match: ``peeter`` should resolve ``peeter@min.ee``.
        # Skip when the token already contains ``@`` (i.e. a literal email
        # was typed) to avoid double-matching and to keep the LIKE pattern
        # well-formed.
        email_local_pattern = f"{token.lower()}@%" if "@" not in token else None
        try:
            row = conn.execute(
                """
                SELECT id FROM users
                WHERE org_id = %s
                  AND (
                      email = %s
                      OR email = %s
                      OR (%s IS NOT NULL AND lower(email) LIKE %s)
                      OR full_name ILIKE %s
                      OR full_name ILIKE %s
                  )
                LIMIT 1
                """,
                (
                    str(org_id),
                    token,  # exact email match e.g. peeter@min.ee
                    token.lower(),  # case-insensitive email
                    email_local_pattern,  # NULL when token has @
                    email_local_pattern,  # local-part LIKE pattern
                    token,  # exact full_name
                    name_variant,  # space-variant full_name
                ),
            ).fetchone()
        except Exception:
            logger.exception("parse_mentions: DB error resolving token %r", token)
            continue

        if row is None:
            continue

        user_id = coerce_uuid(row[0])
        if user_id not in seen:
            seen.add(user_id)
            result.append(user_id)

    return result


def list_annotations_for_version_row(
    conn: Any,
    draft_version_id: uuid.UUID | str,
    row_kind: str,
    row_key: str,
) -> list[Annotation]:
    """Fetch all annotations on a specific impact-report row within a draft version.

    Encodes the §9.4 target contract:
        target_type = 'impact_report_item'
        target_id   = '{row_kind}:{row_key}'
    filtered on the ``draft_version_id`` FK column added in migration 029.

    The composite index ``idx_annotations_version_target`` makes this O(log n)
    even as the annotation table grows.

    Returns an empty list when no annotations exist or on DB error.
    """
    target_id = f"{row_kind}:{row_key}"
    try:
        rows = conn.execute(
            f"""
            SELECT {_ANNOTATION_COLUMNS}
            FROM annotations
            WHERE draft_version_id = %s
              AND target_type = 'impact_report_item'
              AND target_id = %s
            ORDER BY created_at DESC
            """,
            (str(draft_version_id), target_id),
        ).fetchall()
    except Exception:
        logger.exception(
            "Failed to list annotations for version=%s row_kind=%s row_key=%s",
            draft_version_id,
            row_kind,
            row_key,
        )
        return []
    return [_row_to_annotation(row) for row in rows]


# ---------------------------------------------------------------------------
# §9.4 row-annotation write helpers (PR-B)
# ---------------------------------------------------------------------------
#
# The "thread" abstraction is implicit: every row in ``annotations`` with the
# same ``(draft_version_id, target_type='impact_report_item', target_id)``
# triple is one message in the same thread. The first write creates the
# thread; subsequent writes append messages to it.
#
# Resolution / reopen toggles are mirrored across every row in the thread so
# a single SELECT on the latest row reflects the current resolved state. The
# ``stale`` column is intentionally NEVER touched in this PR; that flag is
# managed by the analyse re-run pipeline (PR-C territory).


def _validate_row_kind(row_kind: str) -> None:
    """Raise ValueError if *row_kind* is not in the §9.4 whitelist."""
    if row_kind not in VALID_ROW_KINDS:
        raise ValueError(f"Invalid row_kind: {row_kind!r}. Must be one of {VALID_ROW_KINDS!r}")


def create_row_annotation(
    conn: Any,
    *,
    user_id: uuid.UUID | str,
    org_id: uuid.UUID | str,
    draft_version_id: uuid.UUID | str,
    row_kind: str,
    row_key: str,
    content: str,
) -> Annotation:
    """Insert a new message in a row-annotation thread (encrypted).

    Encrypts ``content`` via Fernet and writes the ciphertext to
    ``content_encrypted``; the legacy plaintext ``content`` column is left
    NULL. Mentions are parsed from the plaintext and resolved against the
    given ``org_id`` so the persisted UUID array only contains in-org
    user IDs.

    Args:
        conn: Open psycopg connection. The caller commits.
        user_id: Author of the message.
        org_id: Author's organisation (used for mention resolution).
        draft_version_id: The version this row annotation is scoped to.
        row_kind: One of :data:`VALID_ROW_KINDS`.
        row_key: The opaque per-kind identifier (entity URI, conflict id,
            etc.). Composed with ``row_kind`` into ``target_id``.
        content: Plaintext message body. Must be non-empty after stripping.

    Returns:
        The newly inserted :class:`Annotation` (with decrypted content).

    Raises:
        ValueError: If ``row_kind`` is not in the whitelist or if
            ``content`` is empty after stripping.
        RuntimeError: If the INSERT returns no row.
    """
    _validate_row_kind(row_kind)
    stripped = content.strip()
    if not stripped:
        raise ValueError("content must not be empty")

    target_id = f"{row_kind}:{row_key}"
    ciphertext = encrypt_text(stripped)
    mentions = parse_mentions(conn, stripped, org_id)

    row = conn.execute(
        f"""
        INSERT INTO annotations
            (user_id, org_id, target_type, target_id,
             content, content_encrypted,
             draft_version_id, mentions)
        VALUES (%s, %s, 'impact_report_item', %s,
                NULL, %s,
                %s, %s)
        RETURNING {_ANNOTATION_COLUMNS}
        """,
        (
            str(user_id),
            str(org_id),
            target_id,
            ciphertext,
            str(draft_version_id),
            [str(m) for m in mentions],
        ),
    ).fetchone()
    if row is None:
        raise RuntimeError("INSERT ... RETURNING annotations produced no row")
    return _row_to_annotation(row)


def resolve_row_thread(
    conn: Any,
    *,
    draft_version_id: uuid.UUID | str,
    row_kind: str,
    row_key: str,
    resolved_by_user_id: uuid.UUID | str,
) -> int:
    """Mark every message in the thread as resolved.

    Resolution is thread-level: we flip ``resolved=TRUE`` on every annotation
    row with the matching ``(draft_version_id, target_type, target_id)``
    triple so subsequent SELECTs surface a consistent state regardless of
    which row the UI samples. The ``stale`` column is left untouched.

    Returns:
        The number of rows updated (0 if the thread does not exist).
    """
    _validate_row_kind(row_kind)
    target_id = f"{row_kind}:{row_key}"
    try:
        cursor = conn.execute(
            """
            UPDATE annotations
            SET resolved = TRUE,
                resolved_by = %s,
                resolved_at = now(),
                updated_at = now()
            WHERE draft_version_id = %s
              AND target_type = 'impact_report_item'
              AND target_id = %s
            """,
            (str(resolved_by_user_id), str(draft_version_id), target_id),
        )
    except Exception:
        logger.exception(
            "Failed to resolve row thread version=%s row_kind=%s row_key=%s",
            draft_version_id,
            row_kind,
            row_key,
        )
        return 0
    return getattr(cursor, "rowcount", 0) or 0


def reopen_row_thread(
    conn: Any,
    *,
    draft_version_id: uuid.UUID | str,
    row_kind: str,
    row_key: str,
) -> int:
    """Flip a thread back to ``resolved=FALSE`` on every message.

    Mirror of :func:`resolve_row_thread`. Clears ``resolved_by`` and
    ``resolved_at`` so audit trails are honest about the current state.
    The ``stale`` column is not touched.

    Returns:
        The number of rows updated (0 if the thread does not exist).
    """
    _validate_row_kind(row_kind)
    target_id = f"{row_kind}:{row_key}"
    try:
        cursor = conn.execute(
            """
            UPDATE annotations
            SET resolved = FALSE,
                resolved_by = NULL,
                resolved_at = NULL,
                updated_at = now()
            WHERE draft_version_id = %s
              AND target_type = 'impact_report_item'
              AND target_id = %s
            """,
            (str(draft_version_id), target_id),
        )
    except Exception:
        logger.exception(
            "Failed to reopen row thread version=%s row_kind=%s row_key=%s",
            draft_version_id,
            row_kind,
            row_key,
        )
        return 0
    return getattr(cursor, "rowcount", 0) or 0


# ---------------------------------------------------------------------------
# §9.4 row-annotation read aggregates (PR-C)
# ---------------------------------------------------------------------------
#
# Two helpers used by the impact-report renderer + analyze pipeline:
#
#   - :func:`count_unresolved_for_version_row` powers the AnnotationButton
#     badge ("3 unresolved messages on this row").
#   - :func:`update_stale_flags_for_version` is invoked at the tail of the
#     analyze handler to flip ``stale=true`` on annotations whose row no
#     longer exists in the just-finished analyze.  Best-effort: any DB
#     failure logs and returns 0 so a stale-flag glitch never derails an
#     otherwise-successful analyze.
# ---------------------------------------------------------------------------


def count_unresolved_for_version_row(
    conn: Any,
    draft_version_id: uuid.UUID | str,
    row_kind: str,
    row_key: str,
) -> int:
    """Return the number of unresolved messages on a single impact-report row.

    Used to render the badge on :func:`AnnotationButton` so the user sees
    "5" before clicking.  ``stale`` rows still count — a stale-but-unresolved
    annotation is the most important one to surface, not the least.

    Returns:
        Integer count (0 when no rows match or on any DB error so a
        transient failure never crashes the report page).
    """
    if row_kind not in VALID_ROW_KINDS:
        # The button MUST NOT raise on a malformed row_kind — that would
        # crash the entire page render.  Treat it as "no annotations".
        return 0
    target_id = f"{row_kind}:{row_key}"
    try:
        row = conn.execute(
            """
            SELECT COUNT(*) FROM annotations
            WHERE draft_version_id = %s
              AND target_type = 'impact_report_item'
              AND target_id = %s
              AND resolved = FALSE
            """,
            (str(draft_version_id), target_id),
        ).fetchone()
    except Exception:
        logger.exception(
            "count_unresolved_for_version_row failed version=%s row_kind=%s row_key=%s",
            draft_version_id,
            row_kind,
            row_key,
        )
        return 0
    if row is None or row[0] is None:
        return 0
    try:
        return int(row[0])
    except (TypeError, ValueError):
        return 0


def update_stale_flags_for_version(
    conn: Any,
    draft_version_id: uuid.UUID | str,
    current_row_keys: set[tuple[str, str]],
) -> int:
    """Reconcile ``annotations.stale`` for one version against the latest analyze.

    Walks every ``impact_report_item`` annotation for the given version and
    flips:

        - ``stale = FALSE`` when ``(row_kind, row_key)`` IS in
          *current_row_keys* (the row reappeared, e.g. after a re-analyze
          surfaced the same conflict again).
        - ``stale = TRUE`` when ``(row_kind, row_key)`` is NOT in
          *current_row_keys* (the row vanished — the latest analyze did not
          produce that finding).

    The reconciliation is idempotent: running this twice with the same
    *current_row_keys* set leaves the DB in the same state.

    Best-effort failure handling: any DB error logs an exception and
    returns 0 so a stale-flag glitch never aborts the analyze pipeline.

    Args:
        conn: Open psycopg connection. The caller commits.
        draft_version_id: The version this analyze run produced.
        current_row_keys: Set of ``(row_kind, row_key)`` tuples for every
            row in the new impact report.

    Returns:
        The number of rows whose ``stale`` flag actually changed (0 on
        no-op or on any DB error).
    """
    try:
        annotation_rows = conn.execute(
            """
            SELECT id, target_id, stale
            FROM annotations
            WHERE draft_version_id = %s
              AND target_type = 'impact_report_item'
            """,
            (str(draft_version_id),),
        ).fetchall()
    except Exception:
        logger.exception(
            "update_stale_flags_for_version: lookup failed version=%s",
            draft_version_id,
        )
        return 0

    if not annotation_rows:
        return 0

    to_set_stale: list[uuid.UUID] = []
    to_clear_stale: list[uuid.UUID] = []
    for row in annotation_rows:
        ann_id_raw = row[0]
        target_id = str(row[1] or "")
        currently_stale = bool(row[2])
        # target_id format is "{row_kind}:{row_key}"; split on the FIRST
        # colon only because row_key (sha256 hex / URI) may contain
        # colons of its own.
        if ":" not in target_id:
            continue
        row_kind, row_key = target_id.split(":", 1)
        is_present = (row_kind, row_key) in current_row_keys
        try:
            ann_id = coerce_uuid(ann_id_raw)
        except Exception:
            continue
        if is_present and currently_stale:
            to_clear_stale.append(ann_id)
        elif not is_present and not currently_stale:
            to_set_stale.append(ann_id)

    changed = 0
    if to_set_stale:
        try:
            conn.execute(
                "UPDATE annotations SET stale = TRUE, updated_at = now() WHERE id = ANY(%s)",
                ([str(uid) for uid in to_set_stale],),
            )
            changed += len(to_set_stale)
        except Exception:
            logger.exception(
                "update_stale_flags_for_version: SET stale failed version=%s count=%d",
                draft_version_id,
                len(to_set_stale),
            )
    if to_clear_stale:
        try:
            conn.execute(
                "UPDATE annotations SET stale = FALSE, updated_at = now() WHERE id = ANY(%s)",
                ([str(uid) for uid in to_clear_stale],),
            )
            changed += len(to_clear_stale)
        except Exception:
            logger.exception(
                "update_stale_flags_for_version: CLEAR stale failed version=%s count=%d",
                draft_version_id,
                len(to_clear_stale),
            )
    return changed
