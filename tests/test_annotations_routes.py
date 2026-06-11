"""Integration tests for the Phase 4 Annotation routes.

Tests exercise the full ``app.main.app`` via ``TestClient`` so
they validate the FastHTML wiring, the auth Beforeware, and the HTMX
fragment swap behaviour. External dependencies -- Postgres -- are
mocked out.

Patterns follow ``tests/test_docs_routes.py`` and
``tests/test_chat_routes.py``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch

from starlette.testclient import TestClient

from app.annotations.models import Annotation, AnnotationReply

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ORG_ID = "11111111-1111-1111-1111-111111111111"
_OTHER_ORG_ID = "22222222-2222-2222-2222-222222222222"
_USER_ID = "33333333-3333-3333-3333-333333333333"
_OTHER_USER_ID = "44444444-4444-4444-4444-444444444444"
_ANN_ID = uuid.UUID("55555555-5555-5555-5555-555555555555")
_REPLY_ID = uuid.UUID("66666666-6666-6666-6666-666666666666")
# #861: a real UUID draft id so the legacy-create target ACL can authorise it.
_DRAFT_TARGET_ID = "77777777-7777-7777-7777-777777777777"


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


def _make_annotation(
    *,
    ann_id: uuid.UUID = _ANN_ID,
    user_id: str = _USER_ID,
    org_id: str = _ORG_ID,
    target_type: str = "draft",
    target_id: str = "test-target-123",
    content: str = "Test markus",
    resolved: bool = False,
) -> Annotation:
    now = datetime.now(UTC)
    return Annotation(
        id=ann_id,
        user_id=uuid.UUID(user_id),
        org_id=uuid.UUID(org_id),
        target_type=target_type,
        target_id=target_id,
        target_metadata=None,
        content=content,
        resolved=resolved,
        resolved_by=None,
        resolved_at=None,
        created_at=now,
        updated_at=now,
    )


def _make_reply(
    *,
    reply_id: uuid.UUID = _REPLY_ID,
    annotation_id: uuid.UUID = _ANN_ID,
    user_id: str = _USER_ID,
    content: str = "Test vastus",
) -> AnnotationReply:
    now = datetime.now(UTC)
    return AnnotationReply(
        id=reply_id,
        annotation_id=annotation_id,
        user_id=uuid.UUID(user_id),
        content=content,
        created_at=now,
    )


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


# ---------------------------------------------------------------------------
# Unauthenticated requests redirect to login
# ---------------------------------------------------------------------------


class TestAuthRequired:
    def test_list_annotations_redirects_unauthenticated(self):
        from app.main import app

        client = TestClient(app, follow_redirects=False)
        resp = client.get("/api/annotations?target_type=draft&target_id=123")
        assert resp.status_code == 303
        assert resp.headers["location"] == "/auth/login"

    def test_create_annotation_redirects_unauthenticated(self):
        from app.main import app

        client = TestClient(app, follow_redirects=False)
        resp = client.post(
            "/api/annotations",
            json={"target_type": "draft", "target_id": "123", "content": "test"},
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/auth/login"


# ---------------------------------------------------------------------------
# GET /api/annotations — list annotations
# ---------------------------------------------------------------------------


class TestListAnnotations:
    @patch("app.annotations.routes._load_annotations_with_replies")
    @patch("app.auth.middleware._get_provider")
    def test_list_returns_popover_fragment(self, mock_prov, mock_load):
        mock_prov.return_value = _stub_provider()
        mock_load.return_value = []

        client = _authed_client()
        resp = client.get("/api/annotations?target_type=draft&target_id=test-123")
        assert resp.status_code == 200
        body = resp.text
        # Check that the popover container is rendered
        assert "annotation-popover" in body
        assert "Markused" in body

    @patch("app.auth.middleware._get_provider")
    def test_list_missing_params_returns_error(self, mock_prov):
        mock_prov.return_value = _stub_provider()

        client = _authed_client()
        resp = client.get("/api/annotations")
        assert resp.status_code == 200
        assert "Puuduvad parameetrid" in resp.text


# ---------------------------------------------------------------------------
# POST /api/annotations — create annotation
# ---------------------------------------------------------------------------


class TestCreateAnnotation:
    @patch("app.annotations.routes.log_annotation_create")
    @patch("app.annotations.routes._load_annotations_with_replies")
    @patch("app.annotations.routes._connect")
    @patch("app.auth.middleware._get_provider")
    def test_create_annotation_via_form(self, mock_prov, mock_conn, mock_load, mock_audit):
        mock_prov.return_value = _stub_provider()
        mock_load.return_value = []

        # Mock the DB connection context manager. The #861 target ACL queries
        # the draft's org_id first; return the caller's org so the draft is
        # in-org and the create proceeds.
        mock_db = MagicMock()
        mock_db.execute.return_value.fetchone.return_value = (uuid.UUID(_ORG_ID),)
        mock_conn.return_value.__enter__ = MagicMock(return_value=mock_db)
        mock_conn.return_value.__exit__ = MagicMock(return_value=False)

        ann = _make_annotation()
        with patch("app.annotations.routes.create_annotation", return_value=ann):
            client = _authed_client()
            resp = client.post(
                "/api/annotations",
                data={
                    # #861: target_id must be a real UUID-owned draft for the
                    # ownership ACL to authorise it.
                    "target_type": "draft",
                    "target_id": _DRAFT_TARGET_ID,
                    "content": "Uus markus",
                },
            )

        assert resp.status_code == 200
        assert "annotation-popover" in resp.text

    @patch("app.annotations.routes.log_annotation_create")
    @patch("app.annotations.routes._load_annotations_with_replies")
    @patch("app.annotations.routes._connect")
    @patch("app.auth.middleware._get_provider")
    def test_create_annotation_via_json(self, mock_prov, mock_conn, mock_load, mock_audit):
        mock_prov.return_value = _stub_provider()
        mock_load.return_value = []

        mock_db = MagicMock()
        mock_db.execute.return_value.fetchone.return_value = (uuid.UUID(_ORG_ID),)
        mock_conn.return_value.__enter__ = MagicMock(return_value=mock_db)
        mock_conn.return_value.__exit__ = MagicMock(return_value=False)

        ann = _make_annotation()
        with patch("app.annotations.routes.create_annotation", return_value=ann):
            client = _authed_client()
            resp = client.post(
                "/api/annotations",
                json={
                    "target_type": "draft",
                    "target_id": _DRAFT_TARGET_ID,
                    "content": "Uus markus",
                },
            )

        assert resp.status_code == 200
        assert "annotation-popover" in resp.text

    @patch("app.annotations.routes._connect")
    @patch("app.auth.middleware._get_provider")
    def test_create_annotation_cross_org_target_returns_404(self, mock_prov, mock_conn):
        """#861: annotating another org's draft is rejected with 404."""
        mock_prov.return_value = _stub_provider()

        # The ACL lookup reports the draft is owned by a DIFFERENT org.
        mock_db = MagicMock()
        mock_db.execute.return_value.fetchone.return_value = (uuid.UUID(_OTHER_ORG_ID),)
        mock_conn.return_value.__enter__ = MagicMock(return_value=mock_db)
        mock_conn.return_value.__exit__ = MagicMock(return_value=False)

        client = _authed_client()
        resp = client.post(
            "/api/annotations",
            json={
                "target_type": "draft",
                "target_id": _DRAFT_TARGET_ID,
                "content": "Uus markus",
            },
        )

        assert resp.status_code == 404

    @patch("app.annotations.routes.notify_annotation_mention")
    @patch("app.annotations.routes.parse_mentions")
    @patch("app.annotations.routes.log_annotation_create")
    @patch("app.annotations.routes._load_annotations_with_replies")
    @patch("app.annotations.routes._connect")
    @patch("app.auth.middleware._get_provider")
    def test_create_with_mention_fires_mention_notification(
        self,
        mock_prov,
        mock_conn,
        mock_load,
        mock_audit,
        mock_parse,
        mock_notify_mention,
    ):
        """#861: the legacy create path resolves @mentions and notifies."""
        mock_prov.return_value = _stub_provider()
        mock_load.return_value = []

        mock_db = MagicMock()
        mock_db.execute.return_value.fetchone.return_value = (uuid.UUID(_ORG_ID),)
        mock_conn.return_value.__enter__ = MagicMock(return_value=mock_db)
        mock_conn.return_value.__exit__ = MagicMock(return_value=False)

        mentioned = uuid.UUID(_OTHER_USER_ID)
        mock_parse.return_value = [mentioned]
        ann = _make_annotation()
        ann.mentions = [mentioned]

        with patch("app.annotations.routes.create_annotation", return_value=ann):
            client = _authed_client()
            resp = client.post(
                "/api/annotations",
                json={
                    "target_type": "draft",
                    "target_id": _DRAFT_TARGET_ID,
                    "content": "Vaata @keegi",
                },
            )

        assert resp.status_code == 200
        mock_parse.assert_called_once()
        mock_notify_mention.assert_called_once()
        assert mock_notify_mention.call_args.kwargs["mentioned_user_ids"] == [mentioned]

    @patch("app.auth.middleware._get_provider")
    def test_create_annotation_empty_content_rejected(self, mock_prov):
        mock_prov.return_value = _stub_provider()

        client = _authed_client()
        resp = client.post(
            "/api/annotations",
            data={
                "target_type": "draft",
                "target_id": "test-123",
                "content": "   ",
            },
        )
        assert resp.status_code == 200
        assert "kohustuslikud" in resp.text


