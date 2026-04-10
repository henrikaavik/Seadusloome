"""AI Law Drafter — Phase 3A.

This package contains the multi-step intent-to-draft pipeline:
state machine, session CRUD, wizard UI routes, transition guards,
background job handlers (Steps 2-5), and prompt templates.
"""

# Import handlers for side effects — they register themselves via
# @register_handler on import. The worker imports ``app.drafter`` at
# startup so the drafter job handlers are available before any job is claimed.
from app.drafter import handlers as _handlers  # noqa: F401  # isort: skip
from app.drafter.errors import DrafterNotAvailableError
from app.drafter.guards import require_real_llm
from app.drafter.session_model import DraftingSession, create_session, get_session
from app.drafter.state_machine import STEP_LABELS_ET, Step

__all__ = [
    "DraftingSession",
    "DrafterNotAvailableError",
    "Step",
    "STEP_LABELS_ET",
    "create_session",
    "get_session",
    "require_real_llm",
]
