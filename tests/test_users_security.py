"""DB-free unit tests for the #857 hardening in ``app.auth.users``.

Covers two changes:

1. ``create_user`` provisions every account with
   ``must_change_password = TRUE`` — all callers are admin/org-admin
   flows, so the initial password is always known to a second party and
   must be rotated on first login (the middleware enforcement itself is
   covered by ``tests/test_auth_middleware.py``; the end-to-end flow by
   the DATABASE_URL-gated test in ``tests/test_admin_password_reset.py``).

2. The temp-password reveal stash: the revealed credential lives in a
   server-side single-use store; the (signed but UNENCRYPTED) session
   cookie carries only an opaque token.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock, patch

import pytest
from starlette.requests import Request

from app.auth.users import (
    _pop_temp_password_reveal,
    _pw_reveal_stash,
    _stash_temp_password_reveal,
    create_user,
)


@pytest.fixture(autouse=True)
def _clean_stash():
    _pw_reveal_stash.clear()
    yield
    _pw_reveal_stash.clear()


# ---------------------------------------------------------------------------
# create_user → must_change_password
# ---------------------------------------------------------------------------


class TestCreateUserForcedRotation:
    def _fake_conn(self) -> MagicMock:
        conn = MagicMock()
        conn.__enter__.return_value = conn
        conn.execute.return_value.fetchone.return_value = (
            "11111111-1111-1111-1111-111111111111",
            "uus@asutus.ee",
            "Uus Kasutaja",
            "drafter",
            None,
            True,
        )
        return conn

    def test_insert_sets_must_change_password_true(self):
        conn = self._fake_conn()
        with patch("app.auth.users._connect", return_value=conn):
            user = create_user("uus@asutus.ee", "Salajane1", "Uus Kasutaja", "drafter")

        assert user is not None
        sql = conn.execute.call_args[0][0]
        assert "must_change_password" in sql
        assert "TRUE" in sql
        # The flag is a literal, not a forgotten extra placeholder.
        params = conn.execute.call_args[0][1]
        assert len(params) == sql.count("%s")

    def test_password_is_hashed_not_plaintext(self):
        conn = self._fake_conn()
        with patch("app.auth.users._connect", return_value=conn):
            create_user("uus@asutus.ee", "Salajane1", "Uus Kasutaja", "drafter")
        params = conn.execute.call_args[0][1]
        assert "Salajane1" not in params

    def test_invalid_role_rejected_without_db(self):
        with patch("app.auth.users._connect") as connect:
            assert create_user("a@b.ee", "Salajane1", "Nimi", "superuser") is None
        connect.assert_not_called()


# ---------------------------------------------------------------------------
# Temp-password reveal stash (server-side, single-use)
# ---------------------------------------------------------------------------


def _req_with_session(session: dict | None = None) -> Request:  # type: ignore[type-arg]
    """Duck-typed request: ``_session_dict`` only touches ``req.session``."""
    if session is None:
        # .session raises AttributeError → handled by _session_dict
        return cast(Request, SimpleNamespace())
    return cast(Request, SimpleNamespace(session=session))


class TestRevealStash:
    def test_password_never_enters_the_session_mapping(self):
        sess: dict = {}
        _stash_temp_password_reveal(_req_with_session(sess), "user-1", "Ajutine1X")

        assert "pw_reset_reveal" in sess
        ref = sess["pw_reset_reveal"]
        assert ref["user_id"] == "user-1"
        assert "password" not in ref
        # Nothing in the session payload contains the credential.
        assert "Ajutine1X" not in repr(sess)
        # The credential lives server-side, keyed by the opaque token.
        assert _pw_reveal_stash[ref["token"]][1] == "Ajutine1X"

    def test_pop_returns_password_exactly_once(self):
        sess: dict = {}
        _stash_temp_password_reveal(_req_with_session(sess), "user-1", "Ajutine1X")

        assert _pop_temp_password_reveal(_req_with_session(sess), "user-1") == "Ajutine1X"
        assert _pw_reveal_stash == {}  # server-side entry burned
        assert "pw_reset_reveal" not in sess  # session reference drained
        assert _pop_temp_password_reveal(_req_with_session(sess), "user-1") is None

    def test_mismatched_user_burns_entry_without_reveal(self):
        sess: dict = {}
        _stash_temp_password_reveal(_req_with_session(sess), "user-1", "Ajutine1X")

        assert _pop_temp_password_reveal(_req_with_session(sess), "user-OTHER") is None
        # Single-use even on mismatch: the entry must not survive for a
        # second attempt against the right user.
        assert _pw_reveal_stash == {}
        assert _pop_temp_password_reveal(_req_with_session(sess), "user-1") is None

    def test_expired_entry_not_revealed(self):
        sess: dict = {}
        _stash_temp_password_reveal(_req_with_session(sess), "user-1", "Ajutine1X")
        token = sess["pw_reset_reveal"]["token"]
        user_id, password, _ = _pw_reveal_stash[token]
        _pw_reveal_stash[token] = (user_id, password, 0.0)  # already expired

        assert _pop_temp_password_reveal(_req_with_session(sess), "user-1") is None
        assert _pw_reveal_stash == {}

    def test_expired_entries_swept_on_next_stash(self):
        sess_old: dict = {}
        _stash_temp_password_reveal(_req_with_session(sess_old), "user-1", "Vana1Parool")
        old_token = sess_old["pw_reset_reveal"]["token"]
        uid, pw, _ = _pw_reveal_stash[old_token]
        _pw_reveal_stash[old_token] = (uid, pw, 0.0)

        sess_new: dict = {}
        _stash_temp_password_reveal(_req_with_session(sess_new), "user-2", "Uus1Parool")

        assert old_token not in _pw_reveal_stash  # abandoned credential purged
        assert len(_pw_reveal_stash) == 1

    def test_no_session_is_a_noop(self):
        _stash_temp_password_reveal(_req_with_session(None), "user-1", "Ajutine1X")
        assert _pw_reveal_stash == {}
        assert _pop_temp_password_reveal(_req_with_session(None), "user-1") is None

    def test_forged_session_reference_reveals_nothing(self):
        """A tampered/stale token in the session must not match anything."""
        sess: dict = {"pw_reset_reveal": {"user_id": "user-1", "token": "forged-token"}}
        assert _pop_temp_password_reveal(_req_with_session(sess), "user-1") is None

    def test_legacy_session_value_with_password_key_is_ignored(self):
        """Pre-#857 sessions stored the password itself — never honour it."""
        sess: dict = {"pw_reset_reveal": {"user_id": "user-1", "password": "Leek1nud"}}
        assert _pop_temp_password_reveal(_req_with_session(sess), "user-1") is None
        assert "pw_reset_reveal" not in sess
