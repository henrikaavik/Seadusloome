"""Tests for app.auth.tara_provider — TARA SSO stub (#546)."""

from __future__ import annotations

import pytest

from app.auth.provider import AuthProvider
from app.auth.tara_provider import TARAAuthProvider


class TestTARAStub:
    def test_is_auth_provider_subclass(self):
        provider = TARAAuthProvider()
        assert isinstance(provider, AuthProvider)

    def test_authenticate_raises(self):
        provider = TARAAuthProvider()
        with pytest.raises(NotImplementedError, match="OIDC redirects"):
            provider.authenticate("test@example.com", "password")

    def test_get_current_user_raises(self):
        provider = TARAAuthProvider()
        with pytest.raises(NotImplementedError, match="OIDC token validation"):
            provider.get_current_user("some.jwt.token")

    def test_logout_raises(self):
        provider = TARAAuthProvider()
        with pytest.raises(NotImplementedError, match="end-session endpoint"):
            provider.logout("session-123")


class TestTARAEnvVarsDocumented:
    def test_env_vars_in_env_example(self):
        """Verify that TARA env vars are listed in .env.example."""
        from pathlib import Path

        project_root = Path(__file__).resolve().parent.parent
        env_example = (project_root / ".env.example").read_text()
        assert "TARA_CLIENT_ID" in env_example
        assert "TARA_CLIENT_SECRET" in env_example
        assert "TARA_REDIRECT_URI" in env_example
        assert "TARA_ISSUER_URL" in env_example
        assert "AUTH_PROVIDER" in env_example
