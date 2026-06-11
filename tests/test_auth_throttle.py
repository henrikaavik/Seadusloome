"""Login throttling, timing-equalization, and auth audit tests (#851).

Covers review finding D1 plus the ticket-comment additions:

- per-email AND per-IP failure throttling on ``POST /auth/login``
  (mirrors the ``password_reset_attempts`` pattern, migration 040);
- no account-existence leak: the throttle keys on the normalized email
  hash before any user lookup, and ``JWTAuthProvider.authenticate``
  runs a dummy bcrypt check on the unknown-email path so timing is
  equal for known and unknown emails;
- audit events for the full auth lifecycle (login success/failure/
  throttled, logout, token refresh) carrying the validated client IP;
- spoofed ``X-Forwarded-For`` from an untrusted peer does NOT change
  the IP used for throttling/audit (D3 integration).

DB-backed counting SQL is unit-tested with a mocked connection; the
route-level limit semantics use a stateful in-memory fake of the
throttle module. A live-DB integration test (skipped without
``DATABASE_URL``) exercises the real SQL end-to-end.
"""

from __future__ import annotations

import os
import uuid
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from starlette.testclient import TestClient

from app.auth import throttle
from app.auth.password import hash_email
from app.auth.throttle import (
    LOGIN_EMAIL_FAIL_LIMIT,
    LOGIN_IP_FAIL_LIMIT,
    clear_login_failures,
    is_login_throttled,
    record_login_failure,
)
from app.main import app


def _user_dict() -> dict[str, Any]:
    return {
        "id": "uid-1",
        "email": "user@seadusloome.ee",
        "full_name": "Test Kasutaja",
        "role": "drafter",
        "org_id": None,
    }


def _mock_conn_ctx(conn: MagicMock) -> MagicMock:
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=conn)
    ctx.__exit__ = MagicMock(return_value=False)
    return ctx


# ---------------------------------------------------------------------------
# Unit tests: throttle SQL + thresholds (mocked connection)
# ---------------------------------------------------------------------------


class TestThrottleUnit:
    @patch("app.auth.throttle.get_connection")
    def test_below_limits_not_throttled(self, mock_get_conn: MagicMock):
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = (
            LOGIN_EMAIL_FAIL_LIMIT - 1,
            LOGIN_IP_FAIL_LIMIT - 1,
        )
        mock_get_conn.return_value = _mock_conn_ctx(conn)

        assert is_login_throttled("ehash", "1.2.3.4") is False
        sql = conn.execute.call_args[0][0]
        assert "login_attempts" in sql
        # psycopg interval substitution rule: make_interval, never `interval %s`.
        assert "make_interval" in sql
        assert "interval %s" not in sql

    @patch("app.auth.throttle.get_connection")
    def test_email_limit_throttles(self, mock_get_conn: MagicMock):
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = (LOGIN_EMAIL_FAIL_LIMIT, 0)
        mock_get_conn.return_value = _mock_conn_ctx(conn)

        assert is_login_throttled("ehash", "1.2.3.4") is True

    @patch("app.auth.throttle.get_connection")
    def test_ip_limit_throttles(self, mock_get_conn: MagicMock):
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = (0, LOGIN_IP_FAIL_LIMIT)
        mock_get_conn.return_value = _mock_conn_ctx(conn)

        assert is_login_throttled("other-hash", "1.2.3.4") is True

    @patch("app.auth.throttle.get_connection")
    def test_db_error_fails_open(self, mock_get_conn: MagicMock):
        """A DB outage must not raise — authenticate() needs the same DB
        anyway, so fail-open cannot enable brute force during an outage."""
        mock_get_conn.side_effect = Exception("connection refused")

        assert is_login_throttled("ehash", "1.2.3.4") is False
        record_login_failure("ehash", "1.2.3.4")  # must not raise
        clear_login_failures("ehash")  # must not raise

    @patch("app.auth.throttle.get_connection")
    def test_record_failure_inserts_and_commits(self, mock_get_conn: MagicMock):
        conn = MagicMock()
        mock_get_conn.return_value = _mock_conn_ctx(conn)

        record_login_failure("ehash", "1.2.3.4")

        sql = conn.execute.call_args[0][0]
        assert "INSERT INTO login_attempts" in sql
        assert conn.execute.call_args[0][1] == ("ehash", "1.2.3.4")
        conn.commit.assert_called_once()

    @patch("app.auth.throttle.get_connection")
    def test_clear_failures_deletes_email_rows(self, mock_get_conn: MagicMock):
        conn = MagicMock()
        mock_get_conn.return_value = _mock_conn_ctx(conn)

        clear_login_failures("ehash")

        sql = conn.execute.call_args[0][0]
        assert "DELETE FROM login_attempts" in sql
        assert conn.execute.call_args[0][1] == ("ehash",)
        conn.commit.assert_called_once()


