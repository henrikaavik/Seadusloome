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
"""

from __future__ import annotations

import logging
import os
from uuid import UUID

from app.db import get_connection

logger = logging.getLogger(__name__)

# Backward-compatible aliases for tests that import these names.
# The actual check functions re-read from the environment each call so
# that config changes take effect without a process restart.
_MAX_MESSAGES_PER_HOUR = int(os.environ.get("CHAT_MAX_MESSAGES_PER_HOUR", "100"))
_MAX_MONTHLY_COST_USD = float(os.environ.get("ORG_MAX_MONTHLY_COST_USD", "50.0"))


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


def check_org_cost_budget(org_id: UUID | str) -> None:
    """Raise :class:`CostBudgetExceeded` if the org's monthly LLM cost exceeds the cap.

    Sums ``cost_usd`` from the ``llm_usage`` table for the current
    calendar month. If the total meets or exceeds
    ``ORG_MAX_MONTHLY_COST_USD``, raises immediately.

    .. note:: **Known TOCTOU race condition** — Two concurrent requests
       can both read the current total, both find it under the cap,
       and both proceed to call the LLM. This means the budget is a
       *soft* cap (fail-open), not a hard wall. The overshoot is
       bounded by the cost of a single LLM call, which is acceptable
       for our use case. A proper fix (``SELECT ... FOR UPDATE`` on a
       running total row) is Phase 4 work.
    """
    max_cost = _get_max_monthly_cost_usd()
    try:
        with get_connection() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(cost_usd), 0) FROM llm_usage "
                "WHERE org_id = %s "
                "AND created_at >= date_trunc('month', now())",
                (str(org_id),),
            ).fetchone()
    except Exception:
        logger.exception("Failed to check org cost budget for org_id=%s", org_id)
        # Fail open
        return

    total_cost = float(row[0]) if row else 0.0

    # Notify org admins when cost hits 80% of the budget.
    if total_cost >= max_cost * 0.8 and total_cost < max_cost:
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
