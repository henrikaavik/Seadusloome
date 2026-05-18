"""Integration tests for the Phase 2 Document Upload routes.

These tests exercise the full ``app.main.app`` via ``TestClient`` so
they validate the FastHTML wiring, the auth Beforeware, and the HTMX
partial swap behaviour. External dependencies — Postgres, Fernet,
JobQueue — are mocked out.

Patterns:
    - ``patch('app.auth.middleware._get_provider')`` lets us hand the
      Beforeware a stubbed ``JWTAuthProvider`` so the request reaches
      the handler with a valid ``req.scope['auth']``.
    - ``patch('app.docs.routes.fetch_drafts_for_org')`` et al. replace
      the DB helpers with plain Python stubs.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock, patch

from starlette.testclient import TestClient

from app.docs.draft_model import Draft
from app.docs.upload import DraftUploadError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_ORG_ID = "11111111-1111-1111-1111-111111111111"
_OTHER_ORG_ID = "22222222-2222-2222-2222-222222222222"
_USER_ID = "33333333-3333-3333-3333-333333333333"


def _authed_user() -> dict[str, Any]:
    return {
        "id": _USER_ID,
        "email": "koostaja@seadusloome.ee",
        "full_name": "Test Koostaja",
        "role": "drafter",
        "org_id": _ORG_ID,
    }


def _make_draft(
    *,
    draft_id: uuid.UUID | None = None,
    org_id: str = _ORG_ID,
    user_id: str = _USER_ID,
    status: str = "uploaded",
    title: str = "Test eelnõu",
    error_message: str | None = None,
    created_at: datetime | None = None,
    updated_at: datetime | None = None,
    doc_type: str = "eelnou",
    parent_vtk_id: uuid.UUID | None = None,
    processing_completed_at: datetime | None = None,
) -> Draft:
    now = datetime.now(UTC)
    resolved_id = draft_id or uuid.UUID("44444444-4444-4444-4444-444444444444")
    return Draft(
        id=resolved_id,
        user_id=uuid.UUID(user_id),
        org_id=uuid.UUID(org_id),
        title=title,
        filename="eelnou.docx",
        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        file_size=2048,
        storage_path="/tmp/ciphertext.enc",
        graph_uri=f"https://data.riik.ee/ontology/estleg/drafts/{resolved_id}",
        status=status,
        parsed_text_encrypted=None,
        entity_count=None,
        error_message=error_message,
        created_at=created_at or now,
        updated_at=updated_at or now,
        doc_type=doc_type,  # type: ignore[arg-type]
        parent_vtk_id=parent_vtk_id,
        processing_completed_at=processing_completed_at,
    )


def _stub_provider() -> MagicMock:
    """Build a provider whose ``get_current_user`` returns ``_authed_user``."""
    provider = MagicMock()
    provider.get_current_user.return_value = _authed_user()
    return provider


def _authed_client() -> TestClient:
    """Return a TestClient preloaded with a valid ``access_token`` cookie.

    The actual token value doesn't matter because the provider is
    mocked — any non-empty string keeps the middleware happy.
    """
    client = TestClient(__import__("app.main", fromlist=["app"]).app, follow_redirects=False)
    client.cookies.set("access_token", "stub-token")
    return client


# ---------------------------------------------------------------------------
# Unauthenticated requests redirect to login
# ---------------------------------------------------------------------------


class TestAuthRequired:
    def test_drafts_list_redirects_unauthenticated(self):
        from app.main import app

        client = TestClient(app, follow_redirects=False)
        resp = client.get("/drafts")
        assert resp.status_code == 303
        assert resp.headers["location"] == "/auth/login"

    def test_new_draft_redirects_unauthenticated(self):
        from app.main import app

        client = TestClient(app, follow_redirects=False)
        resp = client.get("/drafts/new")
        assert resp.status_code == 303
        assert resp.headers["location"] == "/auth/login"

    def test_draft_detail_redirects_unauthenticated(self):
        from app.main import app

        client = TestClient(app, follow_redirects=False)
        resp = client.get("/drafts/44444444-4444-4444-4444-444444444444")
        assert resp.status_code == 303
        assert resp.headers["location"] == "/auth/login"


# ---------------------------------------------------------------------------
# GET /drafts — listing
# ---------------------------------------------------------------------------


class TestDraftsList:
    @patch("app.docs.routes._list.list_users")
    @patch("app.docs.routes._list.list_drafts_for_org_filtered")
    @patch("app.auth.middleware._get_provider")
    def test_empty_list_renders_empty_state(
        self,
        mock_get_provider: MagicMock,
        mock_list_filtered: MagicMock,
        mock_list_users: MagicMock,
    ):
        mock_get_provider.return_value = _stub_provider()
        mock_list_filtered.return_value = ([], 0)
        mock_list_users.return_value = []

        client = _authed_client()
        resp = client.get("/drafts")

        assert resp.status_code == 200
        assert "Eelnõud" in resp.text
        # Empty state CTA must be present.
        assert "Laadi üles uus eelnõu" in resp.text
        assert "ei ole veel ühtegi eelnõu üles laadinud" in resp.text
        # Filter bar must always render so the user can recover.
        assert 'name="q"' in resp.text

    @patch("app.docs.routes._list.list_users")
    @patch("app.docs.routes._list.list_drafts_for_org_filtered")
    @patch("app.auth.middleware._get_provider")
    def test_populated_list_shows_rows(
        self,
        mock_get_provider: MagicMock,
        mock_list_filtered: MagicMock,
        mock_list_users: MagicMock,
    ):
        mock_get_provider.return_value = _stub_provider()
        draft_a = _make_draft(
            draft_id=uuid.UUID("55555555-5555-5555-5555-555555555555"),
            status="uploaded",
            title="Eelnõu A",
        )
        draft_b = _make_draft(
            draft_id=uuid.UUID("66666666-6666-6666-6666-666666666666"),
            status="ready",
            title="Eelnõu B",
        )
        mock_list_filtered.return_value = ([draft_a, draft_b], 2)
        mock_list_users.return_value = []

        client = _authed_client()
        resp = client.get("/drafts")

        assert resp.status_code == 200
        assert "Eelnõu A" in resp.text
        assert "Eelnõu B" in resp.text
        # Fetch was org-scoped.
        mock_list_filtered.assert_called_once()
        call = mock_list_filtered.call_args
        # First positional arg is the org_id.
        assert call.args[0] == _ORG_ID


# ---------------------------------------------------------------------------
# #642: filter bar + URL state + filtered empty state
# ---------------------------------------------------------------------------


_UPLOADER_ID = "44444444-4444-4444-4444-444444444444"


def _make_uploader(
    *,
    user_id: str = _USER_ID,
    full_name: str = "Test Koostaja",
    email: str = "koostaja@seadusloome.ee",
) -> dict[str, Any]:
    return {
        "id": user_id,
        "email": email,
        "full_name": full_name,
        "role": "drafter",
        "org_id": _ORG_ID,
        "is_active": True,
        "org_name": "Test ministeerium",
    }


class TestDraftsListFilterBar:
    """Filter bar UI + URL state round-trip (#642)."""

    @patch("app.docs.routes._list.list_users")
    @patch("app.docs.routes._list.list_drafts_for_org_filtered")
    @patch("app.auth.middleware._get_provider")
    def test_filter_bar_renders_all_controls(
        self,
        mock_get_provider: MagicMock,
        mock_list_filtered: MagicMock,
        mock_list_users: MagicMock,
    ):
        """All seven filter controls must be present in the rendered HTML."""
        mock_get_provider.return_value = _stub_provider()
        mock_list_filtered.return_value = ([], 0)
        mock_list_users.return_value = [_make_uploader()]

        client = _authed_client()
        resp = client.get("/drafts")

        assert resp.status_code == 200
        # Search input
        assert 'name="q"' in resp.text
        # Type checkbox group (both default-checked).
        assert 'name="type"' in resp.text
        assert 'value="eelnou"' in resp.text
        assert 'value="vtk"' in resp.text
        # Status checkboxes -- all six.
        for status in (
            "uploaded",
            "parsing",
            "extracting",
            "analyzing",
            "ready",
            "failed",
        ):
            assert f'value="{status}"' in resp.text
        # Uploader select (defaults to "all" sentinel + the single user).
        assert 'name="uploader"' in resp.text
        assert "Test Koostaja" in resp.text
        # Date range.
        assert 'name="from"' in resp.text
        assert 'name="to"' in resp.text
        # Sort dropdown.
        assert 'name="sort"' in resp.text
        # Reset link.
        assert "Lähtesta filtrid" in resp.text

    @patch("app.docs.routes._list.list_users")
    @patch("app.docs.routes._list.list_drafts_for_org_filtered")
    @patch("app.auth.middleware._get_provider")
    def test_querystring_round_trips_to_filter_helper(
        self,
        mock_get_provider: MagicMock,
        mock_list_filtered: MagicMock,
        mock_list_users: MagicMock,
    ):
        """Pasting a URL with all filters set must call the helper with
        the matching kwargs."""
        mock_get_provider.return_value = _stub_provider()
        mock_list_filtered.return_value = ([], 0)
        mock_list_users.return_value = [_make_uploader(user_id=_UPLOADER_ID)]

        client = _authed_client()
        resp = client.get(
            "/drafts"
            "?q=maantee"
            "&type=eelnou"
            "&status=ready"
            f"&uploader={_UPLOADER_ID}"
            "&from=2026-01-01"
            "&to=2026-04-01"
            "&sort=title_asc"
        )
        assert resp.status_code == 200

        kwargs = mock_list_filtered.call_args.kwargs
        assert kwargs["q"] == "maantee"
        assert kwargs["doc_types"] == {"eelnou"}
        assert kwargs["statuses"] == {"ready"}
        assert str(kwargs["uploader_id"]) == _UPLOADER_ID
        assert kwargs["date_from"].isoformat() == "2026-01-01"
        assert kwargs["date_to"].isoformat() == "2026-04-01"
        assert kwargs["sort"] == "title_asc"

    @patch("app.docs.routes._list.list_users")
    @patch("app.docs.routes._list.list_drafts_for_org_filtered")
    @patch("app.auth.middleware._get_provider")
    def test_filter_bar_pre_fills_from_querystring(
        self,
        mock_get_provider: MagicMock,
        mock_list_filtered: MagicMock,
        mock_list_users: MagicMock,
    ):
        """Search input must echo the current ``q`` so the user sees it.

        This is the load-bearing assertion for browser-back-button
        round-trips: when the browser restores a URL we must also
        restore the filter bar UI state to match.
        """
        mock_get_provider.return_value = _stub_provider()
        mock_list_filtered.return_value = ([], 0)
        mock_list_users.return_value = []

        client = _authed_client()
        resp = client.get("/drafts?q=maanteeseadus&sort=title_asc")
        assert resp.status_code == 200
        assert 'value="maanteeseadus"' in resp.text
        # The selected sort option must be marked.
        assert 'value="title_asc"' in resp.text
        # The default sort must NOT be the selected one.
        assert 'value="title_asc" selected' in resp.text or (
            'selected value="title_asc"' in resp.text
        )

    @patch("app.docs.routes._list.list_users")
    @patch("app.docs.routes._list.list_drafts_for_org_filtered")
    @patch("app.auth.middleware._get_provider")
    def test_unknown_sort_falls_back_to_default(
        self,
        mock_get_provider: MagicMock,
        mock_list_filtered: MagicMock,
        mock_list_users: MagicMock,
    ):
        """A tampered ``sort=hax0r`` querystring must not crash; the
        helper receives the default sort."""
        mock_get_provider.return_value = _stub_provider()
        mock_list_filtered.return_value = ([], 0)
        mock_list_users.return_value = []

        client = _authed_client()
        resp = client.get("/drafts?sort=hax0r")
        assert resp.status_code == 200
        assert mock_list_filtered.call_args.kwargs["sort"] == "created_desc"

    @patch("app.docs.routes._list.list_users")
    @patch("app.docs.routes._list.list_drafts_for_org_filtered")
    @patch("app.auth.middleware._get_provider")
    def test_invalid_uploader_uuid_silently_drops(
        self,
        mock_get_provider: MagicMock,
        mock_list_filtered: MagicMock,
        mock_list_users: MagicMock,
    ):
        """Tampered uploader UUID degrades to "no uploader filter"."""
        mock_get_provider.return_value = _stub_provider()
        mock_list_filtered.return_value = ([], 0)
        mock_list_users.return_value = []

        client = _authed_client()
        resp = client.get("/drafts?uploader=not-a-uuid")
        assert resp.status_code == 200
        assert mock_list_filtered.call_args.kwargs["uploader_id"] is None

    @patch("app.docs.routes._list.list_users")
    @patch("app.docs.routes._list.list_drafts_for_org_filtered")
    @patch("app.auth.middleware._get_provider")
    def test_unknown_status_value_dropped(
        self,
        mock_get_provider: MagicMock,
        mock_list_filtered: MagicMock,
        mock_list_users: MagicMock,
    ):
        """``?status=ready&status=hax`` keeps ``ready`` and drops the rest."""
        mock_get_provider.return_value = _stub_provider()
        mock_list_filtered.return_value = ([], 0)
        mock_list_users.return_value = []

        client = _authed_client()
        resp = client.get("/drafts?status=ready&status=hax")
        assert resp.status_code == 200
        assert mock_list_filtered.call_args.kwargs["statuses"] == {"ready"}


class TestDraftsListEmptyStates:
    @patch("app.docs.routes._list.list_users")
    @patch("app.docs.routes._list.list_drafts_for_org_filtered")
    @patch("app.auth.middleware._get_provider")
    def test_empty_with_filters_uses_filter_empty_state(
        self,
        mock_get_provider: MagicMock,
        mock_list_filtered: MagicMock,
        mock_list_users: MagicMock,
    ):
        """When 0 rows match an active filter the empty-state title
        must steer the user toward ``Lähtesta filtrid`` rather than
        the upload CTA."""
        mock_get_provider.return_value = _stub_provider()
        mock_list_filtered.return_value = ([], 0)
        mock_list_users.return_value = []

        client = _authed_client()
        resp = client.get("/drafts?q=nope")

        assert resp.status_code == 200
        assert "Filtritele vastavaid eelnõusid pole" in resp.text
        # The filter-aware empty state must NOT show the original
        # upload CTA copy.
        assert "ei ole veel ühtegi eelnõu üles laadinud" not in resp.text

    @patch("app.docs.routes._list.list_users")
    @patch("app.docs.routes._list.list_drafts_for_org_filtered")
    @patch("app.auth.middleware._get_provider")
    def test_empty_without_filters_uses_zero_state(
        self,
        mock_get_provider: MagicMock,
        mock_list_filtered: MagicMock,
        mock_list_users: MagicMock,
    ):
        """When no filters are active and the org has no drafts the
        original "no drafts at all" empty state remains in place."""
        mock_get_provider.return_value = _stub_provider()
        mock_list_filtered.return_value = ([], 0)
        mock_list_users.return_value = []

        client = _authed_client()
        resp = client.get("/drafts")

        assert resp.status_code == 200
        assert "ei ole veel ühtegi eelnõu üles laadinud" in resp.text
        assert "Filtritele vastavaid eelnõusid pole" not in resp.text


class TestDraftsListPagination:
    @patch("app.docs.routes._list.list_users")
    @patch("app.docs.routes._list.list_drafts_for_org_filtered")
    @patch("app.auth.middleware._get_provider")
    def test_pagination_links_preserve_filter_querystring(
        self,
        mock_get_provider: MagicMock,
        mock_list_filtered: MagicMock,
        mock_list_users: MagicMock,
    ):
        """Pagination's ``page`` link must round-trip every active filter
        so the user stays inside the same filtered slice when paging."""
        mock_get_provider.return_value = _stub_provider()
        # 60 results -> 3 pages of 25.
        rows = [
            _make_draft(
                draft_id=uuid.UUID(int=i),
                status="ready",
                title=f"Eelnõu {i}",
            )
            for i in range(25)
        ]
        mock_list_filtered.return_value = (rows, 60)
        mock_list_users.return_value = []

        client = _authed_client()
        resp = client.get("/drafts?q=eelnou&status=ready")
        assert resp.status_code == 200
        # Pagination next link must include both ``q`` and ``status``.
        assert "q=eelnou" in resp.text
        assert "status=ready" in resp.text
        # And the page= number must be on the same anchor URL.
        assert "page=2" in resp.text


class TestDraftsListHtmxSwap:
    @patch("app.docs.routes._list.list_users")
    @patch("app.docs.routes._list.list_drafts_for_org_filtered")
    @patch("app.auth.middleware._get_provider")
    def test_htmx_request_returns_table_partial(
        self,
        mock_get_provider: MagicMock,
        mock_list_filtered: MagicMock,
        mock_list_users: MagicMock,
    ):
        """A request with ``HX-Request: true`` must return only the
        table-wrapper partial, not the full page shell.  This keeps
        focus inside the filter bar between filter changes."""
        mock_get_provider.return_value = _stub_provider()
        mock_list_filtered.return_value = ([], 0)
        mock_list_users.return_value = []

        client = _authed_client()
        resp = client.get("/drafts?q=foo", headers={"HX-Request": "true"})

        assert resp.status_code == 200
        # No PageShell artefacts -- no <html>, no <head>, no nav.
        assert "<html" not in resp.text.lower()
        # But the table-wrapper id is present.
        assert 'id="drafts-table-wrapper"' in resp.text

    @patch("app.docs.routes._list.list_users")
    @patch("app.docs.routes._list.list_drafts_for_org_filtered")
    @patch("app.auth.middleware._get_provider")
    def test_full_page_request_returns_page_shell(
        self,
        mock_get_provider: MagicMock,
        mock_list_filtered: MagicMock,
        mock_list_users: MagicMock,
    ):
        """Without the HX header, render the full page shell so direct
        URL navigation produces a complete document."""
        mock_get_provider.return_value = _stub_provider()
        mock_list_filtered.return_value = ([], 0)
        mock_list_users.return_value = []

        client = _authed_client()
        resp = client.get("/drafts")

        assert resp.status_code == 200
        assert "<html" in resp.text.lower()
        assert 'id="drafts-table-wrapper"' in resp.text


# ---------------------------------------------------------------------------
# GET /drafts/new
# ---------------------------------------------------------------------------


class TestNewDraftPage:
    @patch("app.auth.middleware._get_provider")
    def test_new_draft_renders_multipart_form(self, mock_get_provider: MagicMock):
        mock_get_provider.return_value = _stub_provider()

        client = _authed_client()
        resp = client.get("/drafts/new")

        assert resp.status_code == 200
        # The form must be multipart — verifies the raw Form primitive
        # was used instead of AppForm (which defaults to urlencoded).
        assert 'enctype="multipart/form-data"' in resp.text
        assert 'name="title"' in resp.text
        assert 'name="file"' in resp.text
        assert 'type="file"' in resp.text
        assert ".docx" in resp.text

    # #808: VTK picker empty-state UX. When the caller's org has zero
    # VTKs the picker renders a disabled select with an explanatory
    # help message; when there is at least one VTK the picker is
    # enabled and lists the available rows.
    @patch("app.docs.routes._upload.list_vtks_for_org")
    @patch("app.auth.middleware._get_provider")
    def test_new_draft_vtk_picker_empty_state_is_disabled_with_help_text(
        self,
        mock_get_provider: MagicMock,
        mock_list_vtks: MagicMock,
    ):
        """#808: zero VTKs → disabled select + Estonian empty-state help."""
        mock_get_provider.return_value = _stub_provider()
        mock_list_vtks.return_value = []

        client = _authed_client()
        resp = client.get("/drafts/new")

        assert resp.status_code == 200
        # The VTK picker must still render so the form layout is stable.
        assert 'id="field-parent-vtk"' in resp.text
        # Slice out the <select ...> opening tag for the parent-vtk
        # picker and assert it carries the `disabled` attribute.
        # FastHTML serialises boolean attributes as the bare attribute
        # name, and the attribute order in the tag is not guaranteed,
        # so we scan from the nearest preceding `<select` to the next
        # `>` rather than anchoring on `id="..."`.
        id_idx = resp.text.find('id="field-parent-vtk"')
        select_open = resp.text.rfind("<select", 0, id_idx)
        select_end = resp.text.find(">", id_idx)
        assert id_idx != -1
        assert select_open != -1
        assert select_end != -1
        select_tag = resp.text[select_open : select_end + 1]
        assert " disabled" in select_tag or 'disabled="' in select_tag
        # The empty-state help text appears below the select.
        assert "Organisatsioonis pole veel VTKsid" in resp.text

    @patch("app.docs.routes._upload.list_vtks_for_org")
    @patch("app.auth.middleware._get_provider")
    def test_new_draft_vtk_picker_with_rows_is_enabled(
        self,
        mock_get_provider: MagicMock,
        mock_list_vtks: MagicMock,
    ):
        """Regression: ≥1 VTK → picker is NOT disabled and lists labels."""
        mock_get_provider.return_value = _stub_provider()
        mock_list_vtks.return_value = [
            _make_draft(
                draft_id=uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
                doc_type="vtk",
                status="ready",
                title="Maantee VTK",
            ),
        ]

        client = _authed_client()
        resp = client.get("/drafts/new")

        assert resp.status_code == 200
        assert 'id="field-parent-vtk"' in resp.text
        # The select tag must NOT be disabled in the non-empty branch.
        id_idx = resp.text.find('id="field-parent-vtk"')
        select_open = resp.text.rfind("<select", 0, id_idx)
        select_end = resp.text.find(">", id_idx)
        assert id_idx != -1
        assert select_open != -1
        assert select_end != -1
        select_tag = resp.text[select_open : select_end + 1]
        assert " disabled" not in select_tag and 'disabled="' not in select_tag
        # The VTK label must appear as an <option>.
        assert "<option" in resp.text
        assert "Maantee VTK" in resp.text
        # And the empty-state help text must NOT leak into this branch.
        assert "Organisatsioonis pole veel VTKsid" not in resp.text


# ---------------------------------------------------------------------------
# POST /drafts — upload
# ---------------------------------------------------------------------------


class TestCreateDraftHandler:
    @patch("app.docs.routes._upload.log_draft_upload")
    @patch("app.docs.routes._upload.handle_upload")
    @patch("app.auth.middleware._get_provider")
    def test_successful_upload_redirects_to_detail(
        self,
        mock_get_provider: MagicMock,
        mock_handle: MagicMock,
        mock_log: MagicMock,
    ):
        mock_get_provider.return_value = _stub_provider()
        draft = _make_draft(
            draft_id=uuid.UUID("77777777-7777-7777-7777-777777777777"),
        )

        async def _fake_handle(*_a: Any, **_kw: Any) -> Draft:
            return draft

        mock_handle.side_effect = _fake_handle

        client = _authed_client()
        resp = client.post(
            "/drafts",
            data={"title": "Test eelnõu"},
            files={
                "file": (
                    "eelnou.docx",
                    b"Test sisu",
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                )
            },
        )

        assert resp.status_code == 303
        assert resp.headers["location"] == f"/drafts/{draft.id}"
        mock_handle.assert_called_once()
        mock_log.assert_called_once()

    @patch("app.docs.routes._upload.handle_upload")
    @patch("app.auth.middleware._get_provider")
    def test_validation_error_rerenders_form_with_title(
        self,
        mock_get_provider: MagicMock,
        mock_handle: MagicMock,
    ):
        mock_get_provider.return_value = _stub_provider()

        async def _raise(*_a: Any, **_kw: Any) -> Draft:
            raise DraftUploadError("Toetamata failitüüp.")

        mock_handle.side_effect = _raise

        client = _authed_client()
        resp = client.post(
            "/drafts",
            data={"title": "Minu eelnõu"},
            files={
                "file": (
                    "bad.txt",
                    b"content",
                    "text/plain",
                )
            },
        )

        assert resp.status_code == 200
        # Error banner is present.
        assert "Toetamata failitüüp." in resp.text
        # Title was preserved so the user can fix the file and retry.
        assert "Minu eelnõu" in resp.text


# ---------------------------------------------------------------------------
# GET /drafts/{draft_id} — detail page
# ---------------------------------------------------------------------------


class TestDraftDetailPage:
    @patch("app.docs.routes._detail.log_draft_view")
    @patch("app.docs.routes._detail.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_draft_in_own_org_renders(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_log: MagicMock,
    ):
        mock_get_provider.return_value = _stub_provider()
        draft = _make_draft(status="parsing", title="Minu eelnõu")
        mock_fetch.return_value = draft

        client = _authed_client()
        resp = client.get(f"/drafts/{draft.id}")

        assert resp.status_code == 200
        assert "Minu eelnõu" in resp.text
        # Status tracker visible.
        assert "Üles laaditud" in resp.text
        assert "Töötlemine" in resp.text
        # Polling attrs are present because status is not terminal.
        assert f"/drafts/{draft.id}/status" in resp.text
        assert "every 3s" in resp.text
        # Audit log was written.
        mock_log.assert_called_once()

    @patch("app.docs.routes._detail.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_draft_in_other_org_returns_404_page(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
    ):
        mock_get_provider.return_value = _stub_provider()
        # Draft belongs to a different org.
        foreign = _make_draft(org_id=_OTHER_ORG_ID)
        mock_fetch.return_value = foreign

        client = _authed_client()
        resp = client.get(f"/drafts/{foreign.id}")

        # We return the not-found page (HTTP 404, #739) rather than a raw
        # 403 so we never leak the existence of drafts belonging to other
        # organisations.
        assert resp.status_code == 404
        assert "Eelnõu ei leitud" in resp.text
        assert "Minu eelnõu" not in resp.text

    @patch("app.docs.routes._detail.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_missing_draft_returns_404_page(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
    ):
        mock_get_provider.return_value = _stub_provider()
        mock_fetch.return_value = None

        client = _authed_client()
        resp = client.get("/drafts/99999999-9999-9999-9999-999999999999")

        assert resp.status_code == 404
        assert "Eelnõu ei leitud" in resp.text

    @patch("app.docs.routes._detail.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_invalid_uuid_draft_detail_returns_404(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
    ):
        """A malformed draft id never reaches the DB and 404s (#739)."""
        mock_get_provider.return_value = _stub_provider()

        client = _authed_client()
        resp = client.get("/drafts/not-a-uuid")

        assert resp.status_code == 404
        # The not-found page is served as a full HTML document.
        assert resp.headers["content-type"].startswith("text/html")
        assert "Eelnõu ei leitud" in resp.text
        mock_fetch.assert_not_called()

    @patch("app.docs.routes._detail.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_ready_draft_shows_report_link(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
    ):
        mock_get_provider.return_value = _stub_provider()
        draft = _make_draft(status="ready")
        mock_fetch.return_value = draft

        client = _authed_client()
        resp = client.get(f"/drafts/{draft.id}")

        assert resp.status_code == 200
        assert "Vaata mõjuaruannet" in resp.text
        assert f"/drafts/{draft.id}/report" in resp.text
        # #724: a ready draft also offers an "Ava analüüsikeskuses" cross-link
        # into the Normi mõjuahel workflow (which reuses this draft's report).
        assert "Ava analüüsikeskuses" in resp.text
        assert f"/analyysikeskus/normi-mojuahel?sisend={draft.id}" in resp.text
        # #759: a ready draft also offers a "Vaata mõjukaarti" CTA that
        # deep-links into Õiguskaart centred on its impact subgraph.
        assert "Vaata mõjukaarti" in resp.text
        assert f"/explorer?draft={draft.id}" in resp.text
        # No polling for terminal statuses.
        assert "every 3s" not in resp.text

    @patch("app.docs.routes._detail.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_non_ready_draft_has_no_analyysikeskus_link(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
    ):
        """#724: the Analüüsikeskus cross-link is gated on ``status == ready``
        (same guard as the "Vaata mõjuaruannet" CTA). #759: the
        "Vaata mõjukaarti" CTA is gated the same way."""
        mock_get_provider.return_value = _stub_provider()
        draft = _make_draft(status="parsing")
        mock_fetch.return_value = draft

        client = _authed_client()
        resp = client.get(f"/drafts/{draft.id}")

        assert resp.status_code == 200
        assert "Ava analüüsikeskuses" not in resp.text
        assert f"/analyysikeskus/normi-mojuahel?sisend={draft.id}" not in resp.text
        # #759: no impact subgraph exists before the analyse pipeline
        # completes, so the map CTA must not render either.
        assert "Vaata mõjukaarti" not in resp.text
        assert f"/explorer?draft={draft.id}" not in resp.text

    @patch("app.docs.routes._detail.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_failed_draft_shows_error_message(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
    ):
        mock_get_provider.return_value = _stub_provider()
        draft = _make_draft(
            status="failed",
            error_message="Tika teenus on kättesaamatu.",
        )
        mock_fetch.return_value = draft

        client = _authed_client()
        resp = client.get(f"/drafts/{draft.id}")

        assert resp.status_code == 200
        assert "Tika teenus on kättesaamatu." in resp.text
        assert "every 3s" not in resp.text

    @patch("app.docs.routes._detail.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_delete_draft_has_confirmation(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
    ):
        """Regression for #443 / #601.

        The delete flow now uses the shared Modal primitive instead of
        the native ``confirm()`` + ``hx-confirm`` double prompt. The
        visible trigger button opens the modal; the modal's confirm
        button submits a hidden HTMX form behind the scenes. The
        native ``action`` attribute is preserved as the no-JS fallback.
        """
        mock_get_provider.return_value = _stub_provider()
        draft = _make_draft(status="ready")
        mock_fetch.return_value = draft

        client = _authed_client()
        resp = client.get(f"/drafts/{draft.id}")

        assert resp.status_code == 200
        body = resp.text
        # HTMX still drives the delete when the user confirms.
        assert f'hx-post="/drafts/{draft.id}/delete"' in body
        # Native form action remains as the no-JS fallback.
        assert f'action="/drafts/{draft.id}/delete"' in body
        # #601: the dual confirm() + hx-confirm has been replaced by
        # a single accessible Modal. Neither native-prompt artefact
        # should leak back into the page.
        assert "hx-confirm=" not in body
        assert "return confirm(" not in body
        # Modal primitive is mounted and the trigger is wired to it.
        assert 'id="delete-draft-modal"' in body
        assert 'id="delete-draft-trigger"' in body
        assert 'role="dialog"' in body


# ---------------------------------------------------------------------------
# GET /drafts/{draft_id}/status — HTMX polling fragment
# ---------------------------------------------------------------------------


class TestDraftStatusFragment:
    @patch("app.docs.routes._detail.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_status_fragment_returns_partial_without_shell(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
    ):
        mock_get_provider.return_value = _stub_provider()
        draft = _make_draft(status="extracting")
        mock_fetch.return_value = draft

        client = _authed_client()
        # Send the HX-Request header so FastHTML returns a partial instead
        # of wrapping the Div in a full HTML document.
        resp = client.get(
            f"/drafts/{draft.id}/status",
            headers={"HX-Request": "true"},
        )

        assert resp.status_code == 200
        # No PageShell wrapper — no sidebar, no topbar.
        assert "app-shell" not in resp.text
        assert "sidebar" not in resp.text
        # The status tracker is in the body.
        assert "Olemite eraldamine" in resp.text
        assert f"draft-status-{draft.id}" in resp.text

    @patch("app.docs.routes._detail.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_status_fragment_other_org_returns_placeholder(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
    ):
        mock_get_provider.return_value = _stub_provider()
        mock_fetch.return_value = _make_draft(org_id=_OTHER_ORG_ID)

        client = _authed_client()
        resp = client.get("/drafts/44444444-4444-4444-4444-444444444444/status")

        assert resp.status_code == 200
        assert "Eelnõu ei leitud" in resp.text

    @patch("app.docs.routes._detail.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_status_fragment_keeps_polling_when_recently_updated(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
    ):
        """Regression for #470.

        A draft created long ago but with a recent ``updated_at`` is
        still making progress and must keep polling. Using
        ``created_at`` for the stale check would have stopped polling
        prematurely for any draft whose pipeline takes longer than
        the 5-minute window to finish.
        """
        mock_get_provider.return_value = _stub_provider()
        now = datetime.now(UTC)
        draft = _make_draft(
            status="analyzing",
            created_at=now - timedelta(hours=1),
            updated_at=now - timedelta(seconds=10),
        )
        mock_fetch.return_value = draft

        client = _authed_client()
        resp = client.get(
            f"/drafts/{draft.id}/status",
            headers={"HX-Request": "true"},
        )

        assert resp.status_code == 200
        # Polling attributes must still be present. #607: the draft was
        # created >120s ago so the interval backs off to 10s; the
        # recent ``updated_at`` still prevents the stale alert.
        assert 'hx-trigger="every 10s"' in resp.text
        assert "Vajab tähelepanu" not in resp.text

    @patch("app.docs.routes._detail.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_status_fragment_shows_elapsed_and_typical_range(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
    ):
        """#606: the active stage surfaces elapsed time + typical range."""
        mock_get_provider.return_value = _stub_provider()
        now = datetime.now(UTC)
        draft = _make_draft(
            status="extracting",
            created_at=now - timedelta(seconds=100),
            updated_at=now - timedelta(seconds=100),
        )
        mock_fetch.return_value = draft

        client = _authed_client()
        resp = client.get(f"/drafts/{draft.id}/status", headers={"HX-Request": "true"})
        assert resp.status_code == 200
        assert "möödas" in resp.text
        assert "tüüpiline aeg" in resp.text
        # The ticker script must be included on the fragment.
        assert "draft-stage-elapsed" in resp.text

    @patch("app.docs.routes._detail.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_status_fragment_poll_backoff_fresh_is_3s(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
    ):
        """#607: drafts <30s old poll every 3s."""
        mock_get_provider.return_value = _stub_provider()
        now = datetime.now(UTC)
        draft = _make_draft(
            status="parsing",
            created_at=now - timedelta(seconds=5),
            updated_at=now - timedelta(seconds=5),
        )
        mock_fetch.return_value = draft

        client = _authed_client()
        resp = client.get(f"/drafts/{draft.id}/status", headers={"HX-Request": "true"})
        assert resp.status_code == 200
        assert 'hx-trigger="every 3s"' in resp.text

    @patch("app.docs.routes._detail.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_status_fragment_poll_backoff_medium_is_6s(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
    ):
        """#607: drafts 30-120s old poll every 6s."""
        mock_get_provider.return_value = _stub_provider()
        now = datetime.now(UTC)
        draft = _make_draft(
            status="extracting",
            created_at=now - timedelta(seconds=60),
            updated_at=now - timedelta(seconds=5),
        )
        mock_fetch.return_value = draft

        client = _authed_client()
        resp = client.get(f"/drafts/{draft.id}/status", headers={"HX-Request": "true"})
        assert resp.status_code == 200
        assert 'hx-trigger="every 6s"' in resp.text

    @patch("app.docs.routes._detail.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_status_fragment_marks_stale_when_updated_at_is_old(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
    ):
        """Regression for #470.

        A draft whose ``updated_at`` is older than the polling budget
        surfaces the yellow "stuck pipeline" alert and drops the
        polling attributes.
        """
        mock_get_provider.return_value = _stub_provider()
        now = datetime.now(UTC)
        draft = _make_draft(
            status="analyzing",
            created_at=now - timedelta(hours=2),
            updated_at=now - timedelta(hours=1),
        )
        mock_fetch.return_value = draft

        client = _authed_client()
        resp = client.get(
            f"/drafts/{draft.id}/status",
            headers={"HX-Request": "true"},
        )

        assert resp.status_code == 200
        # #606: the admin-dashboard dead-end was replaced with a
        # "Kontrolli uuesti kohe" manual-repoll button + escalation
        # guidance.
        assert "Töötlemine venib" in resp.text
        assert "Kontrolli uuesti kohe" in resp.text
        assert "võtke ühendust meeskonnaga" in resp.text
        # The wrapper's periodic hx-trigger is dropped. The manual
        # repoll button has its own hx-get but no "every Ns" trigger.
        assert "every " not in resp.text


# ---------------------------------------------------------------------------
# POST /drafts/{draft_id}/delete
# ---------------------------------------------------------------------------


class TestDeleteDraftHandler:
    @patch("app.docs.routes._lifecycle.JobQueue")
    @patch("app.docs.routes._lifecycle.log_draft_delete")
    @patch("app.docs.routes._lifecycle.get_draft_artifact_paths")
    @patch("app.docs.routes._lifecycle.delete_draft")
    @patch("app.docs.routes._lifecycle._connect")
    @patch("app.docs.routes._lifecycle.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_delete_removes_draft_and_enqueues_cleanup(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_connect: MagicMock,
        mock_delete: MagicMock,
        mock_artifacts: MagicMock,
        mock_log: MagicMock,
        mock_queue_cls: MagicMock,
    ):
        """#628: the DB-side work runs in a single transaction and the
        external cleanup (encrypted file + Jena graph) is handed off to
        an async ``draft_cleanup`` job so flaky infrastructure can't
        block the user flow."""
        mock_get_provider.return_value = _stub_provider()
        draft = _make_draft()
        mock_fetch.return_value = draft

        mock_conn = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)
        mock_delete.return_value = "/tmp/ciphertext.enc"
        mock_artifacts.return_value = (["/tmp/ciphertext.enc"], [draft.graph_uri])

        queue_instance = MagicMock()
        queue_instance.enqueue.return_value = 99
        mock_queue_cls.return_value = queue_instance

        client = _authed_client()
        resp = client.post(f"/drafts/{draft.id}/delete")

        assert resp.status_code == 303
        assert resp.headers["location"] == "/drafts"
        mock_delete.assert_called_once()
        mock_log.assert_called_once()

        # #736: artifact paths are snapshotted BEFORE the row delete so
        # the cascade-deleted draft_versions rows are still readable.
        mock_artifacts.assert_called_once()

        # #628: a single _connect() transaction covers row delete,
        # rag_chunks clearing and job cancellation. The handler opens
        # exactly one connection now (down from two).
        assert mock_connect.call_count == 1

        # #454: DELETE FROM background_jobs still runs, now inside the
        # same transaction as the row delete.
        delete_job_calls = [
            c
            for c in mock_conn.execute.call_args_list
            if "DELETE FROM background_jobs" in (c.args[0] if c.args else "")
        ]
        assert delete_job_calls, "delete_draft_handler must cancel pending background jobs (#454)"
        delete_call = delete_job_calls[0]
        assert delete_call.args[1] == (str(draft.id),)
        # #478: running jobs must also be in the status filter.
        assert "'running'" in delete_call.args[0]

        # #628/#736: the external cleanup is enqueued as a background job
        # with ARRAYS of storage_paths + graph_uris (one per version) so
        # the worker can retry independently and purge every artifact.
        queue_instance.enqueue.assert_called_once()
        args, kwargs = queue_instance.enqueue.call_args
        assert args[0] == "draft_cleanup"
        payload = args[1]
        assert payload["draft_id"] == str(draft.id)
        assert payload["storage_paths"] == ["/tmp/ciphertext.enc"]
        assert payload["graph_uris"] == [draft.graph_uri]
        # legacy singular keys still present for backward compat
        assert payload["storage_path"] == "/tmp/ciphertext.enc"
        assert payload["graph_uri"] == draft.graph_uri

    @patch("app.docs.routes._lifecycle.JobQueue")
    @patch("app.docs.routes._lifecycle.log_draft_delete")
    @patch("app.docs.routes._lifecycle.get_draft_artifact_paths")
    @patch("app.docs.routes._lifecycle.delete_draft")
    @patch("app.docs.routes._lifecycle._connect")
    @patch("app.docs.routes._lifecycle.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_delete_versioned_draft_enqueues_every_version_artifact(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_connect: MagicMock,
        mock_delete: MagicMock,
        mock_artifacts: MagicMock,
        mock_log: MagicMock,
        mock_queue_cls: MagicMock,
    ):
        """#736: deleting a draft with >=2 versions must enqueue cleanup
        for EVERY version's storage path AND named graph URI, not just
        the latest — older draft_versions rows cascade away on delete,
        so their files/graphs would otherwise orphan on disk and in Jena.
        """
        mock_get_provider.return_value = _stub_provider()
        draft = _make_draft()
        mock_fetch.return_value = draft

        mock_conn = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)
        mock_delete.return_value = "/tmp/v2.enc"
        v1_path, v2_path = "/tmp/v1.enc", "/tmp/v2.enc"
        v1_graph = f"{draft.graph_uri}/v1"
        v2_graph = f"{draft.graph_uri}/v2"
        mock_artifacts.return_value = ([v1_path, v2_path], [v1_graph, v2_graph])

        queue_instance = MagicMock()
        queue_instance.enqueue.return_value = 7
        mock_queue_cls.return_value = queue_instance

        client = _authed_client()
        resp = client.post(f"/drafts/{draft.id}/delete")

        assert resp.status_code == 303
        mock_artifacts.assert_called_once()
        queue_instance.enqueue.assert_called_once()
        payload = queue_instance.enqueue.call_args.args[1]
        # BOTH version files queued for deletion.
        assert payload["storage_paths"] == [v1_path, v2_path]
        # BOTH named graphs queued for deletion...
        assert v1_graph in payload["graph_uris"]
        assert v2_graph in payload["graph_uris"]
        # ...plus the "current" draft.graph_uri (defensively unioned by
        # the handler in case it differs from every version row).
        assert draft.graph_uri in payload["graph_uris"]

    @patch("app.docs.routes._lifecycle.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_delete_other_org_draft_returns_not_found(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
    ):
        mock_get_provider.return_value = _stub_provider()
        mock_fetch.return_value = _make_draft(org_id=_OTHER_ORG_ID)

        client = _authed_client()
        resp = client.post("/drafts/44444444-4444-4444-4444-444444444444/delete")

        assert resp.status_code == 404
        assert "Eelnõu ei leitud" in resp.text

    @patch("app.docs.routes._lifecycle.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_delete_same_org_non_owner_returns_not_found(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
    ):
        """Issue #568: a same-org drafter, reviewer, or org-admin must
        NOT be able to delete someone else's draft. Before the fix any
        same-org viewer could click the delete button and succeed."""
        mock_get_provider.return_value = _stub_provider()
        other_user = "99999999-9999-9999-9999-999999999999"
        mock_fetch.return_value = _make_draft(user_id=other_user)

        client = _authed_client()
        resp = client.post("/drafts/44444444-4444-4444-4444-444444444444/delete")

        assert resp.status_code == 404
        assert "Eelnõu ei leitud" in resp.text

    @patch("app.docs.routes._lifecycle.JobQueue")
    @patch("app.docs.routes._lifecycle.log_draft_delete")
    @patch("app.docs.routes._lifecycle.get_draft_artifact_paths")
    @patch("app.docs.routes._lifecycle.delete_draft")
    @patch("app.docs.routes._lifecycle._connect")
    @patch("app.docs.routes._lifecycle.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_delete_system_admin_cross_org_succeeds(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_connect: MagicMock,
        mock_delete: MagicMock,
        mock_artifacts: MagicMock,
        mock_log: MagicMock,
        mock_queue_cls: MagicMock,
    ):
        """Matrix grants system admin a cross-org delete override."""
        admin = {
            "id": "88888888-8888-8888-8888-888888888888",
            "email": "admin@seadusloome.ee",
            "full_name": "System Admin",
            "role": "admin",
            "org_id": _OTHER_ORG_ID,
        }
        provider = MagicMock()
        provider.get_current_user.return_value = admin
        mock_get_provider.return_value = provider

        draft = _make_draft()  # belongs to _ORG_ID, different from admin's org
        mock_fetch.return_value = draft

        mock_conn = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)
        mock_delete.return_value = "/tmp/ciphertext.enc"
        mock_artifacts.return_value = (["/tmp/ciphertext.enc"], [draft.graph_uri])

        client = _authed_client()
        resp = client.post(f"/drafts/{draft.id}/delete")

        assert resp.status_code == 303
        assert resp.headers["location"] == "/drafts"
        mock_delete.assert_called_once()

    @patch("app.docs.routes._lifecycle.JobQueue")
    @patch("app.docs.routes._lifecycle.log_draft_delete")
    @patch("app.docs.routes._lifecycle.get_draft_artifact_paths")
    @patch("app.docs.routes._lifecycle.delete_draft")
    @patch("app.docs.routes._lifecycle._connect")
    @patch("app.docs.routes._lifecycle.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_delete_draft_htmx_returns_hx_redirect(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_connect: MagicMock,
        mock_delete: MagicMock,
        mock_artifacts: MagicMock,
        mock_log: MagicMock,
        mock_queue_cls: MagicMock,
    ):
        """Regression for #467.

        When HTMX drives the delete form, a plain 303 causes HTMX to
        follow the redirect as an AJAX GET and swap the drafts-list
        partial (which begins with a ``<title>`` tag) into ``<body>``,
        corrupting the layout. The handler must instead return a 204
        with an ``HX-Redirect`` header so HTMX performs a real browser
        navigation.
        """
        mock_get_provider.return_value = _stub_provider()
        draft = _make_draft()
        mock_fetch.return_value = draft

        mock_conn = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)
        mock_delete.return_value = "/tmp/ciphertext.enc"
        mock_artifacts.return_value = (["/tmp/ciphertext.enc"], [draft.graph_uri])
        queue_instance = MagicMock()
        queue_instance.enqueue.return_value = 99
        mock_queue_cls.return_value = queue_instance

        client = _authed_client()
        resp = client.post(
            f"/drafts/{draft.id}/delete",
            headers={"HX-Request": "true"},
        )

        # HTMX path returns 204 + HX-Redirect, not a 303.
        assert resp.status_code == 204
        assert resp.headers["hx-redirect"] == "/drafts"
        # The underlying DB delete must still have run.
        mock_delete.assert_called_once()
        mock_log.assert_called_once()
        # #628: external cleanup is now async — verify the job was enqueued.
        queue_instance.enqueue.assert_called_once()
        assert queue_instance.enqueue.call_args.args[0] == "draft_cleanup"


