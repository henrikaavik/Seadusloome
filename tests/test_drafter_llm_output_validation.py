# pyright: reportOptionalSubscript=false
"""#852 E2 — drafter handlers must FAIL on unusable LLM output.

The provider's ``extract_json`` returns ``{"error": ...}`` instead of
raising when the model's reply is not parseable JSON. These tests prove
the handlers treat that marker — and missing required fields such as
clause text — as a job failure that engages the retry budget and the
abandon-on-final-attempt gating, instead of persisting blank output that
the state machine would wave into review.

Mocking mirrors ``tests/test_drafter_handlers.py``.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from app.drafter.handlers import LLMOutputError, _require_llm_json
from app.drafter.session_model import DraftingSession

_SESSION_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
_USER_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")
_ORG_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")

_STRUCTURE = {
    "title": "Test seadus",
    "chapters": [
        {
            "number": "1",
            "title": "Üldsätted",
            "sections": [{"paragraph": "§ 1", "title": "Reguleerimisala"}],
        }
    ],
}


def _make_session(**overrides: Any) -> DraftingSession:
    now = datetime.now(UTC)
    fields: dict[str, Any] = dict(
        id=_SESSION_ID,
        user_id=_USER_ID,
        org_id=_ORG_ID,
        workflow_type="full_law",
        current_step=2,
        intent="Soovin luua tehisintellekti seaduse",
        clarifications=[],
        research_data_encrypted=None,
        proposed_structure=None,
        draft_content_encrypted=None,
        integrated_draft_id=None,
        status="active",
        created_at=now,
        updated_at=now,
    )
    fields.update(overrides)
    return DraftingSession(**fields)


def _provider(payload: dict[str, Any]) -> MagicMock:
    provider = MagicMock()
    provider.extract_json.return_value = payload
    return provider


def _wire_conn(mock_conn: MagicMock) -> None:
    ctx = MagicMock()
    mock_conn.return_value.__enter__ = MagicMock(return_value=ctx)
    mock_conn.return_value.__exit__ = MagicMock(return_value=False)


# ---------------------------------------------------------------------------
# _require_llm_json contract
# ---------------------------------------------------------------------------


class TestRequireLlmJson:
    def test_error_marker_raises(self):
        with pytest.raises(LLMOutputError, match="could not be parsed"):
            _require_llm_json({"error": "failed to parse"}, context="x")

    def test_non_dict_raises(self):
        with pytest.raises(LLMOutputError, match="non-dict"):
            _require_llm_json(["not", "a", "dict"], context="x")

    def test_stub_payload_passes(self):
        payload = {"stub": True, "prompt": "..."}
        assert _require_llm_json(payload, context="x") is payload

    def test_valid_payload_passes(self):
        payload = {"text": "Sisu."}
        assert _require_llm_json(payload, context="x") is payload

    def test_is_runtime_error_subclass(self):
        """Existing except/re-wrap paths catch RuntimeError."""
        assert issubclass(LLMOutputError, RuntimeError)


# ---------------------------------------------------------------------------
# drafter_clarify
# ---------------------------------------------------------------------------


class TestClarifyParseFailure:
    @patch("app.drafter.handlers.get_connection")
    @patch("app.drafter.handlers.abandon_session")
    @patch("app.drafter.handlers.update_session")
    @patch("app.drafter.handlers.get_default_provider")
    @patch("app.drafter.handlers.SparqlClient")
    @patch("app.drafter.handlers.fetch_session")
    def test_error_payload_raises_and_never_persists(
        self,
        mock_fetch: MagicMock,
        mock_sparql_cls: MagicMock,
        mock_get_provider: MagicMock,
        mock_update: MagicMock,
        mock_abandon: MagicMock,
        mock_conn: MagicMock,
    ):
        mock_fetch.return_value = _make_session()
        mock_sparql_cls.return_value = MagicMock(query=MagicMock(return_value=[]))
        mock_get_provider.return_value = _provider({"error": "failed to parse"})
        _wire_conn(mock_conn)

        from app.drafter.handlers import drafter_clarify

        with pytest.raises(LLMOutputError):
            drafter_clarify({"session_id": str(_SESSION_ID)}, attempt=1, max_attempts=3)

        mock_update.assert_not_called()
        # Attempt 1 of 3: retry budget remains, no abandon yet.
        mock_abandon.assert_not_called()

    @patch("app.drafter.handlers.get_connection")
    @patch("app.drafter.handlers.abandon_session")
    @patch("app.drafter.handlers.update_session")
    @patch("app.drafter.handlers.get_default_provider")
    @patch("app.drafter.handlers.SparqlClient")
    @patch("app.drafter.handlers.fetch_session")
    def test_error_payload_final_attempt_abandons(
        self,
        mock_fetch: MagicMock,
        mock_sparql_cls: MagicMock,
        mock_get_provider: MagicMock,
        mock_update: MagicMock,
        mock_abandon: MagicMock,
        mock_conn: MagicMock,
    ):
        mock_fetch.return_value = _make_session()
        mock_sparql_cls.return_value = MagicMock(query=MagicMock(return_value=[]))
        mock_get_provider.return_value = _provider({"error": "failed to parse"})
        _wire_conn(mock_conn)

        from app.drafter.handlers import drafter_clarify

        with pytest.raises(LLMOutputError):
            drafter_clarify({"session_id": str(_SESSION_ID)}, attempt=3, max_attempts=3)

        mock_update.assert_not_called()
        mock_abandon.assert_called_once()

    @patch("app.drafter.handlers.get_connection")
    @patch("app.drafter.handlers.abandon_session")
    @patch("app.drafter.handlers.update_session")
    @patch("app.drafter.handlers.get_default_provider")
    @patch("app.drafter.handlers.SparqlClient")
    @patch("app.drafter.handlers.fetch_session")
    def test_persist_failure_is_gated_too(
        self,
        mock_fetch: MagicMock,
        mock_sparql_cls: MagicMock,
        mock_get_provider: MagicMock,
        mock_update: MagicMock,
        mock_abandon: MagicMock,
        mock_conn: MagicMock,
    ):
        """Post-LLM persistence now sits inside the gated region: a DB
        write failure on the final attempt abandons the session instead
        of bypassing the gate (the pre-#852 behaviour)."""
        mock_fetch.return_value = _make_session()
        mock_sparql_cls.return_value = MagicMock(query=MagicMock(return_value=[]))
        mock_get_provider.return_value = _provider(
            {"questions": [{"question": "K?", "rationale": "r"}]}
        )
        _wire_conn(mock_conn)
        mock_update.side_effect = RuntimeError("db write failed")

        from app.drafter.handlers import drafter_clarify

        with pytest.raises(RuntimeError, match="db write failed"):
            drafter_clarify({"session_id": str(_SESSION_ID)}, attempt=3, max_attempts=3)

        mock_abandon.assert_called_once()


# ---------------------------------------------------------------------------
# drafter_structure
# ---------------------------------------------------------------------------


class TestStructureParseFailure:
    @patch("app.drafter.handlers.get_connection")
    @patch("app.drafter.handlers.abandon_session")
    @patch("app.drafter.handlers.update_session")
    @patch("app.drafter.handlers.get_default_provider")
    @patch("app.drafter.handlers.SparqlClient")
    @patch("app.drafter.handlers.decrypt_text")
    @patch("app.drafter.handlers.fetch_session")
    def test_error_payload_raises_instead_of_fallback_structure(
        self,
        mock_fetch: MagicMock,
        mock_decrypt: MagicMock,
        mock_sparql_cls: MagicMock,
        mock_get_provider: MagicMock,
        mock_update: MagicMock,
        mock_abandon: MagicMock,
        mock_conn: MagicMock,
    ):
        """A parse failure must NOT be laundered into the fallback
        skeleton structure — it raises and consumes a retry."""
        mock_fetch.return_value = _make_session(
            current_step=4, research_data_encrypted=b"encrypted"
        )
        mock_decrypt.return_value = json.dumps({"provisions": []})
        mock_sparql_cls.return_value = MagicMock(query=MagicMock(return_value=[]))
        mock_get_provider.return_value = _provider({"error": "failed to parse"})
        _wire_conn(mock_conn)

        from app.drafter.handlers import drafter_structure

        with pytest.raises(LLMOutputError):
            drafter_structure({"session_id": str(_SESSION_ID)}, attempt=3, max_attempts=3)

        mock_update.assert_not_called()
        mock_abandon.assert_called_once()

    @patch("app.drafter.handlers.get_connection")
    @patch("app.drafter.handlers.update_session")
    @patch("app.drafter.handlers.get_default_provider")
    @patch("app.drafter.handlers.SparqlClient")
    @patch("app.drafter.handlers.fetch_session")
    def test_stub_payload_still_uses_fallback(
        self,
        mock_fetch: MagicMock,
        mock_sparql_cls: MagicMock,
        mock_get_provider: MagicMock,
        mock_update: MagicMock,
        mock_conn: MagicMock,
    ):
        """Stub mode (no API key) keeps working via the fallback skeleton."""
        mock_fetch.return_value = _make_session(current_step=4)
        mock_sparql_cls.return_value = MagicMock(query=MagicMock(return_value=[]))
        mock_get_provider.return_value = _provider({"stub": True, "prompt": "..."})
        _wire_conn(mock_conn)

        from app.drafter.handlers import drafter_structure

        result = drafter_structure({"session_id": str(_SESSION_ID)})

        assert result["chapters"] == 2  # fallback skeleton
        mock_update.assert_called_once()


# ---------------------------------------------------------------------------
# drafter_draft
# ---------------------------------------------------------------------------


class TestDraftOutputValidation:
    @patch("app.drafter.handlers.encrypt_text")
    @patch("app.drafter.handlers.get_connection")
    @patch("app.drafter.handlers.abandon_session")
    @patch("app.drafter.handlers.update_session")
    @patch("app.drafter.handlers.get_default_provider")
    @patch("app.drafter.handlers.decrypt_text")
    @patch("app.drafter.handlers.fetch_session")
    def test_error_payload_fails_job_and_persists_nothing(
        self,
        mock_fetch: MagicMock,
        mock_decrypt: MagicMock,
        mock_get_provider: MagicMock,
        mock_update: MagicMock,
        mock_abandon: MagicMock,
        mock_conn: MagicMock,
        mock_encrypt: MagicMock,
    ):
        """The exact E2 bug: ``{"error": ...}`` used to become a clause
        with ``text=""`` and the job "succeeded"."""
        mock_fetch.return_value = _make_session(
            current_step=5,
            proposed_structure=_STRUCTURE,
            research_data_encrypted=b"encrypted",
        )
        mock_decrypt.return_value = json.dumps({"provisions": []})
        mock_get_provider.return_value = _provider({"error": "failed to parse"})
        _wire_conn(mock_conn)

        from app.drafter.handlers import drafter_draft

        with pytest.raises(RuntimeError, match="Clause drafting failed"):
            drafter_draft({"session_id": str(_SESSION_ID)}, attempt=1, max_attempts=3)

        # Nothing persisted, nothing encrypted — no blank clauses on disk.
        mock_encrypt.assert_not_called()
        mock_update.assert_not_called()
        # Retry budget remains → not abandoned yet.
        mock_abandon.assert_not_called()

    @patch("app.drafter.handlers.encrypt_text")
    @patch("app.drafter.handlers.get_connection")
    @patch("app.drafter.handlers.abandon_session")
    @patch("app.drafter.handlers.update_session")
    @patch("app.drafter.handlers.get_default_provider")
    @patch("app.drafter.handlers.decrypt_text")
    @patch("app.drafter.handlers.fetch_session")
    def test_error_payload_final_attempt_abandons(
        self,
        mock_fetch: MagicMock,
        mock_decrypt: MagicMock,
        mock_get_provider: MagicMock,
        mock_update: MagicMock,
        mock_abandon: MagicMock,
        mock_conn: MagicMock,
        mock_encrypt: MagicMock,
    ):
        mock_fetch.return_value = _make_session(
            current_step=5,
            proposed_structure=_STRUCTURE,
            research_data_encrypted=b"encrypted",
        )
        mock_decrypt.return_value = json.dumps({"provisions": []})
        mock_get_provider.return_value = _provider({"error": "failed to parse"})
        _wire_conn(mock_conn)

        from app.drafter.handlers import drafter_draft

        with pytest.raises(RuntimeError, match="Clause drafting failed"):
            drafter_draft({"session_id": str(_SESSION_ID)}, attempt=3, max_attempts=3)

        mock_encrypt.assert_not_called()
        mock_update.assert_not_called()
        mock_abandon.assert_called_once()

    @patch("app.drafter.handlers.encrypt_text")
    @patch("app.drafter.handlers.get_connection")
    @patch("app.drafter.handlers.update_session")
    @patch("app.drafter.handlers.get_default_provider")
    @patch("app.drafter.handlers.decrypt_text")
    @patch("app.drafter.handlers.fetch_session")
    def test_blank_clause_text_fails_job(
        self,
        mock_fetch: MagicMock,
        mock_decrypt: MagicMock,
        mock_get_provider: MagicMock,
        mock_update: MagicMock,
        mock_conn: MagicMock,
        mock_encrypt: MagicMock,
    ):
        """Valid JSON but no clause text — required-field validation."""
        mock_fetch.return_value = _make_session(
            current_step=5,
            proposed_structure=_STRUCTURE,
            research_data_encrypted=b"encrypted",
        )
        mock_decrypt.return_value = json.dumps({"provisions": []})
        mock_get_provider.return_value = _provider({"text": "   ", "citations": [], "notes": ""})
        _wire_conn(mock_conn)

        from app.drafter.handlers import drafter_draft

        with pytest.raises(RuntimeError, match="no clause text"):
            drafter_draft({"session_id": str(_SESSION_ID)})

        mock_encrypt.assert_not_called()
        mock_update.assert_not_called()

    @patch("app.drafter.handlers.encrypt_text")
    @patch("app.drafter.handlers.get_connection")
    @patch("app.drafter.handlers.update_session")
    @patch("app.drafter.handlers.get_default_provider")
    @patch("app.drafter.handlers.decrypt_text")
    @patch("app.drafter.handlers.fetch_session")
    def test_stub_payload_is_allowed_through(
        self,
        mock_fetch: MagicMock,
        mock_decrypt: MagicMock,
        mock_get_provider: MagicMock,
        mock_update: MagicMock,
        mock_conn: MagicMock,
        mock_encrypt: MagicMock,
    ):
        """Stub mode has no ``text`` field; the pipeline must still finish."""
        mock_fetch.return_value = _make_session(
            current_step=5,
            proposed_structure=_STRUCTURE,
            research_data_encrypted=b"encrypted",
        )
        mock_decrypt.return_value = json.dumps({"provisions": []})
        mock_get_provider.return_value = _provider({"stub": True, "prompt": "..."})
        mock_encrypt.return_value = b"encrypted-clauses"
        _wire_conn(mock_conn)

        from app.drafter.handlers import drafter_draft

        result = drafter_draft({"session_id": str(_SESSION_ID)})

        assert result["clause_count"] == 1
        mock_update.assert_called_once()

    @patch("app.drafter.handlers.encrypt_text")
    @patch("app.drafter.handlers.get_connection")
    @patch("app.drafter.handlers.update_session")
    @patch("app.drafter.handlers.get_default_provider")
    @patch("app.drafter.handlers.decrypt_text")
    @patch("app.drafter.handlers.fetch_session")
    def test_sectionless_structure_fails_instead_of_empty_review(
        self,
        mock_fetch: MagicMock,
        mock_decrypt: MagicMock,
        mock_get_provider: MagicMock,
        mock_update: MagicMock,
        mock_conn: MagicMock,
        mock_encrypt: MagicMock,
    ):
        """Zero clauses must never be persisted — the step-5→6 guard
        treats any encrypted content as proof of >= 1 clause."""
        mock_fetch.return_value = _make_session(
            current_step=5,
            proposed_structure={"title": "T", "chapters": [{"number": "1", "sections": []}]},
            research_data_encrypted=b"encrypted",
        )
        mock_decrypt.return_value = json.dumps({"provisions": []})
        mock_get_provider.return_value = _provider({"text": "Sisu."})
        _wire_conn(mock_conn)

        from app.drafter.handlers import drafter_draft

        with pytest.raises(RuntimeError, match="no clauses"):
            drafter_draft({"session_id": str(_SESSION_ID)})

        mock_encrypt.assert_not_called()
        mock_update.assert_not_called()


# ---------------------------------------------------------------------------
# drafter_regenerate_clause
# ---------------------------------------------------------------------------


class TestRegenerateParseFailure:
    @patch("app.drafter.handlers.get_connection")
    @patch("app.drafter.handlers.update_session")
    @patch("app.drafter.handlers.get_default_provider")
    @patch("app.drafter.handlers.decrypt_text")
    @patch("app.drafter.handlers.fetch_session")
    def test_error_payload_fails_instead_of_silent_keep(
        self,
        mock_fetch: MagicMock,
        mock_decrypt: MagicMock,
        mock_get_provider: MagicMock,
        mock_update: MagicMock,
        mock_conn: MagicMock,
    ):
        mock_fetch.return_value = _make_session(
            current_step=5,
            proposed_structure=_STRUCTURE,
            draft_content_encrypted=b"encrypted",
        )
        mock_decrypt.return_value = json.dumps(
            {
                "clauses": [
                    {
                        "chapter": "1",
                        "paragraph": "§ 1",
                        "title": "Reguleerimisala",
                        "text": "Vana tekst.",
                        "citations": [],
                        "notes": "",
                    }
                ]
            }
        )
        mock_get_provider.return_value = _provider({"error": "failed to parse"})
        _wire_conn(mock_conn)

        from app.drafter.handlers import drafter_regenerate_clause

        with pytest.raises(RuntimeError, match="Clause regeneration failed"):
            drafter_regenerate_clause({"session_id": str(_SESSION_ID), "clause_index": 0})

        mock_update.assert_not_called()


class TestRegenerateNonblankContract:
    """#852 review F2 — a blank regeneration must never overwrite a good
    clause. ``{"text": ""}`` passes the parse check (no ``error`` key)
    but used to land via ``result.get("text", ...)`` — the key IS
    present, so the blank value replaced the existing text and the job
    "succeeded"."""

    _OLD_TEXT = "Vana kehtiv tekst."

    def _drive(
        self,
        payload: dict[str, Any],
        mock_fetch: MagicMock,
        mock_decrypt: MagicMock,
        mock_get_provider: MagicMock,
        mock_conn: MagicMock,
    ) -> None:
        mock_fetch.return_value = _make_session(
            current_step=5,
            proposed_structure=_STRUCTURE,
            draft_content_encrypted=b"encrypted",
        )
        mock_decrypt.return_value = json.dumps(
            {
                "clauses": [
                    {
                        "chapter": "1",
                        "paragraph": "§ 1",
                        "title": "Reguleerimisala",
                        "text": self._OLD_TEXT,
                        "citations": [],
                        "notes": "",
                    }
                ]
            }
        )
        mock_get_provider.return_value = _provider(payload)
        _wire_conn(mock_conn)

    @patch("app.drafter.handlers.encrypt_text")
    @patch("app.drafter.handlers.get_connection")
    @patch("app.drafter.handlers.update_session")
    @patch("app.drafter.handlers.get_default_provider")
    @patch("app.drafter.handlers.decrypt_text")
    @patch("app.drafter.handlers.fetch_session")
    def test_blank_text_fails_and_leaves_clause_untouched(
        self,
        mock_fetch: MagicMock,
        mock_decrypt: MagicMock,
        mock_get_provider: MagicMock,
        mock_update: MagicMock,
        mock_conn: MagicMock,
        mock_encrypt: MagicMock,
    ):
        self._drive(
            {"text": "", "citations": [], "notes": ""},
            mock_fetch,
            mock_decrypt,
            mock_get_provider,
            mock_conn,
        )

        from app.drafter.handlers import drafter_regenerate_clause

        with pytest.raises(RuntimeError, match="no clause text"):
            drafter_regenerate_clause({"session_id": str(_SESSION_ID), "clause_index": 0})

        # Nothing re-encrypted, nothing persisted — the good clause
        # survives for the retry.
        mock_encrypt.assert_not_called()
        mock_update.assert_not_called()

    @patch("app.drafter.handlers.encrypt_text")
    @patch("app.drafter.handlers.get_connection")
    @patch("app.drafter.handlers.update_session")
    @patch("app.drafter.handlers.get_default_provider")
    @patch("app.drafter.handlers.decrypt_text")
    @patch("app.drafter.handlers.fetch_session")
    def test_whitespace_only_text_fails_too(
        self,
        mock_fetch: MagicMock,
        mock_decrypt: MagicMock,
        mock_get_provider: MagicMock,
        mock_update: MagicMock,
        mock_conn: MagicMock,
        mock_encrypt: MagicMock,
    ):
        self._drive(
            {"text": " \n\t ", "citations": [], "notes": ""},
            mock_fetch,
            mock_decrypt,
            mock_get_provider,
            mock_conn,
        )

        from app.drafter.handlers import drafter_regenerate_clause

        with pytest.raises(RuntimeError, match="no clause text"):
            drafter_regenerate_clause({"session_id": str(_SESSION_ID), "clause_index": 0})

        mock_encrypt.assert_not_called()
        mock_update.assert_not_called()

    @patch("app.drafter.handlers.encrypt_text")
    @patch("app.drafter.handlers.get_connection")
    @patch("app.drafter.handlers.update_session")
    @patch("app.drafter.handlers.get_default_provider")
    @patch("app.drafter.handlers.decrypt_text")
    @patch("app.drafter.handlers.fetch_session")
    def test_valid_regeneration_still_applies(
        self,
        mock_fetch: MagicMock,
        mock_decrypt: MagicMock,
        mock_get_provider: MagicMock,
        mock_update: MagicMock,
        mock_conn: MagicMock,
        mock_encrypt: MagicMock,
    ):
        self._drive(
            {"text": "Uus parem tekst.", "citations": [], "notes": ""},
            mock_fetch,
            mock_decrypt,
            mock_get_provider,
            mock_conn,
        )
        mock_encrypt.return_value = b"encrypted-clauses"

        from app.drafter.handlers import drafter_regenerate_clause

        result = drafter_regenerate_clause({"session_id": str(_SESSION_ID), "clause_index": 0})

        assert result["clause_index"] == 0
        # The persisted clause list carries the NEW text.
        persisted = json.loads(mock_encrypt.call_args.args[0])
        assert persisted["clauses"][0]["text"] == "Uus parem tekst."
        mock_update.assert_called_once()

    @patch("app.drafter.handlers.encrypt_text")
    @patch("app.drafter.handlers.get_connection")
    @patch("app.drafter.handlers.update_session")
    @patch("app.drafter.handlers.get_default_provider")
    @patch("app.drafter.handlers.decrypt_text")
    @patch("app.drafter.handlers.fetch_session")
    def test_stub_payload_keeps_existing_text(
        self,
        mock_fetch: MagicMock,
        mock_decrypt: MagicMock,
        mock_get_provider: MagicMock,
        mock_update: MagicMock,
        mock_conn: MagicMock,
        mock_encrypt: MagicMock,
    ):
        """Keyless local dev: stub regen succeeds without clobbering."""
        self._drive(
            {"stub": True, "prompt": "..."},
            mock_fetch,
            mock_decrypt,
            mock_get_provider,
            mock_conn,
        )
        mock_encrypt.return_value = b"encrypted-clauses"

        from app.drafter.handlers import drafter_regenerate_clause

        result = drafter_regenerate_clause({"session_id": str(_SESSION_ID), "clause_index": 0})

        assert result["clause_index"] == 0
        persisted = json.loads(mock_encrypt.call_args.args[0])
        assert persisted["clauses"][0]["text"] == self._OLD_TEXT