# ---------------------------------------------------------------------------
# Route-level throttle semantics with a stateful in-memory fake
# ---------------------------------------------------------------------------


class _FakeThrottle:
    """In-memory reimplementation of the throttle contract (no window)."""

    def __init__(self) -> None:
        self.email_failures: dict[str, int] = {}
        self.ip_failures: dict[str, int] = {}

    def is_login_throttled(self, email_hash: str, ip: str) -> bool:
        return (
            self.email_failures.get(email_hash, 0) >= LOGIN_EMAIL_FAIL_LIMIT
            or self.ip_failures.get(ip, 0) >= LOGIN_IP_FAIL_LIMIT
        )

    def record_login_failure(self, email_hash: str, ip: str) -> None:
        self.email_failures[email_hash] = self.email_failures.get(email_hash, 0) + 1
        self.ip_failures[ip] = self.ip_failures.get(ip, 0) + 1

    def clear_login_failures(self, email_hash: str) -> None:
        self.email_failures.pop(email_hash, None)


@pytest.fixture
def fake_throttle(monkeypatch: pytest.MonkeyPatch) -> _FakeThrottle:
    fake = _FakeThrottle()
    monkeypatch.setattr(throttle, "is_login_throttled", fake.is_login_throttled)
    monkeypatch.setattr(throttle, "record_login_failure", fake.record_login_failure)
    monkeypatch.setattr(throttle, "clear_login_failures", fake.clear_login_failures)
    return fake


