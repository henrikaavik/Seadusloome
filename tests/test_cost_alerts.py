# pyright: reportArgumentType=false
"""Tests for cost-alert dedupe + the one-time budget-exhausted alert (#861-B).

Covers:
- :func:`app.notifications.wire.notify_cost_alert` deduped to one per
  org/calendar-day (the budget check fires on every LLM turn).
- :func:`app.notifications.wire.notify_cost_exhausted` — a distinct
  100% alert, also deduped per org/day and independent of the 80% one.
- **Atomicity (#882 P2 fix):** the dedupe check + the fan-out inserts run
  in ONE transaction on ONE connection, guarded by a transaction-scoped
  ``pg_advisory_xact_lock`` keyed to ``cost_alert:<org>:<type>:<day>`` —
  so two concurrent budget checks can't both read "not sent" and both
  fan out.
- :func:`app.chat.rate_limiter.check_org_cost_budget` wiring: it fires
  the 80% alert in the [80%, 100%) band and the exhausted alert at >=100%
  (and still raises).
- :func:`app.notifications.notify.notify` ``conn=`` parameter: inserts on
  the caller's connection without committing or pushing (so the insert can
  join an advisory-locked transaction); the no-conn path is unchanged.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from app.chat.rate_limiter import (
    _MAX_MONTHLY_COST_USD,
    CostBudgetExceededError,
    check_org_cost_budget,
)
from app.notifications.models import Notification
from app.notifications.notify import notify
from app.notifications.wire import notify_cost_alert, notify_cost_exhausted

_ORG_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")
_DAY = "2026-06-11"


class _FakeCursor:
    def __init__(self, *, one: Any = None, rows: list[Any] | None = None):
        self._one = one
        self._rows = rows or []

    def fetchone(self) -> Any:
        return self._one

    def fetchall(self) -> list[Any]:
        return self._rows


class _FakeConn:
    """A connection that dispatches ``execute`` by SQL content.

    The atomic fan-out (``_fan_out_cost_alert``) issues, in order:
      1. ``SET LOCAL lock_timeout``           -> no result
      2. ``SELECT to_char(now(), 'YYYY-MM-DD')`` -> the day string
      3. ``SELECT pg_advisory_xact_lock(...)``   -> the advisory lock
      4. dedupe ``SELECT 1 FROM notifications``  -> one row or None
      5. ``SELECT id FROM users``                -> admin rows
      6. one INSERT per admin (via notify(conn=conn))

    Driving by SQL substring (rather than a positional ``side_effect``
    list) keeps the tests robust to the exact statement count and lets us
    assert on the lock-key SQL + prove the same connection carried both
    the dedupe and the inserts.
    """

    def __init__(
        self,
        *,
        already_sent: bool = False,
        admins: list[uuid.UUID] | None = None,
        dedupe_raises: bool = False,
        lock_raises_sqlstate: str | None = None,
    ):
        self._already_sent = already_sent
        self._admins = admins or []
        self._dedupe_raises = dedupe_raises
        self._lock_raises_sqlstate = lock_raises_sqlstate
        self.calls: list[tuple[str, tuple]] = []
        self.commit_count = 0
        self.insert_params: list[tuple] = []

    def execute(self, sql: str, params: tuple = ()) -> _FakeCursor:  # noqa: D401
        self.calls.append((sql, params))
        if "lock_timeout" in sql:
            return _FakeCursor()
        if "to_char" in sql:
            return _FakeCursor(one=(_DAY,))
        if "pg_advisory_xact_lock" in sql:
            if self._lock_raises_sqlstate is not None:
                exc = RuntimeError("lock not available")
                exc.sqlstate = self._lock_raises_sqlstate  # type: ignore[attr-defined]
                raise exc
            return _FakeCursor(one=(True,))
        if "FROM notifications" in sql:
            if self._dedupe_raises:
                raise RuntimeError("db hiccup")
            return _FakeCursor(one=(1,) if self._already_sent else None)
        if "FROM users" in sql:
            return _FakeCursor(rows=[(a,) for a in self._admins])
        if sql.strip().upper().startswith("INSERT"):
            self.insert_params.append(params)
            # create_notification's RETURNING row order is:
            # id, user_id, type, title, body, link, metadata, read, created_at.
            # Synthesise one from the INSERT params so the real
            # create_notification() builds a Notification and notify()
            # returns it (driving the post-commit push).
            user_id, ntype, title, body, link, _metadata = params
            row = (
                str(uuid.uuid4()),
                user_id,
                ntype,
                title,
                body,
                link,
                None,
                False,
                datetime.now(UTC),
            )
            return _FakeCursor(one=row)
        return _FakeCursor()

    def commit(self) -> None:
        self.commit_count += 1


class _ConnectCM:
    def __init__(self, conn: Any):
        self.conn = conn

    def __enter__(self) -> Any:
        return self.conn

    def __exit__(self, *_: Any) -> bool:
        return False


def _lock_sql_call(conn: _FakeConn) -> tuple[str, tuple]:
    for sql, params in conn.calls:
        if "pg_advisory_xact_lock" in sql:
            return sql, params
    raise AssertionError("no pg_advisory_xact_lock call was issued")


# ---------------------------------------------------------------------------
# Atomicity / advisory lock (#882 P2)
# ---------------------------------------------------------------------------


class TestCostAlertAtomicity:
    @patch("app.notifications.wire.push_notification")
    @patch("app.db.get_connection")
    def test_dedupe_and_inserts_share_one_connection_under_lock(self, mock_connect, mock_push):
        """The dedupe SELECT and every admin INSERT must run on the SAME
        connection, after a SET LOCAL lock_timeout and the advisory lock,
        and the transaction must be committed exactly once."""
        admin1, admin2 = uuid.uuid4(), uuid.uuid4()
        conn = _FakeConn(already_sent=False, admins=[admin1, admin2])
        mock_connect.return_value = _ConnectCM(conn)

        notify_cost_alert(_ORG_ID, 42.0, 50.0)

        kinds = [
            "lock_timeout"
            if "lock_timeout" in s
            else "to_char"
            if "to_char" in s
            else "lock"
            if "pg_advisory_xact_lock" in s
            else "dedupe"
            if "FROM notifications" in s
            else "admins"
            if "FROM users" in s
            else "insert"
            if s.strip().upper().startswith("INSERT")
            else "other"
            for s, _ in conn.calls
        ]
        # Order: timeout -> day -> lock -> dedupe -> admins -> 2 inserts.
        assert kinds == [
            "lock_timeout",
            "to_char",
            "lock",
            "dedupe",
            "admins",
            "insert",
            "insert",
        ]
        # Single transaction committed once; pushes happen post-commit.
        assert conn.commit_count == 1
        assert mock_push.call_count == 2

    @patch("app.notifications.wire.push_notification")
    @patch("app.db.get_connection")
    def test_advisory_lock_key_is_org_type_day_scoped(self, mock_connect, mock_push):
        """The lock key must be cost_alert:<org>:<type>:<day> so two
        concurrent checks for the same org/type/day serialise (and a
        different type/day does not)."""
        conn = _FakeConn(already_sent=False, admins=[uuid.uuid4()])
        mock_connect.return_value = _ConnectCM(conn)

        notify_cost_alert(_ORG_ID, 42.0, 50.0)

        sql, params = _lock_sql_call(conn)
        assert "pg_advisory_xact_lock(hashtextextended(%s, 0))" in sql
        assert params == (f"cost_alert:{_ORG_ID}:cost_alert:{_DAY}",)

    @patch("app.notifications.wire.push_notification")
    @patch("app.db.get_connection")
    def test_lock_timeout_is_bounded_before_acquire(self, mock_connect, mock_push):
        """SET LOCAL lock_timeout must be issued before the lock acquire so
        a stuck sibling can't hang the turn (mirrors check_org_cost_budget)."""
        conn = _FakeConn(already_sent=False, admins=[])
        mock_connect.return_value = _ConnectCM(conn)

        notify_cost_alert(_ORG_ID, 42.0, 50.0)

        sqls = [s for s, _ in conn.calls]
        timeout_idx = next(i for i, s in enumerate(sqls) if "lock_timeout" in s)
        lock_idx = next(i for i, s in enumerate(sqls) if "pg_advisory_xact_lock" in s)
        assert timeout_idx < lock_idx

    @patch("app.notifications.wire.notify")
    @patch("app.notifications.wire.push_notification")
    @patch("app.db.get_connection")
    def test_lock_busy_skips_silently_no_fanout(self, mock_connect, mock_push, mock_notify):
        """If the advisory-lock acquire times out (LockNotAvailable, SQLSTATE
        55P03) a sibling holds it and will send the alert — skip silently,
        no inserts, no commit, no push."""
        conn = _FakeConn(lock_raises_sqlstate="55P03", admins=[uuid.uuid4()])
        mock_connect.return_value = _ConnectCM(conn)

        notify_cost_alert(_ORG_ID, 42.0, 50.0)

        mock_notify.assert_not_called()
        mock_push.assert_not_called()
        assert conn.commit_count == 0


