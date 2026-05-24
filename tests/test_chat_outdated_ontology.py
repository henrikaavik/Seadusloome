# pyright: reportArgumentType=false
"""Tests for the outdated-ontology drift banner (#352).

Covers:
  - Migration 037 adds ``messages.ontology_version`` (integration test;
    skipped when ``DATABASE_URL`` is not set).
  - The orchestrator stamps the live ontology snapshot tag on the
    persisted assistant row.
  - The conversation view renders the warning banner when at least one
    assistant message has a stale snapshot AND that message is
    ontology-grounded (RAG context or ontology tool children).
  - The banner is absent when versions match, when versions are
    ``"unknown"``, or when the assistant turn cited no ontology.
  - The banner carries the "Küsi uuesti" affordance pointing at
    ``/chat/{conv_id}?reask=1``.
  - The ``?reask=1`` flow pre-fills the textarea with the most recent
    user turn so the user can re-ask the same question against the
    fresh ontology.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from app.chat.models import Conversation, Message

# ---------------------------------------------------------------------------
# Fixtures and shared helpers (mirrors tests/test_chat_routes.py + _orchestrator.py)
# ---------------------------------------------------------------------------

_ORG_ID = "11111111-1111-1111-1111-111111111111"
_USER_ID = "33333333-3333-3333-3333-333333333333"
_CONV_ID = uuid.UUID("44444444-4444-4444-4444-444444444444")


def _authed_user() -> dict[str, Any]:
    return {
        "id": _USER_ID,
        "email": "kasutaja@seadusloome.ee",
        "full_name": "Test Kasutaja",
        "role": "drafter",
        "org_id": _ORG_ID,
    }


def _make_conversation(
    *,
    conv_id: uuid.UUID = _CONV_ID,
    org_id: str = _ORG_ID,
    user_id: str = _USER_ID,
    title: str = "Test vestlus",
) -> Conversation:
    now = datetime.now(UTC)
    return Conversation(
        id=conv_id,
        user_id=uuid.UUID(user_id),
        org_id=uuid.UUID(org_id),
        title=title,
        context_draft_id=None,
        created_at=now,
        updated_at=now,
    )


def _make_message(
    role: str = "user",
    content: str = "Tere",
    *,
    msg_id: uuid.UUID | None = None,
    ontology_version: str | None = None,
    rag_context: list[dict] | None = None,
    tool_name: str | None = None,
    parent_message_id: uuid.UUID | None = None,
) -> Message:
    now = datetime.now(UTC)
    return Message(
        id=msg_id or uuid.uuid4(),
        conversation_id=_CONV_ID,
        role=role,
        content=content,
        tool_name=tool_name,
        tool_input=None,
        tool_output=None,
        rag_context=rag_context,
        tokens_input=None,
        tokens_output=None,
        model=None,
        created_at=now,
        ontology_version=ontology_version,
        parent_message_id=parent_message_id,
    )


def _stub_provider() -> MagicMock:
    provider = MagicMock()
    provider.get_current_user.return_value = _authed_user()
    return provider


def _authed_client():
    from starlette.testclient import TestClient

    client = TestClient(
        __import__("app.main", fromlist=["app"]).app,
        follow_redirects=False,
    )
    client.cookies.set("access_token", "stub-token")
    return client


# ---------------------------------------------------------------------------
# Migration 037 — column existence (integration test)
# ---------------------------------------------------------------------------


class TestMigration037Column:
    """Verifies that migration 037 adds ``messages.ontology_version`` as a
    nullable TEXT column. Skipped when ``DATABASE_URL`` is unset (same
    pattern as ``tests/test_migration_025.py``)."""

    def test_messages_ontology_version_column_exists(self):
        if not os.getenv("DATABASE_URL"):
            pytest.skip("integration test — DATABASE_URL not set")
        from app.db import get_connection

        with get_connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT data_type, is_nullable
                FROM information_schema.columns
                WHERE table_name = 'messages'
                  AND column_name = 'ontology_version'
                """
            )
            row = cur.fetchone()
        assert row is not None, (
            "messages.ontology_version column missing — migration 037 not applied"
        )
        data_type, is_nullable = row
        assert data_type == "text", f"expected TEXT, got {data_type!r}"
        assert is_nullable == "YES", f"expected nullable, got {is_nullable!r}"


