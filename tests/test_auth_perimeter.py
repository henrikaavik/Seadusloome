"""CSRF origin-check, WS origin allowlist, and trusted-proxy tests (#851).

Covers review findings D2 + D3 and the ticket-comment WS item:

- unsafe-method HTTP requests with cross-site provenance (Origin /
  Referer / Sec-Fetch-Site) are rejected with 403 + Estonian message,
  on representative mutating routes (login, draft delete, chat delete,
  admin purge, annotations);
- same-origin browser requests and HTMX requests pass with zero
  per-form changes; requests without browser provenance headers
  (server-to-server, curl, test clients) pass;
- exempt paths: the HMAC-authenticated GitHub webhook works unchanged
  (verified with a VALID signature + a foreign Origin), signed-URL
  token-bearer downloads and health endpoints are exempt;
- every ``/ws/*`` handshake with a foreign Origin is rejected (close
  1008) before the app accepts; same-origin and origin-less handshakes
  connect;
- ``CSRF_ORIGIN_CHECK=off`` escape hatch disables enforcement;
- ``get_trusted_proxy_hosts`` parses ``TRUSTED_PROXY_HOSTS`` and
  defaults to loopback + RFC1918 (the Coolify/Traefik Docker ranges).

The spoofed X-Forwarded-For ↔ throttle/audit integration lives in
``tests/test_auth_throttle.py`` (it needs the login route fixtures).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
from typing import Any

import pytest
from starlette.responses import PlainTextResponse
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.auth.middleware import _TOKEN_BEARER_PATHS
from app.auth.perimeter import (
    DEFAULT_TRUSTED_PROXY_HOSTS,
    OriginCheckMiddleware,
    evaluate_http_request,
    evaluate_ws_handshake,
    get_trusted_proxy_hosts,
    is_csrf_exempt,
    is_origin_check_enabled,
)
from app.main import app

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _scope(
    headers: dict[str, str] | None = None,
    *,
    scheme: str = "http",
    host: str | None = "testserver",
    type_: str = "http",
    method: str = "POST",
    path: str = "/x",
) -> dict[str, Any]:
    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    if host is not None:
        raw.append((b"host", host.encode()))
    return {
        "type": type_,
        "scheme": scheme,
        "method": method,
        "path": path,
        "headers": raw,
        "client": ("203.0.113.9", 1234),
    }


async def _tiny_app(scope: Any, receive: Any, send: Any) -> None:
    """Minimal ASGI app: 200 'ok' for HTTP, accept-then-idle for WS."""
    if scope["type"] == "http":
        await PlainTextResponse("ok")(scope, receive, send)
        return
    if scope["type"] == "websocket":
        await receive()  # websocket.connect
        await send({"type": "websocket.accept"})
        while True:
            msg = await receive()
            if msg["type"] == "websocket.disconnect":
                return


@pytest.fixture
def tiny_client() -> TestClient:
    return TestClient(OriginCheckMiddleware(_tiny_app))


# ---------------------------------------------------------------------------
# Unit: decision ladder for HTTP requests
# ---------------------------------------------------------------------------


class TestEvaluateHttpRequest:
    def test_matching_origin_allowed(self):
        assert evaluate_http_request(_scope({"Origin": "http://testserver"})) is None

    def test_origin_match_is_case_insensitive(self):
        assert evaluate_http_request(_scope({"Origin": "HTTP://TestServer"})) is None

    def test_foreign_origin_rejected(self):
        reason = evaluate_http_request(_scope({"Origin": "https://evil.example"}))
        assert reason is not None and "evil.example" in reason

    def test_null_origin_rejected(self):
        assert evaluate_http_request(_scope({"Origin": "null"})) is not None

    def test_app_base_url_origin_allowed_even_if_scheme_degraded(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """If proxy trust were misconfigured the scope scheme stays http
        while browsers send the canonical https Origin — APP_BASE_URL
        keeps production logins working."""
        monkeypatch.setenv("APP_BASE_URL", "https://seadusloome.sixtyfour.ee")
        scope = _scope(
            {"Origin": "https://seadusloome.sixtyfour.ee"},
            scheme="http",
            host="seadusloome.sixtyfour.ee",
        )
        assert evaluate_http_request(scope) is None

    def test_referer_fallback_same_origin_allowed(self):
        scope = _scope({"Referer": "http://testserver/auth/login"})
        assert evaluate_http_request(scope) is None

    def test_referer_fallback_foreign_rejected(self):
        scope = _scope({"Referer": "https://evil.example/page"})
        assert evaluate_http_request(scope) is not None

    def test_referer_fallback_malformed_rejected(self):
        assert evaluate_http_request(_scope({"Referer": "garbage"})) is not None

    @pytest.mark.parametrize("value", ["same-origin", "none"])
    def test_sec_fetch_site_allowed_values(self, value: str):
        assert evaluate_http_request(_scope({"Sec-Fetch-Site": value})) is None

    def test_sec_fetch_site_cross_site_rejected(self):
        assert evaluate_http_request(_scope({"Sec-Fetch-Site": "cross-site"})) is not None

    def test_sec_fetch_site_bare_same_site_rejected(self):
        """#851 review round 1: the app is single-origin, so when SFS is
        the deciding signal (no Origin/Referer), ``same-site`` — i.e. a
        sibling subdomain of sixtyfour.ee — is NOT sufficient."""
        assert evaluate_http_request(_scope({"Sec-Fetch-Site": "same-site"})) is not None

    def test_no_provenance_headers_allowed(self):
        """No Origin/Referer/Sec-Fetch-Site → cannot be a cross-site
        browser request (browsers always attach Origin to cross-origin
        POSTs) → allowed, keeping curl/tests/server-to-server working."""
        assert evaluate_http_request(_scope({})) is None

    def test_origin_checked_before_sec_fetch_site(self):
        """A foreign Origin loses even if Sec-Fetch-Site claims same-site
        (strict exact-origin policy — no sibling-subdomain writes)."""
        scope = _scope({"Origin": "https://other.sixtyfour.ee", "Sec-Fetch-Site": "same-site"})
        assert evaluate_http_request(scope) is not None


class TestEvaluateWsHandshake:
    def test_no_origin_allowed(self):
        assert evaluate_ws_handshake(_scope({}, type_="websocket", scheme="ws")) is None

    def test_matching_origin_allowed_ws_scheme_mapping(self):
        scope = _scope({"Origin": "http://testserver"}, type_="websocket", scheme="ws")
        assert evaluate_ws_handshake(scope) is None

    def test_matching_origin_allowed_wss_scheme_mapping(self):
        scope = _scope(
            {"Origin": "https://seadusloome.sixtyfour.ee"},
            type_="websocket",
            scheme="wss",
            host="seadusloome.sixtyfour.ee",
        )
        assert evaluate_ws_handshake(scope) is None

    def test_foreign_origin_rejected(self):
        scope = _scope({"Origin": "https://evil.example"}, type_="websocket", scheme="ws")
        assert evaluate_ws_handshake(scope) is not None


# ---------------------------------------------------------------------------
# Unit: exemptions + escape hatch + proxy trust parsing
# ---------------------------------------------------------------------------


class TestExemptionsAndConfig:
    @pytest.mark.parametrize(
        "path",
        [
            "/webhooks/github",
            "/api/health",
            "/api/ping",
            "/drafts/00000000-0000-0000-0000-000000000001/report/full.docx",
            "/drafts/00000000-0000-0000-0000-000000000001/report/full.pdf",
        ],
    )
    def test_exempt_paths(self, path: str):
        assert is_csrf_exempt(path)

    @pytest.mark.parametrize(
        "path",
        [
            "/auth/login",
            "/auth/logout",
            "/drafts/x/delete",
            "/admin/jobs/purge",
            "/chat/x/delete",
            "/api/annotations",
            "/webhooks/github/extra",  # fullmatch — no prefix bleed
        ],
    )
    def test_non_exempt_paths(self, path: str):
        assert not is_csrf_exempt(path)

    def test_token_bearer_paths_stay_in_sync_with_auth_middleware(self):
        """#307 signed-URL paths must remain exempt if they ever grow
        unsafe methods; the list is imported, not copied — verify each
        pattern is honoured by is_csrf_exempt."""
        for pattern in _TOKEN_BEARER_PATHS:
            sample = pattern.replace("[^/]+", "abc123").replace("\\", "")
            assert re.fullmatch(pattern, sample), f"bad sample for {pattern!r}"
            assert is_csrf_exempt(sample)

    def test_origin_check_enabled_by_default(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("CSRF_ORIGIN_CHECK", raising=False)
        assert is_origin_check_enabled() is True

    @pytest.mark.parametrize("value", ["off", "0", "false", "NO", "Disabled"])
    def test_origin_check_disable_values(self, value: str, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("CSRF_ORIGIN_CHECK", value)
        assert is_origin_check_enabled() is False

    def test_trusted_proxy_default_is_private_ranges(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("TRUSTED_PROXY_HOSTS", raising=False)
        hosts = get_trusted_proxy_hosts()
        assert hosts == list(DEFAULT_TRUSTED_PROXY_HOSTS)
        assert "*" not in hosts

    def test_trusted_proxy_env_override(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("TRUSTED_PROXY_HOSTS", " 172.18.0.0/16 , 127.0.0.1 ")
        assert get_trusted_proxy_hosts() == ["172.18.0.0/16", "127.0.0.1"]

    def test_trusted_proxy_blank_env_falls_back_to_default(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("TRUSTED_PROXY_HOSTS", "   ")
        assert get_trusted_proxy_hosts() == list(DEFAULT_TRUSTED_PROXY_HOSTS)

    @pytest.mark.parametrize(
        "value",
        [
            "*",
            " * ",
            "10.0.0.0/8,*",
            "*,127.0.0.1",
            "*.sixtyfour.ee",
            "172.16.0.0/12, 10.*",
        ],
    )
    def test_trusted_proxy_wildcard_refused_with_safe_fallback(
        self, value: str, monkeypatch: pytest.MonkeyPatch, caplog: Any
    ):
        """#851 review round 1 (P2): a wildcard anywhere in
        TRUSTED_PROXY_HOSTS is REFUSED — error-logged and replaced by the
        private-range defaults — never returned. Previously the function
        warned but still returned ['*'], which flips uvicorn's
        always-trust mode and reopens X-Forwarded-For spoofing (D3)."""
        monkeypatch.setenv("TRUSTED_PROXY_HOSTS", value)
        with caplog.at_level(logging.ERROR, logger="app.auth.perimeter"):
            hosts = get_trusted_proxy_hosts()
        assert hosts == list(DEFAULT_TRUSTED_PROXY_HOSTS)
        assert all("*" not in h for h in hosts)
        assert any(
            "TRUSTED_PROXY_HOSTS" in r.getMessage() and r.levelno == logging.ERROR
            for r in caplog.records
        )

    def test_main_app_no_longer_trusts_all_proxies(self):
        """D3 regression pin: the wired ProxyHeadersMiddleware must not
        be in always-trust mode, nor carry any wildcard entry."""
        from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

        proxy_layers = [m for m in app.user_middleware if m.cls is ProxyHeadersMiddleware]
        assert proxy_layers, "ProxyHeadersMiddleware missing from app"
        for layer in proxy_layers:
            trusted = layer.kwargs.get("trusted_hosts")
            assert trusted not in ("*", ["*"])
            assert isinstance(trusted, list)
            assert all("*" not in h for h in trusted)


# ---------------------------------------------------------------------------
# Middleware behaviour against a tiny ASGI app (all unsafe methods)
# ---------------------------------------------------------------------------


class TestMiddlewareTinyApp:
    @pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
    def test_unsafe_methods_rejected_cross_origin(self, method: str, tiny_client: TestClient):
        resp = tiny_client.request(method, "/anything", headers={"Origin": "https://evil.example"})
        assert resp.status_code == 403
        assert "CSRF-kaitse" in resp.text

    @pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS"])
    def test_safe_methods_never_checked(self, method: str, tiny_client: TestClient):
        resp = tiny_client.request(method, "/anything", headers={"Origin": "https://evil.example"})
        assert resp.status_code == 200

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("same-origin", 200),
            ("none", 200),
            ("same-site", 403),  # sibling subdomain — insufficient (#851 r1)
            ("cross-site", 403),
        ],
    )
    def test_sec_fetch_site_policy_as_deciding_signal(
        self, value: str, expected: int, tiny_client: TestClient
    ):
        """No Origin/Referer present → Sec-Fetch-Site decides: only
        same-origin and none pass; bare same-site is rejected because
        the app is single-origin."""
        resp = tiny_client.post("/anything", headers={"Sec-Fetch-Site": value})
        assert resp.status_code == expected
        if expected == 403:
            assert "CSRF-kaitse" in resp.text

    def test_exempt_path_passes_cross_origin(self, tiny_client: TestClient):
        resp = tiny_client.post("/webhooks/github", headers={"Origin": "https://evil.example"})
        assert resp.status_code == 200

    def test_token_bearer_download_unaffected_by_foreign_origin(self, tiny_client: TestClient):
        path = "/drafts/00000000-0000-0000-0000-000000000001/report/full.docx"
        with_origin = tiny_client.get(
            path, params={"token": "x"}, headers={"Origin": "https://evil.example"}
        )
        without_origin = tiny_client.get(path, params={"token": "x"})
        assert with_origin.status_code == without_origin.status_code == 200

    def test_escape_hatch_disables_http_check(
        self, tiny_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("CSRF_ORIGIN_CHECK", "off")
        resp = tiny_client.post("/anything", headers={"Origin": "https://evil.example"})
        assert resp.status_code == 200

    def test_escape_hatch_disables_ws_check(
        self, tiny_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("CSRF_ORIGIN_CHECK", "off")
        with tiny_client.websocket_connect(
            "/ws/anything", headers={"Origin": "https://evil.example"}
        ):
            pass  # accepted

    def test_ws_foreign_origin_rejected_with_1008(self, tiny_client: TestClient):
        with pytest.raises(WebSocketDisconnect) as excinfo:
            with tiny_client.websocket_connect(
                "/ws/anything", headers={"Origin": "https://evil.example"}
            ):
                pass
        assert excinfo.value.code == 1008


# ---------------------------------------------------------------------------
# Integration on the real app: representative mutating routes
# ---------------------------------------------------------------------------

_MUTATING_PATHS = [
    "/auth/login",  # session-changing (login CSRF)
    "/drafts/00000000-0000-0000-0000-000000000001/delete",  # draft delete
    "/chat/00000000-0000-0000-0000-000000000001/delete",  # chat mutation
    "/admin/jobs/purge",  # admin purge
    "/api/annotations",  # annotation mutation
]


class TestRealAppCsrf:
    @pytest.mark.parametrize("path", _MUTATING_PATHS)
    def test_cross_origin_post_rejected_403(self, path: str, caplog: Any):
        client = TestClient(app, follow_redirects=False)
        with caplog.at_level(logging.WARNING, logger="app.auth.perimeter"):
            resp = client.post(path, headers={"Origin": "https://evil.example"})
        assert resp.status_code == 403
        assert "CSRF-kaitse" in resp.text
        # Rejection is log-visible (DoD: audit/log visible).
        assert any("CSRF origin check rejected" in r.getMessage() for r in caplog.records)

    @pytest.mark.parametrize("path", _MUTATING_PATHS[1:])  # login handled below
    def test_same_origin_post_passes_check(self, path: str):
        """Same-origin POSTs clear the CSRF layer; unauthenticated they
        then get the beforeware's 303 to /auth/login — NOT a 403."""
        client = TestClient(app, follow_redirects=False)
        resp = client.post(path, headers={"Origin": "http://testserver"})
        assert resp.status_code != 403

    def test_same_origin_htmx_login_passes(self, monkeypatch: pytest.MonkeyPatch):
        """HTMX-shaped request (Origin + HX-Request + Sec-Fetch-Site) on
        the login route reaches the handler with zero form changes."""
        from unittest.mock import patch

        from app.auth import throttle

        monkeypatch.setattr(throttle, "is_login_throttled", lambda *_a: False)
        monkeypatch.setattr(throttle, "record_login_failure", lambda *_a: None)
        with (
            patch("app.auth.routes._provider") as provider,
            patch("app.auth.routes.log_action"),
        ):
            provider.authenticate.return_value = None
            client = TestClient(app, follow_redirects=False)
            resp = client.post(
                "/auth/login",
                data={"email": "a@b.ee", "password": "x"},
                headers={
                    "Origin": "http://testserver",
                    "HX-Request": "true",
                    "Sec-Fetch-Site": "same-origin",
                },
            )
        assert resp.status_code == 200
        assert "Vale e-post või parool." in resp.text

    def test_headerless_post_passes_check(self):
        """Non-browser clients (no provenance headers) are not blocked."""
        client = TestClient(app, follow_redirects=False)
        resp = client.post(_MUTATING_PATHS[1])
        assert resp.status_code != 403

    def test_escape_hatch_on_real_app(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("CSRF_ORIGIN_CHECK", "off")
        client = TestClient(app, follow_redirects=False)
        resp = client.post(_MUTATING_PATHS[1], headers={"Origin": "https://evil.example"})
        # Falls through to the auth redirect instead of the CSRF 403.
        assert resp.status_code == 303

    def test_webhook_with_valid_hmac_unaffected(self, monkeypatch: pytest.MonkeyPatch):
        """The HMAC-authenticated webhook must work even with a foreign
        Origin header — proves the exemption end-to-end with a VALID
        signature (ping event: no DB involved)."""
        secret = "test-webhook-secret"
        monkeypatch.setattr("app.sync.webhook.WEBHOOK_SECRET", secret)
        body = b'{"zen": "test ping"}'
        signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

        client = TestClient(app, follow_redirects=False)
        resp = client.post(
            "/webhooks/github",
            content=body,
            headers={
                "Origin": "https://evil.example",
                "X-GitHub-Event": "ping",
                "X-GitHub-Delivery": "00000000-0000-0000-0000-00000000d851",
                "X-Hub-Signature-256": signature,
                "Content-Type": "application/json",
            },
        )
        assert resp.status_code == 200
        assert resp.json() == {"status": "pong"}

    def test_webhook_invalid_hmac_still_rejected_by_handler(self, monkeypatch: pytest.MonkeyPatch):
        """Exemption does not weaken the webhook: its own HMAC check
        still rejects bad signatures (401), it just isn't OUR 403."""
        monkeypatch.setattr("app.sync.webhook.WEBHOOK_SECRET", "test-webhook-secret")
        client = TestClient(app, follow_redirects=False)
        resp = client.post(
            "/webhooks/github",
            content=b"{}",
            headers={
                "Origin": "https://evil.example",
                "X-GitHub-Event": "push",
                "X-Hub-Signature-256": "sha256=deadbeef",
            },
        )
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Integration on the real app: WS handshakes
# ---------------------------------------------------------------------------


class TestRealAppWsOrigin:
    def test_ws_foreign_origin_rejected(self, caplog: Any):
        client = TestClient(app)
        with caplog.at_level(logging.WARNING, logger="app.auth.perimeter"):
            with pytest.raises(WebSocketDisconnect) as excinfo:
                with client.websocket_connect(
                    "/ws/explorer", headers={"Origin": "https://evil.example"}
                ):
                    pass
        assert excinfo.value.code == 1008
        assert any("WS origin check rejected" in r.getMessage() for r in caplog.records)

    def test_ws_same_origin_accepted(self):
        client = TestClient(app)
        with client.websocket_connect("/ws/explorer", headers={"Origin": "http://testserver"}):
            pass  # handshake accepted

    def test_ws_no_origin_accepted(self):
        """Non-browser WS clients carry no Origin — must keep working."""
        client = TestClient(app)
        with client.websocket_connect("/ws/explorer"):
            pass