# ---------------------------------------------------------------------------
# notify_cost_alert dedupe
# ---------------------------------------------------------------------------


class TestNotifyCostAlertDedupe:
    @patch("app.notifications.wire.push_notification")
    @patch("app.db.get_connection")
    def test_sends_when_no_prior_alert_today(self, mock_connect, mock_push):
        """No prior cost_alert today -> dedupe returns None -> fan out +
        one insert per admin + commit + push."""
        admin1 = uuid.uuid4()
        conn = _FakeConn(already_sent=False, admins=[admin1])
        mock_connect.return_value = _ConnectCM(conn)

        notify_cost_alert(_ORG_ID, 42.0, 50.0)

        # One INSERT for the single admin; committed; pushed.
        assert len(conn.insert_params) == 1
        assert conn.commit_count == 1
        assert mock_push.call_count == 1

    @patch("app.notifications.wire.notify")
    @patch("app.db.get_connection")
    def test_suppressed_when_alert_already_sent_today(self, mock_connect, mock_notify):
        """A cost_alert already exists today -> no fan-out, no admin lookup,
        no insert, no commit."""
        conn = _FakeConn(already_sent=True, admins=[uuid.uuid4()])
        mock_connect.return_value = _ConnectCM(conn)

        notify_cost_alert(_ORG_ID, 42.0, 50.0)

        mock_notify.assert_not_called()
        # The admin lookup never ran (suppressed right after the dedupe).
        assert not any("FROM users" in s for s, _ in conn.calls)
        assert conn.commit_count == 0
        # Dedupe SELECT shape (server-day window, org-scoped, typed).
        dedupe_sql = next(s for s, _ in conn.calls if "FROM notifications" in s)
        assert "type = %s" in dedupe_sql
        assert "metadata->>'org_id'" in dedupe_sql
        assert "date_trunc('day', now())" in dedupe_sql

    @patch("app.notifications.wire.push_notification")
    @patch("app.db.get_connection")
    def test_dedupe_query_uses_cost_alert_type(self, mock_connect, mock_push):
        """The dedupe check must key on the 'cost_alert' type specifically."""
        conn = _FakeConn(already_sent=False, admins=[])
        mock_connect.return_value = _ConnectCM(conn)

        notify_cost_alert(_ORG_ID, 42.0, 50.0)

        dedupe_params = next(p for s, p in conn.calls if "FROM notifications" in s)
        assert dedupe_params == ("cost_alert", str(_ORG_ID))

    @patch("app.notifications.wire.push_notification")
    @patch("app.db.get_connection")
    def test_dedupe_db_error_falls_open_to_send(self, mock_connect, mock_push):
        """A failing dedupe query must NOT suppress the alert (degrade to
        send): _cost_alert_already_sent_today returns False on error, so the
        fan-out proceeds."""
        admin1 = uuid.uuid4()
        conn = _FakeConn(dedupe_raises=True, admins=[admin1])
        mock_connect.return_value = _ConnectCM(conn)

        notify_cost_alert(_ORG_ID, 42.0, 50.0)

        assert len(conn.insert_params) == 1
        assert conn.commit_count == 1


