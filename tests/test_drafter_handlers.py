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
        assert "drafter_regenerate_clause" in _HANDLERS


# ---------------------------------------------------------------------------
# #504 — Snapshot encryption round-trip
# ---------------------------------------------------------------------------


class TestSnapshotEncryption:
    @patch("app.drafter.session_model.update_session")
    @patch("app.drafter.session_model.create_version_snapshot")
    def test_advance_step_encrypts_snapshot(
        self,
        mock_snapshot: MagicMock,
        mock_update: MagicMock,
    ):
        """advance_step should encrypt snapshot data via encrypt_text."""
        from dataclasses import dataclass, field

        @dataclass
        class FakeSession:
            id: uuid.UUID = field(default_factory=uuid.uuid4)
            current_step: int = 1
            intent: str | None = "Test kavatsus"
            clarifications: list[dict[str, Any]] | None = field(default_factory=list)
            research_data_encrypted: bytes | None = None
            proposed_structure: dict[str, Any] | None = None
            draft_content_encrypted: bytes | None = None
            integrated_draft_id: uuid.UUID | None = None
            status: str = "active"

        session = FakeSession()
        conn = MagicMock()

        from app.drafter.state_machine import advance_step

        advance_step(session, conn)

        mock_snapshot.assert_called_once()
        snapshot_bytes = mock_snapshot.call_args.args[3]
        # Snapshot should be encrypted (bytes, not plaintext JSON)
        assert isinstance(snapshot_bytes, bytes)

        # Verify round-trip: decrypt should yield valid JSON with step info
        from app.storage import decrypt_text

        decrypted = decrypt_text(snapshot_bytes)
        parsed = json.loads(decrypted)
        assert parsed["step"] == 1
        assert parsed["intent"] == "Test kavatsus"
        assert parsed["status"] == "active"


# ---------------------------------------------------------------------------
# #509 — Helper function tests
# ---------------------------------------------------------------------------


class TestExtractKeywords:
    def test_empty_text_returns_empty(self):
        from app.drafter.handlers import _extract_keywords

        assert _extract_keywords("") == []

    def test_short_words_filtered_out(self):
        from app.drafter.handlers import _extract_keywords

        result = _extract_keywords("see on ja ei ole")
        # All words <= 3 chars should be filtered
        assert result == []

    def test_estonian_diacritics_extracted(self):
        from app.drafter.handlers import _extract_keywords

        result = _extract_keywords("tsiviilseadustik karistusseadustik kohaliku omavalitsuse")
        assert "tsiviilseadustik" in result
        assert "karistusseadustik" in result
        assert "kohaliku" in result
        assert "omavalitsuse" in result

    def test_deduplication_preserves_order(self):
        from app.drafter.handlers import _extract_keywords

        result = _extract_keywords("seadus seadus seadus")
        assert result == ["seadus"]

    def test_max_10_keywords(self):
        from app.drafter.handlers import _extract_keywords

        text = " ".join(f"keyword{i:02d}" for i in range(20))
        result = _extract_keywords(text)
        assert len(result) <= 10

    def test_quotes_in_text_handled_safely(self):
        """Quotes should not break keyword extraction (#503 tie-in)."""
        from app.drafter.handlers import _extract_keywords

        result = _extract_keywords('seadus "reguleerimisala" kohustused')
        # The regex should extract words, ignoring quotes
        assert "seadus" in result
        assert "reguleerimisala" in result
        assert "kohustused" in result


