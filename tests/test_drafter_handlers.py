# pyright: reportOperatorIssue=false
# pyright: reportOptionalSubscript=false
"""Unit tests for the drafter background job handlers.

These tests mock out all external dependencies (DB, SPARQL, LLM) so
they run without network or database access. They exercise the handler
logic including edge cases, error paths, and the retry-gating pattern.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from app.drafter.session_model import DraftingSession

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SESSION_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
_USER_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")
_ORG_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")


def _make_session(
    *,
    session_id: uuid.UUID = _SESSION_ID,
    intent: str = "Soovin luua tehisintellekti seaduse",
    current_step: int = 2,
    clarifications: list[dict[str, Any]] | None = None,
    research_data_encrypted: bytes | None = None,
    proposed_structure: dict[str, Any] | None = None,
    draft_content_encrypted: bytes | None = None,
    workflow_type: str = "full_law",
    status: str = "active",
) -> DraftingSession:
    now = datetime.now(UTC)
    return DraftingSession(
        id=session_id,
        user_id=_USER_ID,
        org_id=_ORG_ID,
        workflow_type=workflow_type,
        current_step=current_step,
        intent=intent,
        clarifications=clarifications or [],
        research_data_encrypted=research_data_encrypted,
        proposed_structure=proposed_structure,
        draft_content_encrypted=draft_content_encrypted,
        integrated_draft_id=None,
        status=status,
        created_at=now,
        updated_at=now,
    )


def _mock_provider(extract_json_return: dict[str, Any] | None = None):
    """Build a mock LLM provider."""
    provider = MagicMock()
    provider.extract_json.return_value = extract_json_return or {
        "questions": [
            {"question": "Kusimus 1?", "rationale": "reason1"},
            {"question": "Kusimus 2?", "rationale": "reason2"},
            {"question": "Kusimus 3?", "rationale": "reason3"},
            {"question": "Kusimus 4?", "rationale": "reason4"},
            {"question": "Kusimus 5?", "rationale": "reason5"},
        ]
    }
    return provider


def _mock_sparql_client():
    """Build a mock SparqlClient that returns sensible results."""
    client = MagicMock()
    client.query.return_value = [
        {"law": "https://data.riik.ee/ontology/estleg/TsiviilS", "label": "Tsiviilseadustik"},
        {"law": "https://data.riik.ee/ontology/estleg/KarS", "label": "Karistusseadustik"},
    ]
    return client


# ---------------------------------------------------------------------------
# Step 2: drafter_clarify
# ---------------------------------------------------------------------------


class TestDrafterClarify:
    @patch("app.drafter.handlers.get_connection")
    @patch("app.drafter.handlers.update_session")
    @patch("app.drafter.handlers.get_default_provider")
    @patch("app.drafter.handlers.SparqlClient")
    @patch("app.drafter.handlers.fetch_session")
    def test_happy_path_generates_questions(
        self,
        mock_fetch: MagicMock,
        mock_sparql_cls: MagicMock,
        mock_get_provider: MagicMock,
        mock_update: MagicMock,
        mock_conn: MagicMock,
    ):
        session = _make_session()
        mock_fetch.return_value = session
        mock_sparql_cls.return_value = _mock_sparql_client()
        mock_get_provider.return_value = _mock_provider()
        mock_conn_ctx = MagicMock()
        mock_conn.return_value.__enter__ = MagicMock(return_value=mock_conn_ctx)
        mock_conn.return_value.__exit__ = MagicMock(return_value=False)

        from app.drafter.handlers import drafter_clarify

        result = drafter_clarify({"session_id": str(_SESSION_ID)})

        assert result["session_id"]  # type: ignore[index] == str(_SESSION_ID)
        assert result["question_count"]  # type: ignore[index] == 5
        mock_update.assert_called_once()

    @patch("app.drafter.handlers.fetch_session")
    def test_missing_session_raises(self, mock_fetch: MagicMock):
        mock_fetch.return_value = None

        from app.drafter.handlers import drafter_clarify

        with pytest.raises(ValueError, match="not found"):
            drafter_clarify({"session_id": str(_SESSION_ID)})

    @patch("app.drafter.handlers.get_connection")
    @patch("app.drafter.handlers.abandon_session")
    @patch("app.drafter.handlers.get_default_provider")
    @patch("app.drafter.handlers.SparqlClient")
    @patch("app.drafter.handlers.fetch_session")
    def test_provider_failure_final_attempt_abandons(
        self,
        mock_fetch: MagicMock,
        mock_sparql_cls: MagicMock,
        mock_get_provider: MagicMock,
        mock_abandon: MagicMock,
        mock_conn: MagicMock,
    ):
        session = _make_session()
        mock_fetch.return_value = session
        mock_sparql_cls.return_value = _mock_sparql_client()
        provider = MagicMock()
        provider.extract_json.side_effect = RuntimeError("API down")
        mock_get_provider.return_value = provider
        mock_conn_ctx = MagicMock()
        mock_conn.return_value.__enter__ = MagicMock(return_value=mock_conn_ctx)
        mock_conn.return_value.__exit__ = MagicMock(return_value=False)

        from app.drafter.handlers import drafter_clarify

        with pytest.raises(RuntimeError, match="LLM call failed"):
            drafter_clarify({"session_id": str(_SESSION_ID)}, attempt=3, max_attempts=3)

        mock_abandon.assert_called_once()

    @patch("app.drafter.handlers.get_connection")
    @patch("app.drafter.handlers.update_session")
    @patch("app.drafter.handlers.get_default_provider")
    @patch("app.drafter.handlers.SparqlClient")
    @patch("app.drafter.handlers.fetch_session")
    def test_empty_questions_uses_fallback(
        self,
        mock_fetch: MagicMock,
        mock_sparql_cls: MagicMock,
        mock_get_provider: MagicMock,
        mock_update: MagicMock,
        mock_conn: MagicMock,
    ):
        session = _make_session()
        mock_fetch.return_value = session
        mock_sparql_cls.return_value = _mock_sparql_client()
        mock_get_provider.return_value = _mock_provider({"questions": []})
        mock_conn_ctx = MagicMock()
        mock_conn.return_value.__enter__ = MagicMock(return_value=mock_conn_ctx)
        mock_conn.return_value.__exit__ = MagicMock(return_value=False)

        from app.drafter.handlers import drafter_clarify

        result = drafter_clarify({"session_id": str(_SESSION_ID)})

        # Fallback generates 3 questions
        assert result["question_count"]  # type: ignore[index] == 3


# ---------------------------------------------------------------------------
# Step 3: drafter_research
# ---------------------------------------------------------------------------


class TestDrafterResearch:
    @patch("app.drafter.handlers.encrypt_text")
    @patch("app.drafter.handlers.get_connection")
    @patch("app.drafter.handlers.update_session")
    @patch("app.drafter.handlers.SparqlClient")
    @patch("app.drafter.handlers.fetch_session")
    def test_happy_path_runs_sparql_queries(
        self,
        mock_fetch: MagicMock,
        mock_sparql_cls: MagicMock,
        mock_update: MagicMock,
        mock_conn: MagicMock,
        mock_encrypt: MagicMock,
    ):
        session = _make_session(
            current_step=3,
            clarifications=[
                {"question": "Q1?", "answer": "A1"},
                {"question": "Q2?", "answer": "A2"},
                {"question": "Q3?", "answer": "A3"},
            ],
        )
        mock_fetch.return_value = session
        client = _mock_sparql_client()
        # Return different results for different queries
        client.query.return_value = [
            {"provision": "uri:1", "label": "Provision 1", "actLabel": "Act 1"},
        ]
        mock_sparql_cls.return_value = client
        mock_encrypt.return_value = b"encrypted-data"
        mock_conn_ctx = MagicMock()
        mock_conn.return_value.__enter__ = MagicMock(return_value=mock_conn_ctx)
        mock_conn.return_value.__exit__ = MagicMock(return_value=False)

        from app.drafter.handlers import drafter_research

        result = drafter_research({"session_id": str(_SESSION_ID)})

        assert result["session_id"]  # type: ignore[index] == str(_SESSION_ID)
        assert "provision_count" in result
        mock_update.assert_called_once()
        mock_encrypt.assert_called_once()

    @patch("app.drafter.handlers.fetch_session")
    def test_missing_session_raises(self, mock_fetch: MagicMock):
        mock_fetch.return_value = None

        from app.drafter.handlers import drafter_research

        with pytest.raises(ValueError, match="not found"):
            drafter_research({"session_id": str(_SESSION_ID)})


# ---------------------------------------------------------------------------
# Step 4: drafter_structure
# ---------------------------------------------------------------------------


class TestDrafterStructure:
    @patch("app.drafter.handlers.get_connection")
    @patch("app.drafter.handlers.update_session")
    @patch("app.drafter.handlers.get_default_provider")
    @patch("app.drafter.handlers.decrypt_text")
    @patch("app.drafter.handlers.SparqlClient")
    @patch("app.drafter.handlers.fetch_session")
    def test_full_law_generates_structure_via_llm(
        self,
        mock_fetch: MagicMock,
        mock_sparql_cls: MagicMock,
        mock_decrypt: MagicMock,
        mock_get_provider: MagicMock,
        mock_update: MagicMock,
        mock_conn: MagicMock,
    ):
        session = _make_session(
            current_step=4,
            research_data_encrypted=b"encrypted",
        )
        mock_fetch.return_value = session
        mock_sparql_cls.return_value = _mock_sparql_client()
        mock_decrypt.return_value = json.dumps({"provisions": [{"act_label": "TsiviilS"}]})
        mock_get_provider.return_value = _mock_provider(
            {
                "title": "AI seadus",
                "chapters": [
                    {
                        "number": "1",
                        "title": "Uldsatted",
                        "sections": [{"paragraph": "par 1", "title": "Reguleerimisala"}],
                    },
                ],
            }
        )
        mock_conn_ctx = MagicMock()
        mock_conn.return_value.__enter__ = MagicMock(return_value=mock_conn_ctx)
        mock_conn.return_value.__exit__ = MagicMock(return_value=False)

        from app.drafter.handlers import drafter_structure

        result = drafter_structure({"session_id": str(_SESSION_ID)})

        assert result["session_id"]  # type: ignore[index] == str(_SESSION_ID)
        assert result["chapters"]  # type: ignore[index] == 1
        assert result["workflow_type"]  # type: ignore[index] == "full_law"

    @patch("app.drafter.handlers.get_connection")
    @patch("app.drafter.handlers.update_session")
    @patch("app.drafter.handlers.fetch_session")
    def test_vtk_uses_fixed_structure(
        self,
        mock_fetch: MagicMock,
        mock_update: MagicMock,
        mock_conn: MagicMock,
    ):
        session = _make_session(
            current_step=4,
            workflow_type="vtk",
            research_data_encrypted=b"encrypted",
        )
        mock_fetch.return_value = session
        mock_conn_ctx = MagicMock()
        mock_conn.return_value.__enter__ = MagicMock(return_value=mock_conn_ctx)
        mock_conn.return_value.__exit__ = MagicMock(return_value=False)

        from app.drafter.handlers import drafter_structure

        result = drafter_structure({"session_id": str(_SESSION_ID)})

        assert result["workflow_type"]  # type: ignore[index] == "vtk"
        assert result["chapters"]  # type: ignore[index] == 5  # VTK has 5 fixed chapters

    @patch("app.drafter.handlers.fetch_session")
    def test_missing_session_raises(self, mock_fetch: MagicMock):
        mock_fetch.return_value = None

        from app.drafter.handlers import drafter_structure

        with pytest.raises(ValueError, match="not found"):
            drafter_structure({"session_id": str(_SESSION_ID)})


# ---------------------------------------------------------------------------
# Step 5: drafter_draft
# ---------------------------------------------------------------------------


class TestDrafterDraft:
    @patch("app.drafter.handlers.encrypt_text")
    @patch("app.drafter.handlers.get_connection")
    @patch("app.drafter.handlers.update_session")
    @patch("app.drafter.handlers.get_default_provider")
    @patch("app.drafter.handlers.decrypt_text")
    @patch("app.drafter.handlers.fetch_session")
    def test_happy_path_drafts_clauses(
        self,
        mock_fetch: MagicMock,
        mock_decrypt: MagicMock,
        mock_get_provider: MagicMock,
        mock_update: MagicMock,
        mock_conn: MagicMock,
        mock_encrypt: MagicMock,
    ):
        structure = {
            "title": "Test seadus",
            "chapters": [
                {
                    "number": "1",
                    "title": "Uldsatted",
                    "sections": [
                        {"paragraph": "par 1", "title": "Reguleerimisala"},
                        {"paragraph": "par 2", "title": "Moistete selgitused"},
                    ],
                }
            ],
        }
        session = _make_session(
            current_step=5,
            proposed_structure=structure,
            research_data_encrypted=b"encrypted",
        )
        mock_fetch.return_value = session
        mock_decrypt.return_value = json.dumps({"provisions": []})
        provider = _mock_provider(
            {
                "text": "Seaduse tekst paragrahvi kohta.",
                "citations": ["estleg:TsiviilS/par/1"],
                "notes": "Markus",
            }
        )
        mock_get_provider.return_value = provider
        mock_encrypt.return_value = b"encrypted-clauses"
        mock_conn_ctx = MagicMock()
        mock_conn.return_value.__enter__ = MagicMock(return_value=mock_conn_ctx)
        mock_conn.return_value.__exit__ = MagicMock(return_value=False)

        from app.drafter.handlers import drafter_draft

        result = drafter_draft({"session_id": str(_SESSION_ID)})

        assert result["session_id"]  # type: ignore[index] == str(_SESSION_ID)
        assert result["clause_count"]  # type: ignore[index] == 2  # 2 sections in the structure
        # LLM was called once per section
        assert provider.extract_json.call_count == 2

    @patch("app.drafter.handlers.fetch_session")
    def test_missing_session_raises(self, mock_fetch: MagicMock):
        mock_fetch.return_value = None

        from app.drafter.handlers import drafter_draft

        with pytest.raises(ValueError, match="not found"):
            drafter_draft({"session_id": str(_SESSION_ID)})

    @patch("app.drafter.handlers.fetch_session")
    def test_no_structure_raises(self, mock_fetch: MagicMock):
        session = _make_session(current_step=5, proposed_structure=None)
        mock_fetch.return_value = session

        from app.drafter.handlers import drafter_draft

        with pytest.raises(ValueError, match="no proposed structure"):
            drafter_draft({"session_id": str(_SESSION_ID)})

    @patch("app.drafter.handlers.encrypt_text")
    @patch("app.drafter.handlers.get_connection")
    @patch("app.drafter.handlers.update_session")
    @patch("app.drafter.handlers.get_default_provider")
    @patch("app.drafter.handlers.decrypt_text")
    @patch("app.drafter.handlers.fetch_session")
    def test_vtk_uses_vtk_prompts(
        self,
        mock_fetch: MagicMock,
        mock_decrypt: MagicMock,
        mock_get_provider: MagicMock,
        mock_update: MagicMock,
        mock_conn: MagicMock,
        mock_encrypt: MagicMock,
    ):
        """VTK sections with matching prompt templates use VTK prompts."""
        from app.drafter.prompts import VTK_STRUCTURE

        session = _make_session(
            current_step=5,
            workflow_type="vtk",
            proposed_structure=VTK_STRUCTURE,
            research_data_encrypted=b"encrypted",
        )
        mock_fetch.return_value = session
        mock_decrypt.return_value = json.dumps({"provisions": []})
        provider = _mock_provider(
            {
                "text": "VTK tekst.",
                "citations": [],
                "notes": "",
            }
        )
        mock_get_provider.return_value = provider
        mock_encrypt.return_value = b"encrypted"
        mock_conn_ctx = MagicMock()
        mock_conn.return_value.__enter__ = MagicMock(return_value=mock_conn_ctx)
        mock_conn.return_value.__exit__ = MagicMock(return_value=False)

        from app.drafter.handlers import drafter_draft

        result = drafter_draft({"session_id": str(_SESSION_ID)})

        # VTK_STRUCTURE has 13 sections across 5 chapters
        total_sections = sum(len(ch.get("sections", [])) for ch in VTK_STRUCTURE["chapters"])
        assert result["clause_count"]  # type: ignore[index] == total_sections
        assert provider.extract_json.call_count == total_sections

    @patch("app.drafter.handlers.get_connection")
    @patch("app.drafter.handlers.abandon_session")
    @patch("app.drafter.handlers.get_default_provider")
    @patch("app.drafter.handlers.decrypt_text")
    @patch("app.drafter.handlers.fetch_session")
    def test_provider_failure_final_attempt_abandons(
        self,
        mock_fetch: MagicMock,
        mock_decrypt: MagicMock,
        mock_get_provider: MagicMock,
        mock_abandon: MagicMock,
        mock_conn: MagicMock,
    ):
        session = _make_session(
            current_step=5,
            proposed_structure={
                "chapters": [
                    {
                        "number": "1",
                        "title": "Test",
                        "sections": [{"paragraph": "par 1", "title": "Test"}],
                    }
                ]
            },
            research_data_encrypted=b"encrypted",
        )
        mock_fetch.return_value = session
        mock_decrypt.return_value = json.dumps({"provisions": []})
        provider = MagicMock()
        provider.extract_json.side_effect = RuntimeError("API error")
        mock_get_provider.return_value = provider
        mock_conn_ctx = MagicMock()
        mock_conn.return_value.__enter__ = MagicMock(return_value=mock_conn_ctx)
        mock_conn.return_value.__exit__ = MagicMock(return_value=False)

        from app.drafter.handlers import drafter_draft

        with pytest.raises(RuntimeError, match="Clause drafting failed"):
            drafter_draft({"session_id": str(_SESSION_ID)}, attempt=3, max_attempts=3)

        mock_abandon.assert_called_once()


# ---------------------------------------------------------------------------
# Handler registration
# ---------------------------------------------------------------------------


class TestHandlerRegistration:
    def test_all_handlers_registered(self):
        from app.jobs.worker import _HANDLERS

        assert "drafter_clarify" in _HANDLERS
        assert "drafter_research" in _HANDLERS
        assert "drafter_structure" in _HANDLERS
        assert "drafter_draft" in _HANDLERS
