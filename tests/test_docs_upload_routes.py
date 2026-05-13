"""Route tests for the configurable upload-size UI (#776).

The upload form embeds the byte cap as a JS constant and renders the
human-readable MB label in three places (the page InfoBox, the file
input's helper text, and the drafts-list InfoBox). All four must derive
from the *same* ``MAX_UPLOAD_SIZE_MB`` env var so a Coolify override
propagates without a redeploy.

These tests monkeypatch ``MAX_UPLOAD_SIZE_MB`` on the way into the
TestClient and then assert that:

* GET /drafts/new bakes the right byte count into ``MAX = <bytes>;``
* GET /drafts/new renders the matching ``"<N> MB"`` label in the help copy
* GET /drafts renders the matching ``"<N> MB"`` label in the workspace banner

The default-50 byte constant remains pinned by
:class:`tests.test_docs_routes.TestUploadPrecheck` so we don't duplicate
that here.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from starlette.testclient import TestClient

# ---------------------------------------------------------------------------
# Helpers (mirrors tests/test_docs_routes.py to keep both suites independent)
# ---------------------------------------------------------------------------


_ORG_ID = "11111111-1111-1111-1111-111111111111"
_USER_ID = "33333333-3333-3333-3333-333333333333"


def _authed_user() -> dict[str, Any]:
    return {
        "id": _USER_ID,
        "email": "koostaja@seadusloome.ee",
        "full_name": "Test Koostaja",
        "role": "drafter",
        "org_id": _ORG_ID,
    }


def _stub_provider() -> MagicMock:
    provider = MagicMock()
    provider.get_current_user.return_value = _authed_user()
    return provider


def _authed_client() -> TestClient:
    client = TestClient(__import__("app.main", fromlist=["app"]).app, follow_redirects=False)
    client.cookies.set("access_token", "stub-token")
    return client


# ---------------------------------------------------------------------------
# Helper-level unit tests
# ---------------------------------------------------------------------------


class TestMaxUploadHelpers:
    """``max_upload_bytes`` / ``max_upload_mb_display`` round-trip the env var."""

    def test_helpers_round_trip_default(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("MAX_UPLOAD_SIZE_MB", raising=False)
        from app.docs.upload import max_upload_bytes, max_upload_mb_display

        assert max_upload_bytes() == 50 * 1024 * 1024
        assert max_upload_mb_display() == "50 MB"

    def test_helpers_round_trip_override(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("MAX_UPLOAD_SIZE_MB", "10")
        from app.docs.upload import max_upload_bytes, max_upload_mb_display

        assert max_upload_bytes() == 10 * 1024 * 1024
        assert max_upload_mb_display() == "10 MB"

    def test_helpers_round_trip_large_override(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("MAX_UPLOAD_SIZE_MB", "200")
        from app.docs.upload import max_upload_bytes, max_upload_mb_display

        assert max_upload_bytes() == 200 * 1024 * 1024
        assert max_upload_mb_display() == "200 MB"

    def test_invalid_value_falls_back_to_default(self, monkeypatch: pytest.MonkeyPatch):
        """A malformed env var must not crash — fall back to 50 MB."""
        monkeypatch.setenv("MAX_UPLOAD_SIZE_MB", "not-a-number")
        from app.docs.upload import max_upload_bytes, max_upload_mb_display

        assert max_upload_bytes() == 50 * 1024 * 1024
        assert max_upload_mb_display() == "50 MB"


# ---------------------------------------------------------------------------
# GET /drafts/new — server-rendered JS + copy follow MAX_UPLOAD_SIZE_MB
# ---------------------------------------------------------------------------


class TestUploadFormSizeFromConfig:
    """The byte count and MB label in the upload form must track the env var."""

    @patch("app.auth.middleware._get_provider")
    def test_form_uses_configured_10mb_limit(
        self,
        mock_get_provider: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """With MAX_UPLOAD_SIZE_MB=10 the JS constant + copy must match."""
        monkeypatch.setenv("MAX_UPLOAD_SIZE_MB", "10")
        mock_get_provider.return_value = _stub_provider()

        client = _authed_client()
        resp = client.get("/drafts/new")

        assert resp.status_code == 200
        body = resp.text
        # 10 MB = 10485760 bytes — embedded in the picker JS.
        assert "10485760" in body
        # The default 50 MB constant must NOT leak through.
        assert "52428800" not in body
        # Human copy in the InfoBox + Small must match the configured value.
        assert "10 MB" in body
        # Spot-check both surfaces: the InfoBox uses "kuni 10 MB" and the
        # input help text uses "Maksimaalne suurus 10 MB".
        assert "kuni 10 MB" in body
        assert "Maksimaalne suurus 10 MB" in body

    @patch("app.auth.middleware._get_provider")
    def test_form_uses_configured_200mb_limit(
        self,
        mock_get_provider: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """With MAX_UPLOAD_SIZE_MB=200 the JS constant + copy must match."""
        monkeypatch.setenv("MAX_UPLOAD_SIZE_MB", "200")
        mock_get_provider.return_value = _stub_provider()

        client = _authed_client()
        resp = client.get("/drafts/new")

        assert resp.status_code == 200
        body = resp.text
        # 200 MB = 209715200 bytes.
        assert "209715200" in body
        # Old default must not appear.
        assert "52428800" not in body
        assert "200 MB" in body
        assert "kuni 200 MB" in body
        assert "Maksimaalne suurus 200 MB" in body

    @patch("app.auth.middleware._get_provider")
    def test_form_default_50mb_when_env_unset(
        self,
        mock_get_provider: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """No env var → 50 MB is the documented default."""
        monkeypatch.delenv("MAX_UPLOAD_SIZE_MB", raising=False)
        mock_get_provider.return_value = _stub_provider()

        client = _authed_client()
        resp = client.get("/drafts/new")

        assert resp.status_code == 200
        body = resp.text
        # 50 MB = 52428800 bytes.
        assert "52428800" in body
        assert "50 MB" in body
        assert "kuni 50 MB" in body


# ---------------------------------------------------------------------------
# GET /drafts — workspace InfoBox follows MAX_UPLOAD_SIZE_MB
# ---------------------------------------------------------------------------


class TestDraftsListSizeFromConfig:
    """The /drafts InfoBox advertises the same limit as the upload form."""

    @patch("app.docs.routes._list.list_users")
    @patch("app.docs.routes._list.list_drafts_for_org_filtered")
    @patch("app.auth.middleware._get_provider")
    def test_drafts_list_uses_configured_10mb_limit(
        self,
        mock_get_provider: MagicMock,
        mock_list_filtered: MagicMock,
        mock_list_users: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.setenv("MAX_UPLOAD_SIZE_MB", "10")
        mock_get_provider.return_value = _stub_provider()
        mock_list_filtered.return_value = ([], 0)
        mock_list_users.return_value = []

        client = _authed_client()
        resp = client.get("/drafts")

        assert resp.status_code == 200
        # The empty-state workspace banner must advertise the configured limit.
        assert "Maksimaalne failisuurus on 10 MB" in resp.text
        # And must not hard-code 50 MB anywhere.
        assert "50 MB" not in resp.text

    @patch("app.docs.routes._list.list_users")
    @patch("app.docs.routes._list.list_drafts_for_org_filtered")
    @patch("app.auth.middleware._get_provider")
    def test_drafts_list_uses_configured_200mb_limit(
        self,
        mock_get_provider: MagicMock,
        mock_list_filtered: MagicMock,
        mock_list_users: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.setenv("MAX_UPLOAD_SIZE_MB", "200")
        mock_get_provider.return_value = _stub_provider()
        mock_list_filtered.return_value = ([], 0)
        mock_list_users.return_value = []

        client = _authed_client()
        resp = client.get("/drafts")

        assert resp.status_code == 200
        assert "Maksimaalne failisuurus on 200 MB" in resp.text
