"""Tests for the signed-URL download flow (issue #307).

Two layers:

1. Unit tests for ``make_download_token`` / ``validate_download_token``
   — round-trip, expiry, draft-id mismatch, HMAC tampering, malformed
   input. These run with no DB / no FastHTML test client.
2. Integration tests for ``GET /drafts/{id}/report/full.docx?token=...``
   — valid token returns the file with the right audit row, expired /
   invalid tokens return 403, the legacy session-auth path still works
   without a token.
"""

from __future__ import annotations

import base64
import json
import time
import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch

from starlette.testclient import TestClient

from app.docs.draft_model import Draft
from app.docs.signed_urls import (
    DEFAULT_TOKEN_TTL_SECONDS,
    make_download_token,
    validate_download_token,
)

_ORG_ID = "11111111-1111-1111-1111-111111111111"
_OTHER_ORG_ID = "22222222-2222-2222-2222-222222222222"
_USER_ID = "33333333-3333-3333-3333-333333333333"
_DRAFT_ID = uuid.UUID("44444444-4444-4444-4444-444444444444")
_OTHER_DRAFT_ID = uuid.UUID("66666666-6666-6666-6666-666666666666")
_REPORT_ID = uuid.UUID("55555555-5555-5555-5555-555555555555")


# ---------------------------------------------------------------------------
# Test fixtures shared with the integration tests
# ---------------------------------------------------------------------------


def _authed_user() -> dict[str, Any]:
    return {
        "id": _USER_ID,
        "email": "koostaja@seadusloome.ee",
        "full_name": "Test Koostaja",
        "role": "drafter",
        "org_id": _ORG_ID,
    }


