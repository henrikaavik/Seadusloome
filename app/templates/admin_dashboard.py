"""Admin dashboard — backward-compatible shim.

The real implementation lives in ``app.admin.*`` sub-modules.  This module
re-exports every public and "underscore-public" name so that existing code
doing ``from app.templates.admin_dashboard import X`` or
``@patch("app.templates.admin_dashboard.X")`` continues to work.

A handful of functions are *rebound* (their ``__globals__`` dict is swapped
to point at **this** module) so that ``@patch`` decorators that replace
``_connect``, ``jena_check_health``, ``_check_postgres``, or
``_get_sync_logs`` on this module actually affect the function at call-time.
Without rebinding, the function objects would resolve those names from the
sub-module where they were originally defined, and the patches would have
no effect.
"""

from __future__ import annotations

import logging
import threading
import types as _types

from fasthtml.common import *  # noqa: F403, F401
from starlette.responses import JSONResponse  # noqa: F401  -- used by rebound health_check

from app.admin._shared import _tooltip  # noqa: F401
from app.admin.audit import (
    _get_audit_log_page as _get_audit_log_page_impl,
)
from app.admin.audit import (
    admin_audit_page as _admin_audit_page_impl,
)
from app.admin.dashboard import (
    admin_dashboard_page as _admin_dashboard_page_impl,
)
from app.admin.health import (
    _check_postgres as _check_postgres_impl,
)
from app.admin.health import (
    _health_card,  # noqa: F401
)
from app.admin.health import (
    health_check as _health_check_impl,
)
from app.admin.jobs import (
    _get_job_queue_snapshot,  # noqa: F401
    _job_queue_card,  # noqa: F401
)
from app.admin.llm_usage import (
    _get_llm_usage_stats,  # noqa: F401
    _llm_usage_card,  # noqa: F401
)
from app.admin.rate_limits import (
    _get_rate_limit_stats,  # noqa: F401
    _rate_limit_card,  # noqa: F401
)
from app.admin.sync import (
    _get_sync_logs as _get_sync_logs_impl,
)
from app.admin.sync import (
    _run_sync_and_clear_flag as _run_sync_and_clear_flag_impl,
)
from app.admin.sync import (
    _sync_card,  # noqa: F401
    _sync_status_badge,  # noqa: F401
    _sync_trigger_form,  # noqa: F401
)
from app.admin.sync import (
    trigger_sync as _trigger_sync_impl,
)
from app.admin.users import (
    _get_user_stats as _get_user_stats_impl,
)
from app.admin.users import (
    _quick_links_card,  # noqa: F401
    _user_stats_card,  # noqa: F401
)
from app.auth.roles import require_role
from app.db import get_connection as _connect  # noqa: F401
from app.sync.jena_loader import check_health as jena_check_health  # noqa: F401
from app.ui.primitives.button import Button  # noqa: F401, F811  -- shadow guard #419

# Module-level state expected by tests (e.g. ``admin_dashboard._sync_in_progress``).
_sync_lock = threading.Lock()
_sync_in_progress = False

logger = logging.getLogger(__name__)

_SYNC_STATUS_MAP = {
    "running": ("running", "K\u00e4imas"),
    "success": ("ok", "\u00d5nnestus"),
    "failed": ("failed", "Eba\u00f5nnestus"),
}

# ---------------------------------------------------------------------------
# Rebinding helper
# ---------------------------------------------------------------------------


def _rebind(fn):
    """Return a copy of *fn* whose ``__globals__`` point to THIS module.

    This lets ``@patch("app.templates.admin_dashboard.X")`` affect the
    function when it looks up ``X`` at call-time, because it now resolves
    names from this module's dict rather than the sub-module's.
    """
    rebound = _types.FunctionType(
        fn.__code__,
        globals(),  # THIS module's global dict
        fn.__name__,
        fn.__defaults__,
        fn.__closure__,
    )
    rebound.__module__ = __name__
    rebound.__qualname__ = fn.__qualname__
    rebound.__doc__ = fn.__doc__
    if fn.__kwdefaults__:
        rebound.__kwdefaults__ = fn.__kwdefaults__
    rebound.__annotations__ = fn.__annotations__
    return rebound


# ---------------------------------------------------------------------------
# Rebound functions — tests patch names on THIS module and expect these
# functions to see the patched values.
# ---------------------------------------------------------------------------

_check_postgres = _rebind(_check_postgres_impl)
_get_sync_logs = _rebind(_get_sync_logs_impl)
_get_user_stats = _rebind(_get_user_stats_impl)
_get_audit_log_page = _rebind(_get_audit_log_page_impl)
health_check = _rebind(_health_check_impl)

# trigger_sync and _run_sync_and_clear_flag use ``global _sync_in_progress``
# which writes to __globals__["_sync_in_progress"] — after rebinding that
# points to THIS module's _sync_in_progress, which is exactly what the
# tests assert against.
_run_sync_and_clear_flag = _rebind(_run_sync_and_clear_flag_impl)
trigger_sync = _rebind(_trigger_sync_impl)

# admin_dashboard_page calls _check_postgres, jena_check_health,
# _get_sync_logs, _get_user_stats — all patchable names.  Rebind it so
# patches on this module take effect when the page is rendered.
admin_dashboard_page = _rebind(_admin_dashboard_page_impl)
admin_audit_page = _rebind(_admin_audit_page_impl)


# ---------------------------------------------------------------------------
# Apply admin role decorator & route registration
# ---------------------------------------------------------------------------

_admin_dashboard = require_role("admin")(admin_dashboard_page)
_admin_audit = require_role("admin")(admin_audit_page)
_admin_sync = require_role("admin")(trigger_sync)


def register_admin_routes(rt) -> None:  # type: ignore[no-untyped-def]
    """Register admin dashboard routes on the FastHTML route decorator *rt*."""
    rt("/admin", methods=["GET"])(_admin_dashboard)
    rt("/admin/audit", methods=["GET"])(_admin_audit)
    rt("/admin/sync", methods=["POST"])(_admin_sync)
    rt("/api/health", methods=["GET"])(health_check)