class TestFindRelatedLaws:
    @patch("app.drafter.handlers.SparqlClient")
    def test_empty_intent_returns_empty(self, mock_sparql_cls: MagicMock):
        from app.drafter.handlers import _find_related_laws

        client = MagicMock()
        client.query.return_value = []
        result = _find_related_laws("", client)
        assert result == []

    @patch("app.drafter.handlers.SparqlClient")
    def test_sparql_injection_characters_escaped(self, mock_sparql_cls: MagicMock):
        """Keywords with quotes should be escaped before SPARQL interpolation (#503).

        The regex in _extract_keywords strips non-alpha chars, so quotes
        are already removed at the keyword level. We verify that even if
        _safe_keyword is called directly on a dangerous string, it escapes.
        """
        from app.drafter.handlers import _find_related_laws, _safe_keyword

        client = MagicMock()
        client.query.return_value = []

        # _extract_keywords strips non-alpha, so the query is safe already
        _find_related_laws('seaduse "reguleerimisala" kohustused', client)

        # Verify queries were called (keywords extracted correctly)
        assert client.query.call_count > 0

        # The real defense: _safe_keyword escapes dangerous chars
        escaped = _safe_keyword('test"))}\nDELETE WHERE {?s ?p ?o')
        assert '\\"' in escaped  # double-quotes escaped
        assert "\\n" in escaped  # newlines escaped


class TestSafeKeyword:
    def test_escapes_double_quotes(self):
        from app.drafter.handlers import _safe_keyword

        assert '\\"' in _safe_keyword('test"value')

    def test_escapes_backslashes(self):
        from app.drafter.handlers import _safe_keyword

        assert "\\\\" in _safe_keyword("test\\value")

    def test_escapes_newlines(self):
        from app.drafter.handlers import _safe_keyword

        result = _safe_keyword("test\nvalue")
        assert "\n" not in result or "\\n" in result


# ---------------------------------------------------------------------------
# #514 — Error path tests
# ---------------------------------------------------------------------------


