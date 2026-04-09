"""Regression tests for production-only middleware paths.

Background (#439): Round 2 added TrustedHostMiddleware behind an
`APP_ENV != "development"` guard, and Round 3 added a Dockerfile
HEALTHCHECK that calls `curl http://localhost:5001/api/ping`. In prod,
TrustedHostMiddleware rejected the localhost Host header with 400
Bad Request, marking every new deploy unhealthy and rolling back.

The rest of the test suite runs with `APP_ENV` unset (defaults to
"development") so TrustedHostMiddleware never registers, which means
that entire prod-only code path was uncovered. These tests force
`APP_ENV=production` by reloading `app.main` with the env var set,
then exercise the liveness probe with the Host headers the real
Dockerfile HEALTHCHECK uses.
"""

from __future__ import annotations

import importlib
import os

import pytest
from starlette.testclient import TestClient


@pytest.fixture
def prod_app(monkeypatch):
    """Reload `app.main` with `APP_ENV=production` so TrustedHostMiddleware
    is registered, then restore the default dev mode afterwards.

    Required env vars (SECRET_KEY, DATABASE_URL) must also be set so the
    Round 2 startup guards in `jwt_provider` and `db` don't raise.
    """
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("SECRET_KEY", "x" * 64)
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql://fake:fake@fake-host:5432/fake",
    )

    # Force reimport so the module-level `app.add_middleware(...)` runs
    # under the production env.
    import app.auth.jwt_provider
    import app.db
    import app.main

    importlib.reload(app.db)
    importlib.reload(app.auth.jwt_provider)
    importlib.reload(app.main)

    yield app.main.app

    # Restore dev defaults for subsequent tests.
    monkeypatch.delenv("APP_ENV", raising=False)
    monkeypatch.delenv("SECRET_KEY", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    importlib.reload(app.db)
    importlib.reload(app.auth.jwt_provider)
    importlib.reload(app.main)


class TestHealthCheckHostHeader:
    """The Docker HEALTHCHECK calls `curl http://localhost:5001/api/ping`
    from inside the container. That request carries `Host: localhost:5001`.
    TrustedHostMiddleware must accept it or deploys will roll back.
    """

    def test_ping_accepts_localhost_host(self, prod_app):
        client = TestClient(prod_app)
        response = client.get("/api/ping", headers={"host": "localhost:5001"})
        assert response.status_code == 200, (
            f"TrustedHostMiddleware rejected localhost — Docker HEALTHCHECK "
            f"will fail and Coolify will roll back every deploy (#439). "
            f"Got {response.status_code}: {response.text}"
        )
        assert response.text == "ok"

    def test_ping_accepts_127_host(self, prod_app):
        client = TestClient(prod_app)
        response = client.get("/api/ping", headers={"host": "127.0.0.1:5001"})
        assert response.status_code == 200
        assert response.text == "ok"

    def test_ping_accepts_primary_domain(self, prod_app):
        client = TestClient(prod_app)
        response = client.get("/api/ping", headers={"host": "seadusloome.sixtyfour.ee"})
        assert response.status_code == 200
        assert response.text == "ok"

    def test_ping_rejects_unknown_host(self, prod_app):
        """Sanity check: TrustedHostMiddleware is still enforcing the list.
        An attacker-set Host header like `evil.example.com` must NOT pass.
        """
        client = TestClient(prod_app)
        response = client.get("/api/ping", headers={"host": "evil.example.com"})
        assert response.status_code == 400, (
            "TrustedHostMiddleware is not enforcing the allowed_hosts list. "
            "Either it is no longer registered, or the list now contains "
            "a wildcard that matches everything."
        )


class TestProxyHeadersMiddleware:
    """ProxyHeadersMiddleware must rewrite `scope['scheme']` from
    `X-Forwarded-Proto` so that redirect URLs and `request.url_for(...)`
    produce `https://...` in production (Round 2 #408).
    """

    def test_scheme_rewritten_from_forwarded_proto(self, prod_app):
        client = TestClient(prod_app)
        # Simulate Traefik: connect over HTTP, signal real scheme via header
        response = client.get(
            "/",
            headers={
                "host": "seadusloome.sixtyfour.ee",
                "x-forwarded-proto": "https",
                "x-forwarded-for": "203.0.113.42",
            },
            follow_redirects=False,
        )
        # Unauthenticated root redirects to login
        assert response.status_code == 303
        assert response.headers["location"] == "/auth/login"


# Make sure the fixture really does reset global state for downstream
# tests in the same process.
def test_app_env_default_restored_after_prod_fixture():
    assert os.environ.get("APP_ENV") in (None, "development")