# ---------------------------------------------------------------------------
# Drift detector (pure function — no DB / TestClient required)
# ---------------------------------------------------------------------------


class TestDriftDetector:
    def test_no_drift_when_versions_match(self):
        from app.chat.routes import _conversation_has_outdated_ontology_citations

        msgs = [
            _make_message("user", "Mis on TsiviilS?"),
            _make_message(
                "assistant",
                "Vastus...",
                ontology_version="2026-05-20T10:00:00+00:00@90000",
                rag_context=[{"source_uri": "estleg:ks_113", "score": 0.9}],
            ),
        ]
        assert (
            _conversation_has_outdated_ontology_citations(msgs, "2026-05-20T10:00:00+00:00@90000")
            is False
        )

    def test_no_drift_when_assistant_version_is_unknown(self):
        """Pre-#352 rows carry NULL (or "unknown"); the banner must NOT fire."""
        from app.chat.routes import _conversation_has_outdated_ontology_citations

        msgs = [
            _make_message("user", "Mis on TsiviilS?"),
            _make_message(
                "assistant",
                "Vastus...",
                ontology_version=None,
                rag_context=[{"source_uri": "estleg:ks_113"}],
            ),
            _make_message(
                "assistant",
                "Vastus2...",
                ontology_version="unknown",
                rag_context=[{"source_uri": "estleg:ks_114"}],
            ),
        ]
        assert (
            _conversation_has_outdated_ontology_citations(msgs, "2026-05-21T10:00:00+00:00@90100")
            is False
        )

    def test_no_drift_when_current_version_is_unknown(self):
        """Live sync_log unavailable → "unknown"; do NOT show banner."""
        from app.chat.routes import _conversation_has_outdated_ontology_citations

        msgs = [
            _make_message(
                "assistant",
                "Vastus...",
                ontology_version="2026-05-20T10:00:00+00:00@90000",
                rag_context=[{"source_uri": "estleg:ks_113"}],
            ),
        ]
        assert _conversation_has_outdated_ontology_citations(msgs, "unknown") is False

    def test_no_drift_when_assistant_did_not_cite_ontology(self):
        """An older snapshot tag without RAG context AND without ontology
        tool children does not warrant a banner — the answer was not
        grounded on the ontology in the first place."""
        from app.chat.routes import _conversation_has_outdated_ontology_citations

        msgs = [
            _make_message("user", "Räägi mulle nalja"),
            _make_message(
                "assistant",
                "Naljakas vastus...",
                ontology_version="2026-05-20T10:00:00+00:00@90000",
                rag_context=None,
            ),
        ]
        assert (
            _conversation_has_outdated_ontology_citations(msgs, "2026-05-21T10:00:00+00:00@90100")
            is False
        )

    def test_drift_detected_when_rag_grounded_assistant_is_stale(self):
        from app.chat.routes import _conversation_has_outdated_ontology_citations

        msgs = [
            _make_message("user", "Mis on TsiviilS?"),
            _make_message(
                "assistant",
                "Vastus tugineb sätetele...",
                ontology_version="2026-05-20T10:00:00+00:00@90000",
                rag_context=[
                    {"source_uri": "estleg:ks_113", "score": 0.9},
                    {"source_uri": "estleg:ks_114", "score": 0.8},
                ],
            ),
        ]
        assert (
            _conversation_has_outdated_ontology_citations(msgs, "2026-05-21T11:00:00+00:00@90100")
            is True
        )

    def test_drift_detected_when_tool_grounded_assistant_is_stale(self):
        """An assistant turn that invoked ``search_provisions`` (or any
        other ontology tool) qualifies even without RAG context — the
        tool answer itself came from the ontology."""
        from app.chat.routes import _conversation_has_outdated_ontology_citations

        assistant_id = uuid.uuid4()
        msgs = [
            _make_message("user", "Otsi sätet TsiviilS § 113"),
            _make_message(
                "assistant",
                "Otsisin ja leidsin...",
                msg_id=assistant_id,
                ontology_version="2026-05-20T10:00:00+00:00@90000",
                rag_context=None,
            ),
            _make_message(
                "tool",
                '{"results": []}',
                tool_name="search_provisions",
                parent_message_id=assistant_id,
            ),
        ]
        assert (
            _conversation_has_outdated_ontology_citations(msgs, "2026-05-21T11:00:00+00:00@90100")
            is True
        )

    def test_drift_ignores_non_ontology_tools(self):
        """Hypothetical future non-ontology tool children should NOT
        trigger the banner. The current _ONTOLOGY_TOOL_NAMES set is the
        gatekeeper."""
        from app.chat.routes import _conversation_has_outdated_ontology_citations

        assistant_id = uuid.uuid4()
        msgs = [
            _make_message("user", "Mis kell on?"),
            _make_message(
                "assistant",
                "Kell on...",
                msg_id=assistant_id,
                ontology_version="2026-05-20T10:00:00+00:00@90000",
                rag_context=None,
            ),
            _make_message(
                "tool",
                '{"now": "2026-05-21"}',
                tool_name="get_current_time",
                parent_message_id=assistant_id,
            ),
        ]
        assert (
            _conversation_has_outdated_ontology_citations(msgs, "2026-05-21T11:00:00+00:00@90100")
            is False
        )