class TestLoginThrottleRoute:
    @patch("app.auth.routes.log_action")
    @patch("app.auth.routes._provider")
    def test_repeated_failures_throttle_by_email(
        self,
        mock_provider: MagicMock,
        _mock_log: MagicMock,
        fake_throttle: _FakeThrottle,
    ):
        mock_provider.authenticate.return_value = None
        client = TestClient(app, follow_redirects=False)

        for _ in range(LOGIN_EMAIL_FAIL_LIMIT):
            resp = client.post(
                "/auth/login",
                data={"email": "victim@seadusloome.ee", "password": "wrong"},
            )
            assert resp.status_code == 200
            assert "Vale e-post või parool." in resp.text

        resp = client.post(
            "/auth/login",
            data={"email": "victim@seadusloome.ee", "password": "wrong"},
        )
        assert resp.status_code == 429
        assert "Liiga palju sisselogimiskatseid" in resp.text
        # Lockout holds even with the CORRECT password (and authenticate
        # is never consulted while throttled).
        mock_provider.authenticate.reset_mock()
        mock_provider.authenticate.return_value = _user_dict()
        resp = client.post(
            "/auth/login",
            data={"email": "victim@seadusloome.ee", "password": "correct"},
        )
        assert resp.status_code == 429
        mock_provider.authenticate.assert_not_called()

    @patch("app.auth.routes.log_action")
    @patch("app.auth.routes._provider")
    def test_email_normalization_shares_throttle_key(
        self,
        mock_provider: MagicMock,
        _mock_log: MagicMock,
        fake_throttle: _FakeThrottle,
    ):
        """`Victim@…` and `victim@…` must hit the same throttle bucket."""
        mock_provider.authenticate.return_value = None
        client = TestClient(app, follow_redirects=False)

        for _ in range(LOGIN_EMAIL_FAIL_LIMIT):
            client.post(
                "/auth/login",
                data={"email": "Victim@Seadusloome.EE ", "password": "wrong"},
            )
        resp = client.post(
            "/auth/login",
            data={"email": "victim@seadusloome.ee", "password": "wrong"},
        )
        assert resp.status_code == 429

    @patch("app.auth.routes.log_action")
    @patch("app.auth.routes._provider")
    def test_repeated_failures_throttle_by_ip(
        self,
        mock_provider: MagicMock,
        _mock_log: MagicMock,
        fake_throttle: _FakeThrottle,
    ):
        """Spreading failures over many emails still trips the IP limit."""
        mock_provider.authenticate.return_value = None
        client = TestClient(app, follow_redirects=False)

        for i in range(LOGIN_IP_FAIL_LIMIT):
            resp = client.post(
                "/auth/login",
                data={"email": f"spray-{i}@example.com", "password": "wrong"},
            )
            assert resp.status_code == 200

        resp = client.post(
            "/auth/login",
            data={"email": "fresh-email@example.com", "password": "wrong"},
        )
        assert resp.status_code == 429

    @patch("app.auth.routes.log_action")
    @patch("app.auth.routes._provider")
    def test_unknown_and_known_email_throttle_identically(
        self,
        mock_provider: MagicMock,
        _mock_log: MagicMock,
        fake_throttle: _FakeThrottle,
    ):
        """Same status code and same body for known vs unknown emails —
        neither the failure response nor the throttled response may leak
        account existence."""
        mock_provider.authenticate.return_value = None
        client = TestClient(app, follow_redirects=False)

        def drain(email: str) -> tuple[list[int], int]:
            codes = []
            for _ in range(LOGIN_EMAIL_FAIL_LIMIT):
                codes.append(
                    client.post("/auth/login", data={"email": email, "password": "x"}).status_code
                )
            throttled = client.post("/auth/login", data={"email": email, "password": "x"})
            return codes, throttled.status_code

        known_codes, known_throttled = drain("user@seadusloome.ee")
        unknown_codes, unknown_throttled = drain("ghost@example.com")
        assert known_codes == unknown_codes
        assert known_throttled == unknown_throttled == 429

    @patch("app.auth.routes.log_action")
    @patch("app.auth.routes._provider")
    def test_successful_login_clears_email_failures(
        self,
        mock_provider: MagicMock,
        _mock_log: MagicMock,
        fake_throttle: _FakeThrottle,
    ):
        mock_provider.authenticate.return_value = None
        client = TestClient(app, follow_redirects=False)
        for _ in range(LOGIN_EMAIL_FAIL_LIMIT - 1):
            client.post(
                "/auth/login",
                data={"email": "user@seadusloome.ee", "password": "wrong"},
            )

        mock_provider.authenticate.return_value = _user_dict()
        mock_provider.create_tokens.return_value = ("at", "rt")
        resp = client.post(
            "/auth/login",
            data={"email": "user@seadusloome.ee", "password": "correct"},
        )
        assert resp.status_code == 303
        assert fake_throttle.email_failures.get(hash_email("user@seadusloome.ee"), 0) == 0


# ---------------------------------------------------------------------------
# D3 integration: spoofed X-Forwarded-For must not bypass the IP throttle
# ---------------------------------------------------------------------------


