"""Integration + unit tests for the §9.4 version-scoped row-annotation routes.

Covers the four PR-B handlers:

    GET    /annotations/version/{draft_version_id}/{row_kind}/{row_key}
    POST   /annotations/version/{draft_version_id}/{row_kind}/{row_key}/messages
    POST   /annotations/version/{draft_version_id}/{row_kind}/{row_key}/resolve
    POST   /annotations/version/{draft_version_id}/{row_kind}/{row_key}/reopen

Plus the supporting model-layer write helpers:

    create_row_annotation, resolve_row_thread, reopen_row_thread

Patterns follow ``tests/test_annotations_routes.py`` and
``tests/test_annotations_models.py`` (mocked DB connection,
``_authed_client`` for auth).

ACL contract (sprint plan §6 Days 1-2):
    - Cross-org draft_version_id → 404 (NOT 403, no leakage)
    - Invalid UUID format → 404
    - Non-existent draft_version_id → 404
    - Invalid row_kind → 400
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from cryptography.fernet import Fernet
from starlette.testclient import TestClient

from app.annotations.models import Annotation

# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------

_ORG_ID = "11111111-1111-1111-1111-111111111111"
_OTHER_ORG_ID = "22222222-2222-2222-2222-222222222222"
_USER_ID = "33333333-3333-3333-3333-333333333333"
_OTHER_USER_ID = "44444444-4444-4444-4444-444444444444"
_VERSION_ID = uuid.UUID("55555555-5555-5555-5555-555555555555")
_OTHER_VERSION_ID = uuid.UUID("66666666-6666-6666-6666-666666666666")
_ANN_ID = uuid.UUID("77777777-7777-7777-7777-777777777777")

_ROW_KIND = "conflict"
_ROW_KEY = "abc-123"
_ROW_BASE = f"/annotations/version/{_VERSION_ID}/{_ROW_KIND}/{_ROW_KEY}"


def _authed_user(
    user_id: str = _USER_ID,
    org_id: str = _ORG_ID,
    role: str = "drafter",
) -> dict[str, Any]:
    return {
        "id": user_id,
        "email": "kasutaja@seadusloome.ee",
        "full_name": "Test Kasutaja",
        "role": role,
        "org_id": org_id,
    }


def _stub_provider(user: dict[str, Any] | None = None) -> MagicMock:
    """Build a provider whose ``get_current_user`` returns the given user."""
    provider = MagicMock()
    provider.get_current_user.return_value = user or _authed_user()
    return provider


def _authed_client() -> TestClient:
    """Return a TestClient preloaded with a valid ``access_token`` cookie."""
    client = TestClient(
        __import__("app.main", fromlist=["app"]).app,
        follow_redirects=False,
    )
    client.cookies.set("access_token", "stub-token")
    return client


def _make_annotation(
    *,
    ann_id: uuid.UUID = _ANN_ID,
    user_id: str = _USER_ID,
    org_id: str = _ORG_ID,
    target_id: str = f"{_ROW_KIND}:{_ROW_KEY}",
    content: str = "Test märkus",
    resolved: bool = False,
    resolved_by: str | None = None,
    draft_version_id: uuid.UUID | None = _VERSION_ID,
    mentions: list[uuid.UUID] | None = None,
    stale: bool = False,
) -> Annotation:
    now = datetime.now(UTC)
    return Annotation(
        id=ann_id,
        user_id=uuid.UUID(user_id),
        org_id=uuid.UUID(org_id),
        target_type="impact_report_item",
        target_id=target_id,
        target_metadata=None,
        content=content,
        resolved=resolved,
        resolved_by=uuid.UUID(resolved_by) if resolved_by else None,
        resolved_at=now if resolved else None,
        created_at=now,
        updated_at=now,
        draft_version_id=draft_version_id,
        mentions=mentions or [],
        stale=stale,
    )


@pytest.fixture
def fernet_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Install a fresh Fernet key so encrypt_text/decrypt_text round-trip works."""
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("STORAGE_ENCRYPTION_KEY", Fernet.generate_key().decode())
    import app.storage.encrypted as encrypted_module

    monkeypatch.setattr(encrypted_module, "_fernet", None)


def _patch_acl_lookup(*, owning_org_id: str | None) -> Any:
    """Build a context manager that stubs the draft_version → org lookup.

    ``owning_org_id`` becomes the value returned by the JOIN; pass ``None``
    to simulate a missing draft_version_id (404 path).
    """
    mock_db = MagicMock()
    if owning_org_id is None:
        mock_db.execute.return_value.fetchone.return_value = None
    else:
        mock_db.execute.return_value.fetchone.return_value = (uuid.UUID(owning_org_id),)
    mock_conn = MagicMock()
    mock_conn.return_value.__enter__ = MagicMock(return_value=mock_db)
    mock_conn.return_value.__exit__ = MagicMock(return_value=False)
    return mock_conn, mock_db


# ---------------------------------------------------------------------------
# Auth / ACL
# ---------------------------------------------------------------------------