# ---------------------------------------------------------------------------
# #629: /drafts/{id}/delete must be registered via register_draft_routes
# ---------------------------------------------------------------------------


class TestDeleteRouteRegistration:
    """Regression guard for #629 — keep the delete route alongside its
    siblings in ``register_draft_routes`` rather than scattered across
    ``app/main.py``. If a future refactor accidentally removes the row,
    a 405 Method Not Allowed response will fire here.
    """

    def test_delete_route_is_registered(self):
        from starlette.routing import Route

        from app import main as main_module

        post_paths = {
            route.path
            for route in main_module.app.router.routes
            if isinstance(route, Route) and "POST" in (route.methods or set())
        }
        assert "/drafts/{draft_id}/delete" in post_paths


# ---------------------------------------------------------------------------
# #599: hx-indicator wires spinners to HTMX-driven forms
# ---------------------------------------------------------------------------


class TestHxIndicator:
    @patch("app.auth.middleware._get_provider")
    def test_upload_form_has_hx_indicator(self, mock_get_provider: MagicMock):
        mock_get_provider.return_value = _stub_provider()
        client = _authed_client()
        resp = client.get("/drafts/new")
        assert resp.status_code == 200
        body = resp.text
        assert 'hx-indicator=".upload-spinner"' in body
        assert "upload-spinner" in body

    @patch("app.docs.routes._detail.log_draft_view")
    @patch("app.docs.routes._detail.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_delete_form_has_hx_indicator(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_log: MagicMock,
    ):
        mock_get_provider.return_value = _stub_provider()
        draft = _make_draft(status="ready")
        mock_fetch.return_value = draft

        client = _authed_client()
        resp = client.get(f"/drafts/{draft.id}")
        assert resp.status_code == 200
        assert 'hx-indicator=".delete-spinner"' in resp.text
        assert "delete-spinner" in resp.text