def _make_draft(*, org_id: str = _ORG_ID, title: str = "Test eelnõu") -> Draft:
    now = datetime.now(UTC)
    return Draft(
        id=_DRAFT_ID,
        user_id=uuid.UUID(_USER_ID),
        org_id=uuid.UUID(org_id),
        title=title,
        filename="eelnou.docx",
        content_type=("application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
        file_size=2048,
        storage_path="/tmp/cipher.enc",
        graph_uri=f"https://data.riik.ee/ontology/estleg/drafts/{_DRAFT_ID}",
        status="ready",
        parsed_text_encrypted=None,
        entity_count=None,
        error_message=None,
        created_at=now,
        updated_at=now,
    )


def _make_report_row() -> tuple:
    findings = {
        "affected_entities": [],
        "conflicts": [],
        "eu_compliance": [],
        "gaps": [],
    }
    return (
        _REPORT_ID,
        _DRAFT_ID,
        0,
        0,
        0,
        0,
        findings,
        "2026-04-09T12:00+00:00@1061123",
        datetime(2026, 4, 9, 12, 0, tzinfo=UTC),
    )


def _stub_provider() -> MagicMock:
    provider = MagicMock()
    provider.get_current_user.return_value = _authed_user()
    return provider


def _authed_client() -> TestClient:
    client = TestClient(__import__("app.main", fromlist=["app"]).app, follow_redirects=False)
    client.cookies.set("access_token", "stub-token")
    return client


# ---------------------------------------------------------------------------
# Layer 1: token helper unit tests
# ---------------------------------------------------------------------------


class TestTokenHelperRoundtrip:
    def test_valid_token_roundtrip_returns_payload(self):
        token = make_download_token(str(_DRAFT_ID), _USER_ID)
        payload = validate_download_token(token, str(_DRAFT_ID))
        assert payload is not None
        assert payload["draft_id"] == str(_DRAFT_ID)
        assert payload["user_id"] == _USER_ID
        # ``exp`` is in the future and within the TTL window.
        assert payload["exp"] > int(time.time())
        assert payload["exp"] <= int(time.time()) + DEFAULT_TOKEN_TTL_SECONDS + 1
        # Nonce is a 16-byte hex string (token_hex(16) == 32 chars).
        assert isinstance(payload["nonce"], str)
        assert len(payload["nonce"]) == 32

    def test_token_format_is_two_base64url_parts(self):
        token = make_download_token(str(_DRAFT_ID), _USER_ID)
        # ``<payload>.<sig>`` — exactly two segments.
        assert token.count(".") == 1
        parts = token.split(".")
        assert len(parts) == 2
        # Both segments are base64url chars only (no '+', '/', '=').
        for part in parts:
            assert all(c.isalnum() or c in {"-", "_"} for c in part), part

    def test_two_tokens_for_same_draft_have_distinct_nonces(self):
        """A fresh nonce per mint means token churn doesn't accidentally
        collide — useful when invalidating one leaked link without
        rotating the whole secret."""
        t1 = make_download_token(str(_DRAFT_ID), _USER_ID)
        t2 = make_download_token(str(_DRAFT_ID), _USER_ID)
        assert t1 != t2


class TestTokenHelperRejectsBadInput:
    def test_expired_token_returns_none(self):
        # TTL=0 → exp is "now"; one tick later validate must reject.
        token = make_download_token(str(_DRAFT_ID), _USER_ID, ttl_seconds=0)
        time.sleep(0.05)
        assert validate_download_token(token, str(_DRAFT_ID)) is None

    def test_wrong_draft_id_returns_none(self):
        token = make_download_token(str(_DRAFT_ID), _USER_ID)
        assert validate_download_token(token, str(_OTHER_DRAFT_ID)) is None

    def test_tampered_hmac_returns_none(self):
        """Flipping one byte of the signature segment must fail the
        constant-time HMAC compare."""
        token = make_download_token(str(_DRAFT_ID), _USER_ID)
        payload_b64, sig_b64 = token.split(".")
        # Flip the first sig byte to something that's still a valid
        # base64url char but produces a different decoded value.
        tampered_sig = ("A" if sig_b64[0] != "A" else "B") + sig_b64[1:]
        tampered = f"{payload_b64}.{tampered_sig}"
        assert validate_download_token(tampered, str(_DRAFT_ID)) is None

    def test_tampered_payload_returns_none(self):
        """Editing the payload (e.g. to bump ``exp``) without re-signing
        is rejected — the HMAC won't match."""
        token = make_download_token(str(_DRAFT_ID), _USER_ID, ttl_seconds=10)
        payload_b64, sig_b64 = token.split(".")
        # Rebuild a payload with a far-future exp but keep the original sig.
        tampered_payload = {
            "draft_id": str(_DRAFT_ID),
            "user_id": _USER_ID,
            "exp": int(time.time()) + 365 * 24 * 3600,
            "nonce": "0" * 32,
        }
        tampered_bytes = json.dumps(
            tampered_payload, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
        tampered_b64 = base64.urlsafe_b64encode(tampered_bytes).rstrip(b"=").decode("ascii")
        tampered_token = f"{tampered_b64}.{sig_b64}"
        assert validate_download_token(tampered_token, str(_DRAFT_ID)) is None

    def test_empty_string_returns_none(self):
        assert validate_download_token("", str(_DRAFT_ID)) is None

    def test_missing_separator_returns_none(self):
        assert validate_download_token("nodot", str(_DRAFT_ID)) is None

    def test_three_segments_returns_none(self):
        assert validate_download_token("a.b.c", str(_DRAFT_ID)) is None

    def test_non_base64_payload_returns_none(self):
        # Star ('*') is not a base64url character so decoding throws.
        assert validate_download_token("***.***", str(_DRAFT_ID)) is None

    def test_non_string_token_returns_none(self):
        # Defensive: callers might pass None / bytes from a query parser.
        assert validate_download_token(None, str(_DRAFT_ID)) is None  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Layer 2: integration tests against the report download route
# ---------------------------------------------------------------------------


class TestReportDownloadWithToken:
    @patch("app.docs.report_routes.log_draft_download")
    @patch("app.docs.report_routes._fetch_latest_report")
    @patch("app.docs.report_routes.fetch_draft")
    def test_valid_token_returns_file_without_session_auth(
        self,
        mock_fetch: MagicMock,
        mock_fetch_report: MagicMock,
        mock_log: MagicMock,
        tmp_path: Any,
    ):
        """A request with a valid ``?token=`` and NO session cookie
        must still stream the file — the token IS the credential."""
        mock_fetch.return_value = _make_draft()
        mock_fetch_report.return_value = _make_report_row()

        docx_file = tmp_path / "report.docx"
        docx_file.write_bytes(b"PK\x03\x04 fake docx bytes")
        import app.docs.docx_export as _docx_export

        original = _docx_export.build_impact_report_docx
        _docx_export.build_impact_report_docx = lambda *a, **kw: docx_file
        try:
            from app.main import app

            # No cookie set — proves token alone is sufficient.
            client = TestClient(app, follow_redirects=False)
            token = make_download_token(str(_DRAFT_ID), _USER_ID)
            resp = client.get(
                f"/drafts/{_DRAFT_ID}/report/full.docx",
                params={"token": token},
            )
        finally:
            _docx_export.build_impact_report_docx = original

        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith(
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        )
        # Audit row written with via_token=True and the minter's id.
        assert mock_log.called, "log_draft_download must be called on success"
        call_kwargs = mock_log.call_args.kwargs
        # signature: log_draft_download(user_id, draft_id, *, via_token=, ...)
        assert mock_log.call_args.args[0] == _USER_ID
        assert str(mock_log.call_args.args[1]) == str(_DRAFT_ID)
        assert call_kwargs.get("via_token") is True

    @patch("app.docs.report_routes._fetch_latest_report")
    @patch("app.docs.report_routes.fetch_draft")
    def test_expired_token_returns_403(
        self,
        mock_fetch: MagicMock,
        mock_fetch_report: MagicMock,
    ):
        mock_fetch.return_value = _make_draft()
        mock_fetch_report.return_value = _make_report_row()

        from app.main import app

        client = TestClient(app, follow_redirects=False)
        token = make_download_token(str(_DRAFT_ID), _USER_ID, ttl_seconds=0)
        time.sleep(0.05)
        resp = client.get(
            f"/drafts/{_DRAFT_ID}/report/full.docx",
            params={"token": token},
        )

        assert resp.status_code == 403
        # Body is Estonian and does NOT leak the failure mode.
        assert "aegunud" in resp.text or "vigane" in resp.text

    @patch("app.docs.report_routes._fetch_latest_report")
    @patch("app.docs.report_routes.fetch_draft")
    def test_malformed_token_returns_403(
        self,
        mock_fetch: MagicMock,
        mock_fetch_report: MagicMock,
    ):
        mock_fetch.return_value = _make_draft()
        mock_fetch_report.return_value = _make_report_row()

        from app.main import app

        client = TestClient(app, follow_redirects=False)
        resp = client.get(
            f"/drafts/{_DRAFT_ID}/report/full.docx",
            params={"token": "not-a-real-token"},
        )

        assert resp.status_code == 403

    @patch("app.docs.report_routes._fetch_latest_report")
    @patch("app.docs.report_routes.fetch_draft")
    def test_token_for_other_draft_returns_403(
        self,
        mock_fetch: MagicMock,
        mock_fetch_report: MagicMock,
    ):
        """A token signed for draft B cannot download draft A."""
        mock_fetch.return_value = _make_draft()
        mock_fetch_report.return_value = _make_report_row()

        from app.main import app

        client = TestClient(app, follow_redirects=False)
        # Mint a token for _OTHER_DRAFT_ID and present it on the
        # _DRAFT_ID download URL.
        token = make_download_token(str(_OTHER_DRAFT_ID), _USER_ID)
        resp = client.get(
            f"/drafts/{_DRAFT_ID}/report/full.docx",
            params={"token": token},
        )

        assert resp.status_code == 403


class TestLegacySessionAuthStillWorks:
    @patch("app.docs.report_routes.log_draft_download")
    @patch("app.docs.report_routes._fetch_latest_report")
    @patch("app.docs.report_routes.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_no_token_with_session_succeeds(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_fetch_report: MagicMock,
        mock_log: MagicMock,
        tmp_path: Any,
    ):
        """The pre-#307 contract must keep working: an authenticated
        user with no ``?token=`` still downloads the file."""
        mock_get_provider.return_value = _stub_provider()
        mock_fetch.return_value = _make_draft()
        mock_fetch_report.return_value = _make_report_row()

        docx_file = tmp_path / "report.docx"
        docx_file.write_bytes(b"PK\x03\x04 fake docx bytes")
        import app.docs.docx_export as _docx_export

        original = _docx_export.build_impact_report_docx
        _docx_export.build_impact_report_docx = lambda *a, **kw: docx_file
        try:
            client = _authed_client()
            resp = client.get(f"/drafts/{_DRAFT_ID}/report/full.docx")
        finally:
            _docx_export.build_impact_report_docx = original

        assert resp.status_code == 200
        # Audit row written with via_token=False (legacy path).
        assert mock_log.called
        call_kwargs = mock_log.call_args.kwargs
        assert call_kwargs.get("via_token") is False

    @patch("app.docs.report_routes._fetch_latest_report")
    @patch("app.docs.report_routes.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_no_token_cross_org_returns_404(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_fetch_report: MagicMock,
    ):
        """The legacy 404-instead-of-403 contract for cross-org callers
        (we don't leak the existence of other orgs' drafts) is
        preserved unchanged when no token is supplied."""
        mock_get_provider.return_value = _stub_provider()
        mock_fetch.return_value = _make_draft(org_id=_OTHER_ORG_ID)
        mock_fetch_report.return_value = _make_report_row()

        client = _authed_client()
        resp = client.get(f"/drafts/{_DRAFT_ID}/report/full.docx")

        # 404 (NOT 403) — matches the pre-#307 behaviour exactly.
        assert resp.status_code == 404
        assert "Eelnõu ei leitud" in resp.text


# ---------------------------------------------------------------------------
# Report page renders the download anchor with a fresh token (#307)
# ---------------------------------------------------------------------------


class TestReportPageEmitsTokenInDownloadAnchor:
    @patch("app.docs.report_routes._fetch_latest_report")
    @patch("app.docs.report_routes.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_report_page_anchor_has_token_query_param(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_fetch_report: MagicMock,
    ):
        mock_get_provider.return_value = _stub_provider()
        mock_fetch.return_value = _make_draft()
        mock_fetch_report.return_value = _make_report_row()

        client = _authed_client()
        resp = client.get(f"/drafts/{_DRAFT_ID}/report")

        assert resp.status_code == 200
        body = resp.text
        # Two download anchors (docx + pdf), each with a ``?token=`` suffix.
        assert f"/drafts/{_DRAFT_ID}/report/full.docx?token=" in body
        assert f"/drafts/{_DRAFT_ID}/report/full.pdf?token=" in body
