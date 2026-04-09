"""Document Upload subsystem for Phase 2.

Exposes the ``drafts`` CRUD helpers, the ``handle_upload`` pipeline, and
the FastHTML route registration. The package is named ``docs`` (not
``drafts``) because it will grow to host the impact-report and export
handlers alongside the upload flow in later Phase 2 batches.
"""

# Import handlers for side effects — they register themselves via
# @register_handler on import. The worker imports ``app.docs`` at
# startup (see app/main.py), so by the time the worker claims any
# job, the real handlers have overridden the fallback stubs in
# app/jobs/worker.py.
from app.docs import analyze_handler as _analyze_handler  # noqa: F401,E402
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
from app.docs.routes import register_draft_routes
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
    "register_draft_routes",
    "update_draft_status",
]
