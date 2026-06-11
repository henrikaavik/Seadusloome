"""Tests for ``app.chat.websocket``.

Tests the WebSocket message parsing, validation, and auth checks.
The orchestrator is mocked out so these tests focus on the WS handler layer.

Uses ``asyncio.run()`` to run async functions, matching the convention
in ``tests/test_chat_tools.py``.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from app.chat.websocket import (
    on_connect,
    on_disconnect,
    ws_chat,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_USER_ID = "11111111-1111-1111-1111-111111111111"
_ORG_ID = "22222222-2222-2222-2222-222222222222"
_CONV_ID = "33333333-3333-3333-3333-333333333333"


def _auth_scope() -> dict[str, Any]:
    return {"auth": {"id": _USER_ID, "org_id": _ORG_ID}}


class _Collector:
    """Async-compatible send collector."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def __call__(self, data: str) -> None:
        self.sent.append(data)


class _FakeWs:
    """Minimal stand-in for the Starlette WebSocket conn that FastHTML
    hands to WS hooks via the unannotated ``ws`` parameter. Stable
    identity across hook dispatches — unlike ``send``, which FastHTML
    rebuilds as a fresh partial per dispatch (#856)."""

    def __init__(self) -> None:
        self.close_calls: list[tuple[int, str]] = []

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.close_calls.append((code, reason))


# ---------------------------------------------------------------------------
# on_connect / on_disconnect
# ---------------------------------------------------------------------------


class TestOnConnect:
    def test_sends_connected_event(self):
        collector = _Collector()
        asyncio.run(on_connect(collector))
        assert len(collector.sent) == 1
        parsed = json.loads(collector.sent[0])
        assert parsed["type"] == "connected"


class TestOnDisconnect:
    def test_disconnect_does_not_raise(self):
        collector = _Collector()
        asyncio.run(on_disconnect(collector))


# ---------------------------------------------------------------------------
# ws_chat — parsing
# ---------------------------------------------------------------------------


class TestWsChatParsing:
    def test_invalid_json_sends_error(self):
        collector = _Collector()
        asyncio.run(ws_chat("not json at all", collector, _auth_scope()))
        assert len(collector.sent) == 1
        parsed = json.loads(collector.sent[0])
        assert parsed["type"] == "error"

    def test_non_dict_json_sends_error(self):
        collector = _Collector()
        asyncio.run(ws_chat('"just a string"', collector, _auth_scope()))
        assert len(collector.sent) == 1
        parsed = json.loads(collector.sent[0])
        assert parsed["type"] == "error"

    def test_unknown_type_ignored(self):
        collector = _Collector()
        asyncio.run(ws_chat(json.dumps({"type": "ping"}), collector, _auth_scope()))
        # Unknown types are silently ignored
        assert len(collector.sent) == 0


# ---------------------------------------------------------------------------
# ws_chat — validation
# ---------------------------------------------------------------------------


class TestWsChatValidation:
    def test_missing_conversation_id_sends_error(self):
        collector = _Collector()
        msg = json.dumps({"type": "send_message", "content": "Tere"})
        asyncio.run(ws_chat(msg, collector, _auth_scope()))
        assert len(collector.sent) == 1
        parsed = json.loads(collector.sent[0])
        assert parsed["type"] == "error"
        assert "conversation_id" in parsed["message"].lower()

    def test_invalid_conversation_id_sends_error(self):
        collector = _Collector()
        msg = json.dumps(
            {
                "type": "send_message",
                "conversation_id": "not-a-uuid",
                "content": "Tere",
            }
        )
        asyncio.run(ws_chat(msg, collector, _auth_scope()))
        assert len(collector.sent) == 1
        parsed = json.loads(collector.sent[0])
        assert parsed["type"] == "error"

    def test_empty_content_sends_error(self):
        collector = _Collector()
        msg = json.dumps(
            {
                "type": "send_message",
                "conversation_id": _CONV_ID,
                "content": "   ",
            }
        )
        asyncio.run(ws_chat(msg, collector, _auth_scope()))
        assert len(collector.sent) == 1
        parsed = json.loads(collector.sent[0])
        assert parsed["type"] == "error"


# ---------------------------------------------------------------------------
# ws_chat — auth
# ---------------------------------------------------------------------------


