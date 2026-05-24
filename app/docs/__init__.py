"""Document Upload subsystem for Phase 2.

Exposes the ``drafts`` CRUD helpers and the ``handle_upload`` pipeline.
The package is named ``docs`` (not ``drafts``) because it hosts the
impact-report and export handlers alongside the upload flow.

Importing this package must NOT pull in any FastHTML / Starlette
route module. The standalone worker (``scripts/run_worker.py``, #348)
imports ``app.docs`` via :func:`app.jobs.registry.register_all_handlers`
to trigger the ``@register_handler`` side effects of each handler
module, and the whole point of the standalone container is that it
stays framework-free. Route registration helpers (``register_draft_routes``,
``register_report_routes``, ``register_draft_ws_routes``,
``register_export_progress_ws_routes``) therefore live ONLY in their
submodules — callers must import them from
:mod:`app.docs.routes`/:mod:`app.docs.report_routes` directly. The
canonical importer is ``app/main.py``.
"""

# Import handlers for side effects — they register themselves via
# @register_handler on import. ``app.jobs.registry.register_all_handlers``
# imports ``app.docs`` at startup so by the time the worker claims any
# job, the real handlers have overridden the fallback stubs in
# app/jobs/worker.py.
from app.docs import analyze_handler as _analyze_handler  # noqa: F401,E402
from app.docs import cleanup_handler as _cleanup_handler  # noqa: F401,E402
from app.docs import export_handler as _export_handler  # noqa: F401,E402
from app.docs import extract_handler as _extract_handler  # noqa: F401,E402
from app.docs import parse_handler as _parse_handler  # noqa: F401,E402
from app.docs.draft_model import (
    Draft,
    count_drafts_for_org,
    create_draft,
    delete_draft,
    get_draft,
    list_drafts_for_org,
    update_draft_status,
)
from app.docs.upload import DraftUploadError, handle_upload

__all__ = [
    "Draft",
    "DraftUploadError",
    "count_drafts_for_org",
    "create_draft",
    "delete_draft",
    "get_draft",
    "handle_upload",
    "list_drafts_for_org",
    "update_draft_status",
]
