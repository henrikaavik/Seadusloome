"""Per-user message rate limit and per-org cost cap.

Provides two guard functions that should be called at the top of
:meth:`ChatOrchestrator.handle_message` (and similar LLM entry points)
to enforce usage limits:

    - :func:`check_message_rate` — per-user sliding-window rate limit
    - :func:`check_org_cost_budget` — per-org monthly cost cap

Both functions query the DB directly and raise descriptive exceptions
when limits are exceeded. The caller is responsible for catching these
and sending appropriate error events to the client.

Configuration is via environment variables with sensible defaults:

    CHAT_MAX_MESSAGES_PER_HOUR  (default: 100)
    ORG_MAX_MONTHLY_COST_USD    (default: 50.0)

In addition, :func:`get_user_quota` and :func:`seconds_until_hourly_reset`
expose the same state to the upcoming ``/api/me/usage`` endpoint so the
frontend can display quota headroom and retry timers.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import UUID

from app.db import get_connection

if TYPE_CHECKING:
    import psycopg

logger = logging.getLogger(__name__)

# Backward-compatible aliases for tests that import these names.
# The actual check functions re-read from the environment each call so
# that config changes take effect without a process restart.
_MAX_MESSAGES_PER_HOUR = int(os.environ.get("CHAT_MAX_MESSAGES_PER_HOUR", "100"))
_MAX_MONTHLY_COST_USD = float(os.environ.get("ORG_MAX_MONTHLY_COST_USD", "50.0"))

# Alert when cost usage crosses this fraction of the monthly cap.
_COST_ALERT_FRACTION = 0.8


def _get_max_messages_per_hour() -> int:
    """Read the rate limit from the environment on each call."""
    return int(os.environ.get("CHAT_MAX_MESSAGES_PER_HOUR", "100"))


def _get_max_monthly_cost_usd() -> float:
    """Read the cost cap from the environment on each call."""
    return float(os.environ.get("ORG_MAX_MONTHLY_COST_USD", "50.0"))


class RateLimitExceededError(Exception):
    """Raised when a user exceeds their per-hour message limit."""


class CostBudgetExceededError(Exception):
    """Raised when an organisation exceeds its monthly LLM cost cap."""


@dataclass(frozen=True)
class UserQuota:
    """Snapshot of a user's current quota consumption.

    All currency values are :class:`~decimal.Decimal` to avoid float
    rounding errors when summing many small LLM charges. Message
    counts are plain integers.
    """

    messages_this_hour: int
    message_limit_per_hour: int
    cost_this_month_usd: Decimal
    cost_limit_per_month_usd: Decimal
    cost_alert_threshold_usd: Decimal  # 80% of limit


def check_message_rate(user_id: UUID | str) -> None:
    """Raise :class:`RateLimitExceeded` if the user has sent too many messages.

    Counts ``role='user'`` messages in the ``messages`` table that were
    created in the last hour. If the count exceeds
    ``CHAT_MAX_MESSAGES_PER_HOUR``, raises immediately.
    """
    max_messages = _get_max_messages_per_hour()
    try:
        with get_connection() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM messages "
                "WHERE conversation_id IN "
                "  (SELECT id FROM conversations WHERE user_id = %s) "
                "AND role = 'user' "
                "AND created_at > now() - interval '1 hour'",
                (str(user_id),),
            ).fetchone()
    except Exception:
        logger.exception("Failed to check message rate for user_id=%s", user_id)
        # Fail open: if the DB is down, don't block the user
        return

    count = row[0] if row else 0
    if count >= max_messages:
        raise RateLimitExceededError(
            f"Olete saatnud {count} sonumit viimase tunni jooksul. "
            f"Limiit on {max_messages} sonumit tunnis."
        )


def check_org_cost_budget(
    org_id: UUID | str,
    conn: psycopg.Connection | None = None,  # type: ignore[type-arg]
) -> None:
    """Raise :class:`CostBudgetExceeded` if the org's monthly LLM cost exceeds the cap.

    Sums ``cost_usd`` from the ``llm_usage`` table for the current
    calendar month. If the total meets or exceeds
    ``ORG_MAX_MONTHLY_COST_USD``, raises immediately.

    **TOCTOU race window and mitigation** — A naive implementation reads
    the running monthly total with a plain ``SELECT`` and returns. Two
    concurrent requests can both observe a total below the cap, both
    pass the check, and both proceed to spend against the budget,
    causing an unbounded overshoot as concurrency scales.

    To close that window, when this function is invoked with an active
    ``conn`` the check runs inside the caller's transaction and takes a
    transaction-scoped PostgreSQL advisory lock keyed to the org id:

        ``pg_advisory_xact_lock(hashtextextended('cost_budget:<org>', 0))``

    This serialises concurrent budget checks for a given org without
    requiring a dedicated lock row or new table. The lock is released
    automatically when the caller's transaction commits or rolls back.
    The caller is expected to insert the message (or otherwise record
    the work that will drive the next LLM charge) inside the same
    transaction, so the "read-then-act" sequence is atomic.

    When called without ``conn`` (the legacy shape), the function opens
    its own short-lived connection and performs the plain SELECT with
    no locking. This mode is intentionally kept for non-critical paths
    (status/usage display) where a momentary race is acceptable.
    """
    max_cost = _get_max_monthly_cost_usd()
    total_cost = 0.0

    try:
        if conn is not None:
            # Serialise concurrent budget checks for this org inside the
            # caller's transaction. The lock is released on commit/rollback.
            conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended('cost_budget:' || %s::text, 0))",
                (str(org_id),),
            )
            row = conn.execute(
                "SELECT COALESCE(SUM(cost_usd), 0) FROM llm_usage "
                "WHERE org_id = %s "
                "AND created_at >= date_trunc('month', now())",
                (str(org_id),),
            ).fetchone()
            total_cost = float(row[0]) if row else 0.0
        else:
            with get_connection() as own_conn:
                row = own_conn.execute(
                    "SELECT COALESCE(SUM(cost_usd), 0) FROM llm_usage "
                    "WHERE org_id = %s "
                    "AND created_at >= date_trunc('month', now())",
                    (str(org_id),),
                ).fetchone()
                total_cost = float(row[0]) if row else 0.0
    except Exception:
        logger.exception("Failed to check org cost budget for org_id=%s", org_id)
        # Fail open
        return

    # Notify org admins when cost hits 80% of the budget.
    if total_cost >= max_cost * _COST_ALERT_FRACTION and total_cost < max_cost:
        try:
            from app.notifications.wire import notify_cost_alert

            notify_cost_alert(org_id, total_cost, max_cost)
        except Exception:
            logger.debug("notify_cost_alert failed (non-critical)", exc_info=True)

    if total_cost >= max_cost:
        raise CostBudgetExceededError(
            f"Organisatsiooni igakuine LLM-i kulueelarve ({max_cost:.2f} USD) "
            f"on taidetud (praegune kulu: {total_cost:.2f} USD)."
        )


def get_user_quota(user_id: UUID | str, org_id: UUID | str) -> UserQuota:
    """Return a snapshot of the user's current rate-limit and cost-budget state.

    Reads the same tables as :func:`check_message_rate` and
    :func:`check_org_cost_budget` but without raising: the caller (the
    ``/api/me/usage`` endpoint) needs the raw numbers to render a
    dashboard. Fails open by returning zero usage when the DB is
    unreachable so the UI can still load.
    """
    message_limit = _get_max_messages_per_hour()
    cost_limit_float = _get_max_monthly_cost_usd()
    cost_limit = Decimal(str(cost_limit_float))
    # 80% alert threshold — keep identical to the admin-notification trigger.
    alert_threshold = (cost_limit * Decimal(str(_COST_ALERT_FRACTION))).quantize(Decimal("0.01"))

    messages_this_hour = 0
    cost_this_month = Decimal("0")

    try:
        with get_connection() as conn:
            message_row = conn.execute(
                "SELECT COUNT(*) FROM messages "
                "WHERE conversation_id IN "
                "  (SELECT id FROM conversations WHERE user_id = %s) "
                "AND role = 'user' "
                "AND created_at > now() - interval '1 hour'",
                (str(user_id),),
            ).fetchone()
            if message_row is not None:
                messages_this_hour = int(message_row[0] or 0)

            cost_row = conn.execute(
                "SELECT COALESCE(SUM(cost_usd), 0) FROM llm_usage "
                "WHERE org_id = %s "
                "AND created_at >= date_trunc('month', now())",
                (str(org_id),),
            ).fetchone()
            if cost_row is not None:
                cost_value = cost_row[0] or 0
                cost_this_month = Decimal(str(cost_value))
    except Exception:
        logger.exception("Failed to read quota for user_id=%s org_id=%s", user_id, org_id)
        # Fall through with zeroed usage.

    return UserQuota(
        messages_this_hour=messages_this_hour,
        message_limit_per_hour=message_limit,
        cost_this_month_usd=cost_this_month,
        cost_limit_per_month_usd=cost_limit,
        cost_alert_threshold_usd=alert_threshold,
    )


def seconds_until_hourly_reset(user_id: UUID | str) -> int:
    """Seconds until the oldest message in the current rate-limit window ages out.

    The sliding window is one hour wide. The earliest ``role='user'``
    message in the last hour determines when the window will have room
    for a new message: ``3600 - (now - oldest_created_at)`` seconds.

    The result is clamped to ``[0, 3600]``. When there is no message in
    the window (or on DB error) we return ``0`` — the user can retry
    immediately.
    """
    try:
        with get_connection() as conn:
            row = conn.execute(
                "SELECT EXTRACT(EPOCH FROM (now() - MIN(created_at))) "
                "FROM messages "
                "WHERE conversation_id IN "
                "  (SELECT id FROM conversations WHERE user_id = %s) "
                "AND role = 'user' "
                "AND created_at > now() - interval '1 hour'",
                (str(user_id),),
            ).fetchone()
    except Exception:
        logger.exception("Failed to compute hourly reset for user_id=%s", user_id)
        return 0

    if row is None or row[0] is None:
        return 0

    age_seconds = float(row[0])
    remaining = 3600 - age_seconds
    if remaining < 0:
        return 0
    if remaining > 3600:
        return 3600
    return int(remaining)