class TestUnauthenticatedRedirects:
    def test_get_panel_redirects(self):
        from app.main import app

        client = TestClient(app, follow_redirects=False)
        resp = client.get(_ROW_BASE)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/auth/login"

    def test_post_message_redirects(self):
        from app.main import app

        client = TestClient(app, follow_redirects=False)
        resp = client.post(f"{_ROW_BASE}/messages", json={"content": "x"})
        assert resp.status_code == 303

    def test_post_resolve_redirects(self):
        from app.main import app

        client = TestClient(app, follow_redirects=False)
        resp = client.post(f"{_ROW_BASE}/resolve")
        assert resp.status_code == 303

    def test_post_reopen_redirects(self):
        from app.main import app

        client = TestClient(app, follow_redirects=False)
        resp = client.post(f"{_ROW_BASE}/reopen")
        assert resp.status_code == 303


class TestAclCrossOrgReturns404:
    """Cross-org draft_version_id MUST return 404 on every route."""

    @patch("app.annotations.routes._connect")
    @patch("app.auth.middleware._get_provider")
    def test_get_panel_cross_org_404(self, mock_prov, mock_connect):
        mock_prov.return_value = _stub_provider()
        mock_conn, _ = _patch_acl_lookup(owning_org_id=_OTHER_ORG_ID)
        mock_connect.side_effect = mock_conn

        client = _authed_client()
        resp = client.get(_ROW_BASE)
        assert resp.status_code == 404

    @patch("app.annotations.routes._connect")
    @patch("app.auth.middleware._get_provider")
    def test_post_message_cross_org_404(self, mock_prov, mock_connect):
        mock_prov.return_value = _stub_provider()
        mock_conn, _ = _patch_acl_lookup(owning_org_id=_OTHER_ORG_ID)
        mock_connect.side_effect = mock_conn

        client = _authed_client()
        resp = client.post(f"{_ROW_BASE}/messages", json={"content": "x"})
        assert resp.status_code == 404

    @patch("app.annotations.routes._connect")
    @patch("app.auth.middleware._get_provider")
    def test_post_resolve_cross_org_404(self, mock_prov, mock_connect):
        mock_prov.return_value = _stub_provider()
        mock_conn, _ = _patch_acl_lookup(owning_org_id=_OTHER_ORG_ID)
        mock_connect.side_effect = mock_conn

        client = _authed_client()
        resp = client.post(f"{_ROW_BASE}/resolve")
        assert resp.status_code == 404

    @patch("app.annotations.routes._connect")
    @patch("app.auth.middleware._get_provider")
    def test_post_reopen_cross_org_404(self, mock_prov, mock_connect):
        mock_prov.return_value = _stub_provider()
        mock_conn, _ = _patch_acl_lookup(owning_org_id=_OTHER_ORG_ID)
        mock_connect.side_effect = mock_conn

        client = _authed_client()
        resp = client.post(f"{_ROW_BASE}/reopen")
        assert resp.status_code == 404


class TestAclInvalidVersionFormatReturns404:
    @patch("app.auth.middleware._get_provider")
    def test_invalid_uuid_format_404(self, mock_prov):
        mock_prov.return_value = _stub_provider()
        client = _authed_client()
        resp = client.get(f"/annotations/version/not-a-uuid/{_ROW_KIND}/{_ROW_KEY}")
        assert resp.status_code == 404


class TestAclNonExistentVersionReturns404:
    @patch("app.annotations.routes._connect")
    @patch("app.auth.middleware._get_provider")
    def test_missing_version_404(self, mock_prov, mock_connect):
        mock_prov.return_value = _stub_provider()
        mock_conn, _ = _patch_acl_lookup(owning_org_id=None)  # JOIN returns NULL
        mock_connect.side_effect = mock_conn

        client = _authed_client()
        resp = client.get(_ROW_BASE)
        assert resp.status_code == 404


class TestRowKindValidation:
    @patch("app.auth.middleware._get_provider")
    def test_invalid_row_kind_returns_400(self, mock_prov):
        mock_prov.return_value = _stub_provider()
        client = _authed_client()
        bad_url = f"/annotations/version/{_VERSION_ID}/invalid/{_ROW_KEY}"
        resp = client.get(bad_url)
        assert resp.status_code == 400

    @patch("app.auth.middleware._get_provider")
    def test_invalid_row_kind_on_post_returns_400(self, mock_prov):
        mock_prov.return_value = _stub_provider()
        client = _authed_client()
        bad_url = f"/annotations/version/{_VERSION_ID}/invalid/{_ROW_KEY}/messages"
        resp = client.post(bad_url, json={"content": "x"})
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# GET /annotations/version/...  — fetches the side-panel fragment
# ---------------------------------------------------------------------------