# ---------------------------------------------------------------------------
# POST /api/annotations/{id}/reply — create reply
# ---------------------------------------------------------------------------


class TestReplyAnnotation:
    @patch("app.notifications.wire.notify_annotation_reply")
    @patch("app.annotations.routes.log_annotation_reply")
    @patch("app.annotations.routes._connect")
    @patch("app.auth.middleware._get_provider")
    def test_reply_returns_updated_fragment(self, mock_prov, mock_conn, mock_audit, mock_notify):
        mock_prov.return_value = _stub_provider()

        ann = _make_annotation()
        reply = _make_reply()

        mock_db = MagicMock()
        mock_conn.return_value.__enter__ = MagicMock(return_value=mock_db)
        mock_conn.return_value.__exit__ = MagicMock(return_value=False)

        with (
            patch("app.annotations.routes.get_annotation", return_value=ann),
            patch("app.annotations.routes.create_reply", return_value=reply),
            patch("app.annotations.routes.list_replies", return_value=[]),
        ):
            client = _authed_client()
            resp = client.post(
                f"/api/annotations/{_ANN_ID}/reply",
                data={"content": "Vastus"},
            )

        assert resp.status_code == 200
        assert "annotation-thread" in resp.text

    @patch("app.annotations.routes._connect")
    @patch("app.auth.middleware._get_provider")
    def test_reply_cross_org_returns_404(self, mock_prov, mock_conn):
        mock_prov.return_value = _stub_provider()

        ann = _make_annotation(org_id=_OTHER_ORG_ID)
        mock_db = MagicMock()
        mock_conn.return_value.__enter__ = MagicMock(return_value=mock_db)
        mock_conn.return_value.__exit__ = MagicMock(return_value=False)

        with patch("app.annotations.routes.get_annotation", return_value=ann):
            client = _authed_client()
            resp = client.post(
                f"/api/annotations/{_ANN_ID}/reply",
                data={"content": "Vastus"},
            )

        assert resp.status_code == 404

    @patch("app.annotations.routes.notify_annotation_mention")
    @patch("app.annotations.routes.notify_annotation_reply")
    @patch("app.annotations.routes.parse_mentions")
    @patch("app.annotations.routes.log_annotation_reply")
    @patch("app.annotations.routes._connect")
    @patch("app.auth.middleware._get_provider")
    def test_reply_with_mention_fires_mention_notification(
        self,
        mock_prov,
        mock_conn,
        mock_audit,
        mock_parse,
        mock_notify_reply,
        mock_notify_mention,
    ):
        """#861: a reply that mentions a user fans out a mention notification
        in addition to the existing reply notification."""
        mock_prov.return_value = _stub_provider()

        mentioned = uuid.UUID(_OTHER_USER_ID)
        ann = _make_annotation()
        reply = _make_reply()
        reply.mentions = [mentioned]
        mock_parse.return_value = [mentioned]

        mock_db = MagicMock()
        mock_conn.return_value.__enter__ = MagicMock(return_value=mock_db)
        mock_conn.return_value.__exit__ = MagicMock(return_value=False)

        with (
            patch("app.annotations.routes.get_annotation", return_value=ann),
            patch("app.annotations.routes.create_reply", return_value=reply),
            patch("app.annotations.routes.list_replies", return_value=[]),
        ):
            client = _authed_client()
            resp = client.post(
                f"/api/annotations/{_ANN_ID}/reply",
                data={"content": "Vaata @keegi"},
            )

        assert resp.status_code == 200
        # create_reply received the resolved mentions.
        assert mock_parse.called
        # Both notifications fired.
        mock_notify_reply.assert_called_once()
        mock_notify_mention.assert_called_once()
        assert mock_notify_mention.call_args.kwargs["mentioned_user_ids"] == [mentioned]