# ---------------------------------------------------------------------------
# Conversation view — end-to-end banner rendering
# ---------------------------------------------------------------------------


_OLD_VERSION = "2026-05-20T10:00:00+00:00@90000"
_NEW_VERSION = "2026-05-21T11:00:00+00:00@90100"


class TestConversationViewBanner:
    @patch("app.chat.routes.get_current_ontology_version", return_value=_NEW_VERSION)
    @patch("app.chat.routes._connect")
    @patch("app.auth.middleware._get_provider")
    def test_no_banner_when_no_messages(self, mock_provider, mock_connect, _mock_ver):
        mock_provider.return_value = _stub_provider()
        conn = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)

        conv = _make_conversation()
        with (
            patch("app.chat.routes.get_conversation", return_value=conv),
            patch("app.chat.routes.list_messages", return_value=[]),
        ):
            client = _authed_client()
            resp = client.get(f"/chat/{_CONV_ID}")
            assert resp.status_code == 200
            assert "Mõned viidatud allikad võivad olla aegunud" not in resp.text

    @patch("app.chat.routes.get_current_ontology_version", return_value=_NEW_VERSION)
    @patch("app.chat.routes._connect")
    @patch("app.auth.middleware._get_provider")
    def test_no_banner_when_all_versions_match(self, mock_provider, mock_connect, _mock_ver):
        mock_provider.return_value = _stub_provider()
        conn = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)

        conv = _make_conversation()
        msgs = [
            _make_message("user", "Mis on TsiviilS?"),
            _make_message(
                "assistant",
                "Vastus...",
                ontology_version=_NEW_VERSION,
                rag_context=[{"source_uri": "estleg:ks_113"}],
            ),
        ]
        with (
            patch("app.chat.routes.get_conversation", return_value=conv),
            patch("app.chat.routes.list_messages", return_value=msgs),
        ):
            client = _authed_client()
            resp = client.get(f"/chat/{_CONV_ID}")
            assert resp.status_code == 200
            assert "Mõned viidatud allikad võivad olla aegunud" not in resp.text

    @patch("app.chat.routes.get_current_ontology_version", return_value=_NEW_VERSION)
    @patch("app.chat.routes._connect")
    @patch("app.auth.middleware._get_provider")
    def test_banner_shown_when_assistant_is_stale(self, mock_provider, mock_connect, _mock_ver):
        mock_provider.return_value = _stub_provider()
        conn = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)

        conv = _make_conversation()
        msgs = [
            _make_message("user", "Mis on TsiviilS § 113?"),
            _make_message(
                "assistant",
                "Vastus tugineb sätetele...",
                ontology_version=_OLD_VERSION,
                rag_context=[{"source_uri": "estleg:ks_113", "score": 0.9}],
            ),
        ]
        with (
            patch("app.chat.routes.get_conversation", return_value=conv),
            patch("app.chat.routes.list_messages", return_value=msgs),
        ):
            client = _authed_client()
            resp = client.get(f"/chat/{_CONV_ID}")
            assert resp.status_code == 200
            assert "Mõned viidatud allikad võivad olla aegunud" in resp.text
            assert "Ontoloogia on uuenenud" in resp.text

    @patch("app.chat.routes.get_current_ontology_version", return_value=_NEW_VERSION)
    @patch("app.chat.routes._connect")
    @patch("app.auth.middleware._get_provider")
    def test_banner_contains_reask_link(self, mock_provider, mock_connect, _mock_ver):
        """DoD: the banner must link to a 'Küsi uuesti' affordance that
        the user can click to re-ask the question against fresh data."""
        mock_provider.return_value = _stub_provider()
        conn = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)

        conv = _make_conversation()
        msgs = [
            _make_message("user", "Mis on TsiviilS?"),
            _make_message(
                "assistant",
                "Vastus...",
                ontology_version=_OLD_VERSION,
                rag_context=[{"source_uri": "estleg:ks_113"}],
            ),
        ]
        with (
            patch("app.chat.routes.get_conversation", return_value=conv),
            patch("app.chat.routes.list_messages", return_value=msgs),
        ):
            client = _authed_client()
            resp = client.get(f"/chat/{_CONV_ID}")
            assert resp.status_code == 200
            assert "Küsi uuesti" in resp.text
            assert f"/chat/{_CONV_ID}?reask=1" in resp.text

    @patch("app.chat.routes.get_current_ontology_version", return_value=_NEW_VERSION)
    @patch("app.chat.routes._connect")
    @patch("app.auth.middleware._get_provider")
    def test_reask_prefills_textarea_with_last_user_message(
        self, mock_provider, mock_connect, _mock_ver
    ):
        """When the user clicks the banner's 'Küsi uuesti' link, the
        view prefills the textarea with the most recent persisted user
        message (so the user can choose whether to re-send it)."""
        mock_provider.return_value = _stub_provider()
        conn = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)

        conv = _make_conversation()
        msgs = [
            _make_message("user", "Mis on TsiviilS?"),
            _make_message(
                "assistant",
                "Vana vastus...",
                ontology_version=_OLD_VERSION,
                rag_context=[{"source_uri": "estleg:ks_113"}],
            ),
            _make_message("user", "Aga § 114?"),
            _make_message(
                "assistant",
                "Veel üks vastus...",
                ontology_version=_OLD_VERSION,
                rag_context=[{"source_uri": "estleg:ks_114"}],
            ),
        ]
        with (
            patch("app.chat.routes.get_conversation", return_value=conv),
            patch("app.chat.routes.list_messages", return_value=msgs),
        ):
            client = _authed_client()
            resp = client.get(f"/chat/{_CONV_ID}?reask=1")
            assert resp.status_code == 200
            # The most recent user turn ends up inside the textarea body.
            # FastHTML renders the textarea's positional string child as
            # its body content.
            assert "Aga § 114?" in resp.text

    @patch("app.chat.routes.get_current_ontology_version", return_value="unknown")
    @patch("app.chat.routes._connect")
    @patch("app.auth.middleware._get_provider")
    def test_no_banner_when_sync_log_unavailable(self, mock_provider, mock_connect, _mock_ver):
        """When the live ontology version resolves to 'unknown' (sync_log
        empty, DB error), we must NOT show a banner — that would be a
        false positive."""
        mock_provider.return_value = _stub_provider()
        conn = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)

        conv = _make_conversation()
        msgs = [
            _make_message("user", "Mis on TsiviilS?"),
            _make_message(
                "assistant",
                "Vastus...",
                ontology_version=_OLD_VERSION,
                rag_context=[{"source_uri": "estleg:ks_113"}],
            ),
        ]
        with (
            patch("app.chat.routes.get_conversation", return_value=conv),
            patch("app.chat.routes.list_messages", return_value=msgs),
        ):
            client = _authed_client()
            resp = client.get(f"/chat/{_CONV_ID}")
            assert resp.status_code == 200
            assert "Mõned viidatud allikad võivad olla aegunud" not in resp.text