# ---------------------------------------------------------------------------
# #602: client-side 50 MB pre-check JS is embedded in the upload form
# ---------------------------------------------------------------------------


class TestUploadPrecheck:
    @patch("app.auth.middleware._get_provider")
    def test_upload_form_includes_size_precheck(self, mock_get_provider: MagicMock):
        mock_get_provider.return_value = _stub_provider()
        client = _authed_client()
        resp = client.get("/drafts/new")
        assert resp.status_code == 200
        body = resp.text
        # The 50 MB limit is encoded as a byte constant in the script.
        assert "52428800" in body
        # Inline error node is present for the pre-check to populate.
        assert 'id="field-file-error"' in body
        assert 'id="field-file-info"' in body


# ---------------------------------------------------------------------------
# #598: flash-message round-trip through session cookie on redirect
# ---------------------------------------------------------------------------


class TestFlashMessages:
    @patch("app.docs.routes._list.list_users")
    @patch("app.docs.routes._list.list_drafts_for_org_filtered")
    @patch("app.docs.routes._lifecycle.JobQueue")
    @patch("app.docs.routes._lifecycle.log_draft_delete")
    @patch("app.docs.routes._lifecycle.delete_draft")
    @patch("app.docs.routes._lifecycle._connect")
    @patch("app.docs.routes._lifecycle.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_delete_flash_toast_appears_on_listing(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_connect: MagicMock,
        mock_delete: MagicMock,
        mock_log: MagicMock,
        mock_queue_cls: MagicMock,
        mock_list_filtered: MagicMock,
        mock_list_users: MagicMock,
    ):
        """A successful delete queues a success toast for /drafts."""
        mock_get_provider.return_value = _stub_provider()
        draft = _make_draft()
        mock_fetch.return_value = draft
        mock_conn = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)
        mock_delete.return_value = "/tmp/ciphertext.enc"
        queue_instance = MagicMock()
        queue_instance.enqueue.return_value = 99
        mock_queue_cls.return_value = queue_instance
        mock_list_filtered.return_value = ([], 0)
        mock_list_users.return_value = []

        client = _authed_client()
        resp = client.post(f"/drafts/{draft.id}/delete")
        assert resp.status_code == 303
        assert any(c.name.startswith("session_") for c in client.cookies.jar), (
            "flash message must set a session cookie"
        )

        listing = client.get("/drafts")
        assert listing.status_code == 200
        assert "Eelnõu kustutatud." in listing.text
        assert 'id="toast-container"' in listing.text
        assert "toast-success" in listing.text

        # A second GET must NOT re-show the toast (it was drained).
        listing2 = client.get("/drafts")
        assert listing2.status_code == 200
        assert "Eelnõu kustutatud." not in listing2.text

    @patch("app.docs.routes._upload.log_draft_upload")
    @patch("app.docs.routes._upload.handle_upload")
    @patch("app.docs.routes._detail.log_draft_view")
    @patch("app.docs.routes._detail.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_upload_success_flashes_toast_on_detail(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_log_view: MagicMock,
        mock_handle: MagicMock,
        mock_log_upload: MagicMock,
    ):
        mock_get_provider.return_value = _stub_provider()
        draft = _make_draft()
        mock_fetch.return_value = draft

        async def _fake_handle(*_a: Any, **_kw: Any) -> Draft:
            return draft

        mock_handle.side_effect = _fake_handle

        client = _authed_client()
        resp = client.post(
            "/drafts",
            data={"title": "Test eelnõu"},
            files={
                "file": (
                    "eelnou.docx",
                    b"Test sisu",
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                )
            },
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == f"/drafts/{draft.id}"

        detail = client.get(f"/drafts/{draft.id}")
        assert detail.status_code == 200
        assert "Eelnõu üles laaditud, analüüs algas." in detail.text
        assert "toast-success" in detail.text

    @patch("app.docs.routes._detail.fetch_draft")
    @patch("app.docs.routes._lifecycle.touch_draft_access")
    @patch("app.docs.routes._lifecycle._connect")
    @patch("app.docs.routes._detail.log_draft_view")
    @patch("app.docs.routes._lifecycle.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_keep_flashes_toast_on_detail(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_log_view: MagicMock,
        mock_connect: MagicMock,
        mock_touch: MagicMock,
        mock_detail_fetch: MagicMock,
    ):
        mock_get_provider.return_value = _stub_provider()
        draft = _make_draft()
        mock_fetch.return_value = draft
        # The follow-up GET /drafts/{id} resolves the draft through the
        # detail module's own ``fetch_draft`` binding — patch it too so
        # the detail page renders the draft (not the 404 page) and the
        # flash toast is asserted against the real content (#739).
        mock_detail_fetch.return_value = draft
        mock_conn = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)

        client = _authed_client()
        resp = client.post(f"/drafts/{draft.id}/keep")
        assert resp.status_code == 303
        assert resp.headers["location"] == f"/drafts/{draft.id}"

        detail = client.get(f"/drafts/{draft.id}")
        assert detail.status_code == 200
        assert "90-päevane loendur lähtestatud." in detail.text
        assert "toast-success" in detail.text


