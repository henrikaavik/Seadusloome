"""Tests for token-version based access-token revocation (#635).

Access tokens trust JWT claims alone in the pre-fix implementation, so
role updates and deactivations do not take effect until the token
expires (up to 60 minutes). The fix embeds a ``tv`` (token version)
claim in the access token and verifies it — along with ``is_active``,
``role`` and ``org_id`` — against the DB on every authenticated request.

These tests exercise ``JWTAuthProvider.get_current_user`` with
``_connect`` mocked so they never touch a real database.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock, patch

import jwt

from app.auth.jwt_provider import (
    JWT_ALGORITHM,
    SECRET_KEY,
)


def _jwt_provider_cls():
    """Re-fetch ``JWTAuthProvider`` after any module reloads performed
    by neighbouring tests in ``tests/test_auth.py``."""
    import app.auth.jwt_provider as mod

    return mod.JWTAuthProvider


def _make_token(
    *,
    sub: str = "uid-1",
    email: str = "a@b.ee",
    role: str = "drafter",
    org_id: str | None = None,
    tv: int | None = 0,
    exp_delta: timedelta = timedelta(hours=1),
    full_name: str = "A B",
) -> str:
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "sub": sub,
        "email": email,
        "role": role,
        "full_name": full_name,
        "org_id": org_id,
        "exp": now + exp_delta,
        "iat": now,
    }
    if tv is not None:
        payload["tv"] = tv
    return jwt.encode(payload, SECRET_KEY, algorithm=JWT_ALGORITHM)


def _patch_connect(conn_ctx: MagicMock):
    """Return a context-manager mock whose __enter__ returns *conn_ctx*."""
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=conn_ctx)
    ctx.__exit__ = MagicMock(return_value=False)
    return ctx


class TestTokenVersionVerification:
    """get_current_user must reject tokens whose ``tv`` claim is stale."""

    def _provider(self):
        return _jwt_provider_cls()(database_url="postgresql://fake:fake@localhost/fake")

    @patch("app.auth.jwt_provider.JWTAuthProvider._connect")
    def test_matching_token_version_accepted(self, mock_connect: MagicMock):
        conn = MagicMock()
        # (token_version, is_active, role, org_id)
        conn.execute.return_value.fetchone.return_value = (0, True, "drafter", None)
        mock_connect.return_value = _patch_connect(conn)

        provider = self._provider()
        token = _make_token(tv=0, role="drafter")
        user = provider.get_current_user(token)

        assert user is not None
        assert user["id"] == "uid-1"
        assert user["role"] == "drafter"

    @patch("app.auth.jwt_provider.JWTAuthProvider._connect")
    def test_stale_token_version_rejected(self, mock_connect: MagicMock):
        """After role change, tv increments; old token must 401."""
        conn = MagicMock()
        # DB says token_version is now 1 (incremented after role change).
        conn.execute.return_value.fetchone.return_value = (1, True, "drafter", None)
        mock_connect.return_value = _patch_connect(conn)

        provider = self._provider()
        # Old token still carries tv=0.
        token = _make_token(tv=0, role="admin")  # stale elevated role
        assert provider.get_current_user(token) is None

    @patch("app.auth.jwt_provider.JWTAuthProvider._connect")
    def test_deactivated_user_rejected_even_with_valid_token(self, mock_connect: MagicMock):
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = (0, False, "drafter", None)
        mock_connect.return_value = _patch_connect(conn)

        provider = self._provider()
        token = _make_token(tv=0)
        assert provider.get_current_user(token) is None

    @patch("app.auth.jwt_provider.JWTAuthProvider._connect")
    def test_user_not_in_db_rejected(self, mock_connect: MagicMock):
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = None
        mock_connect.return_value = _patch_connect(conn)

        provider = self._provider()
        token = _make_token(tv=0)
        assert provider.get_current_user(token) is None

    @patch("app.auth.jwt_provider.JWTAuthProvider._connect")
    def test_role_mismatch_between_token_and_db_rejected(self, mock_connect: MagicMock):
        """If DB role differs from JWT role the token is stale."""
        conn = MagicMock()
        # DB role is drafter, token still claims admin.
        conn.execute.return_value.fetchone.return_value = (0, True, "drafter", None)
        mock_connect.return_value = _patch_connect(conn)

        provider = self._provider()
        token = _make_token(tv=0, role="admin")
        assert provider.get_current_user(token) is None

    @patch("app.auth.jwt_provider.JWTAuthProvider._connect")
    def test_missing_tv_claim_rejected(self, mock_connect: MagicMock):
        """Legacy tokens without tv must be rejected after migration."""
        # No DB call expected; the early validation trips first.
        conn = MagicMock()
        mock_connect.return_value = _patch_connect(conn)

        provider = self._provider()
        token = _make_token(tv=None)
        assert provider.get_current_user(token) is None


class TestCreateTokensEmbedsTokenVersion:
    """create_tokens must fetch and embed the current token_version."""

    @patch("app.auth.jwt_provider.JWTAuthProvider._connect")
    def test_access_token_contains_tv_claim(self, mock_connect: MagicMock):
        conn = MagicMock()
        # Two sequential execute calls: SELECT token_version, then INSERT session.
        # The SELECT returns (5,); the INSERT has no meaningful return.
        select_cursor = MagicMock()
        select_cursor.fetchone.return_value = (5,)
        insert_cursor = MagicMock()
        conn.execute.side_effect = [select_cursor, insert_cursor]
        mock_connect.return_value = _patch_connect(conn)

        provider = _jwt_provider_cls()(database_url="postgresql://fake:fake@localhost/fake")
        user: dict[str, Any] = {
            "id": "uid-1",
            "email": "a@b.ee",
            "full_name": "A B",
            "role": "drafter",
            "org_id": None,
        }
        access_token, _refresh_token = provider.create_tokens(user)  # type: ignore[arg-type]

        decoded = jwt.decode(access_token, SECRET_KEY, algorithms=[JWT_ALGORITHM])
        assert decoded["tv"] == 5


class TestUpdateUserRoleIncrementsTokenVersion:
    """update_user_role must bump token_version atomically."""

    @patch("app.auth.users._connect")
    def test_update_role_bumps_token_version(self, mock_connect: MagicMock):
        from app.auth.users import update_user_role

        conn = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)

        ok = update_user_role("some-uid", "reviewer")
        assert ok is True

        # The UPDATE must touch both role and token_version.
        sql = conn.execute.call_args[0][0]
        assert "UPDATE users" in sql
        assert "token_version" in sql
        assert "role" in sql


class TestDeactivateUserIncrementsTokenVersion:
    """deactivate_user must bump token_version atomically."""

    @patch("app.auth.users._connect")
    def test_deactivate_bumps_token_version(self, mock_connect: MagicMock):
        from app.auth.users import deactivate_user

        conn = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)

        ok = deactivate_user("some-uid")
        assert ok is True

        # First call = UPDATE users (is_active + token_version).
        first_sql = conn.execute.call_args_list[0][0][0]
        assert "UPDATE users" in first_sql
        assert "is_active" in first_sql
        assert "token_version" in first_sql
