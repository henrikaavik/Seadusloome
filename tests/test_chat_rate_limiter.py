# pyright: reportArgumentType=false
"""Tests for ``app.chat.rate_limiter``.

Covers:
- :func:`check_message_rate` exceeding and passing
- :func:`check_org_cost_budget` exceeding and passing
- Graceful degradation when the DB is unavailable (fail-open)
- :func:`check_org_cost_budget` with an injected ``conn`` (FOR UPDATE path)
- :func:`get_user_quota` snapshot shape
- :func:`seconds_until_hourly_reset` window math
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest  # noqa: I001

from app.chat.rate_limiter import (
    _MAX_MESSAGES_PER_HOUR,
    _MAX_MONTHLY_COST_USD,
    CostBudgetExceededError,
    RateLimitExceededError,
    UserQuota,
    check_message_rate,
    check_org_cost_budget,
    get_user_quota,
    seconds_until_hourly_reset,
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


# ---------------------------------------------------------------------------
# check_org_cost_budget — transactional (FOR UPDATE / advisory lock) path
# ---------------------------------------------------------------------------


class TestCheckOrgCostBudgetWithConn:
    def test_advisory_lock_is_taken_when_conn_is_provided(self):
        """When a caller-supplied conn is passed, we must take the advisory lock
        before reading the running total. This serialises concurrent checks."""
        conn = MagicMock()
        # Two execute() calls: lock + select. Chain them via side_effect.
        lock_result = MagicMock()
        select_result = MagicMock()
        select_result.fetchone.return_value = (10.0,)
        conn.execute.side_effect = [lock_result, select_result]

        check_org_cost_budget(_ORG_ID, conn=conn)

        # First call must be the advisory lock.
        first_call_sql = conn.execute.call_args_list[0].args[0]
        assert "pg_advisory_xact_lock" in first_call_sql
        # Second call reads the running total.
        second_call_sql = conn.execute.call_args_list[1].args[0]
        assert "SUM(cost_usd)" in second_call_sql

    def test_second_check_sees_accumulated_cost(self):
        """Proxy for FOR UPDATE correctness: two sequential checks against the
        same budget inside one transaction should see the updated total on the
        second call. We simulate an insert happening between them by returning
        a higher SUM on the second SELECT."""
        conn = MagicMock()

        lock1 = MagicMock()
        select1 = MagicMock()
        select1.fetchone.return_value = (10.0,)  # Under cap — passes.
        lock2 = MagicMock()
        select2 = MagicMock()
        # Simulates another request that successfully charged between the two
        # checks, pushing us over the monthly cap.
        select2.fetchone.return_value = (_MAX_MONTHLY_COST_USD + 5.0,)
        conn.execute.side_effect = [lock1, select1, lock2, select2]

        # First call: still under budget.
        check_org_cost_budget(_ORG_ID, conn=conn)

        # Second call: over budget, should raise.
        with pytest.raises(CostBudgetExceededError):
            check_org_cost_budget(_ORG_ID, conn=conn)

    def test_legacy_call_without_conn_still_works(self):
        """Backward compatibility: calling without conn opens its own
        short-lived connection and does not take an advisory lock."""
        with patch("app.chat.rate_limiter.get_connection") as mock_get_conn:
            conn = MagicMock()
            mock_get_conn.return_value.__enter__ = MagicMock(return_value=conn)
            mock_get_conn.return_value.__exit__ = MagicMock(return_value=False)
            conn.execute.return_value.fetchone.return_value = (5.0,)

            check_org_cost_budget(_ORG_ID)

            # No advisory lock issued on the legacy path.
            executed_sqls = [c.args[0] for c in conn.execute.call_args_list]
            assert not any("pg_advisory_xact_lock" in s for s in executed_sqls)


# ---------------------------------------------------------------------------
# get_user_quota
# ---------------------------------------------------------------------------


class TestGetUserQuota:
    @patch("app.chat.rate_limiter.get_connection")
    def test_returns_expected_values_from_seeded_data(self, mock_get_conn: MagicMock):
        """Seeded data (7 messages this hour, $12.34 this month) must round-trip
        through the dataclass with the right types."""
        conn = MagicMock()
        mock_get_conn.return_value.__enter__ = MagicMock(return_value=conn)
        mock_get_conn.return_value.__exit__ = MagicMock(return_value=False)

        message_cursor = MagicMock()
        message_cursor.fetchone.return_value = (7,)
        cost_cursor = MagicMock()
        cost_cursor.fetchone.return_value = (Decimal("12.34"),)
        conn.execute.side_effect = [message_cursor, cost_cursor]

        quota = get_user_quota(_USER_ID, _ORG_ID)

        assert isinstance(quota, UserQuota)
        assert quota.messages_this_hour == 7
        assert quota.message_limit_per_hour == _MAX_MESSAGES_PER_HOUR
        assert quota.cost_this_month_usd == Decimal("12.34")
        assert quota.cost_limit_per_month_usd == Decimal(str(_MAX_MONTHLY_COST_USD))
        # 80% of limit, rounded to 2 decimals.
        expected_alert = (Decimal(str(_MAX_MONTHLY_COST_USD)) * Decimal("0.8")).quantize(
            Decimal("0.01")
        )
        assert quota.cost_alert_threshold_usd == expected_alert

    @patch("app.chat.rate_limiter.get_connection")
    def test_db_error_returns_zero_usage(self, mock_get_conn: MagicMock):
        """DB failure must not explode the usage dashboard — return zeros."""
        mock_get_conn.side_effect = Exception("DB down")

        quota = get_user_quota(_USER_ID, _ORG_ID)

        assert quota.messages_this_hour == 0
        assert quota.cost_this_month_usd == Decimal("0")
        # Limits still reflect env config.
        assert quota.message_limit_per_hour == _MAX_MESSAGES_PER_HOUR


# ---------------------------------------------------------------------------
# seconds_until_hourly_reset
# ---------------------------------------------------------------------------


class TestSecondsUntilHourlyReset:
    @patch("app.chat.rate_limiter.get_connection")
    def test_oldest_message_55_minutes_ago_returns_about_300s(self, mock_get_conn: MagicMock):
        """55 min old -> 3600 - 3300 = 300 seconds remaining."""
        conn = MagicMock()
        mock_get_conn.return_value.__enter__ = MagicMock(return_value=conn)
        mock_get_conn.return_value.__exit__ = MagicMock(return_value=False)
        # EPOCH seconds since the oldest message = 55 min = 3300 s.
        conn.execute.return_value.fetchone.return_value = (3300.0,)

        remaining = seconds_until_hourly_reset(_USER_ID)

        assert 295 <= remaining <= 305

    @patch("app.chat.rate_limiter.get_connection")
    def test_no_messages_in_window_returns_zero(self, mock_get_conn: MagicMock):
        """Empty window -> no wait time."""
        conn = MagicMock()
        mock_get_conn.return_value.__enter__ = MagicMock(return_value=conn)
        mock_get_conn.return_value.__exit__ = MagicMock(return_value=False)
        conn.execute.return_value.fetchone.return_value = (None,)

        assert seconds_until_hourly_reset(_USER_ID) == 0

    @patch("app.chat.rate_limiter.get_connection")
    def test_very_recent_message_clamped_to_3600(self, mock_get_conn: MagicMock):
        """A message 0.1 s old -> 3599.9 s remaining, clamped to int 3599."""
        conn = MagicMock()
        mock_get_conn.return_value.__enter__ = MagicMock(return_value=conn)
        mock_get_conn.return_value.__exit__ = MagicMock(return_value=False)
        conn.execute.return_value.fetchone.return_value = (0.1,)

        remaining = seconds_until_hourly_reset(_USER_ID)

        assert 3590 <= remaining <= 3600

    @patch("app.chat.rate_limiter.get_connection")
    def test_db_error_returns_zero(self, mock_get_conn: MagicMock):
        """DB error -> fail-open with 0 (caller can retry immediately)."""
        mock_get_conn.side_effect = Exception("DB down")

        assert seconds_until_hourly_reset(_USER_ID) == 0
