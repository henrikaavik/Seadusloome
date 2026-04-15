"""Integration tests for the Phase 2 Batch 4 impact-report routes.

Same patching strategy as ``tests/test_docs_routes.py``: stub the
auth provider via ``_get_provider`` and the DB lookups via the route
module's helper imports. The job queue is patched via the
``app.docs.report_routes.JobQueue`` symbol.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch

from starlette.testclient import TestClient

from app.docs.draft_model import Draft
from app.jobs.queue import Job

_ORG_ID = "11111111-1111-1111-1111-111111111111"
_OTHER_ORG_ID = "22222222-2222-2222-2222-222222222222"
_USER_ID = "33333333-3333-3333-3333-333333333333"

_DRAFT_ID = uuid.UUID("44444444-4444-4444-4444-444444444444")
_REPORT_ID = uuid.UUID("55555555-5555-5555-5555-555555555555")


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
    org_id: str = _ORG_ID,
    title: str = "Test eelnõu",
    status: str = "ready",
) -> Draft:
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
        status=status,
        parsed_text_encrypted=None,
        entity_count=None,
        error_message=None,
        created_at=now,
        updated_at=now,
    )


def _make_report_row(
    *,
    affected: int = 2,
    conflicts: int = 1,
    gaps: int = 0,
    score: int = 42,
    findings: dict | None = None,
) -> tuple:
    findings = findings or {
        "affected_entities": [
            {
                "uri": "urn:x:1",
                "label": "Märkimisväärne säte",
                "type": "https://data.riik.ee/ontology/estleg#EnactedLaw",
            }
            for _ in range(affected)
        ],
        "conflicts": [
            {
                "draft_ref": "Eelnõu § 1",
                "conflicting_entity": "urn:x:c1",
                "conflicting_label": "Vana säte",
                "reason": "Vastuolu",
            }
            for _ in range(conflicts)
        ],
        "eu_compliance": [],
        "gaps": [
            {
                "topic_cluster": "urn:cluster:1",
                "topic_cluster_label": "Andmekaitse",
                "total_provisions": "10",
                "referenced_provisions": "2",
                "description": "Vähene kaetus",
            }
            for _ in range(gaps)
        ],
    }
    return (
        _REPORT_ID,
        _DRAFT_ID,
        affected,
        conflicts,
        gaps,
        score,
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


def _make_job(
    *,
    job_id: int = 7,
    status: str = "running",
    payload: dict | None = None,
    result: dict | None = None,
    error_message: str | None = None,
) -> Job:
    now = datetime.now(UTC)
    return Job(
        id=job_id,
        job_type="export_report",
        payload=payload or {"draft_id": str(_DRAFT_ID), "report_id": str(_REPORT_ID)},
        status=status,
        priority=10,
        attempts=0,
        max_attempts=3,
        claimed_by=None,
        claimed_at=None,
        started_at=now,
        finished_at=None,
        error_message=error_message,
        result=result,
        scheduled_for=now,
        created_at=now,
    )


# ---------------------------------------------------------------------------
# Auth required
# ---------------------------------------------------------------------------


class TestAuthRequired:
    def test_report_page_redirects_unauthenticated(self):
        from app.main import app

        client = TestClient(app, follow_redirects=False)
        resp = client.get(f"/drafts/{_DRAFT_ID}/report")
        assert resp.status_code == 303
        assert resp.headers["location"] == "/auth/login"

    def test_export_post_redirects_unauthenticated(self):
        from app.main import app

        client = TestClient(app, follow_redirects=False)
        resp = client.post(f"/drafts/{_DRAFT_ID}/export")
        assert resp.status_code == 303
        assert resp.headers["location"] == "/auth/login"


# ---------------------------------------------------------------------------
# GET /drafts/{id}/report
# ---------------------------------------------------------------------------


class TestDraftReportPage:
    @patch("app.docs.report_routes._fetch_latest_report")
    @patch("app.docs.report_routes.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_own_org_report_renders_summary(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_fetch_report: MagicMock,
    ):
        mock_get_provider.return_value = _stub_provider()
        mock_fetch.return_value = _make_draft()
        mock_fetch_report.return_value = _make_report_row(score=72)

        client = _authed_client()
        resp = client.get(f"/drafts/{_DRAFT_ID}/report")

        assert resp.status_code == 200
        assert "Test eelnõu" in resp.text
        # Summary card contents
        assert "Mõjuskoor" in resp.text
        assert "72/100" in resp.text
        assert "Mõjutatud üksused" in resp.text
        assert "Konfliktid" in resp.text
        assert "EL-i õigusaktide vastavus" in resp.text
        assert "Lüngad" in resp.text
        # Back link to draft
        assert f"/drafts/{_DRAFT_ID}" in resp.text
        # Open-in-explorer link with draft overlay param
        assert f"/explorer?draft={_DRAFT_ID}" in resp.text
        # Export action
        assert "Laadi alla .docx" in resp.text

    @patch("app.docs.report_routes._fetch_latest_report")
    @patch("app.docs.report_routes.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_no_conflicts_shows_success_alert(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_fetch_report: MagicMock,
    ):
        mock_get_provider.return_value = _stub_provider()
        mock_fetch.return_value = _make_draft()
        mock_fetch_report.return_value = _make_report_row(conflicts=0)

        client = _authed_client()
        resp = client.get(f"/drafts/{_DRAFT_ID}/report")

        assert resp.status_code == 200
        assert "Konflikte ei tuvastatud." in resp.text

    @patch("app.docs.report_routes.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_cross_org_returns_404_page(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
    ):
        mock_get_provider.return_value = _stub_provider()
        mock_fetch.return_value = _make_draft(org_id=_OTHER_ORG_ID)

        client = _authed_client()
        resp = client.get(f"/drafts/{_DRAFT_ID}/report")

        assert resp.status_code == 200
        assert "Eelnõu ei leitud" in resp.text

    @patch("app.docs.report_routes._fetch_latest_report")
    @patch("app.docs.report_routes.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_missing_report_returns_404_page(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_fetch_report: MagicMock,
    ):
        mock_get_provider.return_value = _stub_provider()
        mock_fetch.return_value = _make_draft()
        mock_fetch_report.return_value = None

        client = _authed_client()
        resp = client.get(f"/drafts/{_DRAFT_ID}/report")

        assert resp.status_code == 200
        assert "Eelnõu ei leitud" in resp.text

    @patch("app.docs.report_routes.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_invalid_uuid_returns_404_page(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
    ):
        mock_get_provider.return_value = _stub_provider()

        client = _authed_client()
        resp = client.get("/drafts/not-a-uuid/report")

        assert resp.status_code == 200
        assert "Eelnõu ei leitud" in resp.text
        # fetch_draft must NOT be called when UUID parsing fails.
        mock_fetch.assert_not_called()


# ---------------------------------------------------------------------------
# POST /drafts/{id}/export
# ---------------------------------------------------------------------------


class TestExportDraftReportHandler:
    @patch("app.docs.report_routes.JobQueue")
    @patch("app.docs.report_routes._fetch_latest_report")
    @patch("app.docs.report_routes.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_export_enqueues_job_and_returns_status(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_fetch_report: MagicMock,
        mock_queue_cls: MagicMock,
    ):
        mock_get_provider.return_value = _stub_provider()
        mock_fetch.return_value = _make_draft()
        mock_fetch_report.return_value = _make_report_row()
        queue_instance = MagicMock()
        queue_instance.enqueue.return_value = 99
        mock_queue_cls.return_value = queue_instance

        client = _authed_client()
        resp = client.post(
            f"/drafts/{_DRAFT_ID}/export",
            headers={"HX-Request": "true"},
        )

        assert resp.status_code == 200
        # Spinner copy + polling target are present.
        assert "Eksport käimas" in resp.text
        assert f"/drafts/{_DRAFT_ID}/export-status/99" in resp.text
        # Job was enqueued with the right type and payload.
        queue_instance.enqueue.assert_called_once()
        args, kwargs = queue_instance.enqueue.call_args
        assert args[0] == "export_report"
        assert args[1] == {"draft_id": str(_DRAFT_ID), "report_id": str(_REPORT_ID)}
        assert kwargs.get("priority") == 10

    @patch("app.docs.report_routes.JobQueue")
    @patch("app.docs.report_routes._find_active_export_job")
    @patch("app.docs.report_routes._fetch_latest_report")
    @patch("app.docs.report_routes.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_export_dedupes_active_job(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_fetch_report: MagicMock,
        mock_find_active: MagicMock,
        mock_queue_cls: MagicMock,
    ):
        """#627: when an active export job already exists, reuse its id."""
        mock_get_provider.return_value = _stub_provider()
        mock_fetch.return_value = _make_draft()
        mock_fetch_report.return_value = _make_report_row()
        mock_find_active.return_value = 42
        queue_instance = MagicMock()
        mock_queue_cls.return_value = queue_instance

        client = _authed_client()
        resp = client.post(
            f"/drafts/{_DRAFT_ID}/export",
            headers={"HX-Request": "true"},
        )

        assert resp.status_code == 200
        # No fresh enqueue — the existing job is reused.
        queue_instance.enqueue.assert_not_called()
        assert f"/drafts/{_DRAFT_ID}/export-status/42" in resp.text

    @patch("app.docs.report_routes.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_export_cross_org_returns_404(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
    ):
        mock_get_provider.return_value = _stub_provider()
        mock_fetch.return_value = _make_draft(org_id=_OTHER_ORG_ID)

        client = _authed_client()
        resp = client.post(f"/drafts/{_DRAFT_ID}/export")
        assert resp.status_code == 200
        assert "Eelnõu ei leitud" in resp.text


