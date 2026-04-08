"""Unit tests for the audit logging module.

These tests verify log_action behaviour without a running PostgreSQL instance.
Database errors should be silently caught and logged.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch


class TestLogAction:
    """Tests for app.auth.audit.log_action."""

    @patch("app.auth.audit.psycopg")
    def test_log_action_inserts_row(self, mock_psycopg: MagicMock):
        """log_action should execute an INSERT against the audit_log table."""
        from app.auth.audit import log_action

        mock_conn = MagicMock()
        mock_psycopg.connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_psycopg.connect.return_value.__exit__ = MagicMock(return_value=False)

        log_action("user-123", "org.create", {"org_id": "org-456"})

        mock_conn.execute.assert_called_once()
        call_args = mock_conn.execute.call_args
        sql = call_args[0][0]
        assert "INSERT INTO audit_log" in sql
        params = call_args[0][1]
        assert params[0] == "user-123"
        assert params[1] == "org.create"

    @patch("app.auth.audit.psycopg")
    def test_log_action_with_none_user(self, mock_psycopg: MagicMock):
        """log_action should accept None as user_id for system events."""
        from app.auth.audit import log_action

        mock_conn = MagicMock()
        mock_psycopg.connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_psycopg.connect.return_value.__exit__ = MagicMock(return_value=False)

        log_action(None, "system.startup")

        mock_conn.execute.assert_called_once()
        params = mock_conn.execute.call_args[0][1]
        assert params[0] is None
        assert params[1] == "system.startup"
        # detail should be None when not provided
        assert params[2] is None

    @patch("app.auth.audit.psycopg")
    def test_log_action_with_detail(self, mock_psycopg: MagicMock):
        """log_action should JSON-serialize the detail dict."""
        import json

        from app.auth.audit import log_action

        mock_conn = MagicMock()
        mock_psycopg.connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_psycopg.connect.return_value.__exit__ = MagicMock(return_value=False)

        detail = {"key": "value", "count": 42}
        log_action("user-1", "test.action", detail)

        params = mock_conn.execute.call_args[0][1]
        parsed = json.loads(params[2])
        assert parsed == detail

    @patch("app.auth.audit.psycopg")
    def test_log_action_db_error_does_not_raise(self, mock_psycopg: MagicMock):
        """log_action should catch database errors and not propagate them."""
        from app.auth.audit import log_action

        mock_psycopg.connect.side_effect = Exception("Connection refused")

        # Should not raise
        log_action("user-1", "test.action", {"x": 1})

    @patch("app.auth.audit.psycopg")
    def test_log_action_none_detail_passes_none(self, mock_psycopg: MagicMock):
        """When detail is None, the SQL parameter should be None (not 'null')."""
        from app.auth.audit import log_action

        mock_conn = MagicMock()
        mock_psycopg.connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_psycopg.connect.return_value.__exit__ = MagicMock(return_value=False)

        log_action("user-1", "test.no_detail")

        params = mock_conn.execute.call_args[0][1]
        assert params[2] is None
