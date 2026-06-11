"""LLM and embedding pricing table for cost calculation.

Prices are USD per million tokens, verified 2026-06-11 against the
provider documentation (#854):

* Anthropic: https://platform.claude.com/docs/en/docs/about-claude/pricing
  (redirect target of https://docs.anthropic.com/en/docs/about-claude/pricing)
* Voyage AI: https://docs.voyageai.com/docs/pricing

The table covers every model the app actually configures (``CLAUDE_MODEL``
defaults to ``claude-sonnet-4-6``; ``VOYAGE_MODEL`` defaults to
``voyage-multilingual-2``) plus the plausible env-var overrides. When a
new model is rolled out, add a row here in the same deploy — unknown
models are *recorded with cost 0* and a loud warning (see
:func:`calculate_cost`), so the usage row survives but budget
enforcement undercounts until the row is added.
"""

from __future__ import annotations

import logging
import threading

logger = logging.getLogger(__name__)

# Rates in USD per million tokens (input, output).
PRICING: dict[tuple[str, str], dict[str, float]] = {
    # -- Anthropic Claude --------------------------------------------------
    # Opus 4.5 through 4.8 are all $5 in / $25 out. (The previous table's
    # $15/$75 belonged to the retired Opus 4.0/4.1 generation — a 3x
    # overcharge against opus-4-6; #854 / review E4.)
    ("claude", "claude-fable-5"): {"input": 10.00, "output": 50.00},
    ("claude", "claude-opus-4-8"): {"input": 5.00, "output": 25.00},
    ("claude", "claude-opus-4-7"): {"input": 5.00, "output": 25.00},
    ("claude", "claude-opus-4-6"): {"input": 5.00, "output": 25.00},
    ("claude", "claude-opus-4-5"): {"input": 5.00, "output": 25.00},
    ("claude", "claude-sonnet-4-6"): {"input": 3.00, "output": 15.00},
    ("claude", "claude-sonnet-4-5"): {"input": 3.00, "output": 15.00},
    # Haiku 4.5 is $1/$5 (the previous $0.80/$4.00 was Haiku 3.5 pricing).
    ("claude", "claude-haiku-4-5"): {"input": 1.00, "output": 5.00},
    # -- Voyage AI embeddings ----------------------------------------------
    # Embeddings only consume input tokens; output rate is 0 so the same
    # ``calculate_cost`` formula works for both providers.
    ("voyage", "voyage-multilingual-2"): {"input": 0.12, "output": 0.0},
    ("voyage", "voyage-law-2"): {"input": 0.12, "output": 0.0},
    # Voyage 4 series (current generation) — plausible VOYAGE_MODEL upgrades;
    # verified against docs.voyageai.com/docs/pricing 2026-06-11 (review P3).
    ("voyage", "voyage-4-large"): {"input": 0.12, "output": 0.0},
    ("voyage", "voyage-4"): {"input": 0.06, "output": 0.0},
    ("voyage", "voyage-4-lite"): {"input": 0.02, "output": 0.0},
    ("voyage", "voyage-3-large"): {"input": 0.18, "output": 0.0},
    ("voyage", "voyage-3.5"): {"input": 0.06, "output": 0.0},
    ("voyage", "voyage-3.5-lite"): {"input": 0.02, "output": 0.0},
}

# (provider, model) pairs we've already warned about — the warning fires
# once per process per unknown model so a busy ingest doesn't flood logs.
_warned_unknown: set[tuple[str, str]] = set()
_warned_unknown_lock = threading.Lock()


def _reset_unknown_model_warnings() -> None:
    """Reset the warn-once state (for tests only)."""
    with _warned_unknown_lock:
        _warned_unknown.clear()


def calculate_cost(provider: str, model: str, tokens_input: int, tokens_output: int) -> float:
    """Return the estimated cost in USD for the given token counts.

    Unknown ``(provider, model)`` pairs follow the warn-and-record policy
    chosen for #854: emit a loud ``logger.warning`` (once per model per
    process) and return ``0.0`` so the caller still records the usage row.
    Billing paths must never crash on a missing price — but the gap must
    be visible in logs, not silent as before.
    """
    key = (provider, model)
    rates = PRICING.get(key)
    if rates is None:
        with _warned_unknown_lock:
            first_sighting = key not in _warned_unknown
            if first_sighting:
                _warned_unknown.add(key)
        if first_sighting:
            logger.warning(
                "No pricing entry for provider=%r model=%r — usage will be "
                "recorded with cost_usd=0, so budgets and cost dashboards "
                "undercount until a row is added to app/llm/pricing.py "
                "PRICING (#854).",
                provider,
                model,
            )
        return 0.0
    return (tokens_input * rates["input"] + tokens_output * rates["output"]) / 1_000_000
