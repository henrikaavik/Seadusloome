"""Pre-condition checks for drafter session creation."""

from app.drafter.errors import DrafterNotAvailableError
from app.llm import get_default_provider


def require_real_llm() -> None:
    """Raise if the LLM provider is in stub mode.

    Call this at the TOP of the drafter session creation handler,
    BEFORE any DB writes. If it raises, the UI should render an Alert
    explaining that ANTHROPIC_API_KEY needs to be configured.

    Phase 3B's chat intentionally does NOT use this guard — chat
    gracefully returns "AI ei ole praegu saadaval" when stubbed.
    """
    provider = get_default_provider()
    if getattr(provider, "_stubbed", False):
        raise DrafterNotAvailableError(
            "AI koostaja vajab seadistamist. Palun seadistage ANTHROPIC_API_KEY "
            "Coolify keskkonnamuutujates enne koostaja kasutamist."
        )