# ---------------------------------------------------------------------------
# GET /drafts/{id}/export-status/{job_id}
# ---------------------------------------------------------------------------


class TestExportStatusFragment:
    @patch("app.docs.report_routes.JobQueue")
    @patch("app.docs.report_routes.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_status_running_keeps_polling(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_queue_cls: MagicMock,
    ):
        mock_get_provider.return_value = _stub_provider()
        mock_fetch.return_value = _make_draft()
        queue_instance = MagicMock()
        queue_instance.get.return_value = _make_job(status="running")
        mock_queue_cls.return_value = queue_instance

        client = _authed_client()
        resp = client.get(
            f"/drafts/{_DRAFT_ID}/export-status/7",
            headers={"HX-Request": "true"},
        )

        assert resp.status_code == 200
        assert "Eksport käimas" in resp.text
        assert f"/drafts/{_DRAFT_ID}/export-status/7" in resp.text
        assert "every 2s" in resp.text

    @patch("app.docs.report_routes.JobQueue")
    @patch("app.docs.report_routes.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_status_pending_keeps_polling(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_queue_cls: MagicMock,
    ):
        mock_get_provider.return_value = _stub_provider()
        mock_fetch.return_value = _make_draft()
        queue_instance = MagicMock()
        queue_instance.get.return_value = _make_job(status="pending")
        mock_queue_cls.return_value = queue_instance

        client = _authed_client()
        resp = client.get(f"/drafts/{_DRAFT_ID}/export-status/7")

        assert resp.status_code == 200
        assert "Eksport käimas" in resp.text

    @patch("app.docs.report_routes.JobQueue")
    @patch("app.docs.report_routes.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_status_success_returns_download_link(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_queue_cls: MagicMock,
    ):
        mock_get_provider.return_value = _stub_provider()
        mock_fetch.return_value = _make_draft()
        queue_instance = MagicMock()
        queue_instance.get.return_value = _make_job(
            status="success",
            result={
                "draft_id": str(_DRAFT_ID),
                "report_id": str(_REPORT_ID),
                "docx_path": "/tmp/exports/output.docx",
            },
        )
        mock_queue_cls.return_value = queue_instance

        client = _authed_client()
        resp = client.get(f"/drafts/{_DRAFT_ID}/export-status/7")

        assert resp.status_code == 200
        assert "Laadi alla .docx" in resp.text
        assert f"/drafts/{_DRAFT_ID}/export/7/download" in resp.text
        # No polling attribute in success state.
        assert "every 2s" not in resp.text

    @patch("app.docs.report_routes.JobQueue")
    @patch("app.docs.report_routes.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_status_failed_shows_error_alert(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_queue_cls: MagicMock,
    ):
        mock_get_provider.return_value = _stub_provider()
        mock_fetch.return_value = _make_draft()
        queue_instance = MagicMock()
        queue_instance.get.return_value = _make_job(
            status="failed",
            error_message="Disk full",
        )
        mock_queue_cls.return_value = queue_instance

        client = _authed_client()
        resp = client.get(f"/drafts/{_DRAFT_ID}/export-status/7")

        assert resp.status_code == 200
        assert "Disk full" in resp.text
        assert "Eksport ebaõnnestus" in resp.text