class TestGetRowPanel:
    @patch("app.annotations.routes._load_panel_messages")
    @patch("app.annotations.routes._connect")
    @patch("app.auth.middleware._get_provider")
    def test_returns_fragment_for_valid_version(self, mock_prov, mock_connect, mock_load):
        mock_prov.return_value = _stub_provider()
        mock_conn, _ = _patch_acl_lookup(owning_org_id=_ORG_ID)
        mock_connect.side_effect = mock_conn
        mock_load.return_value = []  # empty thread

        client = _authed_client()
        resp = client.get(_ROW_BASE)

        assert resp.status_code == 200
        body = resp.text
        assert "annotation-side-panel-fragment" in body
        # Estonian "Vastuolu märkused" header for row_kind='conflict'
        assert "Vastuolu" in body
        assert "Märkuseid ei ole veel lisatud" in body

    @patch("app.annotations.routes._load_panel_messages")
    @patch("app.annotations.routes._connect")
    @patch("app.auth.middleware._get_provider")
    def test_renders_messages_in_reverse_chrono(self, mock_prov, mock_connect, mock_load):
        mock_prov.return_value = _stub_provider()
        mock_conn, _ = _patch_acl_lookup(owning_org_id=_ORG_ID)
        mock_connect.side_effect = mock_conn

        # The model already returns DESC; just verify the fragment shows them.
        first = _make_annotation(content="Esimene")
        second = _make_annotation(
            ann_id=uuid.uuid4(),
            content="Teine",
        )
        mock_load.return_value = [second, first]

        with patch(
            "app.annotations.routes._user_display_name",
            return_value="Test Kasutaja",
        ):
            client = _authed_client()
            resp = client.get(_ROW_BASE)

        assert resp.status_code == 200
        # Newest first: "Teine" appears before "Esimene" in the rendered HTML.
        assert resp.text.index("Teine") < resp.text.index("Esimene")

    @patch("app.annotations.routes._load_panel_messages")
    @patch("app.annotations.routes._connect")
    @patch("app.auth.middleware._get_provider")
    def test_stale_banner_shown_when_thread_stale(self, mock_prov, mock_connect, mock_load):
        mock_prov.return_value = _stub_provider()
        mock_conn, _ = _patch_acl_lookup(owning_org_id=_ORG_ID)
        mock_connect.side_effect = mock_conn
        mock_load.return_value = [_make_annotation(stale=True)]

        with patch(
            "app.annotations.routes._user_display_name",
            return_value="Test Kasutaja",
        ):
            client = _authed_client()
            resp = client.get(_ROW_BASE)

        assert resp.status_code == 200
        # Banner contains the Estonian "Aegunud" word.
        assert "aegunud" in resp.text.lower()

    @patch("app.annotations.routes._load_panel_messages")
    @patch("app.annotations.routes._connect")
    @patch("app.auth.middleware._get_provider")
    def test_resolve_button_when_open(self, mock_prov, mock_connect, mock_load):
        mock_prov.return_value = _stub_provider()
        mock_conn, _ = _patch_acl_lookup(owning_org_id=_ORG_ID)
        mock_connect.side_effect = mock_conn
        mock_load.return_value = [_make_annotation(resolved=False)]

        with patch(
            "app.annotations.routes._user_display_name",
            return_value="Test Kasutaja",
        ):
            client = _authed_client()
            resp = client.get(_ROW_BASE)

        assert resp.status_code == 200
        assert "Lahenda" in resp.text

    @patch("app.annotations.routes._load_panel_messages")
    @patch("app.annotations.routes._connect")
    @patch("app.auth.middleware._get_provider")
    def test_reopen_button_when_resolved(self, mock_prov, mock_connect, mock_load):
        mock_prov.return_value = _stub_provider()
        mock_conn, _ = _patch_acl_lookup(owning_org_id=_ORG_ID)
        mock_connect.side_effect = mock_conn
        mock_load.return_value = [_make_annotation(resolved=True, resolved_by=_USER_ID)]

        with patch(
            "app.annotations.routes._user_display_name",
            return_value="Test Kasutaja",
        ):
            client = _authed_client()
            resp = client.get(_ROW_BASE)

        assert resp.status_code == 200
        assert "Ava uuesti" in resp.text


# ---------------------------------------------------------------------------
# POST /annotations/version/.../messages
# ---------------------------------------------------------------------------