# ---------------------------------------------------------------------------
# #600: HX-Trigger draft-ready surfaces the CTA without full-page refresh
# ---------------------------------------------------------------------------


class TestDraftReadyTrigger:
    @patch("app.docs.routes._detail.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_status_fragment_emits_hx_trigger_when_ready(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
    ):
        mock_get_provider.return_value = _stub_provider()
        draft = _make_draft(status="ready")
        mock_fetch.return_value = draft

        client = _authed_client()
        resp = client.get(f"/drafts/{draft.id}/status")
        assert resp.status_code == 200
        # The trigger header is what the detail page actions container
        # listens for (``hx-trigger="draft-ready from:body"``).
        assert resp.headers.get("hx-trigger") == "draft-ready"

    @patch("app.docs.routes._detail.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_status_fragment_no_trigger_while_still_running(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
    ):
        mock_get_provider.return_value = _stub_provider()
        draft = _make_draft(status="parsing")
        mock_fetch.return_value = draft

        client = _authed_client()
        resp = client.get(f"/drafts/{draft.id}/status")
        assert resp.status_code == 200
        # No HX-Trigger header while the draft is still pre-ready.
        assert resp.headers.get("hx-trigger") is None

    @patch("app.docs.routes._detail.log_draft_view")
    @patch("app.docs.routes._detail.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_actions_container_wired_for_draft_ready(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_log: MagicMock,
    ):
        mock_get_provider.return_value = _stub_provider()
        draft = _make_draft(status="parsing")
        mock_fetch.return_value = draft

        client = _authed_client()
        resp = client.get(f"/drafts/{draft.id}")
        assert resp.status_code == 200
        body = resp.text
        # Container exists even before the draft is ready.
        assert f'id="draft-actions-{draft.id}"' in body
        # And is wired to re-fetch itself on the draft-ready event.
        assert 'hx-trigger="draft-ready from:body"' in body
        assert f'hx-get="/drafts/{draft.id}/actions"' in body

    @patch("app.docs.routes._detail.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_actions_fragment_renders_report_cta_when_ready(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
    ):
        mock_get_provider.return_value = _stub_provider()
        draft = _make_draft(status="ready")
        mock_fetch.return_value = draft

        client = _authed_client()
        resp = client.get(f"/drafts/{draft.id}/actions")
        assert resp.status_code == 200
        assert "Vaata mõjuaruannet" in resp.text
        assert f"/drafts/{draft.id}/report" in resp.text


