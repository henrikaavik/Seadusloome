"""Security-header matrix tests for ``SecurityHeadersMiddleware`` (#857).

Every HTTP response — HTML pages, JSON/API, static assets, redirects,
404s, and middleware short-circuits — must carry the defensive header
set. HSTS is production-only (a Strict-Transport-Security header served
over plain-http local dev would poison the browser's HSTS cache for
localhost). The CSP must keep the app's actual frontend working: the
inline theme-init script, ~30 inline Script() islands, inline onclick=
handlers, htmx event filters, the jsdelivr/cdnjs CDN loads, and the
chat/explorer/notifications WebSockets.
"""

from __future__ import annotations

import importlib

import pytest
from starlette.testclient import TestClient

from app.main import _CSP_POLICY, app
from app.ui.theme import THEME_INIT_SCRIPT

EXPECTED_ALWAYS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
}


def _csp_directives(header_value: str) -> dict[str, set[str]]:
    """Parse a CSP header into {directive: {tokens}}."""
    out: dict[str, set[str]] = {}
    for chunk in header_value.split(";"):
        parts = chunk.strip().split()
        if parts:
            out[parts[0]] = set(parts[1:])
    return out


@pytest.fixture
def client() -> TestClient:
    return TestClient(app, follow_redirects=False)


class TestHeaderMatrix:
    """Presence/value matrix across representative response types."""

    @pytest.mark.parametrize(
        "path,expected_status",
        [
            ("/auth/login", 200),  # HTML page
            ("/api/ping", 200),  # API response
            ("/static/css/tokens.css", 200),  # static asset via StaticFiles mount
            ("/", 303),  # redirect (unauthenticated root)
            ("/no-such-page-857", 404),  # router 404 (unknown path)
        ],
    )
    def test_headers_present(self, client: TestClient, path: str, expected_status: int):
        resp = client.get(path)
        assert resp.status_code == expected_status
        for name, value in EXPECTED_ALWAYS.items():
            assert resp.headers.get(name) == value, f"{name} missing/wrong on {path}"
        assert resp.headers.get("Content-Security-Policy") == _CSP_POLICY

    def test_hsts_absent_in_dev(self, client: TestClient):
        """No Strict-Transport-Security outside production (APP_ENV unset → dev)."""
        for path in ("/auth/login", "/api/ping", "/static/css/tokens.css"):
            resp = client.get(path)
            assert "Strict-Transport-Security" not in resp.headers, path

    def test_hsts_present_in_production(self, client: TestClient, monkeypatch: pytest.MonkeyPatch):
        """The HSTS gate reads the normalized APP_ENV per request."""
        monkeypatch.setenv("APP_ENV", "production")
        resp = client.get("/api/ping")
        hsts = resp.headers.get("Strict-Transport-Security")
        assert hsts is not None
        assert "max-age=31536000" in hsts
        assert "includeSubDomains" in hsts

    def test_hsts_not_emitted_for_staging(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ):
        """Only production is TLS-fronted today; staging/unknown get no HSTS."""
        monkeypatch.setenv("APP_ENV", "staging")
        assert "Strict-Transport-Security" not in client.get("/api/ping").headers


