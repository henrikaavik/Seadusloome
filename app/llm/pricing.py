"""LLM pricing table for cost calculation."""

# Rates in USD per million tokens (input, output).
PRICING: dict[tuple[str, str], dict[str, float]] = {
    ("claude", "claude-sonnet-4-6"): {"input": 3.00, "output": 15.00},
    ("claude", "claude-opus-4-6"): {"input": 15.00, "output": 75.00},
    ("claude", "claude-haiku-4-5"): {"input": 0.80, "output": 4.00},
}


def calculate_cost(provider: str, model: str, tokens_input: int, tokens_output: int) -> float:
    """Return the estimated cost in USD for the given token counts."""
    key = (provider, model)
    rates = PRICING.get(key)
    if rates is None:
        return 0.0  # unknown model — log but don't crash
    return (tokens_input * rates["input"] + tokens_output * rates["output"]) / 1_000_000