# ---------------------------------------------------------------------------
# #603: graph_uri no longer leaked to end users
# ---------------------------------------------------------------------------


class TestGraphUriHidden:
    @patch("app.docs.routes._detail.log_draft_view")
    @patch("app.docs.routes._detail.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_detail_page_does_not_render_graph_uri(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_log: MagicMock,
    ):
        mock_get_provider.return_value = _stub_provider()
        draft = _make_draft()
        mock_fetch.return_value = draft

        client = _authed_client()
        resp = client.get(f"/drafts/{draft.id}")
        assert resp.status_code == 200
        assert "Graafi URI" not in resp.text
        # And the actual URI string is not present either.
        assert draft.graph_uri not in resp.text


# ---------------------------------------------------------------------------
# #604: aria-live on the polled status wrapper
# ---------------------------------------------------------------------------


class TestStatusAriaLive:
    @patch("app.docs.routes._detail.log_draft_view")
    @patch("app.docs.routes._detail.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_status_wrapper_has_aria_live_polite(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_log: MagicMock,
    ):
        mock_get_provider.return_value = _stub_provider()
        draft = _make_draft(status="parsing")
        mock_fetch.return_value = draft

        client = _authed_client()
        resp = client.get(f"/drafts/{draft.id}")
        assert resp.status_code == 200
        # The wrapper needs aria-live so VoiceOver/NVDA announce stage
        # transitions as the polled fragment swaps.
        assert f'id="draft-status-{draft.id}"' in resp.text
        assert 'aria-live="polite"' in resp.text


# ---------------------------------------------------------------------------
# #640: VTK linking on upload + dedicated link-vtk route
# ---------------------------------------------------------------------------


_VTK_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
_OTHER_VTK_ID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"


def _make_vtk(
    *,
    vtk_id: str = _VTK_ID,
    org_id: str = _ORG_ID,
    title: str = "Maantee VTK",
    status: str = "ready",
) -> Draft:
    return _make_draft(
        draft_id=uuid.UUID(vtk_id),
        org_id=org_id,
        status=status,
        title=title,
        doc_type="vtk",
    )


class TestUploadWithVtkLinking:
    @patch("app.docs.routes._upload.log_draft_upload")
    @patch("app.docs.routes._upload.handle_upload")
    @patch("app.docs.routes._upload.list_vtks_for_org")
    @patch("app.auth.middleware._get_provider")
    def test_vtk_upload_persists_doc_type(
        self,
        mock_get_provider: MagicMock,
        mock_list_vtks: MagicMock,
        mock_handle: MagicMock,
        mock_log: MagicMock,
    ):
        """VTK upload forwards doc_type='vtk' with no parent link."""
        mock_get_provider.return_value = _stub_provider()
        mock_list_vtks.return_value = []
        draft = _make_draft(doc_type="vtk")

        async def _fake_handle(*_a: Any, **kw: Any) -> Draft:
            _fake_handle.kwargs = kw  # type: ignore[attr-defined]
            return draft

        mock_handle.side_effect = _fake_handle

        client = _authed_client()
        resp = client.post(
            "/drafts",
            data={"title": "VTK A", "doc_type": "vtk"},
            files={"file": ("vtk.docx", b"x", "application/octet-stream")},
        )

        assert resp.status_code == 303
        kw = _fake_handle.kwargs  # type: ignore[attr-defined]
        assert kw["doc_type"] == "vtk"
        assert kw["parent_vtk_id"] is None

    @patch("app.docs.routes._upload.log_draft_upload")
    @patch("app.docs.routes._upload.handle_upload")
    @patch("app.docs.routes._upload._connect")
    @patch("app.docs.routes._upload.list_vtks_for_org")
    @patch("app.auth.middleware._get_provider")
    def test_eelnou_upload_with_vtk_link_persists_both(
        self,
        mock_get_provider: MagicMock,
        mock_list_vtks: MagicMock,
        mock_connect: MagicMock,
        mock_handle: MagicMock,
        mock_log: MagicMock,
    ):
        """Eelnõu upload with parent_vtk_id sends both fields to handle_upload."""
        mock_get_provider.return_value = _stub_provider()
        mock_list_vtks.return_value = []
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = ("vtk",)
        mock_connect.return_value.__enter__ = MagicMock(return_value=conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)

        async def _fake_handle(*_a: Any, **kw: Any) -> Draft:
            _fake_handle.kwargs = kw  # type: ignore[attr-defined]
            return _make_draft()

        mock_handle.side_effect = _fake_handle

        client = _authed_client()
        resp = client.post(
            "/drafts",
            data={
                "title": "Eelnõu",
                "doc_type": "eelnou",
                "parent_vtk_id": _VTK_ID,
            },
            files={"file": ("eelnou.docx", b"x", "application/octet-stream")},
        )

        assert resp.status_code == 303
        kw = _fake_handle.kwargs  # type: ignore[attr-defined]
        assert kw["doc_type"] == "eelnou"
        assert str(kw["parent_vtk_id"]) == _VTK_ID

    @patch("app.docs.routes._upload.list_vtks_for_org")
    @patch("app.docs.routes._upload._connect")
    @patch("app.auth.middleware._get_provider")
    def test_cross_org_vtk_fk_rejected(
        self,
        mock_get_provider: MagicMock,
        mock_connect: MagicMock,
        mock_list_vtks: MagicMock,
    ):
        """A parent_vtk_id pointing at another org's VTK is rejected with
        a 400 + Estonian error. The FK validation query scopes to the
        caller's org so the row simply does not match."""
        mock_get_provider.return_value = _stub_provider()
        mock_list_vtks.return_value = []
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = None
        mock_connect.return_value.__enter__ = MagicMock(return_value=conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)

        client = _authed_client()
        resp = client.post(
            "/drafts",
            data={
                "title": "Eelnõu",
                "doc_type": "eelnou",
                "parent_vtk_id": _OTHER_VTK_ID,
            },
            files={"file": ("x.docx", b"x", "application/octet-stream")},
        )

        assert resp.status_code == 400
        assert "Valitud VTK ei ole kättesaadav." in resp.text

    @patch("app.docs.routes._upload.list_vtks_for_org")
    @patch("app.docs.routes._upload._connect")
    @patch("app.auth.middleware._get_provider")
    def test_parent_pointing_at_eelnou_rejected(
        self,
        mock_get_provider: MagicMock,
        mock_connect: MagicMock,
        mock_list_vtks: MagicMock,
    ):
        """parent_vtk_id must point at a doc_type='vtk' row."""
        mock_get_provider.return_value = _stub_provider()
        mock_list_vtks.return_value = []
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = ("eelnou",)
        mock_connect.return_value.__enter__ = MagicMock(return_value=conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)

        client = _authed_client()
        resp = client.post(
            "/drafts",
            data={
                "title": "Eelnõu",
                "doc_type": "eelnou",
                "parent_vtk_id": _VTK_ID,
            },
            files={"file": ("x.docx", b"x", "application/octet-stream")},
        )

        assert resp.status_code == 400
        assert "Valitud VTK ei ole kättesaadav." in resp.text

    @patch("app.docs.routes._upload.list_vtks_for_org")
    @patch("app.auth.middleware._get_provider")
    def test_vtk_upload_with_parent_rejected(
        self,
        mock_get_provider: MagicMock,
        mock_list_vtks: MagicMock,
    ):
        """A VTK cannot be linked to another VTK — mirrors the DB CHECK."""
        mock_get_provider.return_value = _stub_provider()
        mock_list_vtks.return_value = []

        client = _authed_client()
        resp = client.post(
            "/drafts",
            data={
                "title": "VTK",
                "doc_type": "vtk",
                "parent_vtk_id": _VTK_ID,
            },
            files={"file": ("vtk.docx", b"x", "application/octet-stream")},
        )

        assert resp.status_code == 400
        assert "VTK ei saa olla seotud teise VTKga." in resp.text


class TestLinkVtkHandler:
    @patch("app.docs.routes._detail.write_doc_lineage")
    @patch("app.docs.routes._detail.log_action")
    @patch("app.docs.routes._detail.update_draft_parent_vtk")
    @patch("app.docs.routes._detail._connect")
    @patch("app.docs.routes._detail.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_link_vtk_happy_path_returns_metadata_fragment(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_connect: MagicMock,
        mock_update: MagicMock,
        mock_log: MagicMock,
        mock_write_lineage: MagicMock,
    ):
        mock_get_provider.return_value = _stub_provider()
        draft = _make_draft()
        vtk = _make_vtk()

        # fetch_draft is called multiple times: the auth check, the
        # post-update refresh, and the parent-vtk lookup. Provide a
        # side_effect that keeps returning the right shape.
        mock_fetch.side_effect = [draft, draft, vtk]

        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = ("vtk",)
        mock_connect.return_value.__enter__ = MagicMock(return_value=conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)

        client = _authed_client()
        resp = client.post(
            f"/drafts/{draft.id}/link-vtk",
            data={"parent_vtk_id": _VTK_ID},
        )

        assert resp.status_code == 200
        mock_update.assert_called_once()
        args = mock_update.call_args.args
        assert str(args[2]) == _VTK_ID
        mock_write_lineage.assert_called_once()
        assert 'id="draft-metadata"' in resp.text

    @patch("app.docs.routes._detail._connect")
    @patch("app.docs.routes._detail.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_link_vtk_cross_org_rejected(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_connect: MagicMock,
    ):
        """URL-tampered cross-org FK surfaces as a 400 — the FK
        validation query scopes to the draft's own org so the foreign
        VTK appears to simply not exist."""
        mock_get_provider.return_value = _stub_provider()
        draft = _make_draft()
        mock_fetch.return_value = draft
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = None
        mock_connect.return_value.__enter__ = MagicMock(return_value=conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)

        client = _authed_client()
        resp = client.post(
            f"/drafts/{draft.id}/link-vtk",
            data={"parent_vtk_id": _OTHER_VTK_ID},
        )

        assert resp.status_code == 400
        assert "Valitud VTK ei ole kättesaadav." in resp.text

    @patch("app.docs.routes._detail.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_link_vtk_non_owner_returns_not_found(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
    ):
        """Non-owner same-org drafter cannot link/unlink someone else's draft."""
        mock_get_provider.return_value = _stub_provider()
        other_user = "99999999-9999-9999-9999-999999999999"
        mock_fetch.return_value = _make_draft(user_id=other_user)

        client = _authed_client()
        resp = client.post(
            "/drafts/44444444-4444-4444-4444-444444444444/link-vtk",
            data={"parent_vtk_id": _VTK_ID},
        )

        assert resp.status_code == 404
        assert "Eelnõu ei leitud" in resp.text


class TestLinkVtkModalOnDetailPage:
    @patch("app.docs.routes._detail.list_vtks_for_org")
    @patch("app.docs.routes._detail.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_detail_page_renders_link_vtk_modal_for_owner(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_list_vtks: MagicMock,
    ):
        """Owner viewing an unlinked eelnõu sees the "Seo VTKga" button
        + link-vtk modal with the picker populated."""
        mock_get_provider.return_value = _stub_provider()
        mock_fetch.return_value = _make_draft(status="ready")
        mock_list_vtks.return_value = [_make_vtk(title="Maantee VTK")]

        client = _authed_client()
        resp = client.get("/drafts/44444444-4444-4444-4444-444444444444")

        assert resp.status_code == 200
        assert 'id="draft-metadata"' in resp.text
        assert "Seotud VTK" in resp.text
        assert "Seo VTKga" in resp.text
        assert 'id="link-vtk-modal"' in resp.text
        assert "Maantee VTK" in resp.text
        assert 'hx-post="/drafts/44444444-4444-4444-4444-444444444444/link-vtk"' in resp.text


# ---------------------------------------------------------------------------
# #643: Tüüp column on the drafts list + VTK detail children card
# ---------------------------------------------------------------------------