class TestCSPContent:
    """The policy must be strict where possible and honest where it can't be."""

    def test_baseline_directives(self):
        csp = _csp_directives(_CSP_POLICY)
        assert csp["default-src"] == {"'self'"}
        assert csp["frame-ancestors"] == {"'none'"}
        assert csp["object-src"] == {"'none'"}
        assert csp["base-uri"] == {"'self'"}
        assert csp["form-action"] == {"'self'"}
        assert csp["img-src"] == {"'self'", "data:"}  # data: svg chevron in ui.css
        assert csp["font-src"] == {"'self'"}  # Aino woff2 under /static/fonts

    def test_script_src_keeps_frontend_alive(self):
        csp = _csp_directives(_CSP_POLICY)
        script = csp["script-src"]
        # FastHTML default hdrs (htmx, fasthtml-js, surreal, css-scope-inline)
        # and chat's marked/dompurify come from jsdelivr; explorer D3 from cdnjs.
        assert "https://cdn.jsdelivr.net" in script
        assert "https://cdnjs.cloudflare.com" in script
        # Inline Script() islands and onclick= handlers need 'unsafe-inline';
        # htmx hx-trigger event filters (keyup[key=='Enter']) need 'unsafe-eval'.
        assert "'unsafe-inline'" in script
        assert "'unsafe-eval'" in script

    def test_connect_src_allows_websockets(self):
        """Chat/explorer/notifications open WebSockets from location.host."""
        connect = _csp_directives(_CSP_POLICY)["connect-src"]
        assert "'self'" in connect  # htmx XHR
        assert "ws:" in connect
        assert "wss:" in connect

    def test_no_nonce_or_hash_while_unsafe_inline_is_load_bearing(self):
        """Tripwire: a nonce/hash in script-src makes CSP2+ browsers IGNORE
        'unsafe-inline', which would break every inline Script() island and
        onclick= handler in one deploy. Whoever removes this assertion must
        first externalize those inline islands (see the #857 follow-up note
        in app/main.py).
        """
        script = _csp_directives(_CSP_POLICY)["script-src"]
        for token in script:
            assert not token.startswith("'nonce-"), token
            assert not token.startswith("'sha256-"), token
            assert not token.startswith("'sha384-"), token
            assert not token.startswith("'sha512-"), token

    def test_style_src_allows_inline(self):
        """style= attributes (cost dashboard, explorer) + css-scope-inline."""
        style = _csp_directives(_CSP_POLICY)["style-src"]
        assert style == {"'self'", "'unsafe-inline'"}


class TestThemeScriptUnderCSP:
    """The FOUC-avoidance theme script must actually execute under the CSP."""

    def test_theme_script_inline_and_executable(self, client: TestClient):
        resp = client.get("/auth/login")
        assert resp.status_code == 200
        # The inline script is present verbatim in <head>…
        assert "setAttribute('data-theme', 'dark')" in resp.text
        assert THEME_INIT_SCRIPT.strip() in resp.text
        # …and the served CSP authorizes inline scripts (no nonce/hash that
        # would disable 'unsafe-inline' — see the tripwire test above).
        script = _csp_directives(resp.headers["Content-Security-Policy"])["script-src"]
        assert "'unsafe-inline'" in script

    def test_cdn_script_tags_match_allowlisted_hosts(self, client: TestClient):
        """The CDN hosts in script-src are exactly the ones pages reference."""
        resp = client.get("/auth/login")
        assert 'src="https://cdn.jsdelivr.net' in resp.text  # htmx & co.


class TestSessionCookieHardening:
    """#857: the session cookie is Secure in production, not in local dev."""

    @staticmethod
    def _session_kwargs(asgi_app) -> dict:  # type: ignore[type-arg]
        mws = [m for m in asgi_app.user_middleware if m.cls.__name__ == "SessionMiddleware"]
        assert len(mws) == 1, "expected exactly one SessionMiddleware"
        return dict(mws[0].kwargs)

    def test_dev_app_session_cookie_not_https_only(self):
        """Hard ``True`` would break http TestClient/local dev cookie return."""
        assert self._session_kwargs(app)["https_only"] is False

    def test_prod_app_session_cookie_https_only(self, monkeypatch: pytest.MonkeyPatch):
        """Reload app.main under APP_ENV=production (pattern from
        tests/test_prod_middleware.py) and assert the flag flips."""
        monkeypatch.setenv("APP_ENV", "production")
        monkeypatch.setenv("SECRET_KEY", "x" * 64)
        monkeypatch.setenv("DATABASE_URL", "postgresql://fake:fake@fake-host:5432/fake")

        import app.auth.jwt_provider
        import app.db
        import app.main

        importlib.reload(app.db)
        importlib.reload(app.auth.jwt_provider)
        prod_main = importlib.reload(app.main)
        try:
            assert self._session_kwargs(prod_main.app)["https_only"] is True
        finally:
            monkeypatch.delenv("APP_ENV", raising=False)
            monkeypatch.delenv("SECRET_KEY", raising=False)
            monkeypatch.delenv("DATABASE_URL", raising=False)
            importlib.reload(app.db)
            importlib.reload(app.auth.jwt_provider)
            importlib.reload(app.main)