class TestWsChatAuth:
    def test_unauthenticated_sends_error(self):
        collector = _Collector()
        msg = json.dumps(
            {
                "type": "send_message",
                "conversation_id": _CONV_ID,
                "content": "Tere",
            }
        )
        asyncio.run(ws_chat(msg, collector, scope={}))
        assert len(collector.sent) == 1
        parsed = json.loads(collector.sent[0])
        assert parsed["type"] == "error"
        assert "autentimine" in parsed["message"].lower()

    def test_none_scope_sends_error(self):
        collector = _Collector()
        msg = json.dumps(
            {
                "type": "send_message",
                "conversation_id": _CONV_ID,
                "content": "Tere",
            }
        )
        asyncio.run(ws_chat(msg, collector, scope=None))
        assert len(collector.sent) == 1
        parsed = json.loads(collector.sent[0])
        assert parsed["type"] == "error"


# ---------------------------------------------------------------------------
# ws_chat — delegates to orchestrator
# ---------------------------------------------------------------------------


class TestWsChatDelegatesToOrchestrator:
    @patch("app.chat.websocket.ChatOrchestrator")
    @patch("app.chat.websocket.get_default_provider")
    def test_valid_message_calls_orchestrator(self, mock_provider, mock_orch_cls):
        mock_instance = MagicMock()
        mock_instance.handle_message = AsyncMock()
        mock_orch_cls.return_value = mock_instance

        collector = _Collector()
        msg = json.dumps(
            {
                "type": "send_message",
                "conversation_id": _CONV_ID,
                "content": "Kuidas see seadus moju avaldab?",
            }
        )
        asyncio.run(ws_chat(msg, collector, _auth_scope()))

        mock_instance.handle_message.assert_called_once()
        call_args = mock_instance.handle_message.call_args
        assert str(call_args[0][0]) == _CONV_ID  # conversation_id
        assert call_args[0][1] == "Kuidas see seadus moju avaldab?"  # content
        assert call_args[0][2]["id"] == _USER_ID  # auth


# ---------------------------------------------------------------------------
# C1: WebSocket auth via JWT cookie extraction
# ---------------------------------------------------------------------------
# (Cookie-extraction unit coverage lives in tests/test_ws_auth.py since
# #856 — the per-channel private copies were replaced by the shared
# app.auth.ws_auth helpers.)


class TestWsHandlerAuthExtraction:
    """Test _ws_handler extracts JWT from WS scope and passes auth to ws_chat."""

    @patch("app.chat.websocket.ChatOrchestrator")
    @patch("app.chat.websocket.get_default_provider")
    @patch("app.chat.websocket.JWTAuthProvider")
    def test_ws_handler_with_valid_jwt_cookie(self, mock_jwt_cls, mock_provider, mock_orch_cls):
        """A valid JWT cookie in the handshake headers populates auth."""
        mock_jwt_instance = MagicMock()
        mock_jwt_instance.get_current_user.return_value = {
            "id": _USER_ID,
            "email": "test@test.ee",
            "full_name": "Test User",
            "role": "drafter",
            "org_id": _ORG_ID,
        }
        mock_jwt_cls.return_value = mock_jwt_instance

        mock_orch_instance = MagicMock()
        mock_orch_instance.handle_message = AsyncMock()
        mock_orch_cls.return_value = mock_orch_instance

        from app.chat.websocket import register_chat_ws_routes

        # Capture the registered handler
        mock_app = MagicMock()
        captured_handler: Any = None

        def capture_ws(path, conn=None, disconn=None):
            def decorator(fn):
                nonlocal captured_handler
                captured_handler = fn
                return fn

            return decorator

        mock_app.ws = capture_ws
        register_chat_ws_routes(mock_app)

        assert captured_handler is not None

        # Build a scope with Cookie header containing a JWT token
        scope = {
            "headers": [
                (b"cookie", b"access_token=valid-jwt-token-here"),
            ],
        }

        collector = _Collector()
        msg = json.dumps(
            {
                "type": "send_message",
                "conversation_id": _CONV_ID,
                "content": "Tere!",
            }
        )

        asyncio.run(captured_handler(msg, collector, scope))

        # JWT provider was called with the token
        mock_jwt_instance.get_current_user.assert_called_once_with("valid-jwt-token-here")

        # Orchestrator was called (auth was populated so ws_chat proceeded)
        mock_orch_instance.handle_message.assert_called_once()
        call_args = mock_orch_instance.handle_message.call_args
        assert call_args[0][2]["id"] == _USER_ID

    @patch("app.chat.websocket.JWTAuthProvider")
    def test_ws_handler_without_cookie_sends_error(self, mock_jwt_cls):
        """No JWT cookie in the handshake headers -> auth error."""
        mock_jwt_instance = MagicMock()
        mock_jwt_cls.return_value = mock_jwt_instance

        from app.chat.websocket import register_chat_ws_routes

        mock_app = MagicMock()
        captured_handler: Any = None

        def capture_ws(path, conn=None, disconn=None):
            def decorator(fn):
                nonlocal captured_handler
                captured_handler = fn
                return fn

            return decorator

        mock_app.ws = capture_ws
        register_chat_ws_routes(mock_app)

        assert captured_handler is not None

        # Scope with no cookie header
        scope = {
            "headers": [
                (b"host", b"localhost"),
            ],
        }

        collector = _Collector()
        msg = json.dumps(
            {
                "type": "send_message",
                "conversation_id": _CONV_ID,
                "content": "Tere!",
            }
        )

        asyncio.run(captured_handler(msg, collector, scope))

        # Should send error event (auth required)
        assert len(collector.sent) == 1
        parsed = json.loads(collector.sent[0])
        assert parsed["type"] == "error"
        assert "autentimine" in parsed["message"].lower()