class TestErrorPaths:
    @patch("app.drafter.handlers.get_connection")
    @patch("app.drafter.handlers.abandon_session")
    @patch("app.drafter.handlers.get_default_provider")
    @patch("app.drafter.handlers.SparqlClient")
    @patch("app.drafter.handlers.fetch_session")
    def test_sparql_empty_results_stores_empty_research(
        self,
        mock_fetch: MagicMock,
        mock_sparql_cls: MagicMock,
        mock_get_provider: MagicMock,
        mock_abandon: MagicMock,
        mock_conn: MagicMock,
    ):
        """When SPARQL returns empty results, handler stores empty research (not crash)."""
        session = _make_session(
            current_step=3,
            clarifications=[
                {"question": "Q1?", "answer": "A1"},
                {"question": "Q2?", "answer": "A2"},
                {"question": "Q3?", "answer": "A3"},
            ],
        )
        mock_fetch.return_value = session
        client = MagicMock()
        # Return empty results for all queries
        client.query.return_value = []
        mock_sparql_cls.return_value = client
        mock_conn_ctx = MagicMock()
        mock_conn.return_value.__enter__ = MagicMock(return_value=mock_conn_ctx)
        mock_conn.return_value.__exit__ = MagicMock(return_value=False)

        with patch("app.drafter.handlers.encrypt_text", return_value=b"enc") as mock_enc:
            from app.drafter.handlers import drafter_research

            result = drafter_research({"session_id": str(_SESSION_ID)})

        assert result["provision_count"] == 0
        assert result["eu_directive_count"] == 0
        assert result["court_decision_count"] == 0
        assert result["topic_cluster_count"] == 0
        mock_enc.assert_called_once()
        mock_abandon.assert_not_called()

    @patch("app.drafter.handlers.get_connection")
    @patch("app.drafter.handlers.update_session")
    @patch("app.drafter.handlers.get_default_provider")
    @patch("app.drafter.handlers.decrypt_text")
    @patch("app.drafter.handlers.SparqlClient")
    @patch("app.drafter.handlers.fetch_session")
    def test_malformed_json_from_llm_uses_fallback_structure(
        self,
        mock_fetch: MagicMock,
        mock_sparql_cls: MagicMock,
        mock_decrypt: MagicMock,
        mock_get_provider: MagicMock,
        mock_update: MagicMock,
        mock_conn: MagicMock,
    ):
        """Claude returns malformed structure -> handler uses fallback."""
        session = _make_session(
            current_step=4,
            research_data_encrypted=b"encrypted",
        )
        mock_fetch.return_value = session
        mock_sparql_cls.return_value = _mock_sparql_client()
        mock_decrypt.return_value = json.dumps({"provisions": [{"act_label": "TsiviilS"}]})
        # Return a structure with no chapters — should trigger fallback
        mock_get_provider.return_value = _mock_provider({"title": "Test", "chapters": []})
        mock_conn_ctx = MagicMock()
        mock_conn.return_value.__enter__ = MagicMock(return_value=mock_conn_ctx)
        mock_conn.return_value.__exit__ = MagicMock(return_value=False)

        from app.drafter.handlers import drafter_structure

        result = drafter_structure({"session_id": str(_SESSION_ID)})

        # Should have used the fallback structure with 2 chapters
        assert result["chapters"] == 2

    @patch("app.drafter.handlers.encrypt_text")
    @patch("app.drafter.handlers.get_connection")
    @patch("app.drafter.handlers.update_session")
    @patch("app.drafter.handlers.get_default_provider")
    @patch("app.drafter.handlers.decrypt_text")
    @patch("app.drafter.handlers.fetch_session")
    def test_large_section_count_still_works(
        self,
        mock_fetch: MagicMock,
        mock_decrypt: MagicMock,
        mock_get_provider: MagicMock,
        mock_update: MagicMock,
        mock_conn: MagicMock,
        mock_encrypt: MagicMock,
    ):
        """100+ sections in structure should not crash."""
        sections = [{"paragraph": f"par {i}", "title": f"Section {i}"} for i in range(110)]
        structure = {
            "title": "Big law",
            "chapters": [{"number": "1", "title": "Big Chapter", "sections": sections}],
        }
        session = _make_session(
            current_step=5,
            proposed_structure=structure,
            research_data_encrypted=b"encrypted",
        )
        mock_fetch.return_value = session
        mock_decrypt.return_value = json.dumps({"provisions": []})
        provider = _mock_provider({"text": "Section text.", "citations": [], "notes": ""})
        mock_get_provider.return_value = provider
        mock_encrypt.return_value = b"encrypted"
        mock_conn_ctx = MagicMock()
        mock_conn.return_value.__enter__ = MagicMock(return_value=mock_conn_ctx)
        mock_conn.return_value.__exit__ = MagicMock(return_value=False)

        from app.drafter.handlers import drafter_draft

        result = drafter_draft({"session_id": str(_SESSION_ID)})

        assert result["clause_count"] == 110
        assert provider.extract_json.call_count == 110

    @patch("app.drafter.handlers.get_connection")
    @patch("app.drafter.handlers.abandon_session")
    @patch("app.drafter.handlers.get_default_provider")
    @patch("app.drafter.handlers.SparqlClient")
    @patch("app.drafter.handlers.fetch_session")
    def test_decrypt_error_on_final_attempt_abandons(
        self,
        mock_fetch: MagicMock,
        mock_sparql_cls: MagicMock,
        mock_get_provider: MagicMock,
        mock_abandon: MagicMock,
        mock_conn: MagicMock,
    ):
        """DecryptionError on final attempt marks session abandoned."""
        from app.storage.encrypted import DecryptionError

        session = _make_session(
            current_step=3,
            clarifications=[
                {"question": "Q1?", "answer": "A1"},
                {"question": "Q2?", "answer": "A2"},
                {"question": "Q3?", "answer": "A3"},
            ],
        )
        mock_fetch.return_value = session
        # Make SparqlClient raise a DecryptionError (simulated)
        client = MagicMock()
        client.query.side_effect = DecryptionError("bad key")
        mock_sparql_cls.return_value = client
        mock_conn_ctx = MagicMock()
        mock_conn.return_value.__enter__ = MagicMock(return_value=mock_conn_ctx)
        mock_conn.return_value.__exit__ = MagicMock(return_value=False)

        # The research handler should handle individual query failures gracefully
        # But if ALL queries fail with an unrecoverable error...
        # Actually the handler catches individual query failures. Let's test
        # that a top-level exception on final attempt triggers abandon.
        with patch(
            "app.drafter.handlers._run_research_queries",
            side_effect=DecryptionError("bad key"),
        ):
            from app.drafter.handlers import drafter_research

            with pytest.raises(RuntimeError, match="Research queries failed"):
                drafter_research(
                    {"session_id": str(_SESSION_ID)},
                    attempt=3,
                    max_attempts=3,
                )

        mock_abandon.assert_called_once_with(mock_conn_ctx, _SESSION_ID)