# ---------------------------------------------------------------------------
# POST /api/annotations/{id}/resolve — resolve annotation
# ---------------------------------------------------------------------------


class TestResolveAnnotation:
    @patch("app.annotations.routes.log_annotation_resolve")
    @patch("app.annotations.routes._connect")
    @patch("app.auth.middleware._get_provider")
    def test_resolve_returns_updated_fragment(self, mock_prov, mock_conn, mock_audit):
        mock_prov.return_value = _stub_provider()

        resolved_ann = _make_annotation(resolved=True)

        mock_db = MagicMock()
        mock_conn.return_value.__enter__ = MagicMock(return_value=mock_db)
        mock_conn.return_value.__exit__ = MagicMock(return_value=False)

        with (
            patch("app.annotations.routes.get_annotation", return_value=resolved_ann),
            patch("app.annotations.routes.resolve_annotation", return_value=resolved_ann),
            patch("app.annotations.routes.list_replies", return_value=[]),
        ):
            client = _authed_client()
            resp = client.post(f"/api/annotations/{_ANN_ID}/resolve")

        assert resp.status_code == 200
        assert "annotation-thread" in resp.text


# ---------------------------------------------------------------------------
# DELETE /api/annotations/{id} — delete annotation
# ---------------------------------------------------------------------------