# ---------------------------------------------------------------------------
# Phase UX polish tests (issue #594)
# ---------------------------------------------------------------------------


class TestWsChatMaxLength:
    """Content longer than 10_000 characters is rejected before the orchestrator."""

    @patch("app.chat.websocket.ChatOrchestrator")
    @patch("app.chat.websocket.get_default_provider")
    def test_over_length_rejected_with_error(self, mock_provider, mock_orch_cls):
        mock_instance = MagicMock()
        mock_instance.handle_message = AsyncMock()
        mock_orch_cls.return_value = mock_instance

        collector = _Collector()
        msg = json.dumps(
            {
                "type": "send_message",
                "conversation_id": _CONV_ID,
                "content": "x" * 10_001,
            }
        )
        asyncio.run(ws_chat(msg, collector, _auth_scope()))

        assert len(collector.sent) == 1
        parsed = json.loads(collector.sent[0])
        assert parsed["type"] == "error"
        assert "liiga pikk" in parsed["message"].lower()
        mock_instance.handle_message.assert_not_called()

    @patch("app.chat.websocket.ChatOrchestrator")
    @patch("app.chat.websocket.get_default_provider")
    def test_exactly_limit_accepted(self, mock_provider, mock_orch_cls):
        mock_instance = MagicMock()
        mock_instance.handle_message = AsyncMock()
        mock_orch_cls.return_value = mock_instance

        collector = _Collector()
        msg = json.dumps(
            {
                "type": "send_message",
                "conversation_id": _CONV_ID,
                "content": "x" * 10_000,
            }
        )
        asyncio.run(ws_chat(msg, collector, _auth_scope()))

        mock_instance.handle_message.assert_called_once()


class TestWsChatJwtFailClosed:
    """When JWTAuthProvider construction fails the socket must be closed with 1011.

    Review finding F1 (#856): the close MUST go through ``ws.close()``
    on the raw Starlette conn. Pushing a ``{"type": "websocket.close"}``
    dict through FastHTML's wrapped ``send`` feeds it to ``to_xml`` +
    ``ws.send_text`` — a garbage text frame, and the connection stays
    open. The old test asserted exactly that broken behaviour.
    """

    @patch("app.chat.websocket.JWTAuthProvider")
    def test_jwt_provider_construction_failure_closes_socket(self, mock_jwt_cls):
        mock_jwt_cls.side_effect = RuntimeError("cannot init JWT provider")

        from app.chat.websocket import register_chat_ws_routes

        mock_app = MagicMock()
        captured_handler: Any = None

        def capture_ws(path, conn=None, disconn=None):
            def decorator(fn):
                nonlocal captured_handler
                captured_handler = fn
                return fn

            return decorator

        mock_app.ws = capture_ws
        register_chat_ws_routes(mock_app)
        assert captured_handler is not None

        scope = {
            "headers": [
                (b"cookie", b"access_token=irrelevant"),
            ],
        }

        sent_raw: list[Any] = []

        async def raw_send(data: Any) -> None:
            sent_raw.append(data)

        ws = _FakeWs()
        msg = json.dumps(
            {
                "type": "send_message",
                "conversation_id": _CONV_ID,
                "content": "Tere",
            }
        )

        asyncio.run(captured_handler(msg, raw_send, scope, ws))

        # The fail-closed path verifiably terminates the connection via
        # the raw conn with code 1011 …
        assert ws.close_calls == [(1011, "auth provider unavailable")]
        # … and does NOT push an ASGI close dict through ``send`` (the
        # F1 defect: that produces a text frame, not a close).
        close_dicts_via_send = [
            d for d in sent_raw if isinstance(d, dict) and d.get("type") == "websocket.close"
        ]
        assert close_dicts_via_send == []


