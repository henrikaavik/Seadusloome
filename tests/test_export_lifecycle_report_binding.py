"""#845 ride-along — stale job→report binding re-checked at serving time.

The export worker verifies that the job's ``report_id`` belongs to the
job's ``draft_id`` when it *renders* the file (``export_handler.py``),
but a finished job kept serving its cached artifact afterwards: a
report deleted or re-bound after the job succeeded (re-analysis,
version rollback) could still be downloaded through the stale job row.
``export_status_fragment`` (success branch) and
``download_export_handler`` now re-assert the binding via
``_report_belongs_to_draft`` before serving.

Failure semantics, pinned deliberately:

* missing / malformed ``report_id`` on the job → fail closed (404);
* definitive "no such row" → fail closed (404);
* DB **transport** error → fail open, because the org-scoped
  ``fetch_draft`` + ``can_view_draft`` gate earlier in the handler is
  itself DB-backed and fail-closed (a genuine outage 404s before this
  secondary integrity check ever runs) — mirroring how
  ``touch_draft_access_conn`` swallows DB errors on the same routes.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch

from starlette.testclient import TestClient

from app.docs.draft_model import Draft
from app.docs.report_routes import _report_belongs_to_draft
from app.jobs.queue import Job

_ORG_ID = "11111111-1111-1111-1111-111111111111"
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


def _make_draft() -> Draft:
    now = datetime.now(UTC)
    return Draft(
        id=_DRAFT_ID,
        user_id=uuid.UUID(_USER_ID),
        org_id=uuid.UUID(_ORG_ID),
        title="Test eelnõu",
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


def _make_job(*, status: str = "success", result: dict | None = None) -> Job:
    now = datetime.now(UTC)
    return Job(
        id=7,
        job_type="export_report",
        payload={"draft_id": str(_DRAFT_ID), "report_id": str(_REPORT_ID)},
        status=status,
        priority=10,
        attempts=0,
        max_attempts=3,
        claimed_by=None,
        claimed_at=None,
        started_at=now,
        finished_at=None,
        error_message=None,
        result=result,
        scheduled_for=now,
        created_at=now,
    )


def _stub_provider() -> MagicMock:
    provider = MagicMock()
    provider.get_current_user.return_value = _authed_user()
    return provider


def _authed_client() -> TestClient:
    client = TestClient(__import__("app.main", fromlist=["app"]).app, follow_redirects=False)
    client.cookies.set("access_token", "stub-token")
    return client


def _wire_connection(mock_conn: MagicMock, row: tuple | None) -> MagicMock:
    cursor = MagicMock()
    cursor.fetchone.return_value = row
    conn = MagicMock()
    conn.execute.return_value = cursor
    mock_conn.return_value.__enter__ = MagicMock(return_value=conn)
    mock_conn.return_value.__exit__ = MagicMock(return_value=False)
    return conn


# ---------------------------------------------------------------------------
# Unit: _report_belongs_to_draft
# ---------------------------------------------------------------------------


class TestReportBelongsToDraft:
    @patch("app.docs.report_routes._connect")
    def test_row_present_returns_true(self, mock_conn):
        conn = _wire_connection(mock_conn, (1,))
        assert _report_belongs_to_draft(str(_REPORT_ID), _DRAFT_ID) is True
        sql, params = conn.execute.call_args.args
        assert "impact_reports" in sql
        assert "draft_id = %s" in sql
        assert params == (str(_REPORT_ID), str(_DRAFT_ID))

    @patch("app.docs.report_routes._connect")
    def test_no_row_fails_closed(self, mock_conn):
        _wire_connection(mock_conn, None)
        assert _report_belongs_to_draft(str(_REPORT_ID), _DRAFT_ID) is False

    @patch("app.docs.report_routes._connect")
    def test_missing_or_garbled_report_id_fails_closed_without_db(self, mock_conn):
        assert _report_belongs_to_draft(None, _DRAFT_ID) is False
        assert _report_belongs_to_draft("", _DRAFT_ID) is False
        assert _report_belongs_to_draft("not-a-uuid", _DRAFT_ID) is False
        mock_conn.assert_not_called()

    @patch("app.docs.report_routes._connect")
    def test_db_transport_error_fails_open(self, mock_conn):
        """Documented availability trade-off: the org-scoped draft gate
        earlier in the request is fail-closed and DB-backed, so this
        secondary integrity check must not take downloads offline on a
        transient blip."""
        mock_conn.side_effect = RuntimeError("connection refused")
        assert _report_belongs_to_draft(str(_REPORT_ID), _DRAFT_ID) is True


# ---------------------------------------------------------------------------
# Route: GET /drafts/{id}/export/{job_id}/download
# ---------------------------------------------------------------------------


class TestDownloadRechecksBinding:
    @patch("app.docs.report_routes._report_belongs_to_draft", return_value=False)
    @patch("app.docs.report_routes.JobQueue")
    @patch("app.docs.report_routes.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_stale_report_binding_returns_404(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_queue_cls: MagicMock,
        mock_binding: MagicMock,
        tmp_path: Any,
    ):
        """Even with a successful job AND the file still on disk, a
        report that no longer belongs to the draft must not be served."""
        mock_get_provider.return_value = _stub_provider()
        mock_fetch.return_value = _make_draft()
        docx_file = tmp_path / "report.docx"
        docx_file.write_bytes(b"PK\x03\x04 cached artifact")
        queue_instance = MagicMock()
        queue_instance.get.return_value = _make_job(
            result={"docx_path": str(docx_file)},
        )
        mock_queue_cls.return_value = queue_instance

        resp = _authed_client().get(f"/drafts/{_DRAFT_ID}/export/7/download")

        assert resp.status_code == 404
        mock_binding.assert_called_once()
        args = mock_binding.call_args.args
        assert args[0] == str(_REPORT_ID)
        assert args[1] == _DRAFT_ID

    @patch("app.docs.report_routes._report_belongs_to_draft", return_value=True)
    @patch("app.docs.report_routes.JobQueue")
    @patch("app.docs.report_routes.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_live_binding_still_serves_file(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_queue_cls: MagicMock,
        mock_binding: MagicMock,
        tmp_path: Any,
    ):
        mock_get_provider.return_value = _stub_provider()
        mock_fetch.return_value = _make_draft()
        docx_file = tmp_path / "report.docx"
        docx_file.write_bytes(b"PK\x03\x04 cached artifact")
        queue_instance = MagicMock()
        queue_instance.get.return_value = _make_job(
            result={"docx_path": str(docx_file)},
        )
        mock_queue_cls.return_value = queue_instance

        resp = _authed_client().get(f"/drafts/{_DRAFT_ID}/export/7/download")

        assert resp.status_code == 200
        assert resp.content.startswith(b"PK")
        mock_binding.assert_called_once()


# ---------------------------------------------------------------------------
# Route: GET /drafts/{id}/export-status/{job_id} (success branch)
# ---------------------------------------------------------------------------


class TestStatusFragmentRechecksBinding:
    @patch("app.docs.report_routes._report_belongs_to_draft", return_value=False)
    @patch("app.docs.report_routes.JobQueue")
    @patch("app.docs.report_routes.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_success_with_stale_binding_returns_404(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_queue_cls: MagicMock,
        mock_binding: MagicMock,
    ):
        mock_get_provider.return_value = _stub_provider()
        mock_fetch.return_value = _make_draft()
        queue_instance = MagicMock()
        queue_instance.get.return_value = _make_job(
            result={"docx_path": "/tmp/exports/output.docx"},
        )
        mock_queue_cls.return_value = queue_instance

        resp = _authed_client().get(f"/drafts/{_DRAFT_ID}/export-status/7")

        assert resp.status_code == 404
        assert "Laadi alla" not in resp.text
        mock_binding.assert_called_once()

    @patch("app.docs.report_routes._report_belongs_to_draft")
    @patch("app.docs.report_routes.JobQueue")
    @patch("app.docs.report_routes.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_polling_states_skip_the_db_recheck(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_queue_cls: MagicMock,
        mock_binding: MagicMock,
    ):
        """The binding query must not run on every 2s poll — only when a
        download link / file is about to be served. A pending job whose
        report was deleted fails in the worker anyway (its own guard)."""
        mock_get_provider.return_value = _stub_provider()
        mock_fetch.return_value = _make_draft()
        queue_instance = MagicMock()
        queue_instance.get.return_value = _make_job(status="running")
        mock_queue_cls.return_value = queue_instance

        resp = _authed_client().get(f"/drafts/{_DRAFT_ID}/export-status/7")

        assert resp.status_code == 200
        mock_binding.assert_not_called()
