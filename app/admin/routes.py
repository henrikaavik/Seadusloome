"""Admin route registration — delegates to the backward-compatible shim."""

from app.templates.admin_dashboard import register_admin_routes

__all__ = ["register_admin_routes"]