class TestWsChatStopGeneration:
    """``stop_generation`` cancels an in-flight orchestrator task."""

    def test_stop_cancels_running_task_and_emits_stopped(self):
        cancel_observed: dict[str, bool] = {"called": False}

        async def slow_handle(conv_id, content, auth, send_event):
            try:
                await send_event({"type": "content_delta", "delta": "partial"})
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                cancel_observed["called"] = True
                # orchestrator normally emits stopped; mimic that.
                await send_event({"type": "stopped", "message_id": None})
                raise

        mock_instance = MagicMock()
        mock_instance.handle_message = AsyncMock(side_effect=slow_handle)

        collector = _Collector()

        async def scenario():
            with (
                patch("app.chat.websocket.ChatOrchestrator", return_value=mock_instance),
                patch("app.chat.websocket.get_default_provider"),
            ):
                tasks: dict[str, Any] = {}

                send_msg = json.dumps(
                    {
                        "type": "send_message",
                        "conversation_id": _CONV_ID,
                        "content": "Tere",
                    }
                )
                stop_msg = json.dumps(
                    {
                        "type": "stop_generation",
                        "conversation_id": _CONV_ID,
                    }
                )

                send_task = asyncio.create_task(
                    ws_chat(send_msg, collector, _auth_scope(), active_tasks=tasks)
                )
                # Give the orchestrator a tick to start.
                await asyncio.sleep(0.05)
                await ws_chat(stop_msg, collector, _auth_scope(), active_tasks=tasks)
                await send_task

        asyncio.run(scenario())

        assert cancel_observed["called"] is True
        stopped = [s for s in collector.sent if json.loads(s).get("type") == "stopped"]
        assert len(stopped) >= 1

    def test_stop_without_registry_still_acks(self):
        collector = _Collector()
        msg = json.dumps(
            {
                "type": "stop_generation",
                "conversation_id": _CONV_ID,
            }
        )
        asyncio.run(ws_chat(msg, collector, _auth_scope()))
        assert len(collector.sent) == 1
        parsed = json.loads(collector.sent[0])
        assert parsed["type"] == "stopped"


# ---------------------------------------------------------------------------
# #737 / #738: regenerate action
# ---------------------------------------------------------------------------