# ---------------------------------------------------------------------------
# Orchestrator — stamps ontology_version on assistant message
# ---------------------------------------------------------------------------


_ORCH_USER_ID = "11111111-1111-1111-1111-111111111111"
_ORCH_ORG_ID = "22222222-2222-2222-2222-222222222222"
_ORCH_CONV_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")
_ORCH_MSG_ID = uuid.UUID("55555555-5555-5555-5555-555555555555")


def _orch_auth() -> dict[str, Any]:
    return {"id": _ORCH_USER_ID, "org_id": _ORCH_ORG_ID}


def _orch_conv() -> Conversation:
    now = datetime.now(UTC)
    return Conversation(
        id=_ORCH_CONV_ID,
        user_id=uuid.UUID(_ORCH_USER_ID),
        org_id=uuid.UUID(_ORCH_ORG_ID),
        title="Test",
        context_draft_id=None,
        created_at=now,
        updated_at=now,
    )


def _orch_base_conv_row(conv: Conversation) -> tuple[Any, ...]:
    return (
        conv.id,
        conv.user_id,
        conv.org_id,
        conv.title,
        None,
        conv.created_at,
        conv.updated_at,
    )


def _orch_user_row() -> tuple[Any, ...]:
    """A minimal post-026 messages row tuple for create_message RETURNING."""
    return (
        uuid.uuid4(),
        _ORCH_CONV_ID,
        "user",
        "x",
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        datetime.now(UTC),
        None,
        None,
        None,
        None,
    )