# ---------------------------------------------------------------------------
# notify_cost_exhausted (100%)
# ---------------------------------------------------------------------------


class TestNotifyCostExhausted:
    @patch("app.notifications.wire.notify")
    @patch("app.notifications.wire.push_notification")
    @patch("app.db.get_connection")
    def test_sends_exhausted_alert_to_admins(self, mock_connect, mock_push, mock_notify):
        admin1 = uuid.uuid4()
        # Make notify(conn=conn) report a created row so push fires.
        mock_notify.return_value = MagicMock(spec=Notification)
        conn = _FakeConn(already_sent=False, admins=[admin1])
        mock_connect.return_value = _ConnectCM(conn)

        notify_cost_exhausted(_ORG_ID, 50.0, 50.0)

        mock_notify.assert_called_once()
        call_kwargs = mock_notify.call_args[1]
        assert call_kwargs["user_id"] == admin1
        assert call_kwargs["type"] == "cost_exhausted"
        # Insert joined this transaction.
        assert call_kwargs["conn"] is conn
        # Estonian "budget is full" copy.
        assert "täis" in call_kwargs["title"]
        assert call_kwargs["metadata"]["pct"] == 100  # noqa: PLR2004
        assert conn.commit_count == 1

    @patch("app.notifications.wire.push_notification")
    @patch("app.db.get_connection")
    def test_deduped_independently_of_cost_alert(self, mock_connect, mock_push):
        """The exhausted dedupe keys on 'cost_exhausted', not 'cost_alert',
        so an earlier 80% alert today does not suppress the 100% alert. The
        advisory-lock key is type-scoped too."""
        conn = _FakeConn(already_sent=False, admins=[])
        mock_connect.return_value = _ConnectCM(conn)

        notify_cost_exhausted(_ORG_ID, 55.0, 50.0)

        dedupe_params = next(p for s, p in conn.calls if "FROM notifications" in s)
        assert dedupe_params == ("cost_exhausted", str(_ORG_ID))
        _, lock_params = _lock_sql_call(conn)
        assert lock_params == (f"cost_alert:{_ORG_ID}:cost_exhausted:{_DAY}",)

    @patch("app.notifications.wire.notify")
    @patch("app.db.get_connection")
    def test_suppressed_when_exhausted_already_sent_today(self, mock_connect, mock_notify):
        conn = _FakeConn(already_sent=True, admins=[uuid.uuid4()])
        mock_connect.return_value = _ConnectCM(conn)

        notify_cost_exhausted(_ORG_ID, 60.0, 50.0)

        mock_notify.assert_not_called()
        assert conn.commit_count == 0


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


