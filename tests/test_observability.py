"""Tests for app.observability — Sentry init and PII scrubbing (#544)."""

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

        # Restore module
        importlib.reload(importlib.import_module("app.observability"))
