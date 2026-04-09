"""Unit tests for ``app.docs.export_handler.export_report``.

Mocks every external dependency (Postgres via ``get_connection``,
``build_impact_report_docx``) so the test exercises only the
orchestration: payload validation → DB lookup → docx build → result
dict.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from app.docs.draft_model import Draft
from app.docs.export_handler import export_report

_DRAFT_ID = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_REPORT_ID = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")


def _make_draft() -> Draft:
    now = datetime.now(UTC)
    return Draft(
        id=_DRAFT_ID,
        user_id=uuid.UUID("11111111-1111-1111-1111-111111111111"),
        org_id=uuid.UUID("22222222-2222-2222-2222-222222222222"),
        title="Test eelnõu",
        filename="eelnou.docx",
        content_type=("application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
        file_size=2048,
        storage_path="/tmp/cipher.enc",
        graph_uri=f"https://data.riik.ee/ontology/estleg/drafts/{_DRAFT_ID}",
        status="ready",
        parsed_text="§ 1. Test.",
        entity_count=2,
        error_message=None,
        created_at=now,
        updated_at=now,
    )


def _make_report_row() -> tuple:
    return (
        _REPORT_ID,
        _DRAFT_ID,
        2,
        1,
        0,
        42,
        {"affected_entities": [], "conflicts": [], "eu_compliance": [], "gaps": []},
        "2026-04-09T12:00:00+00:00@1061123",
        datetime(2026, 4, 9, 12, 0, tzinfo=UTC),
    )


class _ConnectCM:
    """Context-manager wrapper for the ``get_connection`` mock."""

    def __init__(self, conn: MagicMock):
        self.conn = conn

    def __enter__(self) -> MagicMock:
        return self.conn

    def __exit__(self, *_: Any) -> bool:
        return False


def _make_conn(report_row: tuple | None) -> MagicMock:
    """Build a connection mock that returns *report_row* on the SELECT."""
    conn = MagicMock()
    conn.execute.return_value.fetchone.return_value = report_row
    return conn


class TestExportReportHappyPath:
    def test_writes_docx_and_returns_path(self):
        draft = _make_draft()
        report_row = _make_report_row()
        conn = _make_conn(report_row)

        fake_path = Path("/tmp/exports/aaa-bbb.docx")

        with (
            patch("app.docs.export_handler.get_connection") as mock_conn,
            patch("app.docs.export_handler.get_draft", return_value=draft),
            patch(
                "app.docs.export_handler.build_impact_report_docx",
                return_value=fake_path,
            ) as mock_build,
        ):
            mock_conn.return_value = _ConnectCM(conn)

            result = export_report({"draft_id": str(_DRAFT_ID), "report_id": str(_REPORT_ID)})

        # build_impact_report_docx received the draft + the raw row tuple.
        mock_build.assert_called_once()
        call = mock_build.call_args
        assert call.args[0] is draft
        assert call.args[1] == report_row

        assert result == {
            "draft_id": str(_DRAFT_ID),
            "report_id": str(_REPORT_ID),
            "docx_path": str(fake_path),
        }


class TestExportReportFailurePaths:
    def test_missing_draft_id_raises(self):
        with pytest.raises(ValueError, match="missing required 'draft_id'"):
            export_report({"report_id": str(_REPORT_ID)})

    def test_missing_report_id_raises(self):
        with pytest.raises(ValueError, match="missing required 'report_id'"):
            export_report({"draft_id": str(_DRAFT_ID)})

    def test_missing_draft_row_raises(self):
        conn = _make_conn(_make_report_row())
        with (
            patch("app.docs.export_handler.get_connection") as mock_conn,
            patch("app.docs.export_handler.get_draft", return_value=None),
            patch("app.docs.export_handler.build_impact_report_docx") as mock_build,
        ):
            mock_conn.return_value = _ConnectCM(conn)
            with pytest.raises(ValueError, match="not found"):
                export_report({"draft_id": str(_DRAFT_ID), "report_id": str(_REPORT_ID)})
        mock_build.assert_not_called()

    def test_missing_report_row_raises(self):
        conn = _make_conn(None)
        with (
            patch("app.docs.export_handler.get_connection") as mock_conn,
            patch("app.docs.export_handler.get_draft", return_value=_make_draft()),
            patch("app.docs.export_handler.build_impact_report_docx") as mock_build,
        ):
            mock_conn.return_value = _ConnectCM(conn)
            with pytest.raises(ValueError, match="not found"):
                export_report({"draft_id": str(_DRAFT_ID), "report_id": str(_REPORT_ID)})
        mock_build.assert_not_called()

    def test_build_failure_propagates(self):
        draft = _make_draft()
        conn = _make_conn(_make_report_row())

        with (
            patch("app.docs.export_handler.get_connection") as mock_conn,
            patch("app.docs.export_handler.get_draft", return_value=draft),
            patch(
                "app.docs.export_handler.build_impact_report_docx",
                side_effect=RuntimeError("disk full"),
            ),
        ):
            mock_conn.return_value = _ConnectCM(conn)
            with pytest.raises(RuntimeError, match="disk full"):
                export_report({"draft_id": str(_DRAFT_ID), "report_id": str(_REPORT_ID)})

    def test_invalid_uuid_payload_raises(self):
        with pytest.raises(ValueError, match="invalid"):
            export_report({"draft_id": "not-a-uuid", "report_id": str(_REPORT_ID)})
