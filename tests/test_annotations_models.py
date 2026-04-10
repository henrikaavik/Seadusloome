"""Unit tests for ``app.annotations.models`` and ``app.annotations.audit``.

Tests the CRUD helpers for ``annotations`` and ``annotation_replies``,
plus the audit log wrappers.
All DB access is mocked -- same patterns as ``tests/test_chat_models.py``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from app.annotations.audit import (
    log_annotation_create,
    log_annotation_delete,
    log_annotation_reply,
    log_annotation_resolve,
)
from app.annotations.models import (
    Annotation,
    AnnotationReply,
    create_annotation,
    create_reply,
    delete_annotation,
    get_annotation,
    list_annotations_for_target,
    list_replies,
    resolve_annotation,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_USER_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
_ORG_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")
_ANN_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")
_REPLY_ID = uuid.UUID("55555555-5555-5555-5555-555555555555")
_RESOLVER_ID = uuid.UUID("66666666-6666-6666-6666-666666666666")


def _make_annotation_row(
    *,
    ann_id: uuid.UUID | None = None,
    user_id: uuid.UUID = _USER_ID,
    org_id: uuid.UUID = _ORG_ID,
    target_type: str = "draft",
    target_id: str = "44444444-4444-4444-4444-444444444444",
    target_metadata: str | None = None,
    content: str = "See vajab muutmist",
    resolved: bool = False,
    resolved_by: uuid.UUID | None = None,
    resolved_at: datetime | None = None,
) -> tuple[Any, ...]:
    """Build a raw cursor row matching _ANNOTATION_COLUMNS order."""
    now = datetime.now(UTC)
    return (
        ann_id or uuid.uuid4(),
        user_id,
        org_id,
        target_type,
        target_id,
        target_metadata,
        content,
        resolved,
        resolved_by,
        resolved_at,
        now,
        now,
    )


def _make_reply_row(
    *,
    reply_id: uuid.UUID | None = None,
    annotation_id: uuid.UUID = _ANN_ID,
    user_id: uuid.UUID = _USER_ID,
    content: str = "Noustun, parandame.",
) -> tuple[Any, ...]:
    """Build a raw cursor row matching _REPLY_COLUMNS order."""
    now = datetime.now(UTC)
    return (
        reply_id or uuid.uuid4(),
        annotation_id,
        user_id,
        content,
        now,
    )


# ---------------------------------------------------------------------------
# create_annotation
# ---------------------------------------------------------------------------


class TestCreateAnnotation:
    def test_create_returns_annotation(self):
        conn = MagicMock()
        ann_id = uuid.uuid4()
        row = _make_annotation_row(ann_id=ann_id)
        conn.execute.return_value.fetchone.return_value = row

        result = create_annotation(conn, _USER_ID, _ORG_ID, "draft", "some-draft-id", "Kommentaar")

        assert isinstance(result, Annotation)
        assert result.id == ann_id
        assert result.user_id == _USER_ID
        assert result.org_id == _ORG_ID
        assert result.resolved is False
        conn.execute.assert_called_once()

    def test_create_with_target_metadata(self):
        conn = MagicMock()
        row = _make_annotation_row(target_metadata='{"section": "3.1"}')
        conn.execute.return_value.fetchone.return_value = row

        result = create_annotation(
            conn,
            _USER_ID,
            _ORG_ID,
            "draft",
            "some-draft-id",
            "Kommentaar",
            target_metadata={"section": "3.1"},
        )

        assert result.target_metadata == {"section": "3.1"}

    def test_create_raises_on_no_row(self):
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = None

        with pytest.raises(RuntimeError, match="produced no row"):
            create_annotation(conn, _USER_ID, _ORG_ID, "draft", "some-draft-id", "Kommentaar")

    def test_create_rejects_invalid_target_type(self):
        conn = MagicMock()
        with pytest.raises(ValueError, match="Invalid target_type"):
            create_annotation(conn, _USER_ID, _ORG_ID, "invalid_type", "some-id", "Kommentaar")


# ---------------------------------------------------------------------------
# get_annotation
# ---------------------------------------------------------------------------


class TestGetAnnotation:
    def test_get_returns_annotation(self):
        conn = MagicMock()
        ann_id = uuid.uuid4()
        row = _make_annotation_row(ann_id=ann_id, content="Test kommentaar")
        conn.execute.return_value.fetchone.return_value = row

        result = get_annotation(conn, ann_id)
        assert result is not None
        assert result.id == ann_id
        assert result.content == "Test kommentaar"

    def test_get_returns_none_for_missing(self):
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = None

        result = get_annotation(conn, uuid.uuid4())
        assert result is None

    def test_get_handles_db_error(self):
        conn = MagicMock()
        conn.execute.side_effect = RuntimeError("DB error")

        result = get_annotation(conn, uuid.uuid4())
        assert result is None


# ---------------------------------------------------------------------------
# list_annotations_for_target
# ---------------------------------------------------------------------------


class TestListAnnotationsForTarget:
    def test_list_returns_annotations(self):
        conn = MagicMock()
        row1 = _make_annotation_row(content="First")
        row2 = _make_annotation_row(content="Second")
        conn.execute.return_value.fetchall.return_value = [row1, row2]

        result = list_annotations_for_target(conn, "draft", "some-id", _ORG_ID)
        assert len(result) == 2
        assert result[0].content == "First"
        assert result[1].content == "Second"

    def test_list_empty(self):
        conn = MagicMock()
        conn.execute.return_value.fetchall.return_value = []

        result = list_annotations_for_target(conn, "draft", "some-id", _ORG_ID)
        assert result == []

    def test_list_handles_db_error(self):
        conn = MagicMock()
        conn.execute.side_effect = RuntimeError("DB error")

        result = list_annotations_for_target(conn, "draft", "some-id", _ORG_ID)
        assert result == []


# ---------------------------------------------------------------------------
# resolve_annotation
# ---------------------------------------------------------------------------


class TestResolveAnnotation:
    def test_resolve_returns_updated_annotation(self):
        conn = MagicMock()
        now = datetime.now(UTC)
        row = _make_annotation_row(
            ann_id=_ANN_ID,
            resolved=True,
            resolved_by=_RESOLVER_ID,
            resolved_at=now,
        )
        conn.execute.return_value.fetchone.return_value = row

        result = resolve_annotation(conn, _ANN_ID, _RESOLVER_ID)

        assert result is not None
        assert result.resolved is True
        assert result.resolved_by == _RESOLVER_ID
        assert result.resolved_at == now
        conn.execute.assert_called_once()

    def test_resolve_returns_none_for_missing(self):
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = None

        result = resolve_annotation(conn, uuid.uuid4(), _RESOLVER_ID)
        assert result is None

    def test_resolve_handles_db_error(self):
        conn = MagicMock()
        conn.execute.side_effect = RuntimeError("DB error")

        result = resolve_annotation(conn, uuid.uuid4(), _RESOLVER_ID)
        assert result is None


# ---------------------------------------------------------------------------
# delete_annotation
# ---------------------------------------------------------------------------


class TestDeleteAnnotation:
    def test_delete(self):
        conn = MagicMock()
        delete_annotation(conn, _ANN_ID)

        conn.execute.assert_called_once()
        sql = conn.execute.call_args.args[0]
        assert "DELETE" in sql
        assert "annotations" in sql


# ---------------------------------------------------------------------------
# create_reply
# ---------------------------------------------------------------------------


class TestCreateReply:
    def test_create_returns_reply(self):
        conn = MagicMock()
        reply_id = uuid.uuid4()
        row = _make_reply_row(reply_id=reply_id, annotation_id=_ANN_ID)
        conn.execute.return_value.fetchone.return_value = row

        result = create_reply(conn, _ANN_ID, _USER_ID, "Vastus")

        assert isinstance(result, AnnotationReply)
        assert result.id == reply_id
        assert result.annotation_id == _ANN_ID
        conn.execute.assert_called_once()

    def test_create_reply_raises_on_no_row(self):
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = None

        with pytest.raises(RuntimeError, match="produced no row"):
            create_reply(conn, _ANN_ID, _USER_ID, "Vastus")


# ---------------------------------------------------------------------------
# list_replies
# ---------------------------------------------------------------------------


class TestListReplies:
    def test_list_returns_replies(self):
        conn = MagicMock()
        row = _make_reply_row(annotation_id=_ANN_ID)
        conn.execute.return_value.fetchall.return_value = [row]

        result = list_replies(conn, _ANN_ID)
        assert len(result) == 1
        assert isinstance(result[0], AnnotationReply)

    def test_list_empty(self):
        conn = MagicMock()
        conn.execute.return_value.fetchall.return_value = []

        result = list_replies(conn, uuid.uuid4())
        assert result == []

    def test_list_handles_db_error(self):
        conn = MagicMock()
        conn.execute.side_effect = RuntimeError("DB error")

        result = list_replies(conn, uuid.uuid4())
        assert result == []


# ---------------------------------------------------------------------------
# Audit helpers
# ---------------------------------------------------------------------------


class TestAuditLogAnnotationCreate:
    @patch("app.annotations.audit.log_action")
    def test_basic_create(self, mock_log):
        log_annotation_create(_USER_ID, _ANN_ID, "draft", "some-draft-id")
        mock_log.assert_called_once()
        args = mock_log.call_args
        assert args[0][0] == str(_USER_ID)
        assert args[0][1] == "annotation.create"
        detail = args[0][2]
        assert detail["annotation_id"] == str(_ANN_ID)
        assert detail["target_type"] == "draft"
        assert detail["target_id"] == "some-draft-id"

    @patch("app.annotations.audit.log_action")
    def test_create_with_none_user(self, mock_log):
        log_annotation_create(None, _ANN_ID, "draft", "some-draft-id")
        assert mock_log.call_args[0][0] is None


class TestAuditLogAnnotationReply:
    @patch("app.annotations.audit.log_action")
    def test_reply(self, mock_log):
        log_annotation_reply(_USER_ID, _ANN_ID, _REPLY_ID)
        mock_log.assert_called_once()
        args = mock_log.call_args
        assert args[0][1] == "annotation.reply"
        detail = args[0][2]
        assert detail["annotation_id"] == str(_ANN_ID)
        assert detail["reply_id"] == str(_REPLY_ID)


class TestAuditLogAnnotationResolve:
    @patch("app.annotations.audit.log_action")
    def test_resolve(self, mock_log):
        log_annotation_resolve(_USER_ID, _ANN_ID)
        mock_log.assert_called_once()
        args = mock_log.call_args
        assert args[0][1] == "annotation.resolve"
        detail = args[0][2]
        assert detail["annotation_id"] == str(_ANN_ID)


class TestAuditLogAnnotationDelete:
    @patch("app.annotations.audit.log_action")
    def test_delete(self, mock_log):
        log_annotation_delete(_USER_ID, _ANN_ID)
        mock_log.assert_called_once()
        args = mock_log.call_args
        assert args[0][1] == "annotation.delete"
        detail = args[0][2]
        assert detail["annotation_id"] == str(_ANN_ID)

    @patch("app.annotations.audit.log_action")
    def test_delete_with_string_ids(self, mock_log):
        log_annotation_delete(str(_USER_ID), str(_ANN_ID))
        mock_log.assert_called_once()
        detail = mock_log.call_args[0][2]
        assert detail["annotation_id"] == str(_ANN_ID)
