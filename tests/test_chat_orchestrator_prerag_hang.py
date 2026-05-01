"""Regression: ChatOrchestrator must persist the user message before
RAG retrieval, and an unresponsive retriever must NOT lose user input.

Closes #658.

Spins up a synthetic conversation, patches the retriever to hang
forever, runs the orchestrator with a short outer deadline, then
verifies the user-message row is in the DB. This is the data-loss
guarantee the rest of #658 ships.

Uses sync ``def test_X()`` with ``asyncio.run(_inner())`` for the
async portions, matching the convention in
``tests/test_chat_orchestrator.py`` (the project doesn't have
pytest-asyncio installed).
"""

from __future__ import annotations

import asyncio
import os
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def test_user_message_persists_when_retriever_hangs() -> None:
    """If the embedder/retriever hangs forever, the user's message
    must already be in the DB by the time RAG starts, and the turn
    must surface a timeout / error instead of silently swallowing the
    message.
    """
    if not os.getenv("DATABASE_URL"):
        pytest.skip("integration test — DATABASE_URL not set")

    from app.chat.orchestrator import ChatOrchestrator
    from app.db import get_connection

    user_id = uuid.uuid4()
    org_id = uuid.uuid4()
    conv_id = uuid.uuid4()

    # Bootstrap synthetic org / user / conversation. The schema uses
    # ``organizations`` (not ``orgs``) and requires ``slug`` + ``name``
    # to both be UNIQUE NOT NULL. ``users`` requires ``password_hash``,
    # ``full_name`` and a CHECK-constrained ``role``.
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO organizations (id, name, slug) VALUES (%s, %s, %s)",
            (org_id, f"test-org-658-{org_id}", f"test-org-658-{org_id}"),
        )
        conn.execute(
            "INSERT INTO users (id, email, password_hash, full_name, role, org_id) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (user_id, f"user-{user_id}@example.com", "x", "Test", "drafter", org_id),
        )
        conn.execute(
            "INSERT INTO conversations (id, user_id, org_id, title) VALUES (%s, %s, %s, %s)",
            (conv_id, user_id, org_id, "test conv 658"),
        )
        conn.commit()

    try:

        async def _run() -> list[dict[str, Any]]:
            hanging_retriever = MagicMock()

            async def _never_returns(*args: Any, **kwargs: Any) -> None:
                await asyncio.sleep(3600)

            hanging_retriever.retrieve = AsyncMock(side_effect=_never_returns)

            # MagicMock LLM is fine — once RAG times out, the orchestrator
            # tries to call ``llm.astream`` which will raise (MagicMock's
            # astream is not an async iterator). The orchestrator catches
            # that in its broad ``except Exception`` and emits an error
            # event. By then the user message is already persisted —
            # which is the whole point of this test.
            orchestrator = ChatOrchestrator(MagicMock(), MagicMock())
            events: list[dict[str, Any]] = []

            async def collect(event: dict[str, Any]) -> None:
                events.append(event)

            with patch.object(orchestrator, "_get_retriever", return_value=hanging_retriever):
                # The RAG retrieve has its own 15s deadline; we expect
                # the orchestrator to either time out RAG (proceed
                # without context) or hit the outer deadline. Either
                # way the user message must already be in the DB.
                try:
                    await asyncio.wait_for(
                        orchestrator.handle_message(
                            conv_id,
                            "Tere, see on test 658.",
                            {"id": str(user_id), "org_id": str(org_id)},
                            collect,
                        ),
                        timeout=20.0,  # > 15s RAG deadline, < 120s turn
                    )
                except TimeoutError:
                    pass  # Expected if downstream wedges

            return events

        events = asyncio.run(_run())

        # Verify: user message IS in the DB regardless of what
        # happened downstream. This is the data-loss guarantee #658
        # ships.
        with get_connection() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM messages WHERE conversation_id = %s AND role = 'user'",
                (conv_id,),
            ).fetchone()
        assert row is not None
        assert row[0] == 1, (
            f"User message must persist even when RAG hangs; got {row[0]} "
            f"messages instead. Events emitted: "
            f"{[e.get('type') for e in events]}"
        )
    finally:
        # Cleanup — always run even if the assertion failed.
        with get_connection() as conn:
            conn.execute("DELETE FROM messages WHERE conversation_id = %s", (conv_id,))
            conn.execute("DELETE FROM conversations WHERE id = %s", (conv_id,))
            conn.execute("DELETE FROM users WHERE id = %s", (user_id,))
            conn.execute("DELETE FROM organizations WHERE id = %s", (org_id,))
            conn.commit()
