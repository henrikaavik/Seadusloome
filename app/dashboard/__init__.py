"""Dashboard ("Töölaud") feature module — the ``/dashboard`` work queue (#860).

Relocated out of the cross-cutting ``app/templates/`` aggregator into a proper
feature module with a clean layering split:

    - :mod:`app.dashboard.service` — framework-free data layer (every
      ``_connect()`` / SPARQL widget query + bookmark CRUD as typed
      ``inputs → rows`` functions; no ``fasthtml`` / ``starlette`` imports so
      the Phase-5 public API / MCP server can wrap them as tools).
    - :mod:`app.dashboard.pages` — FastHTML rendering + route handlers +
      :func:`register_dashboard_routes`, consuming the service functions.

``register_dashboard_routes`` is exported *lazily* (PEP 562 module
``__getattr__``) rather than eagerly imported at package import time. Eagerly
importing it would pull :mod:`app.dashboard.pages` — and therefore ``fasthtml``
— into ``sys.modules`` the moment anyone does ``import app.dashboard.service``,
breaking the framework-free guarantee the service layer is built to keep
(pinned by ``tests/test_dashboard_import_direction.py``). The lazy export means
``from app.dashboard import register_dashboard_routes`` (used by
``app/main.py``) still works and only loads the framework layer when the route
registrar is actually requested.

The lazy export is one half of the guarantee; the other lives in the service
layer itself, which defers its sole ``app.analyysikeskus.eu_transposition``
import to call time (importing that package runs its ``__init__`` → SPARQL
client → ``app.metrics`` → starlette). Both are needed for a fresh
``import app.dashboard.service`` to stay framework-free at runtime.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.dashboard.pages import register_dashboard_routes

__all__ = ["register_dashboard_routes"]


def __getattr__(name: str) -> Any:
    """Lazily resolve the public route registrar from the page layer (PEP 562)."""
    if name == "register_dashboard_routes":
        from app.dashboard.pages import register_dashboard_routes

        return register_dashboard_routes
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