class TestOrchestratorStampsOntologyVersion:
    """The orchestrator must capture the live ontology version once per
    turn and pass it to ``create_message`` for the persisted assistant
    row. Mirrors the patterns in ``tests/test_chat_orchestrator.py``."""

    @patch(
        "app.chat.orchestrator.get_current_ontology_version",
        return_value=_NEW_VERSION,
    )
    @patch("app.chat.orchestrator.create_message")
    @patch("app.chat.orchestrator.get_connection")
    def test_assistant_message_carries_ontology_version(
        self, mock_get_conn, mock_create_msg, _mock_ver
    ):
        # Reuse the existing fake LLM / SPARQL classes from the
        # orchestrator test suite — they already implement every
        # abstract method on ``LLMProvider`` (complete / extract_json /
        # count_tokens / acomplete / astream).
        from app.chat.orchestrator import ChatOrchestrator
        from app.llm.provider import StreamEvent
        from tests.test_chat_orchestrator import FakeLLM, FakeSparql, _Collector

        conn = MagicMock()
        mock_get_conn.return_value.__enter__ = MagicMock(return_value=conn)
        mock_get_conn.return_value.__exit__ = MagicMock(return_value=False)

        conv = _orch_conv()
        call_counter = {"n": 0}

        def side_effect_fetchone():
            call_counter["n"] += 1
            if call_counter["n"] == 1:
                return _orch_base_conv_row(conv)
            return _orch_user_row()

        conn.execute.return_value.fetchone = side_effect_fetchone
        conn.execute.return_value.fetchall.return_value = []
        mock_create_msg.return_value = MagicMock(id=_ORCH_MSG_ID)

        events = [
            [
                StreamEvent(type="content", delta="Tere!"),
                StreamEvent(type="stop", tokens_input=10, tokens_output=5),
            ]
        ]
        orchestrator = ChatOrchestrator(FakeLLM(events), FakeSparql())  # type: ignore[arg-type]
        collector = _Collector()
        asyncio.run(orchestrator.handle_message(_ORCH_CONV_ID, "Tere", _orch_auth(), collector))

        assistant_calls = [
            c for c in mock_create_msg.call_args_list if c.args[2:3] == ("assistant",)
        ]
        assert assistant_calls, "expected at least one assistant create_message call"
        assistant_call = assistant_calls[-1]
        assert assistant_call.kwargs.get("ontology_version") == _NEW_VERSION, (
            "assistant message must be stamped with the live ontology "
            f"snapshot tag — got {assistant_call.kwargs.get('ontology_version')!r}"
        )


# ---------------------------------------------------------------------------
# Cross-org isolation (regression guard — banner must not leak)
# ---------------------------------------------------------------------------


_OTHER_ORG_ID = "22222222-2222-2222-2222-222222222222"


class TestCrossOrgIsolation:
    """Adding the banner must not relax the owner-only access check on
    the conversation view. A user from a different org gets the same
    not-found response they got before #352."""

    @patch("app.chat.routes._connect")
    @patch("app.auth.middleware._get_provider")
    def test_cross_org_still_returns_not_found(self, mock_provider, mock_connect):
        mock_provider.return_value = _stub_provider()
        conn = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)

        other_user = "99999999-9999-9999-9999-999999999999"
        conv = _make_conversation(org_id=_OTHER_ORG_ID, user_id=other_user)
        with patch("app.chat.routes.get_conversation", return_value=conv):
            client = _authed_client()
            resp = client.get(f"/chat/{_CONV_ID}")
            assert resp.status_code == 404
            # And the banner text must NOT appear on the not-found page.
            assert "Mõned viidatud allikad võivad olla aegunud" not in resp.text
