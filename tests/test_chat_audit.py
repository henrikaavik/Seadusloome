"""Unit tests for ``app.chat.audit``.

Verifies that each audit helper calls ``log_action`` with the correct
action label and detail payload. Same pattern as ``tests/test_drafter_audit.py``.
"""

from __future__ import annotations

import uuid
from unittest.mock import patch

from app.chat.audit import (
    log_chat_conversation_create,
    log_chat_conversation_delete,
    log_chat_message_send,
)

_USER_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
_CONV_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")
_MSG_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")
_DRAFT_ID = uuid.UUID("44444444-4444-4444-4444-444444444444")


class TestLogConversationCreate:
    @patch("app.chat.audit.log_action")
    def test_basic_create(self, mock_log):
        log_chat_conversation_create(_USER_ID, _CONV_ID)
        mock_log.assert_called_once()
        args = mock_log.call_args
        assert args[0][0] == str(_USER_ID)
        assert args[0][1] == "chat.conversation.create"
        detail = args[0][2]
        assert detail["conversation_id"] == str(_CONV_ID)
        assert "context_draft_id" not in detail

    @patch("app.chat.audit.log_action")
    def test_create_with_draft_context(self, mock_log):
        log_chat_conversation_create(_USER_ID, _CONV_ID, context_draft_id=_DRAFT_ID)
        detail = mock_log.call_args[0][2]
        assert detail["context_draft_id"] == str(_DRAFT_ID)

    @patch("app.chat.audit.log_action")
    def test_create_with_none_user(self, mock_log):
        log_chat_conversation_create(None, _CONV_ID)
        assert mock_log.call_args[0][0] is None


class TestLogMessageSend:
    @patch("app.chat.audit.log_action")
    def test_message_send(self, mock_log):
        log_chat_message_send(_USER_ID, _CONV_ID, _MSG_ID)
        mock_log.assert_called_once()
        args = mock_log.call_args
        assert args[0][1] == "chat.message.send"
        detail = args[0][2]
        assert detail["conversation_id"] == str(_CONV_ID)
        assert detail["message_id"] == str(_MSG_ID)


class TestLogConversationDelete:
    @patch("app.chat.audit.log_action")
    def test_delete(self, mock_log):
        log_chat_conversation_delete(_USER_ID, _CONV_ID)
        mock_log.assert_called_once()
        args = mock_log.call_args
        assert args[0][1] == "chat.conversation.delete"
        detail = args[0][2]
        assert detail["conversation_id"] == str(_CONV_ID)

    @patch("app.chat.audit.log_action")
    def test_delete_with_string_ids(self, mock_log):
        log_chat_conversation_delete(str(_USER_ID), str(_CONV_ID))
        mock_log.assert_called_once()
        detail = mock_log.call_args[0][2]
        assert detail["conversation_id"] == str(_CONV_ID)