class TestWsChatRegenerate:
    """The ``regenerate`` action re-runs generation from the persisted
    history (no re-sent text, no new user message) — issues #737 / #738."""

    @patch("app.chat.websocket.ChatOrchestrator")
    @patch("app.chat.websocket.get_default_provider")
    def test_regenerate_calls_orchestrator_in_regenerate_mode(self, mock_provider, mock_orch_cls):
        mock_instance = MagicMock()
        mock_instance.handle_message = AsyncMock()
        mock_orch_cls.return_value = mock_instance

        pivot_id = "44444444-4444-4444-4444-444444444444"
        collector = _Collector()
        msg = json.dumps(
            {
                "type": "regenerate",
                "conversation_id": _CONV_ID,
                "pivot_message_id": pivot_id,
            }
        )
        asyncio.run(ws_chat(msg, collector, _auth_scope()))

        mock_instance.handle_message.assert_called_once()
        call = mock_instance.handle_message.call_args
        # conversation_id, then the empty-string regenerate sentinel.
        assert str(call[0][0]) == _CONV_ID
        assert call[0][1] == ""
        assert call[0][2]["id"] == _USER_ID
        # The pivot is forwarded as a UUID keyword arg.
        assert str(call.kwargs["regenerate_pivot_message_id"]) == pivot_id

    @patch("app.chat.websocket.ChatOrchestrator")
    @patch("app.chat.websocket.get_default_provider")
    def test_regenerate_without_pivot_passes_none(self, mock_provider, mock_orch_cls):
        mock_instance = MagicMock()
        mock_instance.handle_message = AsyncMock()
        mock_orch_cls.return_value = mock_instance

        collector = _Collector()
        msg = json.dumps({"type": "regenerate", "conversation_id": _CONV_ID})
        asyncio.run(ws_chat(msg, collector, _auth_scope()))

        mock_instance.handle_message.assert_called_once()
        call = mock_instance.handle_message.call_args
        assert call[0][1] == ""
        assert call.kwargs["regenerate_pivot_message_id"] is None

    @patch("app.chat.websocket.ChatOrchestrator")
    @patch("app.chat.websocket.get_default_provider")
    def test_regenerate_bad_pivot_id_sends_error(self, mock_provider, mock_orch_cls):
        mock_instance = MagicMock()
        mock_instance.handle_message = AsyncMock()
        mock_orch_cls.return_value = mock_instance

        collector = _Collector()
        msg = json.dumps(
            {
                "type": "regenerate",
                "conversation_id": _CONV_ID,
                "pivot_message_id": "not-a-uuid",
            }
        )
        asyncio.run(ws_chat(msg, collector, _auth_scope()))

        mock_instance.handle_message.assert_not_called()
        assert len(collector.sent) == 1
        assert json.loads(collector.sent[0])["type"] == "error"

    def test_regenerate_missing_conversation_id_sends_error(self):
        collector = _Collector()
        asyncio.run(ws_chat(json.dumps({"type": "regenerate"}), collector, _auth_scope()))
        assert len(collector.sent) == 1
        parsed = json.loads(collector.sent[0])
        assert parsed["type"] == "error"
        assert "conversation_id" in parsed["message"].lower()

    def test_regenerate_unauthenticated_sends_error(self):
        collector = _Collector()
        msg = json.dumps({"type": "regenerate", "conversation_id": _CONV_ID})
        asyncio.run(ws_chat(msg, collector, scope={}))
        assert len(collector.sent) == 1
        parsed = json.loads(collector.sent[0])
        assert parsed["type"] == "error"
        assert "autentimine" in parsed["message"].lower()

    def test_regenerate_stop_can_cancel_it(self):
        """``stop_generation`` cancels an in-flight ``regenerate`` too —
        both paths share the active-task registry."""
        cancel_observed: dict[str, bool] = {"called": False}

        async def slow_handle(conv_id, content, auth, send_event, **kwargs):
            try:
                await send_event({"type": "content_delta", "delta": "partial"})
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                cancel_observed["called"] = True
                await send_event({"type": "stopped", "message_id": None})
                raise

        mock_instance = MagicMock()
        mock_instance.handle_message = AsyncMock(side_effect=slow_handle)

        collector = _Collector()

        async def scenario():
            with (
                patch("app.chat.websocket.ChatOrchestrator", return_value=mock_instance),
                patch("app.chat.websocket.get_default_provider"),
            ):
                tasks: dict[str, Any] = {}
                regen_msg = json.dumps({"type": "regenerate", "conversation_id": _CONV_ID})
                stop_msg = json.dumps({"type": "stop_generation", "conversation_id": _CONV_ID})

                regen_task = asyncio.create_task(
                    ws_chat(regen_msg, collector, _auth_scope(), active_tasks=tasks)
                )
                await asyncio.sleep(0.05)
                await ws_chat(stop_msg, collector, _auth_scope(), active_tasks=tasks)
                await regen_task

        asyncio.run(scenario())

        assert cancel_observed["called"] is True
        assert any(json.loads(s).get("type") == "stopped" for s in collector.sent)


def _capture_chat_handler_and_disconnect() -> tuple[Any, Any, dict[int, Any]]:
    """Register the chat WS routes against a fake app; return the
    message handler, the disconnect hook, and the per-connection task
    registry exposed for tests."""
    from app.chat.websocket import register_chat_ws_routes

    mock_app = MagicMock()
    captured: dict[str, Any] = {"handler": None}

    def capture_ws(path, conn=None, disconn=None):
        def decorator(fn):
            captured["handler"] = fn
            return fn

        return decorator

    mock_app.ws = capture_ws
    register_chat_ws_routes(mock_app)
    handler = captured["handler"]
    assert handler is not None
    registry = handler._per_conn_tasks  # type: ignore[attr-defined]
    disconnect = handler._on_disconnect  # type: ignore[attr-defined]
    return handler, disconnect, registry