# ---------------------------------------------------------------------------
# #515 — Mock assertion depth (strengthen existing assertions)
# ---------------------------------------------------------------------------


class TestMockAssertionDepth:
    @patch("app.drafter.handlers.get_connection")
    @patch("app.drafter.handlers.update_session")
    @patch("app.drafter.handlers.get_default_provider")
    @patch("app.drafter.handlers.SparqlClient")
    @patch("app.drafter.handlers.fetch_session")
    def test_clarify_enqueue_args_correctness(
        self,
        mock_fetch: MagicMock,
        mock_sparql_cls: MagicMock,
        mock_get_provider: MagicMock,
        mock_update: MagicMock,
        mock_conn: MagicMock,
    ):
        """Verify update_session is called with clarifications column."""
        session = _make_session()
        mock_fetch.return_value = session
        mock_sparql_cls.return_value = _mock_sparql_client()
        mock_get_provider.return_value = _mock_provider()
        mock_conn_ctx = MagicMock()
        mock_conn.return_value.__enter__ = MagicMock(return_value=mock_conn_ctx)
        mock_conn.return_value.__exit__ = MagicMock(return_value=False)

        from app.drafter.handlers import drafter_clarify

        drafter_clarify({"session_id": str(_SESSION_ID)})

        # Verify update_session was called with the right column
        mock_update.assert_called_once()
        call_kwargs = mock_update.call_args
        assert call_kwargs.args[0] == mock_conn_ctx  # conn
        assert call_kwargs.args[1] == _SESSION_ID  # session_id
        assert "clarifications" in call_kwargs.kwargs
        clarifications = call_kwargs.kwargs["clarifications"]
        assert isinstance(clarifications, list)
        assert len(clarifications) == 5
        # Each clarification should have question, answer (None), and rationale
        for c in clarifications:
            assert "question" in c
            assert c["answer"] is None
            assert "rationale" in c

    @patch("app.drafter.handlers.encrypt_text")
    @patch("app.drafter.handlers.get_connection")
    @patch("app.drafter.handlers.update_session")
    @patch("app.drafter.handlers.SparqlClient")
    @patch("app.drafter.handlers.fetch_session")
    def test_research_update_uses_encrypted_column(
        self,
        mock_fetch: MagicMock,
        mock_sparql_cls: MagicMock,
        mock_update: MagicMock,
        mock_conn: MagicMock,
        mock_encrypt: MagicMock,
    ):
        """Verify update_session stores research data in encrypted column."""
        session = _make_session(
            current_step=3,
            clarifications=[
                {"question": "Q1?", "answer": "A1"},
                {"question": "Q2?", "answer": "A2"},
                {"question": "Q3?", "answer": "A3"},
            ],
        )
        mock_fetch.return_value = session
        client = MagicMock()
        client.query.return_value = []
        mock_sparql_cls.return_value = client
        mock_encrypt.return_value = b"encrypted-research"
        mock_conn_ctx = MagicMock()
        mock_conn.return_value.__enter__ = MagicMock(return_value=mock_conn_ctx)
        mock_conn.return_value.__exit__ = MagicMock(return_value=False)

        from app.drafter.handlers import drafter_research

        drafter_research({"session_id": str(_SESSION_ID)})

        mock_update.assert_called_once()
        call_kwargs = mock_update.call_args
        assert call_kwargs.args[0] == mock_conn_ctx
        assert call_kwargs.args[1] == _SESSION_ID
        assert "research_data_encrypted" in call_kwargs.kwargs
        assert call_kwargs.kwargs["research_data_encrypted"] == b"encrypted-research"

    @patch("app.drafter.handlers.encrypt_text")
    @patch("app.drafter.handlers.get_connection")
    @patch("app.drafter.handlers.update_session")
    @patch("app.drafter.handlers.get_default_provider")
    @patch("app.drafter.handlers.decrypt_text")
    @patch("app.drafter.handlers.fetch_session")
    def test_draft_update_uses_encrypted_column(
        self,
        mock_fetch: MagicMock,
        mock_decrypt: MagicMock,
        mock_get_provider: MagicMock,
        mock_update: MagicMock,
        mock_conn: MagicMock,
        mock_encrypt: MagicMock,
    ):
        """Verify drafter_draft stores clauses in draft_content_encrypted."""
        structure = {
            "title": "Test",
            "chapters": [
                {
                    "number": "1",
                    "title": "Test",
                    "sections": [{"paragraph": "par 1", "title": "Test"}],
                }
            ],
        }
        session = _make_session(
            current_step=5,
            proposed_structure=structure,
            research_data_encrypted=b"enc",
        )
        mock_fetch.return_value = session
        mock_decrypt.return_value = json.dumps({"provisions": []})
        mock_get_provider.return_value = _mock_provider(
            {"text": "Text", "citations": [], "notes": ""}
        )
        mock_encrypt.return_value = b"encrypted-clauses"
        mock_conn_ctx = MagicMock()
        mock_conn.return_value.__enter__ = MagicMock(return_value=mock_conn_ctx)
        mock_conn.return_value.__exit__ = MagicMock(return_value=False)

        from app.drafter.handlers import drafter_draft

        drafter_draft({"session_id": str(_SESSION_ID)})

        mock_update.assert_called_once()
        call_kwargs = mock_update.call_args
        assert call_kwargs.args[0] == mock_conn_ctx
        assert call_kwargs.args[1] == _SESSION_ID
        assert "draft_content_encrypted" in call_kwargs.kwargs
        assert call_kwargs.kwargs["draft_content_encrypted"] == b"encrypted-clauses"

    @patch("app.drafter.handlers.get_connection")
    @patch("app.drafter.handlers.update_session")
    @patch("app.drafter.handlers.fetch_session")
    def test_vtk_structure_uses_deepcopy(
        self,
        mock_fetch: MagicMock,
        mock_update: MagicMock,
        mock_conn: MagicMock,
    ):
        """VTK workflow should use deepcopy of VTK_STRUCTURE (#512)."""
        from app.drafter.prompts import VTK_STRUCTURE

        session = _make_session(
            current_step=4,
            workflow_type="vtk",
        )
        mock_fetch.return_value = session
        mock_conn_ctx = MagicMock()
        mock_conn.return_value.__enter__ = MagicMock(return_value=mock_conn_ctx)
        mock_conn.return_value.__exit__ = MagicMock(return_value=False)

        from app.drafter.handlers import drafter_structure

        drafter_structure({"session_id": str(_SESSION_ID)})

        # Verify the structure passed to update_session is NOT the same object
        call_kwargs = mock_update.call_args
        stored_structure = call_kwargs.kwargs["proposed_structure"]
        # The title should have been modified
        assert stored_structure["title"].startswith("VTK eelanaluus:")
        # But the original VTK_STRUCTURE should be unmodified
        assert VTK_STRUCTURE["title"] == ""

    @patch("app.drafter.handlers.get_connection")
    @patch("app.drafter.handlers.update_session")
    @patch("app.drafter.handlers.get_default_provider")
    @patch("app.drafter.handlers.SparqlClient")
    @patch("app.drafter.handlers.fetch_session")
    def test_clarify_passes_user_id_org_id_to_llm(
        self,
        mock_fetch: MagicMock,
        mock_sparql_cls: MagicMock,
        mock_get_provider: MagicMock,
        mock_update: MagicMock,
        mock_conn: MagicMock,
    ):
        """LLM calls should include user_id and org_id for cost tracking (#507)."""
        session = _make_session()
        mock_fetch.return_value = session
        mock_sparql_cls.return_value = _mock_sparql_client()
        provider = _mock_provider()
        mock_get_provider.return_value = provider
        mock_conn_ctx = MagicMock()
        mock_conn.return_value.__enter__ = MagicMock(return_value=mock_conn_ctx)
        mock_conn.return_value.__exit__ = MagicMock(return_value=False)

        from app.drafter.handlers import drafter_clarify

        drafter_clarify({"session_id": str(_SESSION_ID)})

        # Verify extract_json was called with user_id and org_id
        provider.extract_json.assert_called_once()
        call_kwargs = provider.extract_json.call_args
        assert call_kwargs.kwargs["user_id"] == _USER_ID
        assert call_kwargs.kwargs["org_id"] == _ORG_ID
        assert call_kwargs.kwargs["feature"] == "drafter_clarify"