class TestPostRowMessage:
    @patch("app.annotations.routes.log_row_annotation_create")
    @patch("app.annotations.routes._load_panel_messages")
    @patch("app.annotations.routes.create_row_annotation")
    @patch("app.annotations.routes._connect")
    @patch("app.auth.middleware._get_provider")
    def test_first_message_creates_thread_and_logs_create(
        self,
        mock_prov,
        mock_connect,
        mock_create,
        mock_load,
        mock_audit_create,
    ):
        mock_prov.return_value = _stub_provider()
        mock_conn, _ = _patch_acl_lookup(owning_org_id=_ORG_ID)
        # _connect is called multiple times: ACL, pre-count, write.  Use the
        # same connection mock for all so we can assert generally.
        mock_connect.side_effect = mock_conn

        # Pre-count returns empty (first message); after-write returns the new one.
        new_ann = _make_annotation(content="Esimene rida")
        mock_load.side_effect = [[], [new_ann]]
        mock_create.return_value = new_ann

        with patch(
            "app.annotations.routes._user_display_name",
            return_value="Test Kasutaja",
        ):
            client = _authed_client()
            resp = client.post(
                f"{_ROW_BASE}/messages",
                json={"content": "Esimene rida"},
            )

        assert resp.status_code == 200
        # First message → annotation.row.create audit.
        mock_audit_create.assert_called_once()
        assert mock_audit_create.call_args.args[3] == _ROW_KIND
        assert mock_audit_create.call_args.args[4] == _ROW_KEY

    @patch("app.annotations.routes.log_row_annotation_message")
    @patch("app.annotations.routes._load_panel_messages")
    @patch("app.annotations.routes.create_row_annotation")
    @patch("app.annotations.routes._connect")
    @patch("app.auth.middleware._get_provider")
    def test_followup_message_logs_message_audit(
        self,
        mock_prov,
        mock_connect,
        mock_create,
        mock_load,
        mock_audit_msg,
    ):
        mock_prov.return_value = _stub_provider()
        mock_conn, _ = _patch_acl_lookup(owning_org_id=_ORG_ID)
        mock_connect.side_effect = mock_conn

        existing = _make_annotation(content="Olemasolev")
        new_ann = _make_annotation(ann_id=uuid.uuid4(), content="Vastus")
        # Pre-count returns one; after-write returns both.
        mock_load.side_effect = [[existing], [new_ann, existing]]
        mock_create.return_value = new_ann

        with patch(
            "app.annotations.routes._user_display_name",
            return_value="Test Kasutaja",
        ):
            client = _authed_client()
            resp = client.post(
                f"{_ROW_BASE}/messages",
                json={"content": "Vastus"},
            )

        assert resp.status_code == 200
        # Follow-up → annotation.row.message.create audit.
        mock_audit_msg.assert_called_once()

    @patch("app.annotations.routes._load_panel_messages")
    @patch("app.annotations.routes._connect")
    @patch("app.auth.middleware._get_provider")
    def test_empty_content_returns_400(self, mock_prov, mock_connect, mock_load):
        mock_prov.return_value = _stub_provider()
        mock_conn, _ = _patch_acl_lookup(owning_org_id=_ORG_ID)
        mock_connect.side_effect = mock_conn
        mock_load.return_value = []

        client = _authed_client()
        resp = client.post(f"{_ROW_BASE}/messages", json={"content": "   "})
        assert resp.status_code == 400

    @patch("app.annotations.routes._load_panel_messages")
    @patch("app.annotations.routes._connect")
    @patch("app.auth.middleware._get_provider")
    def test_message_into_resolved_thread_returns_409(self, mock_prov, mock_connect, mock_load):
        """Cannot append to a resolved thread without reopening first."""
        mock_prov.return_value = _stub_provider()
        mock_conn, _ = _patch_acl_lookup(owning_org_id=_ORG_ID)
        mock_connect.side_effect = mock_conn

        # All existing messages are resolved → 409.
        mock_load.return_value = [_make_annotation(resolved=True, resolved_by=_USER_ID)]

        client = _authed_client()
        resp = client.post(f"{_ROW_BASE}/messages", json={"content": "x"})
        assert resp.status_code == 409


class TestMentionParsingOnWrite:
    """Verify @mention resolution: in-org users resolved, out-of-org dropped."""

    def test_in_org_mention_resolved_to_uuid(self, fernet_key):
        from app.annotations.models import create_row_annotation

        in_org_uid = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")

        # parse_mentions issues SELECTs against `users`; the INSERT issues
        # one final SELECT-equivalent (it's a RETURNING).  Track call order
        # by side_effect lists.
        select_returns = [(in_org_uid,)]  # in-org user resolves
        insert_row = (
            uuid.uuid4(),  # id
            uuid.UUID(_USER_ID),  # user_id
            uuid.UUID(_ORG_ID),  # org_id
            "impact_report_item",  # target_type
            f"{_ROW_KIND}:{_ROW_KEY}",  # target_id
            None,  # target_metadata
            None,  # content (NULL — encrypted-only write)
            False,  # resolved
            None,  # resolved_by
            None,  # resolved_at
            datetime.now(UTC),  # created_at
            datetime.now(UTC),  # updated_at
            b"ciphertext",  # content_encrypted
            _VERSION_ID,  # draft_version_id
            [in_org_uid],  # mentions
            False,  # stale
        )

        # Drive the cursor: SELECTs return mention rows, the final INSERT
        # returns the new annotation row.
        cursor = MagicMock()
        # Each .execute() returns a cursor whose .fetchone()/.fetchall() can
        # be called.  We pin .execute to return self and switch fetchone
        # via side_effect.
        cursor.execute.return_value = cursor
        cursor.fetchone.side_effect = [*select_returns, insert_row]

        result = create_row_annotation(
            cursor,
            user_id=_USER_ID,
            org_id=_ORG_ID,
            draft_version_id=_VERSION_ID,
            row_kind=_ROW_KIND,
            row_key=_ROW_KEY,
            content="Vaata @kasutaja kommentaari.",
        )

        # Mentions array on the returned annotation contains the in-org UUID.
        assert in_org_uid in result.mentions

    def test_out_of_org_mention_silently_dropped(self, fernet_key):
        from app.annotations.models import create_row_annotation

        # parse_mentions: SELECT returns None (out-of-org).
        select_returns = [None]
        insert_row = (
            uuid.uuid4(),
            uuid.UUID(_USER_ID),
            uuid.UUID(_ORG_ID),
            "impact_report_item",
            f"{_ROW_KIND}:{_ROW_KEY}",
            None,
            None,
            False,
            None,
            None,
            datetime.now(UTC),
            datetime.now(UTC),
            b"ciphertext",
            _VERSION_ID,
            [],  # mentions empty — out-of-org dropped
            False,
        )
        cursor = MagicMock()
        cursor.execute.return_value = cursor
        cursor.fetchone.side_effect = [*select_returns, insert_row]

        result = create_row_annotation(
            cursor,
            user_id=_USER_ID,
            org_id=_ORG_ID,
            draft_version_id=_VERSION_ID,
            row_kind=_ROW_KIND,
            row_key=_ROW_KEY,
            content="Vaata @stranger kommentaari.",
        )
        assert result.mentions == []


