"""Admin route registration."""

from __future__ import annotations

from app.admin.audit import admin_audit_page
from app.admin.dashboard import admin_dashboard_page
from app.admin.health import health_check
from app.admin.sync import trigger_sync
from app.auth.roles import require_role

# Apply admin role decorator
_admin_dashboard = require_role("admin")(admin_dashboard_page)
_admin_audit = require_role("admin")(admin_audit_page)
_admin_sync = require_role("admin")(trigger_sync)


def register_admin_routes(rt) -> None:  # type: ignore[no-untyped-def]
    """Register admin dashboard routes on the FastHTML route decorator *rt*."""
    rt("/admin", methods=["GET"])(_admin_dashboard)
    rt("/admin/audit", methods=["GET"])(_admin_audit)
    rt("/admin/sync", methods=["POST"])(_admin_sync)
    rt("/api/health", methods=["GET"])(health_check)