class TestDocTypeColumn:
    """Drafts list now renders a Tüüp badge as the leftmost column."""

    @patch("app.docs.routes._list.list_users")
    @patch("app.docs.routes._list.list_drafts_for_org_filtered")
    @patch("app.auth.middleware._get_provider")
    def test_doc_type_badge_per_row(
        self,
        mock_get_provider: MagicMock,
        mock_list_filtered: MagicMock,
        mock_list_users: MagicMock,
    ):
        """Mixed-type listing shows the Eelnõu badge AND the VTK badge."""
        mock_get_provider.return_value = _stub_provider()
        mock_list_users.return_value = []
        eelnou = _make_draft(title="Eelnõu A", status="ready")
        vtk = _make_vtk(title="VTK A")
        mock_list_filtered.return_value = ([eelnou, vtk], 2)

        client = _authed_client()
        resp = client.get("/drafts")

        assert resp.status_code == 200
        # Header for the new column is present
        assert "Tüüp" in resp.text
        # Two badge cells, one per row (different variants)
        assert "doc-type-eelnou" in resp.text
        assert "doc-type-vtk" in resp.text
        # And the labels themselves
        assert ">Eelnõu<" in resp.text
        assert ">VTK<" in resp.text


class TestVtkDetailChildrenCard:
    """VTK detail page surfaces its follow-on eelnõud."""

    @patch("app.docs.routes._detail.list_eelnous_for_vtk")
    @patch("app.docs.routes._detail.list_vtks_for_org")
    @patch("app.docs.routes._detail.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_vtk_detail_does_not_show_seotud_vtk_row(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_list_vtks: MagicMock,
        mock_list_children: MagicMock,
    ):
        """VTKs cannot have parents — the metadata row must be omitted."""
        mock_get_provider.return_value = _stub_provider()
        mock_fetch.return_value = _make_vtk(title="My VTK", status="ready")
        mock_list_vtks.return_value = []
        mock_list_children.return_value = []

        client = _authed_client()
        resp = client.get(f"/drafts/{_VTK_ID}")

        assert resp.status_code == 200
        # The "Seotud VTK" metadata row is the eelnõu-only widget; for
        # a VTK we render neither the <dt> label nor the picker trigger.
        # (The literal phrase "Seotud VTK" can still appear in the
        # children-card empty-state helper text — that's fine.)
        assert "<dt>Seotud VTK</dt>" not in resp.text
        assert "Seo VTKga" not in resp.text

    @patch("app.docs.routes._detail.list_users")
    @patch("app.docs.routes._detail.list_eelnous_for_vtk")
    @patch("app.docs.routes._detail.list_vtks_for_org")
    @patch("app.docs.routes._detail.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_vtk_detail_lists_children_newest_first(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_list_vtks: MagicMock,
        mock_list_children: MagicMock,
        mock_list_users: MagicMock,
    ):
        """Children render in the order returned by the helper, with
        title link, status badge, uploader name, and upload date.

        Uploader resolution must be a single bulk lookup, NOT N+1
        per-child get_user calls.
        """
        mock_get_provider.return_value = _stub_provider()
        mock_fetch.return_value = _make_vtk(title="My VTK", status="ready")
        mock_list_vtks.return_value = []
        # Helper already orders newest-first; route must preserve order.
        newer = _make_draft(
            draft_id=uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
            title="Newer eelnõu",
            status="ready",
        )
        older = _make_draft(
            draft_id=uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"),
            title="Older eelnõu",
            status="parsing",
        )
        mock_list_children.return_value = [newer, older]
        # Bulk uploader resolution — one call returns every user in the
        # org. Both children share the same uploader id (_USER_ID).
        mock_list_users.return_value = [
            {"id": _USER_ID, "full_name": "Jaan Tamm", "email": "jaan@example.ee"}
        ]

        client = _authed_client()
        resp = client.get(f"/drafts/{_VTK_ID}")

        assert resp.status_code == 200
        assert "Sellest VTKst tulenevad eelnõud" in resp.text
        assert "Newer eelnõu" in resp.text
        assert "Older eelnõu" in resp.text
        assert "Jaan Tamm" in resp.text
        # Newest first — find both substrings, assert ordering.
        assert resp.text.index("Newer eelnõu") < resp.text.index("Older eelnõu")
        # Status badges for both children rendered.
        assert "draft-status-ok" in resp.text  # ready -> ok
        assert "draft-status-running" in resp.text  # parsing -> running
        # No-N+1 invariant: list_users called exactly once, regardless
        # of child count.
        assert mock_list_users.call_count == 1
        # And it was scoped to the VTK's org_id.
        assert mock_list_users.call_args.kwargs["org_id"] == _ORG_ID
        # list_eelnous_for_vtk now requires keyword org_id at the SQL
        # layer — no post-filter in the route.
        assert mock_list_children.call_args.kwargs["org_id"] == uuid.UUID(_ORG_ID)

    @patch("app.docs.routes._detail.list_eelnous_for_vtk")
    @patch("app.docs.routes._detail.list_vtks_for_org")
    @patch("app.docs.routes._detail.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_vtk_detail_with_no_children_shows_empty_state(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_list_vtks: MagicMock,
        mock_list_children: MagicMock,
    ):
        """Empty-children state renders the EmptyState primitive copy."""
        mock_get_provider.return_value = _stub_provider()
        mock_fetch.return_value = _make_vtk(title="My VTK", status="ready")
        mock_list_vtks.return_value = []
        mock_list_children.return_value = []

        client = _authed_client()
        resp = client.get(f"/drafts/{_VTK_ID}")

        assert resp.status_code == 200
        assert "Sellest VTKst tulenevad eelnõud" in resp.text
        assert "VTKga pole veel eelnõusid seotud." in resp.text

    @patch("app.docs.routes._detail.list_eelnous_for_vtk")
    @patch("app.docs.routes._detail.list_vtks_for_org")
    @patch("app.docs.routes._detail.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_eelnou_detail_does_not_render_children_card(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_list_vtks: MagicMock,
        mock_list_children: MagicMock,
    ):
        """Children card is VTK-only — eelnõu detail must not render it."""
        mock_get_provider.return_value = _stub_provider()
        mock_fetch.return_value = _make_draft(status="ready")
        mock_list_vtks.return_value = []

        client = _authed_client()
        resp = client.get("/drafts/44444444-4444-4444-4444-444444444444")

        assert resp.status_code == 200
        assert "Sellest VTKst tulenevad eelnõud" not in resp.text
        # And we never even called list_eelnous_for_vtk on the eelnõu path.
        mock_list_children.assert_not_called()


class TestListEelnousForVtkHelper:
    """Route-level wiring of the children helper."""

    @patch("app.docs.routes._detail.list_eelnous_for_vtk")
    @patch("app.docs.routes._detail.list_vtks_for_org")
    @patch("app.docs.routes._detail.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_route_passes_org_id_to_helper(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_list_vtks: MagicMock,
        mock_list_children: MagicMock,
    ):
        """The route must call ``list_eelnous_for_vtk(vtk_id, org_id=...)``
        — primary defence is at the SQL layer, not in the route. Any
        future caller of the helper that forgets ``org_id`` will fail
        loudly (kw-only required) rather than silently leaking."""
        mock_get_provider.return_value = _stub_provider()
        vtk = _make_vtk(title="My VTK", status="ready")
        mock_fetch.return_value = vtk
        mock_list_vtks.return_value = []
        mock_list_children.return_value = []

        client = _authed_client()
        resp = client.get(f"/drafts/{_VTK_ID}")

        assert resp.status_code == 200
        # Helper called with the VTK's own org_id at the SQL boundary.
        assert mock_list_children.call_args.args[0] == uuid.UUID(_VTK_ID)
        assert mock_list_children.call_args.kwargs["org_id"] == uuid.UUID(_ORG_ID)


# ---------------------------------------------------------------------------
# #656 — Retry endpoint for failed drafts
# ---------------------------------------------------------------------------


class TestRetryFailedDraft:
    """POST /drafts/{id}/retry resets a failed draft and re-enqueues parse."""

    @patch("app.docs.retry_handler.log_action")
    @patch("app.docs.retry_handler.JobQueue")
    @patch("app.docs.retry_handler._reset_draft_for_retry")
    @patch("app.docs.retry_handler.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_retry_failed_draft_resets_and_enqueues_parse(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_reset: MagicMock,
        mock_queue_cls: MagicMock,
        mock_log_action: MagicMock,
    ):
        """A failed draft: status resets, error columns clear, parse_draft
        is enqueued, HTMX gets a redirect response, AND the audit log
        records the retry with the prior_error payload (#669, #678)."""
        mock_get_provider.return_value = _stub_provider()
        draft = _make_draft(
            status="failed",
            error_message="Töötlemine ebaõnnestus tehnilisel põhjusel.",
        )
        mock_fetch.return_value = draft
        mock_reset.return_value = True
        mock_queue = MagicMock()
        mock_queue.enqueue.return_value = 42
        mock_queue_cls.return_value = mock_queue

        client = _authed_client()
        resp = client.post(
            f"/drafts/{draft.id}/retry",
            headers={"HX-Request": "true"},
        )

        assert resp.status_code == 204
        assert resp.headers["HX-Redirect"] == f"/drafts/{draft.id}"
        # Row reset happened.
        mock_reset.assert_called_once_with(str(draft.id))
        # Parse job enqueued with the right payload.
        mock_queue.enqueue.assert_called_once()
        enqueue_args = mock_queue.enqueue.call_args
        assert enqueue_args.args[0] == "parse_draft"
        assert enqueue_args.args[1] == {"draft_id": str(draft.id)}
        # Audit log captures the action AND the prior error so we can
        # answer "what did the user retry?" from the audit trail alone.
        mock_log_action.assert_called_once()
        log_args = mock_log_action.call_args
        # log_action(user_id, action, detail)
        assert log_args.args[1] == "draft.retry"
        detail = log_args.args[2]
        assert detail["draft_id"] == str(draft.id)
        assert detail["job_id"] == 42
        assert "prior_error" in detail
        assert detail["prior_error"] == "Töötlemine ebaõnnestus tehnilisel põhjusel."

    @patch("app.docs.retry_handler.JobQueue")
    @patch("app.docs.retry_handler._reset_draft_for_retry")
    @patch("app.docs.retry_handler.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_retry_non_htmx_returns_303(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_reset: MagicMock,
        mock_queue_cls: MagicMock,
    ):
        """POST without ``HX-Request``: happy path still resets + enqueues
        but responds with a plain 303 redirect so full-page navigations
        land back on the detail page (#679)."""
        mock_get_provider.return_value = _stub_provider()
        draft = _make_draft(
            status="failed",
            error_message="Töötlemine ebaõnnestus tehnilisel põhjusel.",
        )
        mock_fetch.return_value = draft
        mock_reset.return_value = True
        mock_queue = MagicMock()
        mock_queue.enqueue.return_value = 99
        mock_queue_cls.return_value = mock_queue

        client = _authed_client()
        # Deliberately NO HX-Request header.
        resp = client.post(f"/drafts/{draft.id}/retry")

        assert resp.status_code == 303
        assert resp.headers["location"] == f"/drafts/{draft.id}"
        # The pipeline still restarted — only the response shape differs.
        mock_reset.assert_called_once_with(str(draft.id))
        mock_queue.enqueue.assert_called_once()

    @patch("app.docs.retry_handler.JobQueue")
    @patch("app.docs.retry_handler._reset_draft_for_retry")
    @patch("app.docs.retry_handler.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_retry_non_failed_draft_is_noop(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_reset: MagicMock,
        mock_queue_cls: MagicMock,
    ):
        """Retrying a draft that's still running must NOT re-enqueue.

        Protects against a stale open tab POSTing after the pipeline
        already restarted — that would otherwise produce two concurrent
        runs on the same draft.
        """
        mock_get_provider.return_value = _stub_provider()
        draft = _make_draft(status="parsing")
        mock_fetch.return_value = draft
        mock_queue = MagicMock()
        mock_queue_cls.return_value = mock_queue

        client = _authed_client()
        resp = client.post(f"/drafts/{draft.id}/retry")

        # Non-HTMX returns a 303 back to the detail page.
        assert resp.status_code == 303
        assert resp.headers["location"] == f"/drafts/{draft.id}"
        # No DB reset and no enqueue.
        mock_reset.assert_not_called()
        mock_queue.enqueue.assert_not_called()

    @patch("app.docs.retry_handler.JobQueue")
    @patch("app.docs.retry_handler._reset_draft_for_retry")
    @patch("app.docs.retry_handler.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_retry_other_org_returns_not_found(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_reset: MagicMock,
        mock_queue_cls: MagicMock,
    ):
        """Cross-org callers resolve to 404 — never a 403 — so we don't
        leak existence of a draft belonging to another organisation."""
        mock_get_provider.return_value = _stub_provider()
        foreign = _make_draft(org_id=_OTHER_ORG_ID, status="failed")
        mock_fetch.return_value = foreign
        mock_queue = MagicMock()
        mock_queue_cls.return_value = mock_queue

        client = _authed_client()
        resp = client.post(f"/drafts/{foreign.id}/retry")

        assert resp.status_code == 404
        assert "Eelnõu ei leitud" in resp.text
        mock_reset.assert_not_called()
        mock_queue.enqueue.assert_not_called()

    def test_retry_unauthenticated_redirects_to_login(self):
        from app.main import app

        client = TestClient(app, follow_redirects=False)
        resp = client.post("/drafts/44444444-4444-4444-4444-444444444444/retry")
        assert resp.status_code == 303
        assert resp.headers["location"] == "/auth/login"

    @patch("app.docs.routes._detail.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_failed_detail_page_renders_retry_button(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
    ):
        """The failed-draft banner must include a 'Proovi uuesti' button
        that POSTs to /drafts/{id}/retry — the user has no other way to
        re-run the pipeline short of re-uploading the file."""
        mock_get_provider.return_value = _stub_provider()
        draft = _make_draft(
            status="failed",
            error_message="Töötlemine ebaõnnestus tehnilisel põhjusel.",
        )
        mock_fetch.return_value = draft

        client = _authed_client()
        resp = client.get(f"/drafts/{draft.id}")

        assert resp.status_code == 200
        assert "Proovi uuesti" in resp.text
        assert f"/drafts/{draft.id}/retry" in resp.text