# ---------------------------------------------------------------------------
# GET /drafts/{id}/export/{job_id}/download
# ---------------------------------------------------------------------------


class TestDownloadExportHandler:
    @patch("app.docs.report_routes.JobQueue")
    @patch("app.docs.report_routes.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_download_returns_file_response(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_queue_cls: MagicMock,
        tmp_path: Any,
    ):
        mock_get_provider.return_value = _stub_provider()
        mock_fetch.return_value = _make_draft(title="Tööõiguse muudatus")
        # Create a real file so FileResponse can stat it.
        docx_file = tmp_path / "report.docx"
        docx_file.write_bytes(b"PK\x03\x04 fake docx content")
        queue_instance = MagicMock()
        queue_instance.get.return_value = _make_job(
            status="success",
            result={
                "draft_id": str(_DRAFT_ID),
                "report_id": str(_REPORT_ID),
                "docx_path": str(docx_file),
            },
        )
        mock_queue_cls.return_value = queue_instance

        client = _authed_client()
        resp = client.get(f"/drafts/{_DRAFT_ID}/export/7/download")

        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith(
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        )
        # Slugified filename in Content-Disposition.
        cd = resp.headers.get("content-disposition", "")
        assert "impact_report_" in cd
        # Estonian diacritics are stripped via NFKD + ASCII fold; the
        # title contained ö, ö and õ which all become bare 'o'.
        assert ".docx" in cd
        assert "muudatus" in cd
        # No diacritics survived.
        for ch in ("ö", "õ", "ä", "ü"):
            assert ch not in cd

    @patch("app.docs.report_routes.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_download_cross_org_returns_404(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
    ):
        mock_get_provider.return_value = _stub_provider()
        mock_fetch.return_value = _make_draft(org_id=_OTHER_ORG_ID)

        client = _authed_client()
        resp = client.get(f"/drafts/{_DRAFT_ID}/export/7/download")
        assert resp.status_code == 200
        assert "Eelnõu ei leitud" in resp.text

    @patch("app.docs.report_routes.JobQueue")
    @patch("app.docs.report_routes.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_download_missing_file_returns_404(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_queue_cls: MagicMock,
    ):
        mock_get_provider.return_value = _stub_provider()
        mock_fetch.return_value = _make_draft()
        queue_instance = MagicMock()
        queue_instance.get.return_value = _make_job(
            status="success",
            result={
                "draft_id": str(_DRAFT_ID),
                "report_id": str(_REPORT_ID),
                "docx_path": "/tmp/does-not-exist-12345.docx",
            },
        )
        mock_queue_cls.return_value = queue_instance

        client = _authed_client()
        resp = client.get(f"/drafts/{_DRAFT_ID}/export/7/download")
        assert resp.status_code == 200
        assert "Eelnõu ei leitud" in resp.text

    @patch("app.docs.report_routes.JobQueue")
    @patch("app.docs.report_routes.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_download_pending_job_returns_404(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_queue_cls: MagicMock,
    ):
        """A still-running job must not yield a 200 download stream."""
        mock_get_provider.return_value = _stub_provider()
        mock_fetch.return_value = _make_draft()
        queue_instance = MagicMock()
        queue_instance.get.return_value = _make_job(status="running")
        mock_queue_cls.return_value = queue_instance

        client = _authed_client()
        resp = client.get(f"/drafts/{_DRAFT_ID}/export/7/download")
        assert resp.status_code == 200
        assert "Eelnõu ei leitud" in resp.text


