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

    **Concurrency, the advisory lock, and the residual race** — A naive
    implementation reads the running monthly total with a plain
    ``SELECT`` and returns. Two concurrent requests can both observe a
    total below the cap, both pass the check, and both proceed to spend
    against the budget, causing an unbounded overshoot as concurrency
    scales.

    When this function is invoked with an active ``conn`` it takes a
    transaction-scoped PostgreSQL advisory lock keyed to the org id:

        ``pg_advisory_xact_lock(hashtextextended('cost_budget:<org>', 0))``

    This serialises *the budget checks themselves* for a given org
    without requiring a dedicated lock row or new table: while one
    request holds the lock, a second request for the same org blocks on
    the lock acquire and therefore cannot run its ``SELECT SUM`` until
    the first request's transaction commits/rolls back. The lock is
    released automatically at that boundary.

    **The lock does NOT span the LLM spend, and that is deliberate.**
    The chat orchestrator (see ``app/chat/orchestrator.py``
    ``_check_budget_in_own_tx`` + ``_phase_check_budget``) calls this in
    its *own* short-lived transaction that commits immediately after the
    check returns — the lock is released before any token is spent. This
    was an intentional trade-off for #658: an earlier design held the
    lock across the user-message persist, but a stuck pre-deploy
    connection holding the lock then blocked every chat turn for that org
    and risked losing the user's input. The persist was therefore moved
    into its own transaction that commits *before* this check runs
    (``_phase_persist_user_message`` precedes ``_phase_check_budget``),
    so the "read total → release lock → later spend tokens" sequence is
    **not** atomic.

    The residual race is bounded and accepted: the lock prevents two
    *simultaneous* checks from racing on a stale read, but it does not
    prevent a request that passed the check from spending after the lock
    is released while a later request also passes (because the spend from
    the first has not yet landed in ``llm_usage``). In the worst case a
    burst of N near-simultaneous turns can each pass at ~99% of the cap
    and collectively overshoot by roughly N turns' worth of spend. For
    the 5-50 concurrent-user target this is a few dollars of overshoot,
    not an unbounded one, and the next turn sees the corrected sum and
    blocks. Closing it fully would require holding a lock (or a reserved
    spend row) across the entire LLM call, which re-introduces the #658
    hang/data-loss failure mode; we explicitly do not do that.

    When called without ``conn`` (the legacy shape, e.g. the drafter
    handlers), the function opens its own short-lived connection and
    performs the plain SELECT with **no** locking — so concurrent
    drafter steps for one org are not even serialised on the check. This
    mode is intentionally kept for paths where a momentary race is
    acceptable; the same bounded-overshoot reasoning applies.

    The advisory-lock acquire is bounded by ``SET LOCAL lock_timeout =
    '3s'``: if a stale connection holds the lock for longer than that,
    psycopg raises ``LockNotAvailable``, the outer ``except`` catches
    it, and we fail-open for this turn (the org will simply have a
    momentary unmetered spend; the next turn will see the corrected
    sum). Trade-off: brief budget-check skew during lock contention vs.
    indefinite hangs that lose user data (#658).
    """
    max_cost = _get_max_monthly_cost_usd()
    total_cost = 0.0

    try:
        if conn is not None:
            # Bound the advisory-lock acquire so a stuck pre-deploy
            # connection holding the lock can't hang the whole turn
            # (#658). On lock_timeout, psycopg raises LockNotAvailable
            # which the outer except catches and we fail-open.
            conn.execute("SET LOCAL lock_timeout = '3s'")
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

    # Notify org admins when cost crosses a threshold. Both wire helpers
    # dedupe to one alert per org/day per type, so calling them on every
    # turn is safe — the dedupe query suppresses the repeat fan-out. The
    # 80% advisory and the 100% "exhausted" alert are mutually exclusive
    # per call (a single ``total_cost`` is in exactly one band).
    if max_cost * _COST_ALERT_FRACTION <= total_cost < max_cost:
        try:
            from app.notifications.wire import notify_cost_alert

            notify_cost_alert(org_id, total_cost, max_cost)
        except Exception:
            logger.debug("notify_cost_alert failed (non-critical)", exc_info=True)
    elif total_cost >= max_cost:
        try:
            from app.notifications.wire import notify_cost_exhausted

            notify_cost_exhausted(org_id, total_cost, max_cost)
        except Exception:
            logger.debug("notify_cost_exhausted failed (non-critical)", exc_info=True)

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