class TestDeleteAnnotation:
    @patch("app.annotations.routes.log_annotation_delete")
    @patch("app.annotations.routes._connect")
    @patch("app.auth.middleware._get_provider")
    def test_delete_own_annotation_succeeds(self, mock_prov, mock_conn, mock_audit):
        mock_prov.return_value = _stub_provider()

        ann = _make_annotation()
        mock_db = MagicMock()
        mock_conn.return_value.__enter__ = MagicMock(return_value=mock_db)
        mock_conn.return_value.__exit__ = MagicMock(return_value=False)

        with (
            patch("app.annotations.routes.get_annotation", return_value=ann),
            patch("app.annotations.routes.delete_annotation"),
        ):
            client = _authed_client()
            resp = client.delete(f"/api/annotations/{_ANN_ID}")

        assert resp.status_code == 200
        assert resp.headers.get("HX-Trigger") == "annotationDeleted"

    @patch("app.annotations.routes._connect")
    @patch("app.auth.middleware._get_provider")
    def test_delete_cross_org_returns_404(self, mock_prov, mock_conn):
        mock_prov.return_value = _stub_provider()

        ann = _make_annotation(org_id=_OTHER_ORG_ID)
        mock_db = MagicMock()
        mock_conn.return_value.__enter__ = MagicMock(return_value=mock_db)
        mock_conn.return_value.__exit__ = MagicMock(return_value=False)

        with patch("app.annotations.routes.get_annotation", return_value=ann):
            client = _authed_client()
            resp = client.delete(f"/api/annotations/{_ANN_ID}")

        assert resp.status_code == 404

    @patch("app.annotations.routes._connect")
    @patch("app.auth.middleware._get_provider")
    def test_delete_other_users_annotation_returns_403(self, mock_prov, mock_conn):
        mock_prov.return_value = _stub_provider()

        ann = _make_annotation(user_id=_OTHER_USER_ID)
        mock_db = MagicMock()
        mock_conn.return_value.__enter__ = MagicMock(return_value=mock_db)
        mock_conn.return_value.__exit__ = MagicMock(return_value=False)

        with patch("app.annotations.routes.get_annotation", return_value=ann):
            client = _authed_client()
            resp = client.delete(f"/api/annotations/{_ANN_ID}")

        assert resp.status_code == 403

    @patch("app.annotations.routes.log_annotation_delete")
    @patch("app.annotations.routes._connect")
    @patch("app.auth.middleware._get_provider")
    def test_admin_can_delete_any_annotation(self, mock_prov, mock_conn, mock_audit):
        mock_prov.return_value = _stub_provider(user=_authed_user(role="admin"))

        ann = _make_annotation(user_id=_OTHER_USER_ID)
        mock_db = MagicMock()
        mock_conn.return_value.__enter__ = MagicMock(return_value=mock_db)
        mock_conn.return_value.__exit__ = MagicMock(return_value=False)

        with (
            patch("app.annotations.routes.get_annotation", return_value=ann),
            patch("app.annotations.routes.delete_annotation"),
        ):
            client = _authed_client()
            resp = client.delete(f"/api/annotations/{_ANN_ID}")

        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# #773: AnnotationPopover with URI target_id stays CSS-safe
