# pyright: reportArgumentType=false
"""Tests for ``app.chat.rate_limiter``.

Covers:
- :func:`check_message_rate` exceeding and passing
- :func:`check_org_cost_budget` exceeding and passing
- Graceful degradation when the DB is unavailable (fail-open)
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest  # noqa: I001

from app.chat.rate_limiter import (
    _MAX_MESSAGES_PER_HOUR,
    _MAX_MONTHLY_COST_USD,
    CostBudgetExceededError,
    RateLimitExceededError,
    check_message_rate,
    check_org_cost_budget,
)

_USER_ID = "11111111-1111-1111-1111-111111111111"
_ORG_ID = "22222222-2222-2222-2222-222222222222"


# ---------------------------------------------------------------------------
# check_message_rate
# ---------------------------------------------------------------------------


class TestCheckMessageRate:
    @patch("app.chat.rate_limiter.get_connection")
    def test_under_limit_passes(self, mock_get_conn: MagicMock):
        """No exception when message count is below the limit."""
        conn = MagicMock()
        mock_get_conn.return_value.__enter__ = MagicMock(return_value=conn)
        mock_get_conn.return_value.__exit__ = MagicMock(return_value=False)
        conn.execute.return_value.fetchone.return_value = (10,)

        # Should not raise
        check_message_rate(_USER_ID)

    @patch("app.chat.rate_limiter.get_connection")
    def test_at_limit_raises(self, mock_get_conn: MagicMock):
        """RateLimitExceededError when count equals the limit."""
        conn = MagicMock()
        mock_get_conn.return_value.__enter__ = MagicMock(return_value=conn)
        mock_get_conn.return_value.__exit__ = MagicMock(return_value=False)
        conn.execute.return_value.fetchone.return_value = (_MAX_MESSAGES_PER_HOUR,)

        with pytest.raises(RateLimitExceededError):
            check_message_rate(_USER_ID)

    @patch("app.chat.rate_limiter.get_connection")
    def test_above_limit_raises(self, mock_get_conn: MagicMock):
        """RateLimitExceededError when count exceeds the limit."""
        conn = MagicMock()
        mock_get_conn.return_value.__enter__ = MagicMock(return_value=conn)
        mock_get_conn.return_value.__exit__ = MagicMock(return_value=False)
        conn.execute.return_value.fetchone.return_value = (_MAX_MESSAGES_PER_HOUR + 50,)

        with pytest.raises(RateLimitExceededError):
            check_message_rate(_USER_ID)

    @patch("app.chat.rate_limiter.get_connection")
    def test_zero_messages_passes(self, mock_get_conn: MagicMock):
        """No exception when no messages at all."""
        conn = MagicMock()
        mock_get_conn.return_value.__enter__ = MagicMock(return_value=conn)
        mock_get_conn.return_value.__exit__ = MagicMock(return_value=False)
        conn.execute.return_value.fetchone.return_value = (0,)

        check_message_rate(_USER_ID)

    @patch("app.chat.rate_limiter.get_connection")
    def test_db_error_fails_open(self, mock_get_conn: MagicMock):
        """When the DB is unreachable, the check passes (fail-open)."""
        mock_get_conn.side_effect = Exception("DB down")

        # Should NOT raise -- fail-open semantics
        check_message_rate(_USER_ID)

    @patch("app.chat.rate_limiter.get_connection")
    def test_error_message_contains_estonian(self, mock_get_conn: MagicMock):
        """The exception message is in Estonian."""
        conn = MagicMock()
        mock_get_conn.return_value.__enter__ = MagicMock(return_value=conn)
        mock_get_conn.return_value.__exit__ = MagicMock(return_value=False)
        conn.execute.return_value.fetchone.return_value = (_MAX_MESSAGES_PER_HOUR,)

        with pytest.raises(RateLimitExceededError, match="sonumit"):
            check_message_rate(_USER_ID)


# ---------------------------------------------------------------------------
# check_org_cost_budget
# ---------------------------------------------------------------------------


class TestCheckOrgCostBudget:
    @patch("app.chat.rate_limiter.get_connection")
    def test_under_budget_passes(self, mock_get_conn: MagicMock):
        """No exception when monthly cost is below the cap."""
        conn = MagicMock()
        mock_get_conn.return_value.__enter__ = MagicMock(return_value=conn)
        mock_get_conn.return_value.__exit__ = MagicMock(return_value=False)
        conn.execute.return_value.fetchone.return_value = (10.0,)

        check_org_cost_budget(_ORG_ID)

    @patch("app.chat.rate_limiter.get_connection")
    def test_at_budget_raises(self, mock_get_conn: MagicMock):
        """CostBudgetExceededError when cost equals the cap."""
        conn = MagicMock()
        mock_get_conn.return_value.__enter__ = MagicMock(return_value=conn)
        mock_get_conn.return_value.__exit__ = MagicMock(return_value=False)
        conn.execute.return_value.fetchone.return_value = (_MAX_MONTHLY_COST_USD,)

        with pytest.raises(CostBudgetExceededError):
            check_org_cost_budget(_ORG_ID)

    @patch("app.chat.rate_limiter.get_connection")
    def test_over_budget_raises(self, mock_get_conn: MagicMock):
        """CostBudgetExceededError when cost exceeds the cap."""
        conn = MagicMock()
        mock_get_conn.return_value.__enter__ = MagicMock(return_value=conn)
        mock_get_conn.return_value.__exit__ = MagicMock(return_value=False)
        conn.execute.return_value.fetchone.return_value = (_MAX_MONTHLY_COST_USD + 100.0,)

        with pytest.raises(CostBudgetExceededError):
            check_org_cost_budget(_ORG_ID)

    @patch("app.chat.rate_limiter.get_connection")
    def test_zero_cost_passes(self, mock_get_conn: MagicMock):
        """No exception when no cost at all."""
        conn = MagicMock()
        mock_get_conn.return_value.__enter__ = MagicMock(return_value=conn)
        mock_get_conn.return_value.__exit__ = MagicMock(return_value=False)
        conn.execute.return_value.fetchone.return_value = (0.0,)

        check_org_cost_budget(_ORG_ID)

    @patch("app.chat.rate_limiter.get_connection")
    def test_db_error_fails_open(self, mock_get_conn: MagicMock):
        """When the DB is unreachable, the check passes (fail-open)."""
        mock_get_conn.side_effect = Exception("DB down")

        # Should NOT raise -- fail-open semantics
        check_org_cost_budget(_ORG_ID)

    @patch("app.chat.rate_limiter.get_connection")
    def test_error_message_contains_usd(self, mock_get_conn: MagicMock):
        """The exception message mentions the budget in USD."""
        conn = MagicMock()
        mock_get_conn.return_value.__enter__ = MagicMock(return_value=conn)
        mock_get_conn.return_value.__exit__ = MagicMock(return_value=False)
        conn.execute.return_value.fetchone.return_value = (_MAX_MONTHLY_COST_USD,)

        with pytest.raises(CostBudgetExceededError, match="USD"):
            check_org_cost_budget(_ORG_ID)
