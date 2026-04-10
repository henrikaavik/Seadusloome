"""TARA SSO authentication provider stub.

TARA (Turvaline Autentimine / Secure Authentication) is the Estonian
national identity authentication service operated by RIA (Riigi
Infosüsteemi Amet). This stub implements the :class:`AuthProvider`
interface but raises :class:`NotImplementedError` on all methods.

Activation steps (Phase 5):
    1. Register at https://tara.ria.ee
    2. Set ``TARA_CLIENT_ID``, ``TARA_CLIENT_SECRET``,
       ``TARA_REDIRECT_URI`` in Coolify environment variables.
    3. Set ``AUTH_PROVIDER=tara`` in Coolify.
    4. Implement the OIDC flow in this class using ``authlib``.

Environment variables (all required when ``AUTH_PROVIDER=tara``):
    TARA_CLIENT_ID       — OAuth 2.0 client ID from RIA
    TARA_CLIENT_SECRET   — OAuth 2.0 client secret from RIA
    TARA_REDIRECT_URI    — Callback URL registered with RIA
                           (e.g. https://seadusloome.sixtyfour.ee/auth/callback)
    TARA_ISSUER_URL      — OIDC issuer URL for discovery
                           (e.g. https://tara.ria.ee)
"""

from __future__ import annotations

from app.auth.provider import AuthProvider, UserDict


class TARAAuthProvider(AuthProvider):
    """Stub for Estonian TARA SSO integration.

    All methods raise ``NotImplementedError`` until the OIDC flow is
    implemented in Phase 5.
    """

    def authenticate(self, email: str, password: str) -> UserDict | None:
        """Not applicable for TARA — authentication happens via OIDC redirect."""
        raise NotImplementedError(
            "TARAAuthProvider.authenticate() is not implemented. "
            "TARA uses OIDC redirects, not password-based auth."
        )

    def get_current_user(self, token: str) -> UserDict | None:
        """Decode a TARA OIDC token and return user data."""
        raise NotImplementedError(
            "TARAAuthProvider.get_current_user() is not yet implemented. "
            "Requires OIDC token validation against the TARA issuer."
        )

    def logout(self, session_id: str) -> None:
        """End a TARA session."""
        raise NotImplementedError(
            "TARAAuthProvider.logout() is not yet implemented. "
            "Requires OIDC end-session endpoint integration."
        )
