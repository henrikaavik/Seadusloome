"""Tests for ``app.chat.websocket``.

Tests the WebSocket message parsing, validation, and auth checks.
The orchestrator is mocked out so these tests focus on the WS handler layer.

Uses ``asyncio.run()`` to run async functions, matching the convention
in ``tests/test_chat_tools.py``.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from app.chat.websocket import on_connect, on_disconnect, ws_chat

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