# ---------------------------------------------------------------------------


class TestAnnotationPopoverWithUriTargetId:
    """Õiguskaart entity popovers pass the entity URI as target_id.

    The popover Div outer id and its inner form's hx-target must be
    CSS-safe (no raw ``/``, ``:``, ``#``, or ``%`` — all of which the
    explorer's old ``encodeURIComponent``-based id used to embed).
    """

    _URI_TARGET_ID = "https://data.riik.ee/ontology/estleg#KarS"

    @patch("app.annotations.routes._load_annotations_with_replies")
    @patch("app.auth.middleware._get_provider")
    def test_list_returns_popover_with_css_safe_id_for_uri(self, mock_prov, mock_load):
        from urllib.parse import quote

        mock_prov.return_value = _stub_provider()
        mock_load.return_value = []

        encoded_target = quote(self._URI_TARGET_ID, safe="")
        url = f"/api/annotations?target_type=entity&target_id={encoded_target}"

        client = _authed_client()
        resp = client.get(url)

        assert resp.status_code == 200
        body = resp.text
        # Popover renders.
        assert "annotation-popover" in body
        # The popover's outer id (and the form's hx-target) must NOT
        # contain raw URI structural chars — those break CSS selectors.
        # Find every id="annotation-popover-...:..." or id with ``/``.
        import re

        ids = re.findall(r'id="(annotation-popover-[^"]*)"', body)
        assert ids, "popover did not render with an id attribute"
        for popover_id in ids:
            assert "/" not in popover_id
            assert "#" not in popover_id
            assert "%" not in popover_id

    def test_popover_component_renders_for_uri_target_id(self):
        """Direct unit test on the surface component — must not raise."""
        from app.ui.surfaces.annotation_popover import AnnotationPopover

        result = AnnotationPopover(
            target_type="entity",
            target_id=self._URI_TARGET_ID,
            annotations=[],
            auth={"id": _USER_ID},
        )
        # The raw URI is preserved in data attributes.
        assert result is not None

    def test_annotation_button_primitive_uri_target_id(self):
        """AnnotationButton's id + hx-target must be CSS-safe for URIs."""
        from app.ui.primitives.annotation_button import AnnotationButton

        # Should not raise; the wrapper's id is hashed.
        result = AnnotationButton(
            target_type="entity",
            target_id=self._URI_TARGET_ID,
            count=2,
        )
        assert result is not None

    def test_button_container_id_differs_from_popover_id(self):
        """#861: the AnnotationButton container and the AnnotationPopover must
        NOT share a DOM id, otherwise the innerHTML swap nests two elements
        with the same id and the popover's create form (outerHTML on that id)
        blows the container away — breaking reload-after-create.
        """
        import re

        from fasthtml.common import to_xml

        from app.annotations.row_keys import target_dom_id
        from app.ui.primitives.annotation_button import AnnotationButton
        from app.ui.surfaces.annotation_popover import AnnotationPopover

        target_type, target_id = "draft", "draft-xyz"
        popover_id = target_dom_id(target_type, target_id)

        btn_html = to_xml(AnnotationButton(target_type, target_id, count=1))
        pop_html = to_xml(
            AnnotationPopover(target_type, target_id, annotations=[], auth={"id": _USER_ID})
        )

        # The button targets the container, whose id is distinct from the
        # popover's own id.
        assert f'hx-target="#{popover_id}-container"' in btn_html
        assert f'id="{popover_id}-container"' in btn_html
        # The container id is NOT the bare popover id.
        container_ids = re.findall(r'id="(annotation-popover-[^"]*-container)"', btn_html)
        assert container_ids == [f"{popover_id}-container"]
        assert f'id="{popover_id}"' not in btn_html

        # The popover's outer id is the bare popover id, and its create form
        # targets that same id (outerHTML replace-in-place on reload).
        assert f'id="{popover_id}"' in pop_html
        assert f'hx-target="#{popover_id}"' in pop_html
        assert f"{popover_id}-container" not in pop_html