# ---------------------------------------------------------------------------
# notify(conn=...) — the in-transaction insert path that makes the fan-out
# atomic (#882 P2). Inserts on the caller's connection; no commit, no push.
# ---------------------------------------------------------------------------


class TestNotifyConnParameter:
    def test_conn_path_inserts_without_commit_or_push(self):
        """notify(conn=conn) must insert on the supplied connection and
        NOT commit and NOT push — the caller owns the transaction."""
        conn = MagicMock()
        # create_notification does conn.execute(...).fetchone().
        row = (
            str(uuid.uuid4()),
            str(uuid.uuid4()),
            "cost_alert",
            "t",
            "b",
            "/admin/costs",
            None,
            False,
            datetime.now(UTC),
        )
        conn.execute.return_value.fetchone.return_value = row

        with patch("app.notifications.websocket.push_to_user") as mock_push_to_user:
            result = notify(
                user_id=uuid.uuid4(),
                type="cost_alert",
                title="t",
                body="b",
                link="/admin/costs",
                conn=conn,
            )

        assert isinstance(result, Notification)
        # The insert ran on the caller's connection...
        assert conn.execute.called
        # ...but notify did NOT commit (caller owns the tx) and did NOT push.
        conn.commit.assert_not_called()
        mock_push_to_user.assert_not_called()

    def test_conn_path_returns_none_on_failed_insert(self):
        """A failed insert (create_notification returns None) must surface
        as None so the caller doesn't try to push a non-existent row."""
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = None  # no RETURNING row

        result = notify(
            user_id=uuid.uuid4(),
            type="cost_alert",
            title="t",
            conn=conn,
        )

        assert result is None
        conn.commit.assert_not_called()

    @patch("app.notifications.notify.get_connection")
    def test_no_conn_path_unchanged_commits_and_pushes(self, mock_get_conn):
        """The default (conn=None) path is unchanged: own connection,
        commit, then push."""
        own = MagicMock()
        row = (
            str(uuid.uuid4()),
            str(uuid.uuid4()),
            "cost_alert",
            "t",
            None,
            None,
            None,
            False,
            datetime.now(UTC),
        )
        own.execute.return_value.fetchone.return_value = row
        mock_get_conn.return_value.__enter__ = MagicMock(return_value=own)
        mock_get_conn.return_value.__exit__ = MagicMock(return_value=False)

        with patch("app.notifications.websocket.push_to_user") as mock_push_to_user:
            result = notify(user_id=uuid.uuid4(), type="cost_alert", title="t")

        assert isinstance(result, Notification)
        own.commit.assert_called_once()
        mock_push_to_user.assert_called_once()
