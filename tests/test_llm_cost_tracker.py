"""Tests for ``app.llm.cost_tracker`` and ``app.llm.pricing``."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from app.llm.pricing import calculate_cost


class TestCalculateCost:
    def test_calculate_cost_known_model(self):
        """Sonnet pricing: 3.00 input / 15.00 output per 1M tokens."""
        cost = calculate_cost("claude", "claude-sonnet-4-6", 1000, 500)
        # (1000 * 3.00 + 500 * 15.00) / 1_000_000
        expected = (3000 + 7500) / 1_000_000
        assert abs(cost - expected) < 1e-9

    def test_calculate_cost_unknown_model_returns_zero(self):
        """Unknown provider/model combos return 0.0 without crashing."""
        cost = calculate_cost("openai", "gpt-4o", 1000, 500)
        assert cost == 0.0

    def test_calculate_cost_opus_is_more_expensive(self):
        """Opus costs more than Sonnet for the same token counts."""
        sonnet = calculate_cost("claude", "claude-sonnet-4-6", 1000, 1000)
        opus = calculate_cost("claude", "claude-opus-4-6", 1000, 1000)
        assert opus > sonnet

    def test_calculate_cost_haiku(self):
        """Haiku pricing: 0.80 input / 4.00 output per 1M tokens."""
        cost = calculate_cost("claude", "claude-haiku-4-5", 1_000_000, 0)
        assert abs(cost - 0.80) < 1e-9

    def test_calculate_cost_zero_tokens(self):
        """Zero tokens should produce zero cost."""
        cost = calculate_cost("claude", "claude-sonnet-4-6", 0, 0)
        assert cost == 0.0


class TestLogUsage:
    @patch("app.llm.cost_tracker.get_connection")
    def test_log_usage_writes_to_db(self, mock_get_conn: MagicMock):
        """log_usage inserts a row with the correct values."""
        from app.llm.cost_tracker import log_usage

        mock_conn = MagicMock()
        mock_get_conn.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_get_conn.return_value.__exit__ = MagicMock(return_value=False)

        log_usage(
            user_id="abc-123",
            org_id="org-456",
            provider="claude",
            model="claude-sonnet-4-6",
            feature="drafter_clarify",
            tokens_input=500,
            tokens_output=200,
        )

        mock_conn.execute.assert_called_once()
        sql, params = mock_conn.execute.call_args[0]
        assert "INSERT INTO llm_usage" in sql
        assert params[0] == "abc-123"
        assert params[1] == "org-456"
        assert params[2] == "claude"
        assert params[3] == "claude-sonnet-4-6"
        assert params[4] == "drafter_clarify"
        assert params[5] == 500
        assert params[6] == 200
        # Cost should be calculated
        assert isinstance(params[7], float)
        assert params[7] > 0
        mock_conn.commit.assert_called_once()

    @patch("app.llm.cost_tracker.get_connection")
    def test_log_usage_swallows_db_error(self, mock_get_conn: MagicMock):
        """DB errors are logged but never propagate to callers."""
        from app.llm.cost_tracker import log_usage

        mock_get_conn.side_effect = Exception("DB unavailable")

        # Must not raise
        log_usage(
            user_id=None,
            org_id=None,
            provider="claude",
            model="claude-sonnet-4-6",
            feature="complete",
            tokens_input=100,
            tokens_output=50,
        )
