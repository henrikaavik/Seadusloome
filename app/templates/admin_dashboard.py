"""Backward-compatible re-export module for the admin dashboard.

The real implementation lives in ``app.admin.*`` sub-modules — see
``app/admin/routes.py`` for route wiring and the per-module handlers
(``app.admin.health``, ``app.admin.sync``, ``app.admin.audit``, ...).

This module exists purely so that code paths that historically imported
from ``app.templates.admin_dashboard`` keep working:

* ``from app.templates.admin_dashboard import register_admin_routes``
* ``from app.templates.admin_dashboard import _get_sync_logs``
* the import-safety test (``tests/test_import_safety.py``), which
  imports this module to verify the design-system ``Button`` symbol
  isn't shadowed by ``from fasthtml.common import *``.

**Do not add new symbols here.** Put new admin code in the relevant
``app.admin.*`` sub-module and patch it on its real path in tests
(e.g. ``@patch("app.admin.sync._get_sync_logs")``). The historical
``_rebind`` / ``_EXPECTED_PAGE_HANDLERS`` machinery is gone — tests now
patch helpers on the modules that own them.
"""

from __future__ import annotations

# Re-exports for ``from app.templates.admin_dashboard import X`` callers.
# The Button re-export also keeps the ``test_import_safety`` guard happy.
from app.admin.health import _check_postgres, health_check  # noqa: F401

# Public — route registration. Imported by ``app.main`` indirectly via
# ``app.admin.__init__``; kept here so old direct imports still resolve.
from app.admin.routes import register_admin_routes
from app.admin.sync import _get_sync_logs, sync_status_card, trigger_sync  # noqa: F401
from app.admin.users import _get_user_stats  # noqa: F401
from app.sync.jena_loader import check_health as jena_check_health  # noqa: F401
from app.ui.primitives.button import Button  # noqa: F401  -- shadow guard #419

__all__ = ["register_admin_routes"]