# ---------------------------------------------------------------------------
# GET /drafts/{id}/report/section/{section} — pagination (#611)
# ---------------------------------------------------------------------------


class TestReportSectionPagination:
    @patch("app.docs.report_routes._fetch_latest_report")
    @patch("app.docs.report_routes.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_report_page_shows_pager_when_more_than_50_rows(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_fetch_report: MagicMock,
    ):
        mock_get_provider.return_value = _stub_provider()
        mock_fetch.return_value = _make_draft()
        many = [
            {
                "uri": f"urn:x:{i}",
                "label": f"Säte {i}",
                "type": "https://data.riik.ee/ontology/estleg#EnactedLaw",
            }
            for i in range(75)
        ]
        mock_fetch_report.return_value = _make_report_row(
            affected=75,
            findings={
                "affected_entities": many,
                "conflicts": [],
                "eu_compliance": [],
                "gaps": [],
            },
        )

        client = _authed_client()
        resp = client.get(f"/drafts/{_DRAFT_ID}/report")
        assert resp.status_code == 200
        # Pager footer + "Näita rohkem" button are both present.
        assert "Kuvatud 50 / 75" in resp.text
        assert "Näita rohkem" in resp.text
        # And the HTMX target for the next batch is wired up.
        assert "/report/section/affected?offset=50" in resp.text

    @patch("app.docs.report_routes._fetch_latest_report")
    @patch("app.docs.report_routes.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_section_fragment_returns_next_batch(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_fetch_report: MagicMock,
    ):
        mock_get_provider.return_value = _stub_provider()
        mock_fetch.return_value = _make_draft()
        many = [
            {
                "uri": f"urn:x:{i}",
                "label": f"Säte {i}",
                "type": "https://data.riik.ee/ontology/estleg#EnactedLaw",
            }
            for i in range(120)
        ]
        mock_fetch_report.return_value = _make_report_row(
            affected=120,
            findings={
                "affected_entities": many,
                "conflicts": [],
                "eu_compliance": [],
                "gaps": [],
            },
        )

        client = _authed_client()
        resp = client.get(f"/drafts/{_DRAFT_ID}/report/section/affected?offset=50&limit=50")
        assert resp.status_code == 200
        # The 51st row (index 50) is in the fragment.
        assert "Säte 50" in resp.text
        # The 101st row (index 100) is NOT yet — the next click loads it.
        assert "Säte 100" not in resp.text
        # Follow-up pager is wired for the remaining rows (20 left).
        assert "Kuvatud 100 / 120" in resp.text
        assert "offset=100" in resp.text

    @patch("app.docs.report_routes._fetch_latest_report")
    @patch("app.docs.report_routes.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_section_fragment_unknown_section_returns_404(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_fetch_report: MagicMock,
    ):
        mock_get_provider.return_value = _stub_provider()
        mock_fetch.return_value = _make_draft()
        mock_fetch_report.return_value = _make_report_row()

        client = _authed_client()
        resp = client.get(f"/drafts/{_DRAFT_ID}/report/section/bogus")
        assert resp.status_code == 200
        assert "Eelnõu ei leitud" in resp.text


