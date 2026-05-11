"""Unit tests for ``app.chat.pending_seed`` — the single-use chat-seed token model.

These tests exercise the encryption boundary + the SELECT/DELETE/commit
semantics with a mocked ``conn`` (the established pattern in
``tests/test_chat_models_encryption.py``). A real Fernet key is installed so
the round-trip is genuine. The "expired" / "already-consumed" / "wrong user"
cases are simulated by having the mocked cursor return ``None`` from
``fetchone`` — which is exactly what the ``WHERE token = %s AND user_id = %s
AND created_at > now() - interval '1 hour'`` predicate produces against a real
DB when the row doesn't match.
"""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock

import pytest
from cryptography.fernet import Fernet

from app.chat.pending_seed import (
    consume_pending_seed,
    create_pending_seed,
    peek_pending_seed,
)

_USER_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")
_OTHER_USER_ID = uuid.UUID("44444444-4444-4444-4444-444444444444")
_ORG_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
_DRAFT_ID = uuid.UUID("55555555-5555-5555-5555-555555555555")
_TOKEN = uuid.UUID("66666666-6666-6666-6666-666666666666")


@pytest.fixture(autouse=True)
def _fernet_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Install a deterministic Fernet key for every test in this module."""
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("STORAGE_ENCRYPTION_KEY", Fernet.generate_key().decode())
    import app.storage.encrypted as encrypted_module

    monkeypatch.setattr(encrypted_module, "_fernet", None)


# ---------------------------------------------------------------------------
# create_pending_seed
# ---------------------------------------------------------------------------


class TestCreatePendingSeed:
    def test_inserts_encrypted_seed_and_returns_token(self):
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = (_TOKEN,)

        seed_text = "Selgita seda leidu: «AvTS § 35» on seotud üksusega «GDPR»."
        token = create_pending_seed(
            conn,
            user_id=_USER_ID,
            org_id=_ORG_ID,
            draft_id=_DRAFT_ID,
            seed_text=seed_text,
        )

        assert token == str(_TOKEN)
        # The SQL is an INSERT ... RETURNING token.
        sql, params = conn.execute.call_args.args
        assert "INSERT INTO pending_chat_seed" in sql
        assert "RETURNING token" in sql
        # Params: user_id, org_id, draft_id, seed_encrypted (bytes, opaque).
        assert params[0] == str(_USER_ID)
        assert params[1] == str(_ORG_ID)
        assert params[2] == str(_DRAFT_ID)
        ciphertext = params[3]
        assert isinstance(ciphertext, bytes)
        # The ciphertext must not be the plaintext bytes.
        assert ciphertext != seed_text.encode("utf-8")
        assert seed_text.encode("utf-8") not in ciphertext

    def test_nullable_org_and_draft_pass_through_as_none(self):
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = (_TOKEN,)

        token = create_pending_seed(
            conn,
            user_id=_USER_ID,
            org_id=None,
            draft_id=None,
            seed_text="ad-hoc finding",
        )
        assert token == str(_TOKEN)
        _sql, params = conn.execute.call_args.args
        assert params[1] is None  # org_id
        assert params[2] is None  # draft_id

    def test_returns_none_on_db_error(self):
        conn = MagicMock()
        conn.execute.side_effect = RuntimeError("db down")
        token = create_pending_seed(
            conn,
            user_id=_USER_ID,
            org_id=_ORG_ID,
            draft_id=None,
            seed_text="x",
        )
        assert token is None

    def test_returns_none_when_insert_yields_no_row(self):
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = None
        token = create_pending_seed(
            conn,
            user_id=_USER_ID,
            org_id=_ORG_ID,
            draft_id=None,
            seed_text="x",
        )
        assert token is None


# ---------------------------------------------------------------------------
# round-trip: create -> consume
# ---------------------------------------------------------------------------


def _row_for(seed_text: str, draft_id: uuid.UUID | None = None) -> tuple:
    """Build a ``(seed_encrypted, draft_id)`` cursor row from a plaintext seed."""
    from app.storage import encrypt_text

    return (encrypt_text(seed_text), str(draft_id) if draft_id else None)


class TestConsumePendingSeed:
    def test_round_trip_decrypts_and_returns_seed_and_draft(self):
        conn = MagicMock()
        seed_text = "Selgita seda leidu: «AvTS § 35». Mida peaksin tähele panema?"
        conn.execute.return_value.fetchone.return_value = _row_for(seed_text, _DRAFT_ID)

        result = consume_pending_seed(conn, token=_TOKEN, user_id=_USER_ID)
        assert result is not None
        got_seed, got_draft = result
        assert got_seed == seed_text
        assert got_draft == _DRAFT_ID

    def test_round_trip_with_no_draft_returns_none_draft(self):
        conn = MagicMock()
        seed_text = "ad-hoc finding question"
        conn.execute.return_value.fetchone.return_value = _row_for(seed_text, None)

        result = consume_pending_seed(conn, token=_TOKEN, user_id=_USER_ID)
        assert result == (seed_text, None)

    def test_deletes_the_row_and_commits(self):
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = _row_for("x", None)

        consume_pending_seed(conn, token=_TOKEN, user_id=_USER_ID)

        # The primary query is a DELETE ... RETURNING (single-use).
        first_sql = conn.execute.call_args_list[0].args[0]
        assert "DELETE FROM pending_chat_seed" in first_sql
        assert "RETURNING seed_encrypted" in first_sql
        # The transaction was committed (so the DELETE lands before render).
        assert conn.commit.called

    def test_wrong_user_returns_none(self):
        # Against a real DB the WHERE user_id = %s clause yields no row; the
        # mock simulates that by returning None from fetchone.
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = None
        result = consume_pending_seed(conn, token=_TOKEN, user_id=_OTHER_USER_ID)
        assert result is None

    def test_expired_token_returns_none(self):
        # The created_at > now() - interval predicate filters out stale rows.
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = None
        result = consume_pending_seed(conn, token=_TOKEN, user_id=_USER_ID)
        assert result is None

    def test_already_consumed_token_returns_none(self):
        # A second consume of the same token finds nothing to DELETE.
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = None
        result = consume_pending_seed(conn, token=_TOKEN, user_id=_USER_ID)
        assert result is None

    def test_bad_token_string_returns_none_without_raising(self):
        conn = MagicMock()
        result = consume_pending_seed(conn, token="not-a-uuid", user_id=_USER_ID)
        assert result is None
        # It should not attempt the DELETE with a garbage token, but it may
        # still issue the GC DELETE — assert it never raised.

    def test_db_error_during_consume_returns_none(self):
        conn = MagicMock()
        conn.execute.side_effect = RuntimeError("db down")
        result = consume_pending_seed(conn, token=_TOKEN, user_id=_USER_ID)
        assert result is None

    def test_garbage_collects_stale_rows(self):
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = _row_for("x", None)

        consume_pending_seed(conn, token=_TOKEN, user_id=_USER_ID)

        # One of the issued statements must be the stale-row GC DELETE.
        all_sql = [c.args[0] for c in conn.execute.call_args_list]
        assert any("created_at < now() - interval" in sql for sql in all_sql)


# ---------------------------------------------------------------------------
# peek_pending_seed (resolve without consuming)
# ---------------------------------------------------------------------------


class TestPeekPendingSeed:
    def test_resolves_without_deleting(self):
        conn = MagicMock()
        seed_text = "peeked seed"
        conn.execute.return_value.fetchone.return_value = _row_for(seed_text, _DRAFT_ID)

        result = peek_pending_seed(conn, token=_TOKEN, user_id=_USER_ID)
        assert result == (seed_text, _DRAFT_ID)

        # The query is a plain SELECT — no DELETE, no commit.
        sql = conn.execute.call_args.args[0]
        assert sql.strip().startswith("SELECT")
        assert "DELETE" not in sql
        assert not conn.commit.called

    def test_wrong_user_returns_none(self):
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = None
        assert peek_pending_seed(conn, token=_TOKEN, user_id=_OTHER_USER_ID) is None

    def test_bad_token_returns_none(self):
        conn = MagicMock()
        assert peek_pending_seed(conn, token="garbage", user_id=_USER_ID) is None
        # A bad token must not even hit the DB.
        assert not conn.execute.called

    def test_db_error_returns_none(self):
        conn = MagicMock()
        conn.execute.side_effect = RuntimeError("db down")
        assert peek_pending_seed(conn, token=_TOKEN, user_id=_USER_ID) is None