# ---------------------------------------------------------------------------
# Encryption round-trip
# ---------------------------------------------------------------------------


class TestEncryptionRoundTrip:
    """End-to-end check: written ciphertext decrypts back to original."""

    def test_message_body_decrypts_back_to_plaintext(self, fernet_key):
        from app.annotations.models import _row_to_annotation
        from app.storage import encrypt_text

        plaintext = "Salajane sõnum mis vajab krüpteerimist."
        ciphertext = encrypt_text(plaintext)

        # Raw bytes MUST NOT equal plaintext (pre-condition).
        assert ciphertext != plaintext.encode("utf-8")

        now = datetime.now(UTC)
        row = (
            uuid.uuid4(),
            uuid.UUID(_USER_ID),
            uuid.UUID(_ORG_ID),
            "impact_report_item",
            f"{_ROW_KIND}:{_ROW_KEY}",
            None,
            None,  # plaintext column NULL
            False,
            None,
            None,
            now,
            now,
            ciphertext,  # encrypted column populated
            _VERSION_ID,
            [],
            False,
        )

        ann = _row_to_annotation(row)
        # Round-trip succeeds.
        assert ann.content == plaintext

    def test_create_row_annotation_writes_ciphertext_not_plaintext(self, fernet_key):
        """The INSERT must NEVER write the plaintext to the content_encrypted column."""
        from app.annotations.models import create_row_annotation

        plaintext = "Tundlik info eelnõust."
        cursor = MagicMock()
        cursor.execute.return_value = cursor
        # parse_mentions has no @ → no SELECT calls; only the INSERT runs.
        now = datetime.now(UTC)
        cursor.fetchone.return_value = (
            uuid.uuid4(),
            uuid.UUID(_USER_ID),
            uuid.UUID(_ORG_ID),
            "impact_report_item",
            f"{_ROW_KIND}:{_ROW_KEY}",
            None,
            None,
            False,
            None,
            None,
            now,
            now,
            b"placeholder-ciphertext",
            _VERSION_ID,
            [],
            False,
        )

        create_row_annotation(
            cursor,
            user_id=_USER_ID,
            org_id=_ORG_ID,
            draft_version_id=_VERSION_ID,
            row_kind=_ROW_KIND,
            row_key=_ROW_KEY,
            content=plaintext,
        )

        # Inspect the INSERT call: positional args = (sql, params_tuple).
        # The 4th param in our INSERT signature is the ciphertext bytes —
        # assert it is NOT the plaintext UTF-8 bytes.
        insert_call = cursor.execute.call_args
        assert insert_call is not None
        sql = insert_call.args[0]
        params = insert_call.args[1]
        assert "INSERT INTO annotations" in sql
        # ciphertext is the 4th param (index 3): user_id, org_id, target_id, ciphertext
        ciphertext_param = params[3]
        assert isinstance(ciphertext_param, bytes)
        assert ciphertext_param != plaintext.encode("utf-8")
        # Plaintext column is NULLed via SQL literal NULL — params has no
        # plaintext.
        assert plaintext not in [p for p in params if isinstance(p, str)]


# ---------------------------------------------------------------------------
# Cross-version isolation
# ---------------------------------------------------------------------------


class TestCrossVersionIsolation:
    """Same (row_kind, row_key) on different versions → independent threads."""

    def test_list_for_version_row_filters_by_version(self):
        from app.annotations.models import list_annotations_for_version_row

        v1_ann = _make_annotation(ann_id=uuid.uuid4(), draft_version_id=_VERSION_ID)
        cursor = MagicMock()
        cursor.execute.return_value = cursor

        # Build the row tuple matching _ANNOTATION_COLUMNS order so the
        # internal _row_to_annotation can hydrate it.
        now = datetime.now(UTC)
        v1_row = (
            v1_ann.id,
            v1_ann.user_id,
            v1_ann.org_id,
            v1_ann.target_type,
            v1_ann.target_id,
            None,
            v1_ann.content,
            False,
            None,
            None,
            now,
            now,
            None,
            v1_ann.draft_version_id,
            [],
            False,
        )
        cursor.fetchall.return_value = [v1_row]

        # Query for v1 → returns the v1 row.
        v1_results = list_annotations_for_version_row(cursor, _VERSION_ID, _ROW_KIND, _ROW_KEY)
        assert len(v1_results) == 1

        # Verify the SQL includes draft_version_id with the v1 UUID
        # (cross-version isolation is enforced at the WHERE clause level).
        sql, params = cursor.execute.call_args.args
        assert "draft_version_id" in sql
        assert str(_VERSION_ID) in params

        # When called for v2, the WHERE clause uses a different UUID;
        # mock the result for that query as empty to confirm independence.
        cursor.fetchall.return_value = []
        v2_results = list_annotations_for_version_row(
            cursor, _OTHER_VERSION_ID, _ROW_KIND, _ROW_KEY
        )
        assert v2_results == []


# ---------------------------------------------------------------------------
# Resolve / reopen toggle
# ---------------------------------------------------------------------------