# ---------------------------------------------------------------------------
# #612: ontology-version drift banner + /report/reanalyze
# ---------------------------------------------------------------------------


class TestOntologyDriftBanner:
    @patch("app.docs.report_routes._current_ontology_version")
    @patch("app.docs.report_routes._fetch_latest_report")
    @patch("app.docs.report_routes.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_drift_banner_renders_when_versions_differ(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_fetch_report: MagicMock,
        mock_current: MagicMock,
    ):
        mock_get_provider.return_value = _stub_provider()
        mock_fetch.return_value = _make_draft()
        mock_fetch_report.return_value = _make_report_row()  # version tag v2.1-ish
        mock_current.return_value = "2026-04-15T00:00+00:00@1061500"

        client = _authed_client()
        resp = client.get(f"/drafts/{_DRAFT_ID}/report")
        assert resp.status_code == 200
        assert "Ontoloogia on uuenenud" in resp.text
        assert "Analüüsi uuesti" in resp.text

    @patch("app.docs.report_routes._current_ontology_version")
    @patch("app.docs.report_routes._fetch_latest_report")
    @patch("app.docs.report_routes.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_drift_banner_absent_when_versions_match(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_fetch_report: MagicMock,
        mock_current: MagicMock,
    ):
        mock_get_provider.return_value = _stub_provider()
        mock_fetch.return_value = _make_draft()
        report_row = _make_report_row()
        mock_fetch_report.return_value = report_row
        mock_current.return_value = str(report_row[7])

        client = _authed_client()
        resp = client.get(f"/drafts/{_DRAFT_ID}/report")
        assert resp.status_code == 200
        assert "Ontoloogia on uuenenud" not in resp.text

    @patch("app.docs.report_routes.JobQueue")
    @patch("app.docs.report_routes.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_reanalyze_enqueues_analyze_job(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_queue_cls: MagicMock,
    ):
        mock_get_provider.return_value = _stub_provider()
        mock_fetch.return_value = _make_draft()
        queue_instance = MagicMock()
        queue_instance.enqueue.return_value = 321
        mock_queue_cls.return_value = queue_instance

        client = _authed_client()
        resp = client.post(
            f"/drafts/{_DRAFT_ID}/report/reanalyze",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        queue_instance.enqueue.assert_called_once()
        args, _ = queue_instance.enqueue.call_args
        assert args[0] == "analyze_impact"
        assert args[1] == {"draft_id": str(_DRAFT_ID)}
        # HTMX fragment replaces the banner with a success alert.
        assert "Analüüs alustati" in resp.text

    @patch("app.docs.report_routes.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_reanalyze_cross_org_returns_404(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
    ):
        mock_get_provider.return_value = _stub_provider()
        mock_fetch.return_value = _make_draft(org_id=_OTHER_ORG_ID)
        client = _authed_client()
        resp = client.post(f"/drafts/{_DRAFT_ID}/report/reanalyze")
        assert resp.status_code == 200
        assert "Eelnõu ei leitud" in resp.text
