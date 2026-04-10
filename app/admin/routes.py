"""Admin route registration."""

from __future__ import annotations

from app.admin.analytics import admin_analytics_page
from app.admin.audit import admin_audit_export, admin_audit_page
from app.admin.cost_dashboard import admin_cost_page
from app.admin.dashboard import admin_dashboard_page
from app.admin.health import health_check
from app.admin.job_monitor import admin_job_retry, admin_jobs_page, admin_jobs_purge
from app.admin.performance import admin_performance_page
from app.admin.sync import trigger_sync
from app.auth.roles import require_role

# Apply admin role decorator
_admin_dashboard = require_role("admin")(admin_dashboard_page)
_admin_audit = require_role("admin")(admin_audit_page)
_admin_audit_export = require_role("admin")(admin_audit_export)
_admin_sync = require_role("admin")(trigger_sync)
_admin_analytics = require_role("admin")(admin_analytics_page)
_admin_costs = require_role("admin")(admin_cost_page)
_admin_jobs = require_role("admin")(admin_jobs_page)
_admin_job_retry = require_role("admin")(admin_job_retry)
_admin_jobs_purge = require_role("admin")(admin_jobs_purge)
_admin_performance = require_role("admin")(admin_performance_page)


def register_admin_routes(rt) -> None:  # type: ignore[no-untyped-def]
    """Register admin dashboard routes on the FastHTML route decorator *rt*."""
    rt("/admin", methods=["GET"])(_admin_dashboard)
    rt("/admin/audit", methods=["GET"])(_admin_audit)
    rt("/admin/audit/export", methods=["GET"])(_admin_audit_export)
    rt("/admin/performance", methods=["GET"])(_admin_performance)
    rt("/admin/sync", methods=["POST"])(_admin_sync)
    rt("/admin/analytics", methods=["GET"])(_admin_analytics)
    rt("/admin/costs", methods=["GET"])(_admin_costs)
    rt("/admin/jobs", methods=["GET"])(_admin_jobs)
    rt("/admin/jobs/{id}/retry", methods=["POST"])(_admin_job_retry)
    rt("/admin/jobs/purge", methods=["POST"])(_admin_jobs_purge)
    rt("/api/health", methods=["GET"])(health_check)
