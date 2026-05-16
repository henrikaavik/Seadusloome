"""Reusable UI components — composite widgets built from primitives + surfaces.

This package holds higher-level components that are domain-agnostic (icon +
text + link patterns) but more opinionated than the bare primitives in
``app.ui.primitives``. New components landing here should be cited from at
least two surfaces; one-off widgets stay near their caller.
"""

from app.ui.components.capability_card import CapabilityCard, capability_href

__all__ = ["CapabilityCard", "capability_href"]
