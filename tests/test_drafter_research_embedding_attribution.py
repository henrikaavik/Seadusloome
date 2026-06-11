"""Drafter research embedding spend is attributed to the session owner (#854).

``drafter_research`` → ``_find_similar_provisions`` →
``app.analyysikeskus.similarity.find_similar`` → ``Retriever`` →
``VoyageProvider`` is a chain whose middle module we can't thread kwargs
through, so the call site wraps the lookup in
:func:`app.rag.embedding.embedding_attribution`. These tests prove the
context is active for the duration of ``find_similar`` (including across
the sync→async ``asyncio.run`` bridge that the similarity module uses)
and stamps the Voyage ``llm_usage`` row with the drafter session's
user/org and the ``drafter_research_embedding`` feature label — instead
of the pre-#854 anonymous ``feature="embedding"`` rows.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from app.drafter.session_model import DraftingSession

_SESSION_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
_USER_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")
_ORG_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")


def _make_session() -> DraftingSession:
    now = datetime.now(UTC)
    return DraftingSession(
        id=_SESSION_ID,
        user_id=_USER_ID,
        org_id=_ORG_ID,
        workflow_type="full_law",
        current_step=3,
        intent="Soovin luua tehisintellekti seaduse",
        clarifications=[{"question": "Q1?", "answer": "A1"}],
        research_data_encrypted=None,
        proposed_structure=None,
        draft_content_encrypted=None,
        integrated_draft_id=None,
        status="active",
        created_at=now,
        updated_at=now,
    )


class TestDrafterResearchEmbeddingAttribution:
    @patch("app.analyysikeskus.similarity.find_similar")
    @patch("app.drafter.handlers.encrypt_text")
    @patch("app.drafter.handlers.get_connection")
    @patch("app.drafter.handlers.update_session")
    @patch("app.drafter.handlers.SparqlClient")
    @patch("app.drafter.handlers.fetch_session")
    def test_find_similar_runs_inside_attribution_context(
        self,
        mock_fetch: MagicMock,
        mock_sparql_cls: MagicMock,
        mock_update: MagicMock,
        mock_conn: MagicMock,
        mock_encrypt: MagicMock,
        mock_find_similar: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """A Voyage cost log fired anywhere inside ``find_similar`` —
        even across the similarity module's ``asyncio.run`` bridge —
        carries the session owner + drafter feature label."""
        monkeypatch.delenv("VOYAGE_API_KEY", raising=False)

        mock_fetch.return_value = _make_session()
        client = MagicMock()
        client.query.return_value = [
            {"provision": "uri:1", "label": "Provision 1", "actLabel": "Act 1"},
        ]
        mock_sparql_cls.return_value = client
        mock_encrypt.return_value = b"encrypted-data"
        mock_conn.return_value.__enter__ = MagicMock(return_value=MagicMock())
        mock_conn.return_value.__exit__ = MagicMock(return_value=False)

        logged_usage: list[dict[str, Any]] = []

        def _fake_find_similar(**kwargs: Any) -> list[Any]:
            """Stand-in for the analyysikeskus chain: trigger a real
            ``VoyageProvider._log_cost`` via the same sync→async bridge
            the similarity module uses."""
            from app.rag.embedding import VoyageProvider

            provider = VoyageProvider()  # stub mode; _log_cost is real

            async def _run() -> None:
                with patch(
                    "app.llm.cost_tracker.log_usage",
                    side_effect=lambda **kw: logged_usage.append(kw),
                ):
                    provider._log_cost(1234)

            asyncio.run(_run())
            return []

        mock_find_similar.side_effect = _fake_find_similar

        from app.drafter.handlers import drafter_research

        result = drafter_research({"session_id": str(_SESSION_ID)})

        assert result is not None
        assert result["session_id"] == str(_SESSION_ID)
        mock_find_similar.assert_called()
        assert logged_usage, "expected the fake find_similar to log Voyage usage"
        usage = logged_usage[0]
        assert usage["user_id"] == _USER_ID
        assert usage["org_id"] == _ORG_ID
        assert usage["feature"] == "drafter_research_embedding"
        assert usage["provider"] == "voyage"
        assert usage["tokens_input"] == 1234

    @patch("app.analyysikeskus.similarity.find_similar")
    @patch("app.drafter.handlers.encrypt_text")
    @patch("app.drafter.handlers.get_connection")
    @patch("app.drafter.handlers.update_session")
    @patch("app.drafter.handlers.SparqlClient")
    @patch("app.drafter.handlers.fetch_session")
    def test_attribution_context_cleared_after_research(
        self,
        mock_fetch: MagicMock,
        mock_sparql_cls: MagicMock,
        mock_update: MagicMock,
        mock_conn: MagicMock,
        mock_encrypt: MagicMock,
        mock_find_similar: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """The contextvar must not leak past the enrichment block — a
        later unrelated embedding stays unattributed."""
        monkeypatch.delenv("VOYAGE_API_KEY", raising=False)

        mock_fetch.return_value = _make_session()
        client = MagicMock()
        client.query.return_value = [
            {"provision": "uri:1", "label": "Provision 1", "actLabel": "Act 1"},
        ]
        mock_sparql_cls.return_value = client
        mock_encrypt.return_value = b"encrypted-data"
        mock_conn.return_value.__enter__ = MagicMock(return_value=MagicMock())
        mock_conn.return_value.__exit__ = MagicMock(return_value=False)
        mock_find_similar.return_value = []

        from app.drafter.handlers import drafter_research
        from app.rag.embedding import VoyageProvider

        drafter_research({"session_id": str(_SESSION_ID)})

        logged_usage: list[dict[str, Any]] = []
        provider = VoyageProvider()
        with patch(
            "app.llm.cost_tracker.log_usage",
            side_effect=lambda **kw: logged_usage.append(kw),
        ):
            provider._log_cost(10)

        assert logged_usage[0]["user_id"] is None
        assert logged_usage[0]["org_id"] is None
        assert logged_usage[0]["feature"] == "embedding"
