"""Unit tests for ``app.docs.websocket.ws_draft_status`` (#608).

Tests the message-level handler in isolation by passing a stub
``send`` callable + an explicit ``scope`` dict so we don't need a
real ASGI fixture. The full ``register_draft_ws_routes`` wrapper that
extracts JWT cookies is exercised at a higher level by integration
tests in production.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from unittest.mock import patch

import pytest

from app.docs import status_events
from app.docs import websocket as draft_ws


def _reset_subscribers() -> None:
    status_events._subscribers.clear()


@pytest.fixture(autouse=True)
def _isolate_subscribers():
    _reset_subscribers()
    yield
    _reset_subscribers()


def _make_draft(*, status: str = "extracting", org_id: str = "org-1"):
    """Tiny stand-in for a Draft row — only the attributes the handler reads.

    Uses ``SimpleNamespace`` rather than a ``class _Draft: pass`` shell
    because pyright's stricter mode flags dynamic attribute assignment
    on a bare class as ``reportAttributeAccessIssue``.
    """
    from types import SimpleNamespace

    return SimpleNamespace(
        id=uuid.uuid4(),
        status=status,
        org_id=org_id,
        error_message=None,
    )


class TestSubscribeMessageValidation:
    def test_invalid_json_yields_error_event(self):
        async def _run() -> list[str]:
            received: list[str] = []

            async def send(p: str) -> None:
                received.append(p)

            await draft_ws.ws_draft_status("not-json{", send, scope={})
            return received

        received = asyncio.run(_run())
        assert any("Vigane JSON" in r for r in received)

    def test_non_dict_message_yields_error_event(self):
        async def _run() -> list[str]:
            received: list[str] = []

            async def send(p: str) -> None:
                received.append(p)

            await draft_ws.ws_draft_status('["not a dict"]', send, scope={})
            return received

        received = asyncio.run(_run())
        messages = [json.loads(r).get("message", "") for r in received]
        assert any("Vigane sõnum" in m for m in messages)

    def test_unknown_message_type_silently_ignored(self):
        """Forward-compatibility: sending a future message type
        (e.g. 'unsubscribe') must not surface an error to the user."""

        async def _run() -> list[str]:
            received: list[str] = []

            async def send(p: str) -> None:
                received.append(p)

            await draft_ws.ws_draft_status(
                json.dumps({"type": "unsubscribe", "draft_id": str(uuid.uuid4())}),
                send,
                scope={},
            )
            return received

        received = asyncio.run(_run())
        assert received == []

    def test_subscribe_without_draft_id_yields_error(self):
        async def _run() -> list[str]:
            received: list[str] = []

            async def send(p: str) -> None:
                received.append(p)

            await draft_ws.ws_draft_status(
                json.dumps({"type": "subscribe"}),
                send,
                scope={"auth": {"id": "u1"}},
            )
            return received

        received = asyncio.run(_run())
        assert any("draft_id" in r for r in received)

    def test_subscribe_with_invalid_uuid_yields_error(self):
        async def _run() -> list[str]:
            received: list[str] = []

            async def send(p: str) -> None:
                received.append(p)

            await draft_ws.ws_draft_status(
                json.dumps({"type": "subscribe", "draft_id": "not-a-uuid"}),
                send,
                scope={"auth": {"id": "u1"}},
            )
            return received

        received = asyncio.run(_run())
        assert any("Vigane draft_id" in r for r in received)


class TestSubscribeAuth:
    def test_unauthenticated_subscribe_yields_error(self):
        async def _run() -> list[str]:
            received: list[str] = []

            async def send(p: str) -> None:
                received.append(p)

            await draft_ws.ws_draft_status(
                json.dumps({"type": "subscribe", "draft_id": str(uuid.uuid4())}),
                send,
                scope={},  # no auth
            )
            return received

        received = asyncio.run(_run())
        assert any("Autentimine" in r for r in received)

    def test_subscribe_to_missing_draft_yields_404_style_error(self):
        async def _run() -> list[str]:
            received: list[str] = []

            async def send(p: str) -> None:
                received.append(p)

            with patch("app.docs.websocket.fetch_draft", return_value=None):
                await draft_ws.ws_draft_status(
                    json.dumps({"type": "subscribe", "draft_id": str(uuid.uuid4())}),
                    send,
                    scope={"auth": {"id": "u1", "org_id": "org-1"}},
                )
            return received

        received = asyncio.run(_run())
        messages = [json.loads(r).get("message", "") for r in received]
        assert any("Eelnõu ei leitud" in m for m in messages)

    def test_subscribe_cross_org_yields_404_style_error_not_403(self):
        """Cross-org subscription is rejected with the same generic
        'not found' error as a missing draft so we never leak existence
        of out-of-scope drafts."""

        async def _run() -> list[str]:
            received: list[str] = []

            async def send(p: str) -> None:
                received.append(p)

            draft = _make_draft(org_id="other-org")
            with (
                patch("app.docs.websocket.fetch_draft", return_value=draft),
                patch("app.docs.websocket.can_view_draft", return_value=False),
            ):
                await draft_ws.ws_draft_status(
                    json.dumps({"type": "subscribe", "draft_id": str(draft.id)}),
                    send,
                    scope={"auth": {"id": "u1", "org_id": "org-1"}},
                )
            return received

        received = asyncio.run(_run())
        messages = [json.loads(r).get("message", "") for r in received]
        assert any("Eelnõu ei leitud" in m for m in messages)
        # No 'forbidden'-flavour error.
        assert not any("403" in m or "puudub" in m.lower() for m in messages)


class TestSubscribeSuccess:
    def test_subscribe_pushes_initial_state_and_registers(self):
        """A successful subscribe must:
        1. Send an ``initial`` event with the current draft status.
        2. Register the send callable in the subscriber registry.
        """

        async def _run() -> tuple[list[str], int, asyncio.Task]:
            received: list[str] = []

            async def send(p: str) -> None:
                received.append(p)

            draft = _make_draft(status="extracting", org_id="org-1")
            with (
                patch("app.docs.websocket.fetch_draft", return_value=draft),
                patch("app.docs.websocket.can_view_draft", return_value=True),
            ):
                handler_task = asyncio.create_task(
                    draft_ws.ws_draft_status(
                        json.dumps({"type": "subscribe", "draft_id": str(draft.id)}),
                        send,
                        scope={"auth": {"id": "u1", "org_id": "org-1"}},
                    )
                )
                # Give the handler a tick to hit the subscribe + initial-send.
                await asyncio.sleep(0.05)
                count = await status_events.subscriber_count(draft.id)
                return received, count, handler_task

        received, count, handler_task = asyncio.run(_coordinate_handler_lifecycle(_run()))

        assert count == 1, f"expected 1 subscriber after successful subscribe, got {count}"
        # Initial state event was sent.
        initial_events = [
            json.loads(r) for r in received if json.loads(r).get("type") == "initial"
        ]
        assert len(initial_events) == 1
        assert initial_events[0]["status"] == "extracting"


async def _coordinate_handler_lifecycle(run_coroutine):
    """Drive the test runner + cancel the handler task at the end so the
    test doesn't hang on the handler's ``await asyncio.Event().wait()``."""
    received, count, handler_task = await run_coroutine
    handler_task.cancel()
    try:
        await handler_task
    except asyncio.CancelledError:
        pass
    return received, count, handler_task


class TestSubscribeReceivesEmittedEvent:
    def test_status_event_after_subscribe_reaches_subscriber(self):
        """End-to-end: subscribe via the handler, emit via the pub/sub
        primitive, verify the subscriber receives the event."""

        async def _run() -> list[str]:
            received: list[str] = []

            async def send(p: str) -> None:
                received.append(p)

            draft = _make_draft(status="extracting", org_id="org-1")
            with (
                patch("app.docs.websocket.fetch_draft", return_value=draft),
                patch("app.docs.websocket.can_view_draft", return_value=True),
            ):
                handler_task = asyncio.create_task(
                    draft_ws.ws_draft_status(
                        json.dumps({"type": "subscribe", "draft_id": str(draft.id)}),
                        send,
                        scope={"auth": {"id": "u1", "org_id": "org-1"}},
                    )
                )
                await asyncio.sleep(0.05)  # let subscribe complete

                await status_events.emit(draft.id, type="status", status="analyzing")
                await asyncio.sleep(0.05)  # let dispatch complete

                handler_task.cancel()
                try:
                    await handler_task
                except asyncio.CancelledError:
                    pass

            return received

        received = asyncio.run(_run())
        # We expect both the initial state event AND the analyzing event.
        types = [json.loads(r).get("type") for r in received]
        assert "initial" in types
        # The status event from emit() carried our payload.
        status_events_received = [
            json.loads(r) for r in received if json.loads(r).get("type") == "status"
        ]
        assert any(e.get("status") == "analyzing" for e in status_events_received)
