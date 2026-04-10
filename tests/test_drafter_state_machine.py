"""Unit tests for ``app.drafter.state_machine``.

Tests the 7-step state machine: Step enum, transition guards,
``can_advance``, and ``advance_step``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from app.drafter.state_machine import (
    STEP_LABELS_ET,
    Step,
    StepTransitionError,
    advance_step,
    can_advance,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@dataclass
class FakeSession:
    """Minimal stand-in for DraftingSession to exercise guards."""

    id: uuid.UUID = field(default_factory=uuid.uuid4)
    current_step: int = 1
    intent: str | None = None
    clarifications: list[dict[str, Any]] | None = field(default_factory=list)
    research_data_encrypted: bytes | None = None
    proposed_structure: dict[str, Any] | None = None
    draft_content_encrypted: bytes | None = None
    integrated_draft_id: uuid.UUID | None = None
    status: str = "active"


# ---------------------------------------------------------------------------
# Step enum basics
# ---------------------------------------------------------------------------


class TestStepEnum:
    def test_step_enum_has_7_values(self):
        assert len(Step) == 7

    def test_step_values_are_sequential(self):
        values = [int(s) for s in Step]
        assert values == [1, 2, 3, 4, 5, 6, 7]

    def test_step_labels_are_estonian(self):
        """Every step must have an Estonian label and none should look English."""
        assert len(STEP_LABELS_ET) == 7
        for step in Step:
            label = STEP_LABELS_ET[step]
            assert isinstance(label, str)
            assert len(label) > 0
            # Basic check: none of the English step names should appear
            assert label not in (
                "Intent",
                "Clarify",
                "Research",
                "Structure",
                "Draft",
                "Review",
                "Export",
            )


# ---------------------------------------------------------------------------
# can_advance — transition guards
# ---------------------------------------------------------------------------


class TestCanAdvance:
    def test_can_advance_intent_to_clarify_requires_intent(self):
        session = FakeSession(current_step=1, intent=None)
        assert can_advance(session, Step.CLARIFY) is False

        session.intent = ""
        assert can_advance(session, Step.CLARIFY) is False

        session.intent = "   "
        assert can_advance(session, Step.CLARIFY) is False

        session.intent = "Soovin koostada seaduse"
        assert can_advance(session, Step.CLARIFY) is True

    def test_can_advance_clarify_requires_3_answers(self):
        session = FakeSession(current_step=2, intent="ok")
        session.clarifications = []
        assert can_advance(session, Step.RESEARCH) is False

        session.clarifications = [{"q": "a"}, {"q": "b"}]
        assert can_advance(session, Step.RESEARCH) is False

        session.clarifications = [{"q": "a"}, {"q": "b"}, {"q": "c"}]
        assert can_advance(session, Step.RESEARCH) is True

    def test_can_advance_clarify_none_clarifications_fails(self):
        session = FakeSession(current_step=2, intent="ok")
        session.clarifications = None
        assert can_advance(session, Step.RESEARCH) is False

    def test_can_advance_research_requires_data(self):
        session = FakeSession(current_step=3)
        session.research_data_encrypted = None
        assert can_advance(session, Step.STRUCTURE) is False

        session.research_data_encrypted = b"encrypted-research"
        assert can_advance(session, Step.STRUCTURE) is True

    def test_can_advance_structure_requires_structure(self):
        session = FakeSession(current_step=4)
        session.proposed_structure = None
        assert can_advance(session, Step.DRAFT) is False

        session.proposed_structure = {"sections": []}
        assert can_advance(session, Step.DRAFT) is True

    def test_can_advance_draft_requires_clauses(self):
        session = FakeSession(current_step=5)
        session.draft_content_encrypted = None
        assert can_advance(session, Step.REVIEW) is False

        session.draft_content_encrypted = b"encrypted-draft"
        assert can_advance(session, Step.REVIEW) is True

    def test_can_advance_review_requires_draft_id(self):
        session = FakeSession(current_step=6)
        session.integrated_draft_id = None
        assert can_advance(session, Step.EXPORT) is False

        session.integrated_draft_id = uuid.uuid4()
        assert can_advance(session, Step.EXPORT) is True

    def test_cannot_skip_steps(self):
        """Cannot jump from step 1 to step 3."""
        session = FakeSession(current_step=1, intent="ok")
        assert can_advance(session, Step.RESEARCH) is False

    def test_cannot_advance_from_export(self):
        """EXPORT is the terminal step."""
        session = FakeSession(current_step=7)
        # There is no Step(8), so trying to advance should fail.
        assert can_advance(session, Step.EXPORT) is False


# ---------------------------------------------------------------------------
# advance_step
# ---------------------------------------------------------------------------


class TestAdvanceStep:
    @patch("app.drafter.session_model.update_session")
    @patch("app.drafter.session_model.create_version_snapshot")
    def test_advance_creates_snapshot(
        self,
        mock_snapshot: MagicMock,
        mock_update: MagicMock,
    ):
        session = FakeSession(current_step=1, intent="Test kavatsus")
        conn = MagicMock()

        new_step = advance_step(session, conn)

        assert new_step == Step.CLARIFY
        mock_snapshot.assert_called_once()
        # Verify the snapshot was for step 1
        call_args = mock_snapshot.call_args
        assert call_args.args[1] == session.id
        assert call_args.args[2] == 1
        # Verify step was updated to 2
        mock_update.assert_called_once_with(conn, session.id, current_step=2)

    def test_advance_from_export_raises(self):
        session = FakeSession(current_step=7)
        conn = MagicMock()

        with pytest.raises(StepTransitionError, match="viimane"):
            advance_step(session, conn)

    @patch("app.drafter.session_model.update_session")
    @patch("app.drafter.session_model.create_version_snapshot")
    def test_advance_without_prerequisites_raises(
        self,
        mock_snapshot: MagicMock,
        mock_update: MagicMock,
    ):
        """Cannot advance step 1 without intent."""
        session = FakeSession(current_step=1, intent=None)
        conn = MagicMock()

        with pytest.raises(StepTransitionError, match="eeltingimused"):
            advance_step(session, conn)

        # No snapshot or update should have been made
        mock_snapshot.assert_not_called()
        mock_update.assert_not_called()
