"""Admin route registration.

Owns the wiring between FastHTML's ``rt`` decorator and the page handlers
that live in the ``app.admin.*`` sub-modules. Every route is wrapped in
``require_role("admin")`` so unauthenticated or non-admin callers get the
standard auth redirect/403 before the handler runs.

Why this module exists (post-refactor pattern; it replaced the
historical ``app.templates.admin_dashboard`` shim, now removed):

* Each admin sub-module (``health``, ``sync``, ``audit``, ``analytics``,
  ``cost_dashboard``, ``job_monitor``, ``performance``) owns its own
  handlers and DB helpers. Tests patch helpers on the real module path
  (e.g. ``@patch("app.admin.sync._get_sync_logs")``) — there is no more
  ``__globals__`` rebinding indirection.
* Handlers that depend on private helpers in the same module either
  import them at module scope (when they only touch public names) or
  re-import them as locals inside the handler body (when they are kept
  patchable by tests).
* Adding a new admin route: implement the handler in the relevant
  sub-module, then add one ``rt(...)`` line below. No shim updates
  needed; no completeness invariant to maintain.
"""

from __future__ import annotations

from app.admin.analytics import (
    admin_analytics_export,
    admin_analytics_page,
    admin_analytics_refresh,
)
from app.admin.audit import (
    admin_audit_detail,
    admin_audit_export,
    admin_audit_page,
)
from app.admin.cost_dashboard import admin_cost_export, admin_cost_page
from app.admin.dashboard import admin_dashboard_page
from app.admin.health import admin_health_page, health_check
from app.admin.job_monitor import (
    admin_job_detail,
    admin_job_retry,
    admin_jobs_page,
    admin_jobs_purge,
)
from app.admin.performance import admin_performance_page
from app.admin.sentry_panel import admin_sentry_page
from app.admin.sync import admin_sync_history_page, sync_status_card, trigger_sync
from app.auth.roles import require_role

__all__ = ["register_admin_routes"]


def register_admin_routes(rt) -> None:  # type: ignore[no-untyped-def]
    """Register admin dashboard routes on the FastHTML route decorator *rt*.

    All ``/admin/*`` routes are wrapped with ``require_role("admin")``; the
    JSON ``/api/health`` endpoint stays unauthenticated so external uptime
    monitors (Coolify, etc.) can hit it.
    """
    rt("/admin", methods=["GET"])(require_role("admin")(admin_dashboard_page))
    rt("/admin/audit", methods=["GET"])(require_role("admin")(admin_audit_page))
    rt("/admin/audit/export", methods=["GET"])(require_role("admin")(admin_audit_export))
    rt("/admin/audit/detail/{id}", methods=["GET"])(require_role("admin")(admin_audit_detail))
    rt("/admin/performance", methods=["GET"])(require_role("admin")(admin_performance_page))
    rt("/admin/sync", methods=["POST"])(require_role("admin")(trigger_sync))
    rt("/admin/sync/status", methods=["GET"])(require_role("admin")(sync_status_card))
    rt("/admin/sync/history", methods=["GET"])(require_role("admin")(admin_sync_history_page))
    rt("/admin/analytics", methods=["GET"])(require_role("admin")(admin_analytics_page))
    rt("/admin/analytics/refresh", methods=["POST"])(
        require_role("admin")(admin_analytics_refresh)
    )
    rt("/admin/analytics/export", methods=["GET"])(require_role("admin")(admin_analytics_export))
    rt("/admin/costs", methods=["GET"])(require_role("admin")(admin_cost_page))
    rt("/admin/costs/export", methods=["GET"])(require_role("admin")(admin_cost_export))
    rt("/admin/jobs", methods=["GET"])(require_role("admin")(admin_jobs_page))
    rt("/admin/jobs/{id}/retry", methods=["POST"])(require_role("admin")(admin_job_retry))
    rt("/admin/jobs/purge", methods=["POST"])(require_role("admin")(admin_jobs_purge))
    rt("/api/health", methods=["GET"])(health_check)
    rt("/admin/health/aggregator", methods=["GET"])(require_role("admin")(admin_health_page))
    rt("/admin/sentry", methods=["GET"])(require_role("admin")(admin_sentry_page))
    rt("/admin/jobs/{id}/detail", methods=["GET"])(require_role("admin")(admin_job_detail))