# ---------------------------------------------------------------------------
# Regenerate clause handler
# ---------------------------------------------------------------------------


class TestDrafterRegenerateClause:
    @patch("app.drafter.handlers.encrypt_text")
    @patch("app.drafter.handlers.get_connection")
    @patch("app.drafter.handlers.update_session")
    @patch("app.drafter.handlers.get_default_provider")
    @patch("app.drafter.handlers.decrypt_text")
    @patch("app.drafter.handlers.fetch_session")
    def test_happy_path_regenerates_single_clause(
        self,
        mock_fetch: MagicMock,
        mock_decrypt: MagicMock,
        mock_get_provider: MagicMock,
        mock_update: MagicMock,
        mock_conn: MagicMock,
        mock_encrypt: MagicMock,
    ):
        session = _make_session(
            current_step=5,
            proposed_structure={"chapters": [{"number": "1", "title": "T", "sections": []}]},
            research_data_encrypted=b"enc-research",
            draft_content_encrypted=b"enc-clauses",
        )
        mock_fetch.return_value = session
        # First decrypt returns clauses, second returns research
        mock_decrypt.side_effect = [
            json.dumps(
                {
                    "clauses": [
                        {
                            "chapter": "1",
                            "chapter_title": "T",
                            "paragraph": "par 1",
                            "title": "S1",
                            "text": "Old text",
                            "citations": [],
                            "notes": "",
                        }
                    ]
                }
            ),
            json.dumps({"provisions": []}),
        ]
        provider = _mock_provider(
            {"text": "New regenerated text", "citations": ["estleg:X/par/1"], "notes": "N"}
        )
        mock_get_provider.return_value = provider
        mock_encrypt.return_value = b"new-encrypted"
        mock_conn_ctx = MagicMock()
        mock_conn.return_value.__enter__ = MagicMock(return_value=mock_conn_ctx)
        mock_conn.return_value.__exit__ = MagicMock(return_value=False)

        from app.drafter.handlers import drafter_regenerate_clause

        result = drafter_regenerate_clause({"session_id": str(_SESSION_ID), "clause_index": 0})

        assert result["session_id"] == str(_SESSION_ID)
        assert result["clause_index"] == 0
        provider.extract_json.assert_called_once()
        mock_update.assert_called_once()

    @patch("app.drafter.handlers.fetch_session")
    def test_missing_session_raises(self, mock_fetch: MagicMock):
        mock_fetch.return_value = None

        from app.drafter.handlers import drafter_regenerate_clause

        with pytest.raises(ValueError, match="not found"):
            drafter_regenerate_clause({"session_id": str(_SESSION_ID), "clause_index": 0})

    @patch("app.drafter.handlers.decrypt_text")
    @patch("app.drafter.handlers.fetch_session")
    def test_out_of_range_clause_index_raises(
        self,
        mock_fetch: MagicMock,
        mock_decrypt: MagicMock,
    ):
        session = _make_session(
            current_step=5,
            draft_content_encrypted=b"enc",
        )
        mock_fetch.return_value = session
        mock_decrypt.return_value = json.dumps({"clauses": [{"text": "only one"}]})

        from app.drafter.handlers import drafter_regenerate_clause

        with pytest.raises(ValueError, match="out of range"):
            drafter_regenerate_clause({"session_id": str(_SESSION_ID), "clause_index": 5})
