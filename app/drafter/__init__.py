"""AI Law Drafter — Phase 3A.

This package contains the multi-step intent-to-draft pipeline:
state machine, session CRUD, wizard UI routes, and transition guards.
"""

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
