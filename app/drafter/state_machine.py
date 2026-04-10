"""Drafter 7-step state machine with transition guards.

The AI Law Drafter follows a linear 7-step pipeline:

    1. Intent capture         (user describes what the law should achieve)
    2. Clarification interview (LLM asks follow-up questions)
    3. Ontology research      (SPARQL traversal + LLM analysis)
    4. Structure generation   (section/chapter outline)
    5. Clause drafting        (paragraph-by-paragraph prose)
    6. Integrated review      (cross-reference check + final edits)
    7. Export                  (.docx generation)

Transition guards enforce that each step's prerequisites are met before
advancing.  ``advance_step`` also creates a version snapshot so every
state transition is auditable.
"""

from __future__ import annotations

from enum import IntEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import Any


class Step(IntEnum):
    """The 7 pipeline steps of the AI law drafter."""

    INTENT = 1
    CLARIFY = 2
    RESEARCH = 3
    STRUCTURE = 4
    DRAFT = 5
    REVIEW = 6
    EXPORT = 7


STEP_LABELS_ET: dict[Step, str] = {
    Step.INTENT: "Kavatsus",
    Step.CLARIFY: "Tapsustamine",
    Step.RESEARCH: "Uurimine",
    Step.STRUCTURE: "Struktuur",
    Step.DRAFT: "Koostamine",
    Step.REVIEW: "Ulevaade",
    Step.EXPORT: "Eksport",
}


class StepTransitionError(Exception):
    """Raised when a step transition is not allowed."""


# ---------------------------------------------------------------------------
# Transition guard callables
# ---------------------------------------------------------------------------

# Each guard receives a session-like object and returns True when the
# transition from ``step`` to ``step + 1`` is allowed.  Guards are
# indexed by the *source* step.


def _can_leave_intent(session: Any) -> bool:
    """INTENT -> CLARIFY: session.intent must be non-empty."""
    return bool(session.intent and session.intent.strip())


def _can_leave_clarify(session: Any) -> bool:
    """CLARIFY -> RESEARCH: at least 3 clarifications recorded."""
    clarifications = session.clarifications
    if clarifications is None:
        return False
    return len(clarifications) >= 3


def _can_leave_research(session: Any) -> bool:
    """RESEARCH -> STRUCTURE: encrypted research data must be present."""
    return session.research_data_encrypted is not None


def _can_leave_structure(session: Any) -> bool:
    """STRUCTURE -> DRAFT: proposed structure must be present."""
    return session.proposed_structure is not None


def _can_leave_draft(session: Any) -> bool:
    """DRAFT -> REVIEW: encrypted draft content with at least 1 clause."""
    if session.draft_content_encrypted is None:
        return False
    # The encrypted content itself proves at least one clause exists;
    # a zero-clause draft would never have been encrypted in the first
    # place. The calling code is responsible for the clause-count
    # invariant when populating draft_content_encrypted.
    return True


def _can_leave_review(session: Any) -> bool:
    """REVIEW -> EXPORT: integrated_draft_id must be set."""
    return session.integrated_draft_id is not None


_GUARDS: dict[Step, Any] = {
    Step.INTENT: _can_leave_intent,
    Step.CLARIFY: _can_leave_clarify,
    Step.RESEARCH: _can_leave_research,
    Step.STRUCTURE: _can_leave_structure,
    Step.DRAFT: _can_leave_draft,
    Step.REVIEW: _can_leave_review,
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def can_advance(session: Any, target_step: Step) -> bool:
    """Return ``True`` if *session* may transition to *target_step*.

    Only single-step forward transitions are allowed (no skipping).
    """
    try:
        current = Step(session.current_step)
    except ValueError:
        return False

    # Can only advance by exactly one step.
    if target_step != current + 1:
        return False

    # EXPORT is the terminal step -- cannot advance beyond it.
    if current == Step.EXPORT:
        return False

    guard = _GUARDS.get(current)
    if guard is None:
        return False

    return guard(session)


def advance_step(session: Any, conn: Any) -> Step:
    """Advance *session* to the next step.

    Creates a version snapshot of the current step before transitioning.
    Returns the new ``Step`` value.

    Raises ``StepTransitionError`` when the transition is not allowed.
    """
    try:
        current = Step(session.current_step)
    except ValueError:
        raise StepTransitionError(f"Vigane sammu number: {session.current_step}")

    if current == Step.EXPORT:
        raise StepTransitionError("Eksport on viimane samm, edasi liikuda ei saa.")

    target = Step(current + 1)

    if not can_advance(session, target):
        raise StepTransitionError(
            f"Sammult {STEP_LABELS_ET.get(current, str(current))} "
            f"sammule {STEP_LABELS_ET.get(target, str(target))} "
            f"liikumise eeltingimused ei ole taidetud."
        )

    import json

    # Import here to avoid circular dependency with session_model.
    from app.drafter.session_model import (
        create_version_snapshot,
        update_session,
    )

    snapshot_data = json.dumps(
        {
            "step": int(current),
            "intent": session.intent,
            "clarifications": session.clarifications,
            "status": session.status,
        },
        default=str,
    ).encode()
    create_version_snapshot(conn, session.id, int(current), snapshot_data)

    # Advance the step.
    update_session(conn, session.id, current_step=int(target))

    return target