class TestWsHandlerDisconnectCleanup:
    """Disconnect mid-stream must cancel pending tasks and drop the registry key.

    Review finding F4 (#856): the registry is keyed on the stable conn
    identity (``id(ws)``). FastHTML rebuilds ``send`` as a fresh
    partial per hook dispatch, so this test deliberately hands the
    disconnect hook a DIFFERENT ``send`` object than the handler saw —
    exactly what production does. (The previous version of this test
    passed the same ``send`` to both hooks, which is the identity
    assumption that masked F3/F4.)
    """

    @patch("app.chat.websocket.ChatOrchestrator")
    @patch("app.chat.websocket.get_default_provider")
    @patch("app.chat.websocket.JWTAuthProvider")
    def test_disconnect_drops_registry_key(self, mock_jwt_cls, mock_provider, mock_orch_cls):
        mock_jwt_instance = MagicMock()
        mock_jwt_instance.get_current_user.return_value = {
            "id": _USER_ID,
            "org_id": _ORG_ID,
            "role": "drafter",
            "email": "t@t.ee",
            "full_name": "T",
        }
        mock_jwt_cls.return_value = mock_jwt_instance

        # Orchestrator stub that blocks so we have a pending task when
        # the disconnect fires.
        async def slow_handle(conv_id, content, auth, send_event):
            try:
                await send_event({"type": "content_delta", "delta": "x"})
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                raise

        mock_instance = MagicMock()
        mock_instance.handle_message = AsyncMock(side_effect=slow_handle)
        mock_orch_cls.return_value = mock_instance

        handler_fn, disconnect_fn, registry = _capture_chat_handler_and_disconnect()

        scope = {"headers": [(b"cookie", b"access_token=irrelevant")]}
        ws = _FakeWs()

        # Two DIFFERENT send objects for the same connection — the
        # production shape (FastHTML rebuilds the partial per dispatch).
        async def handler_send(data: Any) -> None:
            return

        async def disconnect_send(data: Any) -> None:
            return

        send_msg = json.dumps(
            {
                "type": "send_message",
                "conversation_id": _CONV_ID,
                "content": "Tere",
            }
        )

        async def scenario() -> None:
            handler_task = asyncio.create_task(handler_fn(send_msg, handler_send, scope, ws))
            # Let the orchestrator register its per-conv task.
            await asyncio.sleep(0.05)
            # Exactly one connection is registered, keyed on id(ws).
            assert id(ws) in registry
            assert len(registry[id(ws)]) == 1
            # Simulate the socket going away — different send object.
            await disconnect_fn(disconnect_send, ws)
            # Registry slot gone, tasks cancelled.
            assert id(ws) not in registry
            # Let the handler coroutine drain the CancelledError.
            try:
                await handler_task
            except Exception:
                pass

        asyncio.run(scenario())

        # After teardown, zero keys remain for this connection.
        assert id(ws) not in registry
        assert all(k != id(ws) for k in registry)


class TestStopGenerationAcrossDispatches:
    """The real-world F4 scenario (#856): ``stop_generation`` arrives as
    a SECOND WS message, for which FastHTML builds a brand-new ``send``
    partial. With the old ``id(send)``-keyed registry the stop handler
    looked into an empty dict and silently cancelled nothing; keying on
    ``id(ws)`` pairs the two dispatches correctly."""

    @patch("app.chat.websocket.ChatOrchestrator")
    @patch("app.chat.websocket.get_default_provider")
    @patch("app.chat.websocket.JWTAuthProvider")
    def test_stop_message_with_fresh_send_cancels_running_stream(
        self, mock_jwt_cls, mock_provider, mock_orch_cls
    ):
        mock_jwt_instance = MagicMock()
        mock_jwt_instance.get_current_user.return_value = {
            "id": _USER_ID,
            "org_id": _ORG_ID,
            "role": "drafter",
            "email": "t@t.ee",
            "full_name": "T",
        }
        mock_jwt_cls.return_value = mock_jwt_instance

        cancel_observed: dict[str, bool] = {"called": False}

        async def slow_handle(conv_id, content, auth, send_event):
            try:
                await send_event({"type": "content_delta", "delta": "partial"})
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                cancel_observed["called"] = True
                await send_event({"type": "stopped", "message_id": None})
                raise

        mock_instance = MagicMock()
        mock_instance.handle_message = AsyncMock(side_effect=slow_handle)
        mock_orch_cls.return_value = mock_instance

        handler_fn, _disconnect_fn, registry = _capture_chat_handler_and_disconnect()

        scope = {"headers": [(b"cookie", b"access_token=irrelevant")]}
        ws = _FakeWs()
        first_send = _Collector()
        second_send = _Collector()  # fresh partial, same conn
        assert first_send is not second_send

        send_msg = json.dumps(
            {"type": "send_message", "conversation_id": _CONV_ID, "content": "Tere"}
        )
        stop_msg = json.dumps({"type": "stop_generation", "conversation_id": _CONV_ID})

        async def scenario() -> None:
            streaming = asyncio.create_task(handler_fn(send_msg, first_send, scope, ws))
            await asyncio.sleep(0.05)
            # Second dispatch: different send object, same ws.
            await handler_fn(stop_msg, second_send, scope, ws)
            await streaming

        asyncio.run(scenario())

        assert cancel_observed["called"] is True
        # The registry slot was drained after the stream ended.
        assert id(ws) not in registry