class TestResetDraftForRetrySql:
    """#670: the retry reset must also clear ``processing_completed_at``
    so a re-run writes a fresh completion time instead of keeping the
    stale one from the prior failed attempt.
    """

    @patch("app.docs.retry_handler._connect")
    def test_reset_clears_processing_completed_at(self, mock_connect: MagicMock):
        from app.docs.retry_handler import _reset_draft_for_retry

        conn = MagicMock()
        conn.execute.return_value.rowcount = 1
        mock_connect.return_value.__enter__.return_value = conn

        assert _reset_draft_for_retry("44444444-4444-4444-4444-444444444444") is True

        # #618 PR-B: update_draft_status now writes to BOTH draft_versions
        # AND drafts in the same call.  The drafts UPDATE owns the
        # processing_completed_at clear.
        drafts_call = next(
            c for c in conn.execute.call_args_list if "update drafts" in c.args[0].lower()
        )
        sql = drafts_call.args[0].lower()
        assert "processing_completed_at = null" in sql


# ---------------------------------------------------------------------------
# #657 — Elapsed "möödas" counter: frozen on terminal, correct H:MM:SS
# ---------------------------------------------------------------------------


class TestElapsedCounterTerminalStates:
    """Live ticker must stop on terminal drafts and format hours correctly."""

    @patch("app.docs.routes._detail.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_ready_draft_does_not_render_live_ticker(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
    ):
        """A ``ready`` draft: NO ``.draft-stage-elapsed`` class anywhere,
        NO "möödas" suffix. Instead a frozen "Analüüsitud" label renders
        the total processing duration.
        """
        mock_get_provider.return_value = _stub_provider()
        now = datetime.now(UTC)
        draft = _make_draft(
            status="ready",
            created_at=now - timedelta(hours=2, minutes=30),
            updated_at=now - timedelta(hours=1),  # ready long ago
        )
        mock_fetch.return_value = draft

        client = _authed_client()
        resp = client.get(f"/drafts/{draft.id}")

        assert resp.status_code == 200
        # No live-ticker hooks.
        assert "draft-stage-elapsed" not in resp.text
        assert "möödas" not in resp.text
        # Frozen completion label is present. Duration ≈ 1h30m =
        # 5400s; the label surfaces "h" so we can assert the unit
        # without pinning the exact value under test-run clock jitter.
        assert "Analüüsitud" in resp.text

    @patch("app.docs.routes._detail.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_failed_draft_does_not_render_live_ticker(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
    ):
        """A ``failed`` draft must never leave a live ticker on the
        page. The ticker script element is gated on the presence of
        ``.draft-stage-elapsed`` so we assert on both.
        """
        mock_get_provider.return_value = _stub_provider()
        now = datetime.now(UTC)
        draft = _make_draft(
            status="failed",
            error_message="Töötlemine ebaõnnestus tehnilisel põhjusel.",
            created_at=now - timedelta(hours=4),
            updated_at=now - timedelta(hours=4),
        )
        mock_fetch.return_value = draft

        client = _authed_client()
        resp = client.get(f"/drafts/{draft.id}")

        assert resp.status_code == 200
        assert "draft-stage-elapsed" not in resp.text
        assert "möödas" not in resp.text
        assert "__draftElapsedTimer" not in resp.text

    @patch("app.docs.routes._detail.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_multi_hour_elapsed_formats_as_h_mm_ss(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
    ):
        """A pipeline that's genuinely been running for >1h must render
        the ``H:MM:SS möödas`` format server-side, not the broken
        three-digit ``MMM:SS`` overflow."""
        from app.docs.routes import _format_elapsed

        # Server-side helper: past 60 minutes the format switches.
        assert _format_elapsed(59) == "0:59 möödas"
        assert _format_elapsed(3599) == "59:59 möödas"
        assert _format_elapsed(3600) == "1:00:00 möödas"
        assert _format_elapsed(3661) == "1:01:01 möödas"
        assert _format_elapsed(36000) == "10:00:00 möödas"

    @patch("app.docs.routes._detail.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_ticker_script_clears_interval_when_no_nodes(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
    ):
        """The inline ticker script must clear its interval when the
        DOM swap leaves no ``.draft-stage-elapsed`` nodes behind.

        We can't run the JS in a unit test, so we assert that the
        emitted script contains the guard branch that performs the
        cleanup. The rest is covered by the no-ticker-on-terminal
        tests above, which confirm that the swap-in HTML has zero
        ``.draft-stage-elapsed`` elements in the first place.
        """
        mock_get_provider.return_value = _stub_provider()
        now = datetime.now(UTC)
        draft = _make_draft(
            status="extracting",
            created_at=now - timedelta(seconds=120),
            updated_at=now - timedelta(seconds=120),
        )
        mock_fetch.return_value = draft

        client = _authed_client()
        resp = client.get(
            f"/drafts/{draft.id}/status",
            headers={"HX-Request": "true"},
        )

        assert resp.status_code == 200
        # Sanity: running draft must still attach a ticker.
        assert "draft-stage-elapsed" in resp.text
        # Guard branch: when no nodes remain, clearInterval runs.
        assert "nodes.length === 0" in resp.text


class TestProcessingCompletedAtFreeze:
    """#670: the final "Analüüsitud" label must be frozen at pipeline
    completion time, not recomputed from ``updated_at`` (which bumps
    on every edit and would retroactively inflate the duration)."""

    def test_prefers_processing_completed_at_over_updated_at(self):
        """When ``processing_completed_at`` is set, use it even if
        ``updated_at`` is much later (e.g. a rename happened after
        the draft finished processing).
        """
        from app.docs.routes import _processing_duration_seconds

        now = datetime.now(UTC)
        draft = _make_draft(
            status="ready",
            created_at=now - timedelta(hours=12, seconds=42),
            updated_at=now,  # rename happened 12h later
            processing_completed_at=now - timedelta(hours=12),
        )

        # Pipeline took 42s, not 12h.
        assert _processing_duration_seconds(draft) == 42

    def test_falls_back_to_updated_at_when_completion_null(self):
        """Legacy rows (before migration 023 backfill, or edge cases)
        still render a duration via ``updated_at - created_at``.
        """
        from app.docs.routes import _processing_duration_seconds

        now = datetime.now(UTC)
        draft = _make_draft(
            status="ready",
            created_at=now - timedelta(seconds=90),
            updated_at=now,
            processing_completed_at=None,
        )

        assert _processing_duration_seconds(draft) == 90

    def test_returns_none_when_created_at_missing(self):
        """Defensive: ``_processing_duration_seconds`` must not crash
        on a draft with no ``created_at`` (practically impossible
        given the NOT NULL constraint, but the helper is defensive).
        """
        from app.docs.routes import _processing_duration_seconds

        draft = _make_draft(status="ready")
        # Simulate a missing timestamp. ``created_at`` is typed as
        # ``datetime`` so this is a deliberate bypass for defensive-path
        # coverage.
        draft.created_at = None  # type: ignore[assignment]
        assert _processing_duration_seconds(draft) is None

    @patch("app.docs.routes._detail.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_ready_draft_rename_does_not_inflate_label(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
    ):
        """End-to-end: a draft that finished in 42 seconds but was
        renamed 12 hours later must still render a ``0:42`` /
        ``42 s`` duration in the completion label, NOT a 12 h
        duration driven by the bumped ``updated_at``.
        """
        mock_get_provider.return_value = _stub_provider()
        now = datetime.now(UTC)
        draft = _make_draft(
            status="ready",
            created_at=now - timedelta(hours=12, seconds=42),
            updated_at=now,  # simulated rename just now
            processing_completed_at=now - timedelta(hours=12),  # true completion
        )
        mock_fetch.return_value = draft

        client = _authed_client()
        resp = client.get(f"/drafts/{draft.id}")

        assert resp.status_code == 200
        assert "Analüüsitud" in resp.text
        # The bug: if the helper used updated_at, the label would
        # contain the 12-hour duration marker ("12 h" / "12:00:00").
        # Neither must appear.
        assert "12 h" not in resp.text
        assert "12:00:00" not in resp.text


# ---------------------------------------------------------------------------
# #656 — Migration 021 applies idempotently (mask leaked env errors)
# ---------------------------------------------------------------------------


class TestMaskLeakedEnvErrorsMigration:
    """Migration 021 masks leaked env var strings without destroying data.

    The migration runs against live Postgres in CI; here we verify the
    SQL file is well-formed and the masking logic behaves as documented
    by running the UPDATE statements against an in-memory sqlite-ish
    stub. We do NOT spin up a real PG — that's the integration suite's
    job — but we DO exercise the exact idempotency properties the
    migration claims.

    Real-DB integration harness (#680): as of 2026-04-17 this repo has
    no conftest-level fixture that spins up a transient postgres /
    pgvector container for migration testing. ``tests/conftest.py``
    only sets ``DISABLE_BACKGROUND_WORKER=1`` and ``app.db`` is always
    mocked in test code (see ``@patch('app.db.get_connection')``
    elsewhere in this file). A real-DB assertion of migrations 021 +
    022 would require adding that fixture — tracked separately — so
    here we limit ourselves to SQL-text assertions that catch drift
    between the migration files and their spec. Do NOT add a broken
    integration test in place; it would silently become a no-op.
    """

    def test_migration_file_exists_and_references_canonical_message(self):
        """Migration 021 must exist and hardcode the exact MSG_UNKNOWN
        string from error_mapping.py. If these ever drift a future
        migration must realign them."""
        from pathlib import Path

        from app.docs.error_mapping import MSG_UNKNOWN

        migration_path = (
            Path(__file__).parent.parent / "migrations" / "021_mask_leaked_env_errors.sql"
        )
        assert migration_path.exists(), "Migration 021 must exist"
        sql = migration_path.read_text()
        # The canonical Estonian message is the replacement target.
        assert MSG_UNKNOWN in sql, "Migration must hardcode MSG_UNKNOWN exactly"
        # Every secret env var name the spec enumerates must be matched.
        for marker in (
            "ANTHROPIC_API_KEY",
            "STORAGE_ENCRYPTION_KEY",
            "TIKA_URL",
            "APP_ENV=",
            "VOYAGE_API_KEY",
        ):
            assert marker in sql, f"Migration must match {marker}"
        # Guard: the canonical-message row exclusion prevents a re-run
        # from re-touching already-masked rows (idempotency).
        assert "error_message != '" in sql

    def test_migration_preserves_original_in_error_debug(self):
        """The spec calls for error_debug to receive the original
        message only when it is NULL. The migration must implement that
        'copy but don't clobber' semantics."""
        from pathlib import Path

        migration_path = (
            Path(__file__).parent.parent / "migrations" / "021_mask_leaked_env_errors.sql"
        )
        sql = migration_path.read_text()
        assert "error_debug IS NULL" in sql

    def test_migration_has_no_destructive_down(self):
        """Non-destructive by design: original is saved in error_debug.
        The migration file must NOT ship a DOWN migration that could
        be accidentally applied and lose the preserved copy."""
        from pathlib import Path

        migration_path = (
            Path(__file__).parent.parent / "migrations" / "021_mask_leaked_env_errors.sql"
        )
        sql = migration_path.read_text().lower()
        # No "drop", "delete from drafts", or "alter table ... drop"
        # statements — the migration only UPDATEs.
        assert "delete from drafts" not in sql
        assert "drop table" not in sql
        assert "drop column" not in sql

    # ------------------------------------------------------------------
    # Migration 022 — widened leak patterns (#667)
    # ------------------------------------------------------------------

    def test_migration_022_exists_and_references_canonical_message(self):
        """Migration 022 widens 021's pattern set and MUST reuse the
        same MSG_UNKNOWN canonical replacement so the user-facing
        fallback stays consistent across rewrites."""
        from pathlib import Path

        from app.docs.error_mapping import MSG_UNKNOWN

        migration_path = (
            Path(__file__).parent.parent / "migrations" / "022_widen_leaked_error_masking.sql"
        )
        assert migration_path.exists(), "Migration 022 must exist"
        sql = migration_path.read_text()
        assert MSG_UNKNOWN in sql, "Migration 022 must hardcode MSG_UNKNOWN exactly"
        # Every widened leak pattern the spec enumerates must be present.
        for marker in (
            "DATABASE_URL",
            "FUSEKI_URL",
            "JWT_SECRET",
            "SMTP_HOST",
            "SMTP_USER",
            "SMTP_PASSWORD",
            "Traceback (most recent call last):",
            'File "/app/',
        ):
            assert marker in sql, f"Migration 022 must match {marker!r}"
        # Length-gated env-var-assignment regex: [A-Z_]{6,}= over 200 chars.
        assert "[A-Z_]{6,}=" in sql
        assert "char_length(error_message) > 200" in sql

    def test_migration_022_is_idempotent_and_non_clobbering(self):
        """022 must mirror 021's idempotency guards: error_debug is
        only filled when NULL, and error_message is only rewritten when
        it is not already the canonical Estonian fallback."""
        from pathlib import Path

        migration_path = (
            Path(__file__).parent.parent / "migrations" / "022_widen_leaked_error_masking.sql"
        )
        sql = migration_path.read_text()
        # "copy but don't clobber" guard for error_debug.
        assert "error_debug IS NULL" in sql
        # Idempotency guard for the user-facing message rewrite.
        assert "error_message != '" in sql

    def test_migration_022_has_no_destructive_down(self):
        """022 must be update-only, like 021."""
        from pathlib import Path

        migration_path = (
            Path(__file__).parent.parent / "migrations" / "022_widen_leaked_error_masking.sql"
        )
        sql = migration_path.read_text().lower()
        assert "delete from drafts" not in sql
        assert "drop table" not in sql
        assert "drop column" not in sql