class TestResolveReopenToggle:
    @patch("app.annotations.routes.log_row_annotation_resolve")
    @patch("app.annotations.routes._load_panel_messages")
    @patch("app.annotations.routes.resolve_row_thread")
    @patch("app.annotations.routes._connect")
    @patch("app.auth.middleware._get_provider")
    def test_resolve_flips_resolved_and_logs(
        self,
        mock_prov,
        mock_connect,
        mock_resolve,
        mock_load,
        mock_audit,
    ):
        mock_prov.return_value = _stub_provider()
        mock_conn, _ = _patch_acl_lookup(owning_org_id=_ORG_ID)
        mock_connect.side_effect = mock_conn

        mock_resolve.return_value = 1  # one row updated
        mock_load.return_value = [_make_annotation(resolved=True, resolved_by=_USER_ID)]

        with patch(
            "app.annotations.routes._user_display_name",
            return_value="Test Kasutaja",
        ):
            client = _authed_client()
            resp = client.post(f"{_ROW_BASE}/resolve")

        assert resp.status_code == 200
        # Audit log emitted.
        mock_audit.assert_called_once()
        # The fragment now shows the reopen button (because resolved).
        assert "Ava uuesti" in resp.text

    @patch("app.annotations.routes.log_row_annotation_reopen")
    @patch("app.annotations.routes._load_panel_messages")
    @patch("app.annotations.routes.reopen_row_thread")
    @patch("app.annotations.routes._connect")
    @patch("app.auth.middleware._get_provider")
    def test_reopen_flips_resolved_and_logs(
        self,
        mock_prov,
        mock_connect,
        mock_reopen,
        mock_load,
        mock_audit,
    ):
        mock_prov.return_value = _stub_provider()
        mock_conn, _ = _patch_acl_lookup(owning_org_id=_ORG_ID)
        mock_connect.side_effect = mock_conn

        mock_reopen.return_value = 1
        mock_load.return_value = [_make_annotation(resolved=False)]

        with patch(
            "app.annotations.routes._user_display_name",
            return_value="Test Kasutaja",
        ):
            client = _authed_client()
            resp = client.post(f"{_ROW_BASE}/reopen")

        assert resp.status_code == 200
        mock_audit.assert_called_once()
        # The fragment now shows the resolve button again.
        assert "Lahenda" in resp.text

    def test_resolve_row_thread_sets_resolved_by_and_resolved_at(self):
        """Service-layer assertion: resolve_row_thread sets the right columns."""
        from app.annotations.models import resolve_row_thread

        cursor = MagicMock()
        cursor.execute.return_value = cursor
        cursor.rowcount = 2  # two rows updated

        updated = resolve_row_thread(
            cursor,
            draft_version_id=_VERSION_ID,
            row_kind=_ROW_KIND,
            row_key=_ROW_KEY,
            resolved_by_user_id=_USER_ID,
        )
        assert updated == 2

        sql, params = cursor.execute.call_args.args
        assert "resolved = TRUE" in sql
        assert "resolved_by = %s" in sql
        assert "resolved_at = now()" in sql
        assert _USER_ID in params

    def test_reopen_row_thread_clears_resolved_by_and_at(self):
        """Service-layer assertion: reopen clears resolved_by + resolved_at."""
        from app.annotations.models import reopen_row_thread

        cursor = MagicMock()
        cursor.execute.return_value = cursor
        cursor.rowcount = 1

        updated = reopen_row_thread(
            cursor,
            draft_version_id=_VERSION_ID,
            row_kind=_ROW_KIND,
            row_key=_ROW_KEY,
        )
        assert updated == 1

        sql, _ = cursor.execute.call_args.args
        assert "resolved = FALSE" in sql
        assert "resolved_by = NULL" in sql
        assert "resolved_at = NULL" in sql


# ---------------------------------------------------------------------------
# Stale flag preservation
# ---------------------------------------------------------------------------


class TestStaleFlagPreserved:
    """PR-B service does NOT touch the stale column; analyse logic is PR-C."""

    def test_create_row_annotation_does_not_set_stale(self, fernet_key):
        """The INSERT column list must not reference stale (only RETURNING does).

        Column-list references like ``mentions, stale`` in ``RETURNING ...``
        are fine — that's a SELECT, not a write — but the INSERT INTO list
        must not name ``stale`` so the schema default (``FALSE``) is used.
        """
        from app.annotations.models import create_row_annotation

        cursor = MagicMock()
        cursor.execute.return_value = cursor
        now = datetime.now(UTC)
        cursor.fetchone.return_value = (
            uuid.uuid4(),
            uuid.UUID(_USER_ID),
            uuid.UUID(_ORG_ID),
            "impact_report_item",
            f"{_ROW_KIND}:{_ROW_KEY}",
            None,
            None,
            False,
            None,
            None,
            now,
            now,
            b"ciphertext",
            _VERSION_ID,
            [],
            False,
        )

        create_row_annotation(
            cursor,
            user_id=_USER_ID,
            org_id=_ORG_ID,
            draft_version_id=_VERSION_ID,
            row_kind=_ROW_KIND,
            row_key=_ROW_KEY,
            content="x",
        )

        sql = cursor.execute.call_args.args[0]
        # Extract the INSERT column list (between the first '(' and ')')
        # to assert stale isn't named there.
        insert_idx = sql.upper().index("INSERT INTO ANNOTATIONS")
        column_list_start = sql.index("(", insert_idx)
        column_list_end = sql.index(")", column_list_start)
        column_list = sql[column_list_start : column_list_end + 1].lower()
        assert "stale" not in column_list

    def test_resolve_does_not_touch_stale(self):
        from app.annotations.models import resolve_row_thread

        cursor = MagicMock()
        cursor.execute.return_value = cursor
        cursor.rowcount = 1

        resolve_row_thread(
            cursor,
            draft_version_id=_VERSION_ID,
            row_kind=_ROW_KIND,
            row_key=_ROW_KEY,
            resolved_by_user_id=_USER_ID,
        )

        sql = cursor.execute.call_args.args[0]
        # The UPDATE statement may reference the stale column only if it
        # writes to it; the SET clause must not contain "stale =".
        assert "stale =" not in sql.lower()
        assert "stale=" not in sql.lower()

    def test_reopen_does_not_touch_stale(self):
        from app.annotations.models import reopen_row_thread

        cursor = MagicMock()
        cursor.execute.return_value = cursor
        cursor.rowcount = 1

        reopen_row_thread(
            cursor,
            draft_version_id=_VERSION_ID,
            row_kind=_ROW_KIND,
            row_key=_ROW_KEY,
        )

        sql = cursor.execute.call_args.args[0]
        assert "stale =" not in sql.lower()
        assert "stale=" not in sql.lower()


