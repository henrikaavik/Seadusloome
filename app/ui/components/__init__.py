"""Reusable UI components — composite widgets built from primitives + surfaces.

Lives one level below ``app.ui.layout`` (page chrome) and above
``app.ui.primitives`` (Button/Input/Icon/...). A component here may
compose primitives, talk to FastHTML/HTMX, and pull from
``app.ui.capabilities``; it must not import from routes/domain modules
(that would create import cycles when routes import from here).

New components landing here should be cited from at least two surfaces;
one-off widgets stay near their caller.
"""

# Re-export only the component primitives — the routes module imports
# PageShell, which imports TopBar, which imports the global_search
# component; pulling search_routes into this package __init__ would
# create a circular import the moment ``app.ui.layout`` is touched. The
# routes module is imported directly by ``app.main`` instead.
from app.ui.components.capability_card import CapabilityCard, capability_href
from app.ui.components.global_search import (
    GlobalSearchBar,
    GlobalSearchMobileButton,
    render_dropdown,
)

__all__ = [
    "CapabilityCard",
    "GlobalSearchBar",
    "GlobalSearchMobileButton",
    "capability_href",
    "render_dropdown",
]
