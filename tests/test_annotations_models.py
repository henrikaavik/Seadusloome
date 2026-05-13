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
    list_annotations_for_version_row,
    list_replies,
    parse_mentions,
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
    content: str | None = "See vajab muutmist",
    resolved: bool = False,
    resolved_by: uuid.UUID | None = None,
    resolved_at: datetime | None = None,
    content_encrypted: bytes | None = None,
    draft_version_id: uuid.UUID | None = None,
    mentions: list[uuid.UUID] | None = None,
    stale: bool = False,
) -> tuple[Any, ...]:
    """Build a raw cursor row matching _ANNOTATION_COLUMNS order (migration 029)."""
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
        content_encrypted,
        draft_version_id,
        mentions or [],
        stale,
    )


def _make_reply_row(
    *,
    reply_id: uuid.UUID | None = None,
    annotation_id: uuid.UUID = _ANN_ID,
    user_id: uuid.UUID = _USER_ID,
    content: str | None = "Noustun, parandame.",
    content_encrypted: bytes | None = None,
    mentions: list[uuid.UUID] | None = None,
) -> tuple[Any, ...]:
    """Build a raw cursor row matching _REPLY_COLUMNS order (migration 029)."""
    now = datetime.now(UTC)
    return (
        reply_id or uuid.uuid4(),
        annotation_id,
        user_id,
        content,
        now,
        content_encrypted,
        mentions or [],
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

    # ------------------------------------------------------------------
    # #772 — encryption-at-rest for generic annotation writes
    # ------------------------------------------------------------------

    def test_create_writes_ciphertext_and_no_plaintext(self):
        """create_annotation() must populate content_encrypted and leave
        content NULL in the INSERT, mirroring create_row_annotation()."""
        from app.storage import decrypt_text

        conn = MagicMock()
        # Mock the RETURNING row with the encrypted column populated so
        # _row_to_annotation reads back the ciphertext path.
        captured_ciphertext: dict[str, bytes] = {}

        def _execute(sql: str, params: tuple[Any, ...]) -> Any:
            # The encrypted byte payload is the LAST positional param in the
            # new (NULL, %s) tail of the INSERT.
            ciphertext_param = params[-1]
            assert isinstance(ciphertext_param, bytes)
            captured_ciphertext["bytes"] = ciphertext_param
            row = _make_annotation_row(
                content=None,
                content_encrypted=ciphertext_param,
            )
            cursor = MagicMock()
            cursor.fetchone.return_value = row
            return cursor

        conn.execute.side_effect = _execute

        plaintext = "See on tundlik märkus."
        result = create_annotation(conn, _USER_ID, _ORG_ID, "draft", "draft-id", plaintext)

        # The ciphertext round-trips through encrypt/decrypt.
        assert decrypt_text(captured_ciphertext["bytes"]) == plaintext
        # The Annotation surface object reports the decrypted plaintext.
        assert result.content == plaintext

        # The INSERT SQL writes NULL for content and the ciphertext for
        # content_encrypted — guarantees no plaintext lands in the legacy
        # column even on a half-rolled-back deploy.
        sql_used = conn.execute.call_args.args[0]
        assert "content_encrypted" in sql_used
        assert "NULL" in sql_used

    def test_create_does_not_pass_plaintext_to_query(self):
        """The plaintext body must never appear in the SQL parameters."""
        conn = MagicMock()
        plaintext = "Salajane juriidiline arvamus § 3 kohta."
        row = _make_annotation_row(content=None, content_encrypted=b"unused")
        conn.execute.return_value.fetchone.return_value = row

        # Patch encrypt_text inside the model module so the assertion below
        # is independent of the actual ciphertext format.
        with patch("app.annotations.models.encrypt_text", return_value=b"CIPHER") as mock_enc:
            create_annotation(conn, _USER_ID, _ORG_ID, "draft", "draft-id", plaintext)

        mock_enc.assert_called_once_with(plaintext)
        params = conn.execute.call_args.args[1]
        assert plaintext not in params
        # The ciphertext bytes ARE in the params tail.
        assert b"CIPHER" in params


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

    # ------------------------------------------------------------------
    # #772 — encryption-at-rest for reply writes
    # ------------------------------------------------------------------

    def test_create_reply_writes_ciphertext_and_no_plaintext(self):
        """create_reply() must populate content_encrypted and leave
        content NULL in the INSERT."""
        from app.storage import decrypt_text

        conn = MagicMock()
        captured_ciphertext: dict[str, bytes] = {}

        def _execute(sql: str, params: tuple[Any, ...]) -> Any:
            ciphertext_param = params[-1]
            assert isinstance(ciphertext_param, bytes)
            captured_ciphertext["bytes"] = ciphertext_param
            row = _make_reply_row(content=None, content_encrypted=ciphertext_param)
            cursor = MagicMock()
            cursor.fetchone.return_value = row
            return cursor

        conn.execute.side_effect = _execute

        plaintext = "Vastus tundlikule märkusele."
        result = create_reply(conn, _ANN_ID, _USER_ID, plaintext)

        # Ciphertext round-trips.
        assert decrypt_text(captured_ciphertext["bytes"]) == plaintext
        # The returned object exposes the decrypted plaintext.
        assert result.content == plaintext

        # The INSERT SQL writes NULL for content and the ciphertext for
        # content_encrypted.
        sql_used = conn.execute.call_args.args[0]
        assert "content_encrypted" in sql_used
        assert "NULL" in sql_used

    def test_create_reply_does_not_pass_plaintext_to_query(self):
        """The reply plaintext body must never appear in the SQL parameters."""
        conn = MagicMock()
        plaintext = "Salajane vastus § 5 kohta."
        row = _make_reply_row(content=None, content_encrypted=b"unused")
        conn.execute.return_value.fetchone.return_value = row

        with patch("app.annotations.models.encrypt_text", return_value=b"CIPHER") as mock_enc:
            create_reply(conn, _ANN_ID, _USER_ID, plaintext)

        mock_enc.assert_called_once_with(plaintext)
        params = conn.execute.call_args.args[1]
        assert plaintext not in params
        assert b"CIPHER" in params

    # ------------------------------------------------------------------
    # Legacy plaintext fallback reads still work for both shapes
    # ------------------------------------------------------------------

    def test_legacy_plaintext_annotation_round_trips_through_get(self):
        """A pre-encryption row (content set, content_encrypted NULL) is
        readable through get_annotation()."""
        conn = MagicMock()
        legacy_row = _make_annotation_row(
            content="Vana plaintekst rida",
            content_encrypted=None,
        )
        conn.execute.return_value.fetchone.return_value = legacy_row

        ann = get_annotation(conn, _ANN_ID)
        assert ann is not None
        assert ann.content == "Vana plaintekst rida"

    def test_legacy_plaintext_reply_round_trips_through_list(self):
        """A pre-encryption reply row reads back via list_replies()."""
        conn = MagicMock()
        legacy_row = _make_reply_row(
            content="Vana vastus plaintextina",
            content_encrypted=None,
        )
        conn.execute.return_value.fetchall.return_value = [legacy_row]

        replies = list_replies(conn, _ANN_ID)
        assert len(replies) == 1
        assert replies[0].content == "Vana vastus plaintextina"


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


# ---------------------------------------------------------------------------
# Migration 029 extensions — TestRowAnnotationExtensions
# ---------------------------------------------------------------------------

_VERSION_ID = uuid.UUID("77777777-7777-7777-7777-777777777777")
_OTHER_USER_ID = uuid.UUID("88888888-8888-8888-8888-888888888888")


@pytest.fixture(autouse=True)
def _fernet_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Install a fresh Fernet key for every test in this module.

    Autouse because create_annotation() and create_reply() now call
    encrypt_text() at write time (#772), so even the existing CRUD tests
    need a key available; tests that previously didn't request the
    fixture still work because the fixture is a no-op aside from the
    env-var setup.
    """
    from cryptography.fernet import Fernet

    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("STORAGE_ENCRYPTION_KEY", Fernet.generate_key().decode())
    import app.storage.encrypted as encrypted_module

    monkeypatch.setattr(encrypted_module, "_fernet", None)


class TestRowAnnotationExtensions:
    """Tests for the migration 029 column extensions on _row_to_annotation
    and the new read helpers parse_mentions / list_annotations_for_version_row."""

    # ------------------------------------------------------------------
    # _row_to_annotation: encrypted content
    # ------------------------------------------------------------------

    def test_row_reads_content_from_encrypted_column(self, _fernet_key):
        """content_encrypted present and non-NULL → decrypted value is used."""
        from app.storage import encrypt_text

        plaintext = "See eelnõu § 3 vajab täpsustamist."
        ciphertext = encrypt_text(plaintext)

        row = _make_annotation_row(
            content=None,  # plaintext column NULL (new write style)
            content_encrypted=ciphertext,
            draft_version_id=_VERSION_ID,
        )

        from app.annotations.models import _row_to_annotation

        ann = _row_to_annotation(row)
        assert ann.content == plaintext

    def test_row_falls_back_to_plaintext_when_encrypted_null(self):
        """Legacy row: content_encrypted is NULL, content has the plaintext."""
        row = _make_annotation_row(
            content="Vana rida ilma krüpteerimiseta",
            content_encrypted=None,
        )

        from app.annotations.models import _row_to_annotation

        ann = _row_to_annotation(row)
        assert ann.content == "Vana rida ilma krüpteerimiseta"

    def test_row_reads_draft_version_id(self):
        """draft_version_id is hydrated from the row."""
        row = _make_annotation_row(draft_version_id=_VERSION_ID)

        from app.annotations.models import _row_to_annotation

        ann = _row_to_annotation(row)
        assert ann.draft_version_id == _VERSION_ID

    def test_row_reads_mentions(self):
        """mentions UUID list is hydrated from the row."""
        row = _make_annotation_row(mentions=[_USER_ID, _OTHER_USER_ID])

        from app.annotations.models import _row_to_annotation

        ann = _row_to_annotation(row)
        assert ann.mentions == [_USER_ID, _OTHER_USER_ID]

    def test_row_reads_stale_flag(self):
        """stale=True is preserved through _row_to_annotation."""
        row = _make_annotation_row(stale=True)

        from app.annotations.models import _row_to_annotation

        ann = _row_to_annotation(row)
        assert ann.stale is True

    def test_row_defaults_for_legacy_row(self):
        """A 12-column legacy row (pre-migration-029) uses safe defaults."""
        now = datetime.now(UTC)
        # Replicate what a pre-029 row looks like (12 columns, no new ones)
        legacy_row = (
            uuid.uuid4(),  # id
            _USER_ID,  # user_id
            _ORG_ID,  # org_id
            "draft",  # target_type
            "some-id",  # target_id
            None,  # target_metadata
            "Vana sisu",  # content
            False,  # resolved
            None,  # resolved_by
            None,  # resolved_at
            now,  # created_at
            now,  # updated_at
            # no content_encrypted, draft_version_id, mentions, stale columns
        )

        from app.annotations.models import _row_to_annotation

        ann = _row_to_annotation(legacy_row)
        assert ann.content == "Vana sisu"
        assert ann.draft_version_id is None
        assert ann.mentions == []
        assert ann.stale is False

    # ------------------------------------------------------------------
    # _row_to_reply: encrypted content
    # ------------------------------------------------------------------

    def test_reply_row_reads_content_from_encrypted_column(self, _fernet_key):
        """Reply: content_encrypted non-NULL → decrypted value used."""
        from app.storage import encrypt_text

        plaintext = "Vastus krüpteeritud kujul."
        ciphertext = encrypt_text(plaintext)

        row = _make_reply_row(content=None, content_encrypted=ciphertext)

        from app.annotations.models import _row_to_reply

        reply = _row_to_reply(row)
        assert reply.content == plaintext

    def test_reply_row_falls_back_to_plaintext(self):
        """Reply legacy row: content_encrypted NULL → plaintext column used."""
        row = _make_reply_row(content="Vana vastus", content_encrypted=None)

        from app.annotations.models import _row_to_reply

        reply = _row_to_reply(row)
        assert reply.content == "Vana vastus"

    def test_reply_row_reads_mentions(self):
        """Reply mentions list is hydrated correctly."""
        row = _make_reply_row(mentions=[_USER_ID])

        from app.annotations.models import _row_to_reply

        reply = _row_to_reply(row)
        assert reply.mentions == [_USER_ID]

    # ------------------------------------------------------------------
    # parse_mentions
    # ------------------------------------------------------------------

    def test_parse_mentions_resolves_in_org_user(self):
        """@token that matches a user in the same org is resolved to their UUID."""
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = (_USER_ID,)

        result = parse_mentions(conn, "Vaata @peeter.pärn kommentaare.", _ORG_ID)

        assert result == [_USER_ID]
        # The query must include org_id to prevent cross-org probing.
        sql = conn.execute.call_args.args[0]
        assert "org_id" in sql

    def test_parse_mentions_drops_out_of_org_user(self):
        """@token that does NOT match any user in the org is silently dropped."""
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = None

        result = parse_mentions(conn, "Vaata @võõras tulemusi.", _ORG_ID)

        assert result == []

    def test_parse_mentions_deduplicates_same_user(self):
        """Mentioning the same user twice returns only one UUID."""
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = (_USER_ID,)

        result = parse_mentions(conn, "@peeter.pärn ja ka @peeter.pärn uuesti.", _ORG_ID)

        assert result == [_USER_ID]

    def test_parse_mentions_empty_content(self):
        """Content with no @ tokens returns an empty list without hitting the DB."""
        conn = MagicMock()

        result = parse_mentions(conn, "Lihtsalt tekst, pole mainimisi.", _ORG_ID)

        assert result == []
        conn.execute.assert_not_called()

    def test_parse_mentions_db_error_is_swallowed(self):
        """A DB error on a single token is logged and skipped; others proceed."""
        conn = MagicMock()
        conn.execute.side_effect = RuntimeError("DB error")

        # Should not raise — returns empty list gracefully.
        result = parse_mentions(conn, "@kasutaja kommenteerib.", _ORG_ID)
        assert result == []

    # ------------------------------------------------------------------
    # list_annotations_for_version_row
    # ------------------------------------------------------------------

    def test_list_for_version_row_queries_correct_target(self):
        """Helper builds target_id as '{row_kind}:{row_key}' and filters by version."""
        conn = MagicMock()
        row = _make_annotation_row(
            target_type="impact_report_item",
            target_id="conflict:abc123",
            draft_version_id=_VERSION_ID,
        )
        conn.execute.return_value.fetchall.return_value = [row]

        results = list_annotations_for_version_row(conn, _VERSION_ID, "conflict", "abc123")

        assert len(results) == 1
        assert results[0].target_id == "conflict:abc123"

        sql, params = conn.execute.call_args.args
        assert "impact_report_item" in sql
        assert "draft_version_id" in sql
        # target_id param must be the colon-delimited form
        assert "conflict:abc123" in params

    def test_list_for_version_row_returns_empty_on_db_error(self):
        """DB errors are caught and an empty list is returned."""
        conn = MagicMock()
        conn.execute.side_effect = RuntimeError("DB error")

        result = list_annotations_for_version_row(conn, _VERSION_ID, "gap", "some-key")
        assert result == []

    def test_list_for_version_row_empty_result(self):
        """No matching rows → empty list, no exception."""
        conn = MagicMock()
        conn.execute.return_value.fetchall.return_value = []

        result = list_annotations_for_version_row(
            conn, _VERSION_ID, "entity", "http://example.org/Provision/1"
        )
        assert result == []

    # ------------------------------------------------------------------
    # §4.2-equivalent contract: new API surface is exported
    # ------------------------------------------------------------------

    def test_new_helpers_are_exported_from_module(self):
        """Placeholder: asserts that PR-A exposes the helpers PR-B will call.

        The actual write-path contract test (encryption mandatory on create)
        lands in PR-B.
        """
        import app.annotations.models as m

        assert callable(m.parse_mentions)
        assert callable(m.list_annotations_for_version_row)