# ---------------------------------------------------------------------------
# Model-layer unit tests for the new helpers
# ---------------------------------------------------------------------------


class TestRowKindWhitelist:
    def test_create_rejects_invalid_row_kind(self, fernet_key):
        from app.annotations.models import create_row_annotation

        with pytest.raises(ValueError, match="Invalid row_kind"):
            create_row_annotation(
                MagicMock(),
                user_id=_USER_ID,
                org_id=_ORG_ID,
                draft_version_id=_VERSION_ID,
                row_kind="bogus",
                row_key="x",
                content="x",
            )

    def test_create_rejects_empty_content(self, fernet_key):
        from app.annotations.models import create_row_annotation

        with pytest.raises(ValueError, match="content must not be empty"):
            create_row_annotation(
                MagicMock(),
                user_id=_USER_ID,
                org_id=_ORG_ID,
                draft_version_id=_VERSION_ID,
                row_kind=_ROW_KIND,
                row_key=_ROW_KEY,
                content="   ",
            )

    def test_resolve_rejects_invalid_row_kind(self):
        from app.annotations.models import resolve_row_thread

        with pytest.raises(ValueError, match="Invalid row_kind"):
            resolve_row_thread(
                MagicMock(),
                draft_version_id=_VERSION_ID,
                row_kind="bogus",
                row_key="x",
                resolved_by_user_id=_USER_ID,
            )

    def test_reopen_rejects_invalid_row_kind(self):
        from app.annotations.models import reopen_row_thread

        with pytest.raises(ValueError, match="Invalid row_kind"):
            reopen_row_thread(
                MagicMock(),
                draft_version_id=_VERSION_ID,
                row_kind="bogus",
                row_key="x",
            )


# ---------------------------------------------------------------------------
# #773: URL-encoded URI row_key round-trip
# ---------------------------------------------------------------------------