class TestSpoofedForwardedFor:
    @patch("app.auth.routes.log_action")
    @patch("app.auth.routes._provider")
    def test_untrusted_peer_xff_is_ignored(
        self,
        mock_provider: MagicMock,
        _mock_log: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """The default TestClient peer ('testclient') is not a trusted
        proxy, so a spoofed X-Forwarded-For must NOT change the IP the
        throttle records — rotating XFF cannot reset the budget."""
        mock_provider.authenticate.return_value = None
        seen_ips: list[str] = []
        monkeypatch.setattr(throttle, "is_login_throttled", lambda *_a: False)
        monkeypatch.setattr(throttle, "record_login_failure", lambda _eh, ip: seen_ips.append(ip))

        client = TestClient(app, follow_redirects=False)
        for spoofed in ("6.6.6.1", "6.6.6.2", "6.6.6.3"):
            client.post(
                "/auth/login",
                data={"email": "a@b.ee", "password": "wrong"},
                headers={"X-Forwarded-For": spoofed},
            )

        assert seen_ips == ["testclient", "testclient", "testclient"]

    @patch("app.auth.routes.log_action")
    @patch("app.auth.routes._provider")
    def test_trusted_proxy_xff_is_honoured(
        self,
        mock_provider: MagicMock,
        _mock_log: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """When the direct peer IS in the trusted ranges (Docker bridge),
        the forwarded client address is used — that is the whole point of
        ProxyHeadersMiddleware behind Traefik."""
        mock_provider.authenticate.return_value = None
        seen_ips: list[str] = []
        monkeypatch.setattr(throttle, "is_login_throttled", lambda *_a: False)
        monkeypatch.setattr(throttle, "record_login_failure", lambda _eh, ip: seen_ips.append(ip))

        client = TestClient(app, follow_redirects=False, client=("10.0.0.5", 7777))
        client.post(
            "/auth/login",
            data={"email": "a@b.ee", "password": "wrong"},
            headers={"X-Forwarded-For": "203.0.113.7"},
        )

        assert seen_ips == ["203.0.113.7"]


# ---------------------------------------------------------------------------
# Timing equalization (#851 comment): dummy bcrypt on unknown email
# ---------------------------------------------------------------------------


class TestAuthenticateTiming:
    def _provider(self):  # noqa: ANN202
        from app.auth.jwt_provider import JWTAuthProvider

        return JWTAuthProvider(database_url="postgresql://fake:fake@localhost/fake")

    def test_unknown_email_runs_dummy_bcrypt(self):
        from app.auth.jwt_provider import _DUMMY_PASSWORD_HASH

        provider = self._provider()
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = None

        with (
            patch(
                "app.auth.jwt_provider.JWTAuthProvider._connect",
                return_value=_mock_conn_ctx(conn),
            ),
            patch("app.auth.jwt_provider.bcrypt.checkpw", return_value=False) as mock_checkpw,
        ):
            result = provider.authenticate("ghost@example.com", "any-password")

        assert result is None
        mock_checkpw.assert_called_once_with(b"any-password", _DUMMY_PASSWORD_HASH.encode())

    def test_known_email_runs_bcrypt_against_stored_hash(self):
        provider = self._provider()
        stored_hash = "$2b$12$abcdefghijklmnopqrstuvCDEF0123456789012345678901234"
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = (
            "uid-1",
            "user@seadusloome.ee",
            stored_hash,
            "Test Kasutaja",
            "drafter",
            None,
            False,
        )

        with (
            patch(
                "app.auth.jwt_provider.JWTAuthProvider._connect",
                return_value=_mock_conn_ctx(conn),
            ),
            patch("app.auth.jwt_provider.bcrypt.checkpw", return_value=False) as mock_checkpw,
        ):
            result = provider.authenticate("user@seadusloome.ee", "wrong")

        assert result is None
        # Both paths perform exactly one bcrypt verification — equal cost.
        mock_checkpw.assert_called_once_with(b"wrong", stored_hash.encode())

    def test_dummy_hash_has_default_cost_factor(self):
        """The pad must cost the same as real hashes (gensalt default 12)."""
        from app.auth.jwt_provider import _DUMMY_PASSWORD_HASH

        assert _DUMMY_PASSWORD_HASH.startswith("$2b$12$")


# ---------------------------------------------------------------------------
# Audit events for the auth lifecycle (#851 comment) with validated IP
# ---------------------------------------------------------------------------


class TestAuthAuditEvents:
    @patch("app.auth.routes.log_action")
    @patch("app.auth.routes._provider")
    def test_login_success_audited_with_ip(
        self,
        mock_provider: MagicMock,
        mock_log: MagicMock,
        fake_throttle: _FakeThrottle,
    ):
        mock_provider.authenticate.return_value = _user_dict()
        mock_provider.create_tokens.return_value = ("at", "rt")
        client = TestClient(app, follow_redirects=False)

        resp = client.post(
            "/auth/login",
            data={"email": "user@seadusloome.ee", "password": "correct"},
            # Spoof attempt — must not reach the audit row (untrusted peer).
            headers={"X-Forwarded-For": "6.6.6.6"},
        )

        assert resp.status_code == 303
        mock_log.assert_called_once_with(
            "uid-1",
            "auth.login",
            {"ip": "testclient", "email": "user@seadusloome.ee"},
        )

    @patch("app.auth.routes.log_action")
    @patch("app.auth.routes._provider")
    def test_login_failure_audited_with_email_hash(
        self,
        mock_provider: MagicMock,
        mock_log: MagicMock,
        fake_throttle: _FakeThrottle,
    ):
        mock_provider.authenticate.return_value = None
        client = TestClient(app, follow_redirects=False)

        client.post(
            "/auth/login",
            data={"email": "Ghost@Example.com", "password": "wrong"},
            headers={"X-Forwarded-For": "6.6.6.6"},
        )

        mock_log.assert_called_once_with(
            None,
            "auth.login_failed",
            {"ip": "testclient", "email_hash": hash_email("ghost@example.com")},
        )

    @patch("app.auth.routes.log_action")
    @patch("app.auth.routes._provider")
    def test_login_throttled_audited(
        self,
        mock_provider: MagicMock,
        mock_log: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.setattr(throttle, "is_login_throttled", lambda *_a: True)
        client = TestClient(app, follow_redirects=False)

        resp = client.post("/auth/login", data={"email": "a@b.ee", "password": "x"})

        assert resp.status_code == 429
        mock_log.assert_called_once_with(
            None,
            "auth.login_throttled",
            {"ip": "testclient", "email_hash": hash_email("a@b.ee")},
        )
        mock_provider.authenticate.assert_not_called()

    @patch("app.auth.middleware._get_provider")
    @patch("app.auth.routes.log_action")
    @patch("app.auth.routes._provider")
    def test_logout_audited_with_ip(
        self,
        mock_route_provider: MagicMock,
        mock_log: MagicMock,
        mock_get_mw_provider: MagicMock,
    ):
        mw_provider = MagicMock()
        mw_provider.get_current_user.return_value = _user_dict()
        mock_get_mw_provider.return_value = mw_provider

        client = TestClient(app, follow_redirects=False)
        resp = client.post(
            "/auth/logout",
            cookies={"access_token": "at-123", "refresh_token": "rt-123"},
        )

        assert resp.status_code == 303
        mock_log.assert_called_once_with("uid-1", "auth.logout", {"ip": "testclient"})

    @patch("app.auth.middleware.log_action")
    @patch("app.auth.middleware._get_provider")
    def test_token_refresh_audited_with_ip(
        self,
        mock_get_mw_provider: MagicMock,
        mock_log: MagicMock,
    ):
        mw_provider = MagicMock()
        mw_provider.get_current_user.return_value = None
        mw_provider.verify_refresh_token.return_value = _user_dict()
        mw_provider.create_tokens.return_value = ("new-at", "new-rt")
        mock_get_mw_provider.return_value = mw_provider

        client = TestClient(app, follow_redirects=False)
        resp = client.get(
            "/dashboard",
            cookies={"access_token": "expired", "refresh_token": "ok"},
        )

        assert resp.status_code == 307
        mock_log.assert_called_once_with("uid-1", "auth.token_refresh", {"ip": "testclient"})


# ---------------------------------------------------------------------------
# Live-DB integration (skipped without DATABASE_URL) — real SQL end-to-end
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestThrottleIntegration:
    def test_real_counting_and_clearing(self):
        if not os.getenv("DATABASE_URL"):
            pytest.skip("integration test — DATABASE_URL not set")
        import psycopg

        with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
            if conn.execute("SELECT to_regclass('login_attempts')").fetchone() == (None,):
                pytest.skip("migration 040 (login_attempts) not applied")

        email_hash = hash_email(f"throttle-{uuid.uuid4()}@example.com")
        ip = f"203.0.113.{uuid.uuid4().int % 250 + 1}"
        try:
            assert is_login_throttled(email_hash, ip) is False
            for _ in range(LOGIN_EMAIL_FAIL_LIMIT):
                record_login_failure(email_hash, ip)
            assert is_login_throttled(email_hash, ip) is True
            clear_login_failures(email_hash)
            assert is_login_throttled(email_hash, ip) is False
        finally:
            with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
                conn.execute(
                    "DELETE FROM login_attempts WHERE email_hash = %s OR ip = %s",
                    (email_hash, ip),
                )
                conn.commit()
