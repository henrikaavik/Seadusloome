"""Unit tests for ``app.docs.status_events`` (#608).

Pure-asyncio tests against the in-memory pub/sub primitive. The WS
endpoint and the cross-thread emit_threadsafe path have their own
test files.
"""

from __future__ import annotations

import asyncio
import json
import threading
import uuid

import pytest

from app.docs import status_events


def _reset_subscribers() -> None:
    """Clear the module-level subscriber registry between tests so
    isolation isn't broken by leaked sets from earlier failures."""
    status_events._subscribers.clear()


@pytest.fixture(autouse=True)
def _isolate_subscribers():
    _reset_subscribers()
    yield
    _reset_subscribers()


class TestSubscribeEmit:
    def test_subscriber_receives_emitted_event(self):
        async def _run() -> list[str]:
            received: list[str] = []

            async def fake_send(payload: str) -> None:
                received.append(payload)

            draft_id = uuid.uuid4()
            await status_events.subscribe(draft_id, fake_send)
            await status_events.emit(draft_id, type="status", status="ready")
            return received

        received = asyncio.run(_run())
        assert len(received) == 1
        payload = json.loads(received[0])
        assert payload["type"] == "status"
        assert payload["status"] == "ready"

    def test_emit_with_no_subscribers_is_noop(self):
        """Emitting to a draft with no subscribers must not raise."""

        async def _run():
            await status_events.emit(uuid.uuid4(), type="status", status="ready")

        # Just shouldn't raise.
        asyncio.run(_run())

    def test_emit_includes_draft_id_in_payload(self):
        async def _run() -> str:
            received: list[str] = []

            async def fake_send(payload: str) -> None:
                received.append(payload)

            draft_id = uuid.uuid4()
            await status_events.subscribe(draft_id, fake_send)
            await status_events.emit(draft_id, type="status", status="extracting")
            return received[0]

        encoded = asyncio.run(_run())
        payload = json.loads(encoded)
        assert "draft_id" in payload

    def test_multiple_subscribers_all_receive(self):
        async def _run() -> tuple[list, list]:
            r1: list[str] = []
            r2: list[str] = []

            async def send1(p: str) -> None:
                r1.append(p)

            async def send2(p: str) -> None:
                r2.append(p)

            draft_id = uuid.uuid4()
            await status_events.subscribe(draft_id, send1)
            await status_events.subscribe(draft_id, send2)
            await status_events.emit(draft_id, type="status", status="ready")
            return r1, r2

        r1, r2 = asyncio.run(_run())
        assert len(r1) == 1 and len(r2) == 1


class TestUnsubscribe:
    def test_unsubscribed_does_not_receive(self):
        async def _run() -> list[str]:
            received: list[str] = []

            async def fake_send(payload: str) -> None:
                received.append(payload)

            draft_id = uuid.uuid4()
            await status_events.subscribe(draft_id, fake_send)
            await status_events.unsubscribe(draft_id, fake_send)
            await status_events.emit(draft_id, type="status", status="ready")
            return received

        received = asyncio.run(_run())
        assert received == []

    def test_unsubscribe_unknown_callable_is_idempotent(self):
        """Unsubscribing a callable that was never subscribed must
        not raise — common race when a connection drops mid-handshake."""

        async def _run():
            async def fake_send(payload: str) -> None:
                pass

            await status_events.unsubscribe(uuid.uuid4(), fake_send)

        asyncio.run(_run())  # must not raise

    def test_subscriber_count_reflects_registry(self):
        async def _run() -> tuple[int, int, int]:
            async def fake_send(payload: str) -> None:
                pass

            draft_id = uuid.uuid4()
            zero = await status_events.subscriber_count(draft_id)
            await status_events.subscribe(draft_id, fake_send)
            one = await status_events.subscriber_count(draft_id)
            await status_events.unsubscribe(draft_id, fake_send)
            zero_again = await status_events.subscriber_count(draft_id)
            return zero, one, zero_again

        before, during, after = asyncio.run(_run())
        assert (before, during, after) == (0, 1, 0)


class TestDeadSubscriberReaping:
    def test_failing_subscriber_is_removed(self):
        """When a send raises, the subscriber must be removed so it
        doesn't continue to break future emits."""

        async def _run() -> tuple[int, list[str]]:
            healthy_received: list[str] = []

            async def healthy_send(payload: str) -> None:
                healthy_received.append(payload)

            async def dead_send(payload: str) -> None:
                raise RuntimeError("socket closed")

            draft_id = uuid.uuid4()
            await status_events.subscribe(draft_id, healthy_send)
            await status_events.subscribe(draft_id, dead_send)
            await status_events.emit(draft_id, type="status", status="ready")
            count_after = await status_events.subscriber_count(draft_id)
            return count_after, healthy_received

        count, received = asyncio.run(_run())
        # Only healthy subscriber remains.
        assert count == 1
        # Healthy subscriber still received the event despite the
        # other one raising.
        assert len(received) == 1


class TestThreadsafeEmit:
    def test_emit_threadsafe_dispatches_onto_registered_loop(self):
        """A call from a worker thread schedules the emit onto the
        web event loop and reaches a subscriber that lives on that loop."""
        received: list[str] = []
        ready = threading.Event()
        loop_holder: dict = {}

        async def runner():
            async def send(payload: str) -> None:
                received.append(payload)

            draft_id = uuid.uuid4()
            await status_events.subscribe(draft_id, send)

            # Signal the worker thread that the loop is ready and
            # hand it the loop reference + draft id to publish to.
            loop_holder["loop"] = asyncio.get_running_loop()
            loop_holder["draft_id"] = draft_id
            status_events.register_event_loop(asyncio.get_running_loop())
            ready.set()

            # Wait for the worker thread to publish, then give the
            # event loop a tick to run the scheduled coroutine.
            await asyncio.sleep(0.2)

        def worker():
            ready.wait(timeout=2.0)
            status_events.emit_threadsafe(loop_holder["draft_id"], type="status", status="ready")

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        try:
            asyncio.run(runner())
        finally:
            t.join(timeout=2.0)

        assert len(received) == 1
        payload = json.loads(received[0])
        assert payload["status"] == "ready"

    def test_emit_threadsafe_without_registered_loop_is_silent(self):
        """If no loop has been registered (test mode / stub run) the
        helper must drop the emit silently rather than raising."""
        # Reset the loop ref to None to simulate "not yet registered".
        status_events._event_loop = None
        # Must not raise.
        status_events.emit_threadsafe(uuid.uuid4(), type="status", status="ready")