# ===========================================================================
# #618 PR-C — versioning UI: timeline section + diff route
# ===========================================================================


def _make_version(
    *,
    version_id: uuid.UUID | None = None,
    draft_id: uuid.UUID | None = None,
    user_id: str = _USER_ID,
    version_number: int = 1,
    reading_stage: str = "vtk",
    parsed_text_encrypted: bytes | None = None,
    status: str = "ready",
    created_at: datetime | None = None,
):
    """Build a ``DraftVersion`` for the timeline + diff tests (#618 PR-C)."""
    from app.docs.version_model import DraftVersion

    resolved_draft_id = draft_id or uuid.UUID("44444444-4444-4444-4444-444444444444")
    resolved_id = version_id or uuid.UUID(f"99999999-9999-9999-9999-{version_number:012d}")
    return DraftVersion(
        id=resolved_id,
        draft_id=resolved_draft_id,
        version_number=version_number,
        reading_stage=reading_stage,
        parsed_text_encrypted=parsed_text_encrypted,
        storage_path=f"/tmp/v{version_number}.enc",
        graph_uri=(
            f"https://data.riik.ee/ontology/estleg/drafts/{resolved_draft_id}/v{version_number}"
        ),
        status=status,
        created_at=created_at or datetime.now(UTC),
        created_by=uuid.UUID(user_id),
    )


def _stub_connect(mock_connect: MagicMock) -> MagicMock:
    """Wire a ``patch('app.docs.routes._connect')`` to behave as a
    successful context manager handing back a MagicMock connection.

    Used by the timeline tests because ``draft_detail_page`` opens a
    ``with _connect() as conn`` block before invoking
    ``_version_timeline_rows`` — without this stub the connection
    fails and the helper mock never fires.
    """
    mock_conn = MagicMock()
    mock_connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
    mock_connect.return_value.__exit__ = MagicMock(return_value=False)
    return mock_conn


class TestVersionTimelineOnDetailPage:
    """The detail page renders a "Versioonide ajalugu" card with one row per version."""

    @patch("app.docs.routes._detail._version_timeline_rows")
    @patch("app.docs.routes._detail._connect")
    @patch("app.docs.routes._detail.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_renders_one_row_per_version_in_ascending_order(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_connect: MagicMock,
        mock_timeline: MagicMock,
    ):
        mock_get_provider.return_value = _stub_provider()
        draft = _make_draft(status="ready")
        mock_fetch.return_value = draft
        _stub_connect(mock_connect)
        # Helper returns rows already in v1 → v3 order. The test
        # asserts the order survives into the rendered markup so a
        # later refactor can't silently flip the table direction.
        mock_timeline.return_value = [
            {
                "version": _make_version(version_number=1, reading_stage="vtk"),
                "uploader_label": "Mari Maasikas",
                "is_first": True,
            },
            {
                "version": _make_version(version_number=2, reading_stage="reading_1"),
                "uploader_label": "Mari Maasikas",
                "is_first": False,
            },
            {
                "version": _make_version(version_number=3, reading_stage="reading_2"),
                "uploader_label": "Jüri Mustikas",
                "is_first": False,
            },
        ]

        client = _authed_client()
        resp = client.get(f"/drafts/{draft.id}")

        assert resp.status_code == 200
        body = resp.text
        assert "Versioonide ajalugu" in body
        # Each version number is rendered.
        assert "v1" in body
        assert "v2" in body
        assert "v3" in body
        # Ascending order: v1 must appear before v2, v2 before v3.
        assert body.index("v1") < body.index("v2") < body.index("v3")
        # Reading stage labels in Estonian.
        assert "VTK" in body
        assert "1. lugemine" in body
        assert "2. lugemine" in body
        # Uploader display names from the resolved row.
        assert "Mari Maasikas" in body
        assert "Jüri Mustikas" in body

    @patch("app.docs.routes._detail._version_timeline_rows")
    @patch("app.docs.routes._detail._connect")
    @patch("app.docs.routes._detail.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_first_version_has_no_diff_button(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_connect: MagicMock,
        mock_timeline: MagicMock,
    ):
        """v1 has no predecessor — the "Erinevus" button must be absent on
        v1 but present on every later version."""
        mock_get_provider.return_value = _stub_provider()
        draft = _make_draft(status="ready")
        mock_fetch.return_value = draft
        _stub_connect(mock_connect)
        mock_timeline.return_value = [
            {
                "version": _make_version(version_number=1),
                "uploader_label": "Mari",
                "is_first": True,
            },
            {
                "version": _make_version(version_number=2, reading_stage="reading_1"),
                "uploader_label": "Mari",
                "is_first": False,
            },
        ]

        client = _authed_client()
        resp = client.get(f"/drafts/{draft.id}")

        assert resp.status_code == 200
        body = resp.text
        # Only ONE diff link rendered (v2 vs v1). v1 has no predecessor.
        assert body.count(f"/drafts/{draft.id}/diff?from=1&amp;to=2") == 1
        # No diff link starting at version 0 (would be a v1 button).
        assert "from=0" not in body

    @patch("app.docs.routes._detail._version_timeline_rows")
    @patch("app.docs.routes._detail._connect")
    @patch("app.docs.routes._detail.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_every_version_has_open_button(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_connect: MagicMock,
        mock_timeline: MagicMock,
    ):
        """The "Ava" link routes to /drafts/{id}/report?version=<v.id>
        for every version in the timeline."""
        mock_get_provider.return_value = _stub_provider()
        draft = _make_draft(status="ready")
        mock_fetch.return_value = draft
        _stub_connect(mock_connect)
        v1 = _make_version(version_number=1)
        v2 = _make_version(version_number=2, reading_stage="reading_1")
        mock_timeline.return_value = [
            {"version": v1, "uploader_label": "Mari", "is_first": True},
            {"version": v2, "uploader_label": "Mari", "is_first": False},
        ]

        client = _authed_client()
        resp = client.get(f"/drafts/{draft.id}")

        assert resp.status_code == 200
        body = resp.text
        # Each version's report link uses its own UUID.
        assert f"/drafts/{draft.id}/report?version={v1.id}" in body
        assert f"/drafts/{draft.id}/report?version={v2.id}" in body
        # "Ava" button label appears at least twice (once per version).
        assert body.count(">Ava<") >= 2


class TestDraftDiffPage:
    """GET /drafts/{draft_id}/diff?from=<v1>&to=<v2> — side-by-side diff page."""

    @patch("app.docs.routes._detail_versions.list_versions_for_draft")
    @patch("app.docs.routes._detail_versions._connect")
    @patch("app.docs.routes._detail_versions.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_valid_request_renders_diff_table(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_connect: MagicMock,
        mock_list_versions: MagicMock,
    ):
        from app.storage.encrypted import encrypt_text

        mock_get_provider.return_value = _stub_provider()
        draft = _make_draft(status="ready")
        mock_fetch.return_value = draft
        _stub_connect(mock_connect)
        # Two versions with real Fernet ciphertext so decrypt_text
        # round-trips inside the route.
        v1 = _make_version(
            version_number=1,
            reading_stage="vtk",
            parsed_text_encrypted=encrypt_text("§ 1. Vana sõnastus."),
        )
        v2 = _make_version(
            version_number=2,
            reading_stage="reading_1",
            parsed_text_encrypted=encrypt_text("§ 1. Uus sõnastus."),
        )
        mock_list_versions.return_value = [v2, v1]  # DESC, like the real helper.

        client = _authed_client()
        resp = client.get(f"/drafts/{draft.id}/diff?from=1&to=2")

        assert resp.status_code == 200
        body = resp.text
        # Page title carries the v1 → v2 hint so the user always sees
        # which direction the diff runs in.
        assert "Versioonide erinevus v1" in body
        assert "v2" in body
        # The diff table is mounted with its CSS hook.
        assert "diff-table" in body
        # Both versions' text lines surface in the side-by-side cells.
        assert "Vana sõnastus" in body
        assert "Uus sõnastus" in body
        # Side-by-side column headers.
        assert "Vana versioon" in body
        assert "Uus versioon" in body
        # Back-link to the parent draft.
        assert f"/drafts/{draft.id}" in body

    @patch("app.docs.routes._detail_versions.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_cross_org_returns_not_found_page(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
    ):
        """Cross-org callers must hit the 404 page — never leak existence."""
        mock_get_provider.return_value = _stub_provider()
        # Draft belongs to a different org than the authed user.
        foreign = _make_draft(org_id=_OTHER_ORG_ID)
        mock_fetch.return_value = foreign

        client = _authed_client()
        resp = client.get(f"/drafts/{foreign.id}/diff?from=1&to=2")

        assert resp.status_code == 404
        assert "Eelnõu ei leitud" in resp.text
        # The diff table must NOT render for an unauthorised viewer.
        assert "diff-table" not in resp.text

    @patch("app.docs.routes._detail_versions.list_versions_for_draft")
    @patch("app.docs.routes._detail_versions._connect")
    @patch("app.docs.routes._detail_versions.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_nonexistent_version_number_returns_not_found(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_connect: MagicMock,
        mock_list_versions: MagicMock,
    ):
        """Asking for a version number that doesn't exist must 404."""
        mock_get_provider.return_value = _stub_provider()
        draft = _make_draft(status="ready")
        mock_fetch.return_value = draft
        _stub_connect(mock_connect)
        # Only v1 exists; the request asks for v=99 → not found.
        mock_list_versions.return_value = [_make_version(version_number=1)]

        client = _authed_client()
        resp = client.get(f"/drafts/{draft.id}/diff?from=1&to=99")

        assert resp.status_code == 404
        assert "Eelnõu ei leitud" in resp.text
        assert "diff-table" not in resp.text

    @patch("app.docs.routes._detail_versions.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_missing_query_params_returns_not_found(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
    ):
        mock_get_provider.return_value = _stub_provider()
        draft = _make_draft(status="ready")
        mock_fetch.return_value = draft

        client = _authed_client()
        # Neither `from` nor `to` provided.
        resp = client.get(f"/drafts/{draft.id}/diff")

        assert resp.status_code == 404
        assert "Eelnõu ei leitud" in resp.text

    @patch("app.docs.routes._detail_versions.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_non_numeric_query_params_returns_not_found(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
    ):
        mock_get_provider.return_value = _stub_provider()
        draft = _make_draft(status="ready")
        mock_fetch.return_value = draft

        client = _authed_client()
        # 'one' is not a valid int.
        resp = client.get(f"/drafts/{draft.id}/diff?from=one&to=two")

        assert resp.status_code == 404
        assert "Eelnõu ei leitud" in resp.text

    @patch("app.docs.routes._detail_versions.list_versions_for_draft")
    @patch("app.docs.routes._detail_versions._connect")
    @patch("app.docs.routes._detail_versions.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_swapped_from_to_pair_is_normalised(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_connect: MagicMock,
        mock_list_versions: MagicMock,
    ):
        """from=2&to=1 is silently flipped to from=1&to=2 so old links
        from the timeline-rendering refactor still work."""
        from app.storage.encrypted import encrypt_text

        mock_get_provider.return_value = _stub_provider()
        draft = _make_draft(status="ready")
        mock_fetch.return_value = draft
        _stub_connect(mock_connect)
        v1 = _make_version(
            version_number=1,
            reading_stage="vtk",
            parsed_text_encrypted=encrypt_text("vana"),
        )
        v2 = _make_version(
            version_number=2,
            reading_stage="reading_1",
            parsed_text_encrypted=encrypt_text("uus"),
        )
        mock_list_versions.return_value = [v2, v1]

        client = _authed_client()
        # Reversed pair — must still render the diff (not 404).
        resp = client.get(f"/drafts/{draft.id}/diff?from=2&to=1")

        assert resp.status_code == 200
        body = resp.text
        # Title is rendered with the normalised v1 → v2 direction.
        assert "Versioonide erinevus v1" in body
        assert "diff-table" in body

    @patch("app.docs.routes._detail_versions.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_same_version_compared_to_itself_returns_not_found(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
    ):
        """Diffing a version against itself is a no-op — return 404 so
        the user lands back on the detail page rather than seeing an
        all-unchanged table that wastes a page-load."""
        mock_get_provider.return_value = _stub_provider()
        draft = _make_draft(status="ready")
        mock_fetch.return_value = draft

        client = _authed_client()
        resp = client.get(f"/drafts/{draft.id}/diff?from=2&to=2")

        assert resp.status_code == 404
        assert "Eelnõu ei leitud" in resp.text

    def test_diff_route_redirects_unauthenticated(self):
        """Unauthenticated callers get bounced to /auth/login."""
        from app.main import app

        client = TestClient(app, follow_redirects=False)
        resp = client.get("/drafts/44444444-4444-4444-4444-444444444444/diff?from=1&to=2")
        assert resp.status_code == 303
        assert resp.headers["location"] == "/auth/login"
