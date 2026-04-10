"""LLM usage logging and cost tracking."""

from __future__ import annotations

import logging
from uuid import UUID

from app.db import get_connection
from app.llm.pricing import calculate_cost

logger = logging.getLogger(__name__)


def log_usage(
    *,
    user_id: UUID | str | None,
    org_id: UUID | str | None,
    provider: str,
    model: str,
    feature: str,
    tokens_input: int,
    tokens_output: int,
) -> None:
    """Record a single LLM API call in the llm_usage table."""
    cost = calculate_cost(provider, model, tokens_input, tokens_output)
    try:
        with get_connection() as conn:
            conn.execute(
                "INSERT INTO llm_usage"
                " (user_id, org_id, provider, model, feature,"
                " tokens_input, tokens_output, cost_usd)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    str(user_id) if user_id else None,
                    str(org_id) if org_id else None,
                    provider,
                    model,
                    feature,
                    tokens_input,
                    tokens_output,
                    cost,
                ),
            )
            conn.commit()
    except Exception:
        logger.exception("Failed to log LLM usage")