# ---------------------------------------------------------------------------
# #856 regression — conversation authz gate on every WS path
# ---------------------------------------------------------------------------


class TestConversationAuthzGate:
    """The WS layer deliberately has no authz gate of its own: every
    chat WS path (``send_message`` AND ``regenerate``) relies on the
    orchestrator exercising :func:`app.auth.policy.can_access_conversation`
    before any generation. These regression tests drive the REAL
    ``ChatOrchestrator`` (with the DB-touching phases patched) and pin
    that the gate runs and denies on both paths."""

    def _conversation(self) -> Any:
        from app.chat.models import Conversation

        now = datetime.now(UTC)
        return Conversation(
            id=uuid.UUID(_CONV_ID),
            # Owned by ANOTHER user — the gate must deny our caller.
            user_id=uuid.UUID("99999999-9999-9999-9999-999999999999"),
            org_id=uuid.UUID(_ORG_ID),
            title="Salajane vestlus",
            context_draft_id=None,
            created_at=now,
            updated_at=now,
        )

    def _drive(self, msg_dict: dict[str, Any]) -> tuple[MagicMock, _Collector]:
        conversation = self._conversation()
        collector = _Collector()
        with (
            # Real ChatOrchestrator; only the LLM provider is stubbed.
            patch("app.chat.websocket.get_default_provider", return_value=MagicMock()),
            patch("app.chat.orchestrator.check_message_rate"),
            patch("app.chat.orchestrator._load_conversation", return_value=conversation),
            patch(
                "app.chat.orchestrator.can_access_conversation",
                return_value=False,
            ) as gate,
        ):
            asyncio.run(ws_chat(json.dumps(msg_dict), collector, _auth_scope()))
        return gate, collector

    def test_send_message_path_exercises_can_access_conversation(self):
        gate, collector = self._drive(
            {
                "type": "send_message",
                "conversation_id": _CONV_ID,
                "content": "Tere",
            }
        )

        gate.assert_called_once()
        auth_arg, conversation_arg = gate.call_args[0]
        assert auth_arg["id"] == _USER_ID
        assert str(conversation_arg.id) == _CONV_ID

        # Denial surfaces as the orchestrator's error event — no stream.
        events = [json.loads(s) for s in collector.sent]
        assert any(
            e.get("type") == "error" and "õigus" in e.get("message", "").lower() for e in events
        ), f"expected an access-denied error event, got: {events}"
        assert not any(e.get("type") == "content_delta" for e in events)

    def test_regenerate_path_exercises_can_access_conversation(self):
        gate, collector = self._drive(
            {
                "type": "regenerate",
                "conversation_id": _CONV_ID,
            }
        )

        gate.assert_called_once()
        auth_arg, conversation_arg = gate.call_args[0]
        assert auth_arg["id"] == _USER_ID
        assert str(conversation_arg.id) == _CONV_ID

        events = [json.loads(s) for s in collector.sent]
        assert any(
            e.get("type") == "error" and "õigus" in e.get("message", "").lower() for e in events
        ), f"expected an access-denied error event, got: {events}"
        assert not any(e.get("type") == "content_delta" for e in events)
