"""SQL-execution test for ``migrations/021_mask_leaked_env_errors.sql`` (#680).

Migration 021 rewrites historical ``drafts.error_message`` rows whose
text leaks environment-variable / deployment-mode internals (e.g.
``ANTHROPIC_API_KEY must be set when APP_ENV=production``) into the
canonical Estonian fallback string, preserving the original raw text
in ``drafts.error_debug`` for admin triage. It is idempotent and
non-destructive (see the migration's docstring).

This test seeds a small fixture set covering the four cases the
migration cares about:

    (a) ``ANTHROPIC_API_KEY`` leak, no prior debug → rewritten + raw
        text preserved in ``error_debug``.
    (b) ``VOYAGE_API_KEY`` leak, no prior debug → same treatment as (a).
    (c) Benign Estonian message that mentions no env var → untouched.
    (d) ``ANTHROPIC_API_KEY`` leak with pre-existing ``error_debug`` →
        ``error_message`` rewritten but ``error_debug`` preserved
        (the migration must never clobber a richer debug value).

The migration SQL is read off disk and executed through the same
connection that seeded the fixtures — we deliberately do not shell out
to ``psql`` so the test runs against whatever DB the rest of the suite
is pointed at (Postgres in CI, local Docker compose in dev). The whole
test runs inside one transaction that is rolled back at the end so the
shared test database is left exactly as we found it.

Pattern mirrors ``tests/test_migration_025.py``: the test ``pytest.skip``s
when ``DATABASE_URL`` is unset so unit-only runs (no Postgres) stay
green.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import LiteralString, cast

import psycopg
import pytest

# Path to the migration under test. Resolved relative to the repository
# root via this file's location so the test works regardless of the
# pytest invocation directory.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_MIGRATION_PATH = _REPO_ROOT / "migrations" / "021_mask_leaked_env_errors.sql"

# Must match ``MSG_UNKNOWN`` in ``app/docs/error_mapping.py`` and the
# string hardcoded in the migration. Duplicated here on purpose: the
# test guards the migration-to-app contract; pulling MSG_UNKNOWN from
# the Python module would defeat the check.
_CANONICAL_FALLBACK = "Töötlemine ebaõnnestus tehnilisel põhjusel. Meeskond on teavitatud."

# A benign Estonian message that mentions no env var name — must be
# left untouched by the migration's WHERE clause.
_BENIGN_MESSAGE = "Üleslaadimine ebaõnnestus: fail on liiga suur"


def _connect() -> psycopg.Connection:
    return psycopg.connect(os.environ["DATABASE_URL"])


def _insert_org_and_user(conn: psycopg.Connection) -> tuple[uuid.UUID, uuid.UUID]:
    """Create a throwaway org + user so we can satisfy the drafts FKs.

    The seed rows are deleted in the test teardown — nothing else in
    the database references them.
    """
    org_id = uuid.uuid4()
    user_id = uuid.uuid4()
    conn.execute(
        "INSERT INTO organizations (id, name, slug) VALUES (%s, %s, %s)",
        (org_id, f"mig021-org-{org_id}", f"mig021-org-{org_id}"),
    )
    conn.execute(
        "INSERT INTO users (id, email, password_hash, full_name, role, org_id) "
        "VALUES (%s, %s, '$2b$12$placeholderhashplaceholderhashplaceholderhash', "
        "'Mig021 User', 'drafter', %s)",
        (user_id, f"mig021-{user_id}@example.com", org_id),
    )
    return org_id, user_id


def _insert_draft(
    conn: psycopg.Connection,
    *,
    user_id: uuid.UUID,
    org_id: uuid.UUID,
    error_message: str,
    error_debug: str | None,
) -> uuid.UUID:
    """Insert a single ``drafts`` row with ``status='failed'`` and return its id.

    All other NOT NULL columns get throwaway placeholder values — the
    migration's UPDATE only cares about ``error_message`` / ``error_debug``.
    """
    draft_id = uuid.uuid4()
    conn.execute(
        """
        INSERT INTO drafts (
            id, user_id, org_id, title, filename, content_type, file_size,
            storage_path, graph_uri, status, error_message, error_debug
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'failed', %s, %s)
        RETURNING id
        """,
        (
            draft_id,
            user_id,
            org_id,
            f"mig021 draft {draft_id}",
            f"mig021-{draft_id}.docx",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            1024,
            f"/tmp/mig021/{draft_id}.bin",
            f"urn:draft:mig021:{draft_id}",
            error_message,
            error_debug,
        ),
    )
    return draft_id


def test_migration_021_masks_leaked_env_errors():
    if not os.getenv("DATABASE_URL"):
        pytest.skip("integration test — DATABASE_URL not set")

    sql = _MIGRATION_PATH.read_text(encoding="utf-8")

    # Open ONE connection and use a single transaction we roll back at
    # the end. That way:
    #   1. The seed rows and the migration's UPDATE both happen inside
    #      the same logical txn the test owns.
    #   2. Nothing leaks into the shared test database when assertions
    #      pass *or* fail — the rollback in the finally block always
    #      runs.
    conn = _connect()
    conn.autocommit = False
    try:
        org_id, user_id = _insert_org_and_user(conn)

        # Case (a): ANTHROPIC_API_KEY leak, no prior debug. Migration
        # should rewrite error_message and copy the raw string into
        # error_debug.
        id_a = _insert_draft(
            conn,
            user_id=user_id,
            org_id=org_id,
            error_message="ANTHROPIC_API_KEY must be set when APP_ENV=production",
            error_debug=None,
        )

        # Case (b): VOYAGE_API_KEY leak, no prior debug — covers the
        # second leak pattern listed in the migration's WHERE clause.
        id_b = _insert_draft(
            conn,
            user_id=user_id,
            org_id=org_id,
            error_message="VOYAGE_API_KEY missing",
            error_debug=None,
        )

        # Case (c): benign Estonian error — must be left untouched.
        id_c = _insert_draft(
            conn,
            user_id=user_id,
            org_id=org_id,
            error_message=_BENIGN_MESSAGE,
            error_debug=None,
        )

        # Case (d): ANTHROPIC_API_KEY leak with pre-existing debug —
        # error_message must be rewritten but error_debug must NOT be
        # clobbered (the migration's "WHERE error_debug IS NULL" guard).
        existing_debug = "pre-existing debug data"
        id_d = _insert_draft(
            conn,
            user_id=user_id,
            org_id=org_id,
            error_message="ANTHROPIC_API_KEY missing",
            error_debug=existing_debug,
        )

        # Execute the migration SQL through the same connection. The
        # SQL contains two UPDATE statements terminated with ``;``;
        # psycopg's cursor.execute() handles multi-statement strings
        # in a single round-trip. The ``cast`` placates pyright — the
        # migration text is loaded off the repo (not user input) so
        # there is no SQL-injection risk to flag.
        with conn.cursor() as cur:
            cur.execute(cast(LiteralString, sql))

        # ---- Assertions ----
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, error_message, error_debug FROM drafts WHERE id = ANY(%s) ORDER BY id",
                ([id_a, id_b, id_c, id_d],),
            )
            rows = {row[0]: (row[1], row[2]) for row in cur.fetchall()}

        # (a) rewritten + raw text preserved in error_debug.
        msg_a, debug_a = rows[id_a]
        assert msg_a == _CANONICAL_FALLBACK, (
            f"case (a): expected canonical fallback, got {msg_a!r}"
        )
        assert debug_a == "ANTHROPIC_API_KEY must be set when APP_ENV=production", (
            f"case (a): expected raw message in error_debug, got {debug_a!r}"
        )

        # (b) same treatment for the VOYAGE_API_KEY leak.
        msg_b, debug_b = rows[id_b]
        assert msg_b == _CANONICAL_FALLBACK, (
            f"case (b): expected canonical fallback, got {msg_b!r}"
        )
        assert debug_b == "VOYAGE_API_KEY missing", (
            f"case (b): expected raw message in error_debug, got {debug_b!r}"
        )

        # (c) benign message untouched on BOTH columns.
        msg_c, debug_c = rows[id_c]
        assert msg_c == _BENIGN_MESSAGE, (
            f"case (c): benign message should be untouched, got {msg_c!r}"
        )
        assert debug_c is None, (
            f"case (c): error_debug must stay NULL for non-leak rows, got {debug_c!r}"
        )

        # (d) error_message rewritten but pre-existing error_debug NOT
        # clobbered — this is the load-bearing guarantee of the
        # migration's `WHERE error_debug IS NULL` step-1 clause.
        msg_d, debug_d = rows[id_d]
        assert msg_d == _CANONICAL_FALLBACK, (
            f"case (d): expected canonical fallback, got {msg_d!r}"
        )
        assert debug_d == existing_debug, (
            f"case (d): pre-existing error_debug must NOT be overwritten, got {debug_d!r}"
        )

    finally:
        # Always roll back so the shared test DB is left untouched.
        conn.rollback()
        conn.close()


def test_migration_021_is_idempotent():
    """Running the migration twice must be safe — the second pass is a no-op.

    Mirrors the idempotency promise in the migration's docstring: rows
    that already carry the canonical fallback string are filtered out
    by the WHERE clause's ``error_message != <fallback>`` guard, so a
    second invocation must leave the table unchanged.
    """
    if not os.getenv("DATABASE_URL"):
        pytest.skip("integration test — DATABASE_URL not set")

    sql = _MIGRATION_PATH.read_text(encoding="utf-8")

    conn = _connect()
    conn.autocommit = False
    try:
        org_id, user_id = _insert_org_and_user(conn)
        draft_id = _insert_draft(
            conn,
            user_id=user_id,
            org_id=org_id,
            error_message="ANTHROPIC_API_KEY must be set when APP_ENV=production",
            error_debug=None,
        )

        sql_typed = cast(LiteralString, sql)

        # First pass — rewrite happens.
        with conn.cursor() as cur:
            cur.execute(sql_typed)

        with conn.cursor() as cur:
            cur.execute(
                "SELECT error_message, error_debug, updated_at FROM drafts WHERE id = %s",
                (draft_id,),
            )
            row = cur.fetchone()
        assert row is not None
        first_msg, first_debug, first_updated_at = row
        assert first_msg == _CANONICAL_FALLBACK
        assert first_debug == "ANTHROPIC_API_KEY must be set when APP_ENV=production"

        # Second pass — must be a no-op on this row.
        with conn.cursor() as cur:
            cur.execute(sql_typed)

        with conn.cursor() as cur:
            cur.execute(
                "SELECT error_message, error_debug, updated_at FROM drafts WHERE id = %s",
                (draft_id,),
            )
            row2 = cur.fetchone()
        assert row2 is not None
        second_msg, second_debug, second_updated_at = row2
        assert second_msg == _CANONICAL_FALLBACK
        assert second_debug == first_debug
        # Most importantly the timestamp must NOT have moved — that
        # would mean the row was re-UPDATED unnecessarily.
        assert second_updated_at == first_updated_at, (
            "idempotency check: second migration pass must not bump updated_at"
        )

    finally:
        conn.rollback()
        conn.close()