class TestUriRowKeyRoundTrip:
    """Affected-entity + EU rows store the raw ontology URI as the row_key.

    These URIs carry ``/``, ``:``, and ``#`` characters — all of which
    Starlette decodes BEFORE routing, so a plain ``{row_key}`` path
    segment 404s. The route uses the ``:path`` converter + the handler
    decodes the percent-encoded segment so the original URI reaches the
    DB layer intact.
    """

    _URI_ROW_KEY = "https://data.riik.ee/ontology/estleg#KarS"

    @patch("app.annotations.routes._load_panel_messages")
    @patch("app.annotations.routes._connect")
    @patch("app.auth.middleware._get_provider")
    def test_get_panel_resolves_encoded_uri_to_original(self, mock_prov, mock_connect, mock_load):
        from urllib.parse import quote

        mock_prov.return_value = _stub_provider()
        mock_conn, _ = _patch_acl_lookup(owning_org_id=_ORG_ID)
        mock_connect.side_effect = mock_conn
        mock_load.return_value = []

        encoded = quote(self._URI_ROW_KEY, safe="")
        url = f"/annotations/version/{_VERSION_ID}/entity/{encoded}"

        client = _authed_client()
        resp = client.get(url)

        assert resp.status_code == 200
        # _load_panel_messages received the DECODED URI, not the
        # percent-encoded path segment.
        assert mock_load.call_args is not None
        _, kwargs = mock_load.call_args
        if kwargs:
            decoded_arg = kwargs.get("row_key")
        else:
            decoded_arg = mock_load.call_args.args[2]
        assert decoded_arg == self._URI_ROW_KEY

    @patch("app.annotations.routes._load_panel_messages")
    @patch("app.annotations.routes._connect")
    @patch("app.auth.middleware._get_provider")
    def test_uri_row_key_handler_renders_fragment(self, mock_prov, mock_connect, mock_load):
        """The handler renders the panel without raising for a URI row_key."""
        from urllib.parse import quote

        mock_prov.return_value = _stub_provider()
        mock_conn, _ = _patch_acl_lookup(owning_org_id=_ORG_ID)
        mock_connect.side_effect = mock_conn
        mock_load.return_value = []

        encoded = quote(self._URI_ROW_KEY, safe="")
        # Use "entity" row_kind because entity URIs are the real-world
        # source of URI row keys.
        url = f"/annotations/version/{_VERSION_ID}/entity/{encoded}"

        client = _authed_client()
        resp = client.get(url)

        assert resp.status_code == 200
        # The fragment carries the original URI in its data attribute so
        # JS / tests can round-trip it.
        assert self._URI_ROW_KEY in resp.text

    @patch("app.annotations.routes._load_panel_messages")
    @patch("app.annotations.routes.create_row_annotation")
    @patch("app.annotations.routes._connect")
    @patch("app.auth.middleware._get_provider")
    def test_post_message_with_uri_row_key(self, mock_prov, mock_connect, mock_create, mock_load):
        from urllib.parse import quote

        mock_prov.return_value = _stub_provider()
        mock_conn, _ = _patch_acl_lookup(owning_org_id=_ORG_ID)
        mock_connect.side_effect = mock_conn

        new_ann = _make_annotation(
            target_id=f"entity:{self._URI_ROW_KEY}",
            content="Märkus URI rea kohta.",
        )
        # Pre-count empty, after-write returns the new ann.
        mock_load.side_effect = [[], [new_ann]]
        mock_create.return_value = new_ann

        encoded = quote(self._URI_ROW_KEY, safe="")
        url = f"/annotations/version/{_VERSION_ID}/entity/{encoded}/messages"

        with patch(
            "app.annotations.routes._user_display_name",
            return_value="Test Kasutaja",
        ):
            client = _authed_client()
            resp = client.post(url, json={"content": "Märkus URI rea kohta."})

        assert resp.status_code == 200
        # The create call received the DECODED URI as row_key, not the
        # percent-encoded form.
        _, create_kwargs = mock_create.call_args
        assert create_kwargs.get("row_key") == self._URI_ROW_KEY

    @patch("app.annotations.routes._load_panel_messages")
    @patch("app.annotations.routes.resolve_row_thread")
    @patch("app.annotations.routes._connect")
    @patch("app.auth.middleware._get_provider")
    def test_post_resolve_with_uri_row_key(self, mock_prov, mock_connect, mock_resolve, mock_load):
        from urllib.parse import quote

        mock_prov.return_value = _stub_provider()
        mock_conn, _ = _patch_acl_lookup(owning_org_id=_ORG_ID)
        mock_connect.side_effect = mock_conn
        mock_resolve.return_value = 1
        mock_load.return_value = []

        encoded = quote(self._URI_ROW_KEY, safe="")
        url = f"/annotations/version/{_VERSION_ID}/entity/{encoded}/resolve"

        client = _authed_client()
        resp = client.post(url)

        assert resp.status_code == 200
        _, kwargs = mock_resolve.call_args
        assert kwargs.get("row_key") == self._URI_ROW_KEY


# ---------------------------------------------------------------------------
# #773: safe_row_key / decode_row_key / target_dom_id helpers
# ---------------------------------------------------------------------------


class TestRowKeyEncodingHelpers:
    """Unit tests for the URL- + CSS-safety helpers."""

    _URI = "https://data.riik.ee/ontology/estleg#KarS"

    def test_safe_row_key_percent_encodes_slash_colon_hash(self):
        from app.annotations.row_keys import safe_row_key

        encoded = safe_row_key(self._URI)
        # No literal ``/``, ``#``, or ``:`` in the encoded form.
        assert "/" not in encoded
        assert "#" not in encoded
        assert ":" not in encoded
        # The Estonian provision name still round-trips.
        assert "KarS" in encoded

    def test_decode_row_key_inverts_safe_row_key(self):
        from app.annotations.row_keys import decode_row_key, safe_row_key

        assert decode_row_key(safe_row_key(self._URI)) == self._URI

    def test_safe_row_key_is_noop_for_hashed_keys(self):
        """sha256-32 hex digests contain only [0-9a-f] so safe_row_key is a no-op."""
        from app.annotations.row_keys import safe_row_key

        digest = "abcdef0123456789abcdef0123456789"
        assert safe_row_key(digest) == digest

    def test_target_dom_id_is_css_safe(self):
        """The returned id contains only chars valid in CSS / HTMX selectors."""
        from app.annotations.row_keys import target_dom_id

        dom_id = target_dom_id("entity", self._URI)
        # No reserved characters that need backslash-escaping in CSS.
        for ch in ("/", ":", "#", "%", "?", "&", "="):
            assert ch not in dom_id
        # Stable prefix so callers can grep / identify it.
        assert dom_id.startswith("annotation-popover-entity-")

    def test_target_dom_id_is_deterministic(self):
        """Same input always yields the same id."""
        from app.annotations.row_keys import target_dom_id

        a = target_dom_id("entity", self._URI)
        b = target_dom_id("entity", self._URI)
        assert a == b

    def test_target_dom_id_distinguishes_kind(self):
        """Same target_id under different kinds yields different ids."""
        from app.annotations.row_keys import target_dom_id

        a = target_dom_id("entity", self._URI)
        b = target_dom_id("conflict", self._URI)
        assert a != b

    def test_target_dom_id_handles_empty(self):
        """Empty target_id still produces a valid CSS id (no crash)."""
        from app.annotations.row_keys import target_dom_id

        dom_id = target_dom_id("entity", "")
        assert dom_id.startswith("annotation-popover-entity-")
        # No invalid chars.
        for ch in ("/", ":", "#", "%"):
            assert ch not in dom_id
