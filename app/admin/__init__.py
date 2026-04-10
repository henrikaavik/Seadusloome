"""Admin package — system health, sync, jobs, usage, audit, and dashboard.

Re-exports ``register_admin_routes`` from the backward-compatible shim so
that ``from app.admin import register_admin_routes`` works correctly and
existing ``@patch("app.templates.admin_dashboard.…")`` decorators in the
test suite keep taking effect.
"""

from app.templates.admin_dashboard import register_admin_routes

__all__ = ["register_admin_routes"]
