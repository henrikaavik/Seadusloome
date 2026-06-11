# pyright: reportArgumentType=false
"""Tests for cost-alert dedupe + the one-time budget-exhausted alert (#861-B).

Covers:
- :func:`app.notifications.wire.notify_cost_alert` deduped to one per
  org/calendar-day (the budget check fires on every LLM turn).
- :func:`app.notifications.wire.notify_cost_exhausted` — a distinct
  100% alert, also deduped per org/day and independent of the 80% one.
- :func:`app.chat.rate_limiter.check_org_cost_budget` wiring: it fires
  the 80% alert in the [80%, 100%) band and the exhausted alert at >=100%
  (and still raises).
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from app.chat.rate_limiter import (
    _MAX_MONTHLY_COST_USD,
    CostBudgetExceededError,
    check_org_cost_budget,
)
from app.notifications.wire import notify_cost_alert, notify_cost_exhausted

_ORG_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")


class _ConnectCM:
    def __init__(self, conn: MagicMock):
        self.conn = conn

    def __enter__(self) -> MagicMock:
        return self.conn

    def __exit__(self, *_: Any) -> bool:
        return False


# ---------------------------------------------------------------------------
# notify_cost_alert dedupe
# ---------------------------------------------------------------------------


class TestNotifyCostAlertDedupe:
    @patch("app.notifications.wire.notify")
    @patch("app.db.get_connection")
    def test_sends_when_no_prior_alert_today(self, mock_connect, mock_notify):
        """No prior cost_alert today -> dedupe query returns None -> fan out."""
        admin1 = uuid.uuid4()
        conn = MagicMock()
        # 1st execute: dedupe check (no row). 2nd: admin lookup.
        dedupe_cursor = MagicMock()
        dedupe_cursor.fetchone.return_value = None
        admins_cursor = MagicMock()
        admins_cursor.fetchall.return_value = [(admin1,)]
        conn.execute.side_effect = [dedupe_cursor, admins_cursor]
        mock_connect.return_value = _ConnectCM(conn)

        notify_cost_alert(_ORG_ID, 42.0, 50.0)

        mock_notify.assert_called_once()
        assert mock_notify.call_args[1]["type"] == "cost_alert"
        assert "84%" in mock_notify.call_args[1]["title"]

    @patch("app.notifications.wire.notify")
    @patch("app.db.get_connection")
    def test_suppressed_when_alert_already_sent_today(self, mock_connect, mock_notify):
        """A cost_alert already exists today -> no fan-out, no admin lookup."""
        conn = MagicMock()
        dedupe_cursor = MagicMock()
        dedupe_cursor.fetchone.return_value = (1,)  # a row exists
        conn.execute.return_value = dedupe_cursor
        mock_connect.return_value = _ConnectCM(conn)

        notify_cost_alert(_ORG_ID, 42.0, 50.0)

        mock_notify.assert_not_called()
        # Only the dedupe SELECT ran — the admin lookup was skipped.
        assert conn.execute.call_count == 1
        sql = conn.execute.call_args[0][0]
        assert "type = %s" in sql
        assert "metadata->>'org_id'" in sql
        assert "date_trunc('day', now())" in sql

    @patch("app.notifications.wire.notify")
    @patch("app.db.get_connection")
    def test_dedupe_query_uses_cost_alert_type(self, mock_connect, mock_notify):
        """The dedupe check must key on the 'cost_alert' type specifically."""
        conn = MagicMock()
        dedupe_cursor = MagicMock()
        dedupe_cursor.fetchone.return_value = None
        admins_cursor = MagicMock()
        admins_cursor.fetchall.return_value = []
        conn.execute.side_effect = [dedupe_cursor, admins_cursor]
        mock_connect.return_value = _ConnectCM(conn)

        notify_cost_alert(_ORG_ID, 42.0, 50.0)

        first_params = conn.execute.call_args_list[0][0][1]
        assert first_params == ("cost_alert", str(_ORG_ID))

    @patch("app.notifications.wire.notify")
    @patch("app.db.get_connection")
    def test_dedupe_db_error_falls_open_to_send(self, mock_connect, mock_notify):
        """A failing dedupe query must NOT suppress the alert (degrade to send)."""
        admin1 = uuid.uuid4()
        conn = MagicMock()
        dedupe_cursor = MagicMock()
        dedupe_cursor.fetchone.side_effect = Exception("db hiccup")
        admins_cursor = MagicMock()
        admins_cursor.fetchall.return_value = [(admin1,)]
        conn.execute.side_effect = [dedupe_cursor, admins_cursor]
        mock_connect.return_value = _ConnectCM(conn)

        notify_cost_alert(_ORG_ID, 42.0, 50.0)

        mock_notify.assert_called_once()


# ---------------------------------------------------------------------------
# notify_cost_exhausted (100%)
# ---------------------------------------------------------------------------


class TestNotifyCostExhausted:
    @patch("app.notifications.wire.notify")
    @patch("app.db.get_connection")
    def test_sends_exhausted_alert_to_admins(self, mock_connect, mock_notify):
        admin1 = uuid.uuid4()
        conn = MagicMock()
        dedupe_cursor = MagicMock()
        dedupe_cursor.fetchone.return_value = None
        admins_cursor = MagicMock()
        admins_cursor.fetchall.return_value = [(admin1,)]
        conn.execute.side_effect = [dedupe_cursor, admins_cursor]
        mock_connect.return_value = _ConnectCM(conn)

        notify_cost_exhausted(_ORG_ID, 50.0, 50.0)

        mock_notify.assert_called_once()
        call_kwargs = mock_notify.call_args[1]
        assert call_kwargs["user_id"] == admin1
        assert call_kwargs["type"] == "cost_exhausted"
        # Estonian "budget is full" copy.
        assert "täis" in call_kwargs["title"]
        assert call_kwargs["metadata"]["pct"] == 100  # noqa: PLR2004

    @patch("app.notifications.wire.notify")
    @patch("app.db.get_connection")
    def test_deduped_independently_of_cost_alert(self, mock_connect, mock_notify):
        """The exhausted dedupe keys on 'cost_exhausted', not 'cost_alert',
        so an earlier 80% alert today does not suppress the 100% alert."""
        conn = MagicMock()
        dedupe_cursor = MagicMock()
        dedupe_cursor.fetchone.return_value = None
        admins_cursor = MagicMock()
        admins_cursor.fetchall.return_value = []
        conn.execute.side_effect = [dedupe_cursor, admins_cursor]
        mock_connect.return_value = _ConnectCM(conn)

        notify_cost_exhausted(_ORG_ID, 55.0, 50.0)

        first_params = conn.execute.call_args_list[0][0][1]
        assert first_params == ("cost_exhausted", str(_ORG_ID))

    @patch("app.notifications.wire.notify")
    @patch("app.db.get_connection")
    def test_suppressed_when_exhausted_already_sent_today(self, mock_connect, mock_notify):
        conn = MagicMock()
        dedupe_cursor = MagicMock()
        dedupe_cursor.fetchone.return_value = (1,)
        conn.execute.return_value = dedupe_cursor
        mock_connect.return_value = _ConnectCM(conn)

        notify_cost_exhausted(_ORG_ID, 60.0, 50.0)

        mock_notify.assert_not_called()
        assert conn.execute.call_count == 1


# ---------------------------------------------------------------------------
# check_org_cost_budget wiring -> which alert fires in which band
# ---------------------------------------------------------------------------


class TestCheckBudgetAlertWiring:
    @patch("app.chat.rate_limiter.get_connection")
    def test_80pct_band_fires_cost_alert_not_exhausted(self, mock_get_conn):
        conn = MagicMock()
        mock_get_conn.return_value.__enter__ = MagicMock(return_value=conn)
        mock_get_conn.return_value.__exit__ = MagicMock(return_value=False)
        # 85% of cap.
        conn.execute.return_value.fetchone.return_value = (_MAX_MONTHLY_COST_USD * 0.85,)

        with (
            patch("app.notifications.wire.notify_cost_alert") as mock_alert,
            patch("app.notifications.wire.notify_cost_exhausted") as mock_exhausted,
        ):
            check_org_cost_budget(_ORG_ID)

        mock_alert.assert_called_once()
        mock_exhausted.assert_not_called()

    @patch("app.chat.rate_limiter.get_connection")
    def test_at_cap_fires_exhausted_and_raises(self, mock_get_conn):
        conn = MagicMock()
        mock_get_conn.return_value.__enter__ = MagicMock(return_value=conn)
        mock_get_conn.return_value.__exit__ = MagicMock(return_value=False)
        conn.execute.return_value.fetchone.return_value = (_MAX_MONTHLY_COST_USD,)

        with (
            patch("app.notifications.wire.notify_cost_alert") as mock_alert,
            patch("app.notifications.wire.notify_cost_exhausted") as mock_exhausted,
            pytest.raises(CostBudgetExceededError),
        ):
            check_org_cost_budget(_ORG_ID)

        # At exactly 100% the advisory 80% alert must NOT also fire.
        mock_alert.assert_not_called()
        mock_exhausted.assert_called_once()

    @patch("app.chat.rate_limiter.get_connection")
    def test_under_80pct_fires_neither(self, mock_get_conn):
        conn = MagicMock()
        mock_get_conn.return_value.__enter__ = MagicMock(return_value=conn)
        mock_get_conn.return_value.__exit__ = MagicMock(return_value=False)
        conn.execute.return_value.fetchone.return_value = (_MAX_MONTHLY_COST_USD * 0.5,)

        with (
            patch("app.notifications.wire.notify_cost_alert") as mock_alert,
            patch("app.notifications.wire.notify_cost_exhausted") as mock_exhausted,
        ):
            check_org_cost_budget(_ORG_ID)

        mock_alert.assert_not_called()
        mock_exhausted.assert_not_called()

    @patch("app.chat.rate_limiter.get_connection")
    def test_over_cap_fires_exhausted_not_alert(self, mock_get_conn):
        """Well over the cap (e.g. 150%) still fires only the exhausted alert."""
        conn = MagicMock()
        mock_get_conn.return_value.__enter__ = MagicMock(return_value=conn)
        mock_get_conn.return_value.__exit__ = MagicMock(return_value=False)
        conn.execute.return_value.fetchone.return_value = (_MAX_MONTHLY_COST_USD * 1.5,)

        with (
            patch("app.notifications.wire.notify_cost_alert") as mock_alert,
            patch("app.notifications.wire.notify_cost_exhausted") as mock_exhausted,
            pytest.raises(CostBudgetExceededError),
        ):
            check_org_cost_budget(_ORG_ID)

        mock_alert.assert_not_called()
        mock_exhausted.assert_called_once()
