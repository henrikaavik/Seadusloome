"""Tests for app.observability — Sentry init and PII scrubbing (#544, #846)."""

from __future__ import annotations

import importlib
from unittest.mock import MagicMock, patch

import pytest


class TestGetGitSha:
    def test_reads_env_var(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("GIT_SHA", "abc1234")
        from app.observability import _get_git_sha

        assert _get_git_sha() == "abc1234"

    def test_falls_back_to_git(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("GIT_SHA", raising=False)
        from app.observability import _get_git_sha

        # In the test repo, git should be available
        sha = _get_git_sha()
        assert isinstance(sha, str)
        assert len(sha) >= 7  # short hash

    def test_returns_unknown_when_both_fail(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("GIT_SHA", raising=False)
        with patch("subprocess.check_output", side_effect=FileNotFoundError):
            from app.observability import _get_git_sha

            assert _get_git_sha() == "unknown"


class TestScrubPii:
    def test_removes_user_context(self):
        from app.observability import _scrub_pii

        event = {"user": {"email": "test@example.com", "id": "123"}, "message": "err"}
        result = _scrub_pii(event, {})
        assert result is not None
        assert "user" not in result
        assert result["message"] == "err"

    def test_redacts_breadcrumb_emails(self):
        from app.observability import _scrub_pii

        event = {
            "breadcrumbs": {
                "values": [
                    {"data": {"email": "secret@example.com", "other": "keep"}},
                ]
            }
        }
        result = _scrub_pii(event, {})
        assert result is not None
        bc_data = result["breadcrumbs"]["values"][0]["data"]
        assert bc_data["email"] == "[Redacted]"
        assert bc_data["other"] == "keep"

    def test_redacts_frame_vars(self):
        from app.observability import _scrub_pii

        event = {
            "exception": {
                "values": [
                    {
                        "stacktrace": {
                            "frames": [
                                {
                                    "vars": {
                                        "email": "user@test.ee",
                                        "full_name": "Test User",
                                        "password": "s3cret",
                                        "token": "jwt.xxx",
                                        "x": 42,
                                    }
                                }
                            ]
                        }
                    }
                ]
            }
        }
        result = _scrub_pii(event, {})
        assert result is not None
        local_vars = result["exception"]["values"][0]["stacktrace"]["frames"][0]["vars"]
        assert local_vars["email"] == "[Redacted]"
        assert local_vars["full_name"] == "[Redacted]"
        assert local_vars["password"] == "[Redacted]"
        assert local_vars["token"] == "[Redacted]"
        assert local_vars["x"] == 42

    def test_handles_missing_breadcrumbs_gracefully(self):
        from app.observability import _scrub_pii

        event = {"message": "simple error"}
        result = _scrub_pii(event, {})
        assert result is not None
        assert result["message"] == "simple error"

    def test_handles_missing_stacktrace_gracefully(self):
        from app.observability import _scrub_pii

        event = {"exception": {"values": [{"type": "ValueError"}]}}
        result = _scrub_pii(event, {})
        assert result is not None


_RESET_TOKEN = "ab12cd34" * 8  # token_hex(32) → 64 hex chars


class TestRequestScrubbing:
    """#846 — event["request"] must never ship bearer credentials."""

    def test_reset_path_redacted_in_url(self):
        from app.observability import _scrub_pii

        event = {
            "request": {
                "url": f"https://seadusloome.sixtyfour.ee/auth/reset/{_RESET_TOKEN}",
                "method": "GET",
            }
        }
        result = _scrub_pii(event, {})
        assert result is not None
        assert result["request"]["url"] == (
            "https://seadusloome.sixtyfour.ee/auth/reset/[redacted]"
        )
        # Route context survives for debugging.
        assert result["request"]["method"] == "GET"

    def test_generic_hex_path_segment_redacted(self):
        from app.observability import _scrub_pii

        event = {"request": {"url": f"https://x.ee/files/{'deadbeef' * 4}/view"}}
        result = _scrub_pii(event, {})
        assert result is not None
        assert result["request"]["url"] == "https://x.ee/files/[redacted]/view"

    def test_sensitive_query_params_redacted_in_url(self):
        from app.observability import _scrub_pii

        event = {
            "request": {
                # Home-grown signed download token: 2 b64url segments —
                # the JWT regex does NOT match it, so param-name
                # redaction is the only line of defence.
                "url": ("https://x.ee/docs/raport?token=eyJkcmFmdF9pZCJ9.c2lnbmF0dXJl&vaade=koik")
            }
        }
        result = _scrub_pii(event, {})
        assert result is not None
        assert result["request"]["url"] == ("https://x.ee/docs/raport?token=[redacted]&vaade=koik")

    def test_query_string_field_scrubbed(self):
        from app.observability import _scrub_pii

        event = {
            "request": {
                "query_string": (
                    "token=abc123&seed=550e8400-e29b-41d4-a716-446655440000&focus=estleg%3AKarS"
                )
            }
        }
        result = _scrub_pii(event, {})
        assert result is not None
        assert result["request"]["query_string"] == (
            "token=[redacted]&seed=[redacted]&focus=estleg%3AKarS"
        )

    def test_auth_headers_redacted_benign_headers_kept(self):
        from app.observability import _scrub_pii

        event = {
            "request": {
                "headers": {
                    "Authorization": "Bearer abc.def",
                    "Cookie": "access_token=xyz",
                    "X-Api-Key": "k-123456",
                    "Referer": f"https://x.ee/auth/reset/{_RESET_TOKEN}?token=abc",
                    "Accept": "text/html",
                }
            }
        }
        result = _scrub_pii(event, {})
        assert result is not None
        headers = result["request"]["headers"]
        assert headers["Authorization"] == "[Redacted]"
        assert headers["Cookie"] == "[Redacted]"
        assert headers["X-Api-Key"] == "[Redacted]"
        # Referer keeps the route but loses both credentials.
        assert headers["Referer"] == ("https://x.ee/auth/reset/[redacted]?token=[redacted]")
        assert headers["Accept"] == "text/html"

    def test_cookies_redacted_wholesale(self):
        from app.observability import _scrub_pii

        event = {"request": {"cookies": {"access_token": "ey.x", "theme": "dark"}}}
        result = _scrub_pii(event, {})
        assert result is not None
        assert result["request"]["cookies"] == "[Redacted]"

    def test_form_data_dict_scrubbed(self):
        from app.observability import _scrub_pii

        event = {
            "request": {
                "data": {
                    "password": "hunter2",
                    "email": "mari@example.ee",
                    "comment": "isikukood 38501010002",
                }
            }
        }
        result = _scrub_pii(event, {})
        assert result is not None
        data = result["request"]["data"]
        assert data["password"] == "[Redacted]"
        assert data["email"] == "[Redacted]"
        assert data["comment"] == "isikukood [REDACTED_ISIKUKOOD]"

    def test_form_data_string_scrubbed(self):
        from app.observability import _scrub_pii

        event = {"request": {"data": "password=hunter2&note=ok"}}
        result = _scrub_pii(event, {})
        assert result is not None
        assert result["request"]["data"] == "password=[redacted]&note=ok"

    def test_benign_request_kept_intact(self):
        from app.observability import _scrub_pii

        event = {
            "request": {
                "url": "https://x.ee/analyysikeskus/normi-mojuahel",
                "query_string": "sisend=PS+%C2%A7+12&vaade=koik",
                "method": "GET",
            }
        }
        result = _scrub_pii(event, {})
        assert result is not None
        assert result["request"]["url"] == ("https://x.ee/analyysikeskus/normi-mojuahel")
        assert result["request"]["query_string"] == "sisend=PS+%C2%A7+12&vaade=koik"


class TestTransactionScrubbing:
    """#846 — the same function must scrub transaction events and spans."""

    def test_transaction_request_and_spans_scrubbed(self):
        from app.observability import _scrub_pii

        event = {
            "type": "transaction",
            "transaction": "/auth/reset/{token}",
            "request": {"url": f"https://x.ee/auth/reset/{_RESET_TOKEN}"},
            "spans": [
                {
                    "description": ("GET https://api.example.ee/callback?access_token=abc123"),
                    "data": {
                        "url": "https://api.example.ee/callback?access_token=abc123",
                        "status_code": 200,
                    },
                }
            ],
        }
        result = _scrub_pii(event, {})
        assert result is not None
        assert result["request"]["url"] == "https://x.ee/auth/reset/[redacted]"
        span = result["spans"][0]
        assert span["description"] == (
            "GET https://api.example.ee/callback?access_token=[redacted]"
        )
        assert span["data"]["url"] == ("https://api.example.ee/callback?access_token=[redacted]")
        # Useful telemetry survives.
        assert span["data"]["status_code"] == 200
        assert result["transaction"] == "/auth/reset/{token}"


class TestBreadcrumbScrubbing:
    """#846 — httpx/log breadcrumbs carry the same token-bearing URLs."""

    def test_httpx_breadcrumb_url_redacted(self):
        from app.observability import _scrub_pii

        event = {
            "breadcrumbs": {
                "values": [
                    {
                        "category": "httplib",
                        "data": {
                            "url": "https://x.ee/download?token=tok-123",
                            "method": "GET",
                            "status_code": 200,
                        },
                    }
                ]
            }
        }
        result = _scrub_pii(event, {})
        assert result is not None
        data = result["breadcrumbs"]["values"][0]["data"]
        assert data["url"] == "https://x.ee/download?token=[redacted]"
        assert data["method"] == "GET"
        assert data["status_code"] == 200

    def test_breadcrumb_message_reset_link_redacted(self):
        from app.observability import _scrub_pii

        event = {
            "breadcrumbs": {
                "values": [
                    {
                        "message": (
                            f"Password reset link sent: https://x.ee/auth/reset/{_RESET_TOKEN}"
                        )
                    }
                ]
            }
        }
        result = _scrub_pii(event, {})
        assert result is not None
        message = result["breadcrumbs"]["values"][0]["message"]
        assert _RESET_TOKEN not in message
        assert message == ("Password reset link sent: https://x.ee/auth/reset/[redacted]")

    def test_breadcrumb_estonian_pii_scrubbed(self):
        from app.observability import _scrub_pii

        event = {
            "breadcrumbs": {
                "values": [{"message": ("klient 38501010002 maksis kontolt EE382200221020145685")}]
            }
        }
        result = _scrub_pii(event, {})
        assert result is not None
        message = result["breadcrumbs"]["values"][0]["message"]
        assert message == ("klient [REDACTED_ISIKUKOOD] maksis kontolt [REDACTED_IBAN]")

    def test_breadcrumbs_as_list_shape_supported(self):
        from app.observability import _scrub_pii

        event = {"breadcrumbs": [{"message": "token=abc123"}]}
        result = _scrub_pii(event, {})
        assert result is not None
        assert result["breadcrumbs"][0]["message"] == "token=[redacted]"


class TestLogentryScrubbing:
    def test_logentry_message_and_params_scrubbed(self):
        from app.observability import _scrub_pii

        event = {
            "logentry": {
                "message": "reset url %s for mari@example.ee",
                "params": [f"https://x.ee/auth/reset/{_RESET_TOKEN}"],
            }
        }
        result = _scrub_pii(event, {})
        assert result is not None
        logentry = result["logentry"]
        assert logentry["message"] == "reset url %s for [REDACTED_EMAIL]"
        assert logentry["params"] == ["https://x.ee/auth/reset/[redacted]"]


class TestInitSentry:
    def test_noop_without_dsn(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("SENTRY_DSN", raising=False)
        # Should not raise, should not import sentry_sdk
        from app.observability import init_sentry

        init_sentry()  # no-op

    def test_calls_sentry_init_with_dsn(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("SENTRY_DSN", "https://key@sentry.io/123")
        monkeypatch.setenv("APP_ENV", "testing")
        monkeypatch.setenv("GIT_SHA", "test123")

        mock_init = MagicMock()
        with patch.dict("sys.modules", {"sentry_sdk": MagicMock(init=mock_init)}):
            # Re-import to get fresh module state
            mod = importlib.reload(importlib.import_module("app.observability"))
            mod.init_sentry()

            mock_init.assert_called_once()
            call_kwargs = mock_init.call_args[1]
            assert call_kwargs["dsn"] == "https://key@sentry.io/123"
            assert call_kwargs["traces_sample_rate"] == 0.1
            assert call_kwargs["release"] == "test123"
            assert call_kwargs["environment"] == "testing"
            assert call_kwargs["before_send"] is not None
            # #846: transactions must run through the SAME scrubber so
            # sampled request URLs (reset links, ?token= downloads)
            # never ship unscrubbed.
            assert call_kwargs["before_send_transaction"] is not None
            assert call_kwargs["before_send_transaction"] is call_kwargs["before_send"]

        # Restore module
        importlib.reload(importlib.import_module("app.observability"))
