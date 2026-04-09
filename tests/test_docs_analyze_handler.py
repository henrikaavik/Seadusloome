"""Unit tests for ``app.docs.analyze_handler.analyze_impact``.

Every external dependency (Postgres, Jena via the ``put_named_graph``
helper, the :class:`ImpactAnalyzer`) is mocked out so the test never
talks to a real service. The tests cover the state transitions, the
impact_reports row insert, the ontology-version lookup, and the
cleanup behaviour when a pass fails.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from app.docs.analyze_handler import analyze_impact
from app.docs.draft_model import Draft
from app.docs.impact.analyzer import ImpactFindings

_DRAFT_ID = uuid.UUID("77777777-7777-7777-7777-777777777777")
_GRAPH_URI = f"https://data.riik.ee/ontology/estleg/drafts/{_DRAFT_ID}"


def _make_draft(status: str = "analyzing") -> Draft:
    now = datetime.now(UTC)
    return Draft(
        id=_DRAFT_ID,
        user_id=uuid.UUID("55555555-5555-5555-5555-555555555555"),
        org_id=uuid.UUID("66666666-6666-6666-6666-666666666666"),
        title="Test eelnõu",
        filename="eelnou.docx",
        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        file_size=2048,
        storage_path="/tmp/cipher.enc",
        graph_uri=_GRAPH_URI,
        status=status,
        parsed_text="§ 1. Test.",
        entity_count=2,
        error_message=None,
        created_at=now,
        updated_at=now,
    )


def _findings(
    *,
    affected: int = 2,
    conflicts: int = 0,
    gaps: int = 0,
) -> ImpactFindings:
    return ImpactFindings(
        affected_entities=[
            {"uri": "urn:x", "label": "X", "type": "urn:t"} for _ in range(affected)
        ],
        conflicts=[
            {"draft_ref": "urn:d", "conflicting_entity": "urn:c", "reason": "r"}
            for _ in range(conflicts)
        ],
        gaps=[{"topic_cluster": "urn:tc", "description": "desc"} for _ in range(gaps)],
        eu_compliance=[],
        affected_count=affected,
        conflict_count=conflicts,
        gap_count=gaps,
    )


class _ConnectCM:
    """Context-manager wrapper around a cursor-ish mock."""

    def __init__(self, conn: MagicMock):
        self.conn = conn

    def __enter__(self) -> MagicMock:
        return self.conn

    def __exit__(self, *_: Any) -> bool:
        return False


def _make_load_conn(
    *,
    draft: Draft | None = None,
    entity_rows: list[tuple] | None = None,
) -> MagicMock:
    """Build a mock for the initial ``get_connection`` block.

    ``get_draft`` is patched separately, so this connection only needs
    to answer the ``select ... from draft_entities`` query.
    """
    conn = MagicMock()
    fetchall_result = entity_rows or []
    conn.execute.return_value.fetchall.return_value = fetchall_result
    return conn


def _make_insert_conn() -> MagicMock:
    """Build a mock for the insert/update connection."""
    conn = MagicMock()
    conn.execute.return_value.rowcount = 1
    return conn


def _make_sync_conn(row: tuple | None) -> MagicMock:
    conn = MagicMock()
    conn.execute.return_value.fetchone.return_value = row
    return conn


class TestAnalyzeImpactHappyPath:
    def test_writes_impact_report_and_flips_status(self):
        draft = _make_draft()
        entity_rows = [
            ("KarS § 133", "urn:kars-133", 0.9, "provision", json.dumps({})),
            ("TsÜS § 12", "urn:tsus-12", 0.85, "provision", json.dumps({})),
        ]

        load_conn = _make_load_conn(entity_rows=entity_rows)
        insert_conn = _make_insert_conn()
        sync_conn = _make_sync_conn((datetime(2026, 4, 9, 12, 0, tzinfo=UTC), 1061123))

        mock_analyzer = MagicMock()
        mock_analyzer.analyze.return_value = _findings(affected=2, conflicts=1, gaps=1)

        with (
            patch("app.docs.analyze_handler.get_connection") as mock_get_conn,
            patch("app.docs.analyze_handler.get_draft", return_value=draft),
            patch(
                "app.docs.analyze_handler.build_draft_graph",
                return_value="# turtle",
            ) as mock_build,
            patch(
                "app.docs.analyze_handler.put_named_graph",
                return_value=True,
            ) as mock_put,
            patch(
                "app.docs.analyze_handler.ImpactAnalyzer",
                return_value=mock_analyzer,
            ),
            patch(
                "app.docs.analyze_handler.calculate_impact_score",
                return_value=42,
            ),
        ):
            mock_get_conn.side_effect = [
                _ConnectCM(load_conn),
                _ConnectCM(sync_conn),
                _ConnectCM(insert_conn),
            ]

            result = analyze_impact({"draft_id": str(_DRAFT_ID)})

        # Build + put_named_graph were called with the draft's graph URI.
        mock_build.assert_called_once()
        assert mock_build.call_args.args[0] is draft
        mock_put.assert_called_once_with(_GRAPH_URI, "# turtle")

        # The insert_conn must have received one INSERT into impact_reports
        # and one UPDATE drafts.
        calls = insert_conn.execute.call_args_list
        sql_texts = [c.args[0].lower() for c in calls]
        assert any("insert into impact_reports" in s for s in sql_texts)
        assert any("update drafts" in s and "ready" in s for s in sql_texts)
        insert_conn.commit.assert_called_once()

        # Return payload contains the critical summary fields.
        assert result["draft_id"] == str(_DRAFT_ID)
        assert result["impact_score"] == 42
        assert result["affected_count"] == 2
        assert result["conflict_count"] == 1
        assert result["gap_count"] == 1
        assert "report_id" in result

    def test_ontology_version_includes_sync_log_metadata(self):
        draft = _make_draft()
        load_conn = _make_load_conn(entity_rows=[])
        insert_conn = _make_insert_conn()
        sync_conn = _make_sync_conn((datetime(2026, 4, 1, 8, 30, tzinfo=UTC), 42))

        mock_analyzer = MagicMock()
        mock_analyzer.analyze.return_value = _findings(affected=0)

        with (
            patch("app.docs.analyze_handler.get_connection") as mock_get_conn,
            patch("app.docs.analyze_handler.get_draft", return_value=draft),
            patch(
                "app.docs.analyze_handler.build_draft_graph",
                return_value="# ttl",
            ),
            patch(
                "app.docs.analyze_handler.put_named_graph",
                return_value=True,
            ),
            patch(
                "app.docs.analyze_handler.ImpactAnalyzer",
                return_value=mock_analyzer,
            ),
            patch(
                "app.docs.analyze_handler.calculate_impact_score",
                return_value=0,
            ),
        ):
            mock_get_conn.side_effect = [
                _ConnectCM(load_conn),
                _ConnectCM(sync_conn),
                _ConnectCM(insert_conn),
            ]
            analyze_impact({"draft_id": str(_DRAFT_ID)})

        # The impact_reports INSERT must have received the composite version.
        insert_calls = [
            c
            for c in insert_conn.execute.call_args_list
            if "insert into impact_reports" in c.args[0].lower()
        ]
        assert len(insert_calls) == 1
        params = insert_calls[0].args[1]
        ontology_version = params[-1]
        assert "2026-04-01" in ontology_version
        assert ontology_version.endswith("@42")

    def test_ontology_version_unknown_when_sync_log_empty(self):
        draft = _make_draft()
        load_conn = _make_load_conn(entity_rows=[])
        insert_conn = _make_insert_conn()
        sync_conn = _make_sync_conn(None)  # empty sync_log

        mock_analyzer = MagicMock()
        mock_analyzer.analyze.return_value = _findings(affected=0)

        with (
            patch("app.docs.analyze_handler.get_connection") as mock_get_conn,
            patch("app.docs.analyze_handler.get_draft", return_value=draft),
            patch(
                "app.docs.analyze_handler.build_draft_graph",
                return_value="# ttl",
            ),
            patch(
                "app.docs.analyze_handler.put_named_graph",
                return_value=True,
            ),
            patch(
                "app.docs.analyze_handler.ImpactAnalyzer",
                return_value=mock_analyzer,
            ),
            patch(
                "app.docs.analyze_handler.calculate_impact_score",
                return_value=0,
            ),
        ):
            mock_get_conn.side_effect = [
                _ConnectCM(load_conn),
                _ConnectCM(sync_conn),
                _ConnectCM(insert_conn),
            ]
            analyze_impact({"draft_id": str(_DRAFT_ID)})

        insert_calls = [
            c
            for c in insert_conn.execute.call_args_list
            if "insert into impact_reports" in c.args[0].lower()
        ]
        params = insert_calls[0].args[1]
        assert params[-1] == "unknown"


class TestAnalyzeImpactFailurePaths:
    def test_missing_draft_raises_value_error(self):
        load_conn = MagicMock()
        load_conn.execute.return_value.fetchall.return_value = []

        with (
            patch("app.docs.analyze_handler.get_connection") as mock_get_conn,
            patch("app.docs.analyze_handler.get_draft", return_value=None),
        ):
            mock_get_conn.return_value = _ConnectCM(load_conn)
            with pytest.raises(ValueError, match="not found"):
                analyze_impact({"draft_id": str(_DRAFT_ID)})

    def test_missing_draft_id_in_payload(self):
        with pytest.raises(ValueError, match="draft_id"):
            analyze_impact({})

    def test_put_named_graph_failure_marks_draft_failed_on_final_attempt(self):
        draft = _make_draft()
        load_conn = _make_load_conn(entity_rows=[])
        fail_conn = _make_insert_conn()  # used by _mark_draft_failed

        with (
            patch("app.docs.analyze_handler.get_connection") as mock_get_conn,
            patch("app.docs.analyze_handler.get_draft", return_value=draft),
            patch("app.docs.analyze_handler.build_draft_graph", return_value="# t"),
            patch(
                "app.docs.analyze_handler.put_named_graph",
                return_value=False,
            ),
        ):
            mock_get_conn.side_effect = [
                _ConnectCM(load_conn),
                _ConnectCM(fail_conn),  # for _mark_draft_failed
            ]
            with pytest.raises(RuntimeError, match="Failed to load draft graph"):
                analyze_impact(
                    {"draft_id": str(_DRAFT_ID)},
                    attempt=3,
                    max_attempts=3,
                )

        # The failure-path connection must have seen a status='failed' update.
        fail_sql = [c.args[0].lower() for c in fail_conn.execute.call_args_list]
        assert any("update drafts" in s for s in fail_sql)

    def test_put_named_graph_failure_does_not_mark_failed_when_retry_pending(self):
        """#448: a transient Jena failure on attempt 1 must not flip the draft."""
        draft = _make_draft()
        load_conn = _make_load_conn(entity_rows=[])

        with (
            patch("app.docs.analyze_handler.get_connection") as mock_get_conn,
            patch("app.docs.analyze_handler.get_draft", return_value=draft),
            patch("app.docs.analyze_handler.build_draft_graph", return_value="# t"),
            patch(
                "app.docs.analyze_handler.put_named_graph",
                return_value=False,
            ),
        ):
            # Only one connection is opened — the failed-status update
            # never runs because the handler defers the flip.
            mock_get_conn.side_effect = [_ConnectCM(load_conn)]
            with pytest.raises(RuntimeError, match="Failed to load draft graph"):
                analyze_impact(
                    {"draft_id": str(_DRAFT_ID)},
                    attempt=1,
                    max_attempts=3,
                )

        # Only the initial load connection was opened.
        assert mock_get_conn.call_count == 1

    def test_analyzer_exception_marks_draft_failed_on_final_attempt(self):
        """#456: cleanup of the named graph is no longer the analyzer's job.

        Graph lifecycle is owned by ``delete_draft_handler`` so we no
        longer drop the graph on analyse failure (used to mask
        transient Jena hiccups). Only the draft status flip remains.
        """
        draft = _make_draft()
        load_conn = _make_load_conn(entity_rows=[])
        fail_conn = _make_insert_conn()

        analyzer_mock = MagicMock()
        analyzer_mock.analyze.side_effect = RuntimeError("sparql boom")

        with (
            patch("app.docs.analyze_handler.get_connection") as mock_get_conn,
            patch("app.docs.analyze_handler.get_draft", return_value=draft),
            patch("app.docs.analyze_handler.build_draft_graph", return_value="# t"),
            patch(
                "app.docs.analyze_handler.put_named_graph",
                return_value=True,
            ),
            patch(
                "app.docs.analyze_handler.ImpactAnalyzer",
                return_value=analyzer_mock,
            ),
        ):
            mock_get_conn.side_effect = [
                _ConnectCM(load_conn),
                _ConnectCM(fail_conn),  # _mark_draft_failed
            ]
            with pytest.raises(RuntimeError, match="sparql boom"):
                analyze_impact(
                    {"draft_id": str(_DRAFT_ID)},
                    attempt=3,
                    max_attempts=3,
                )

        # The failure-path connection must have seen a status='failed' update.
        fail_sql = [c.args[0].lower() for c in fail_conn.execute.call_args_list]
        assert any("update drafts" in s for s in fail_sql)


class TestRegistration:
    def test_handler_is_registered_in_worker_registry(self):
        """The import side effect must replace the Phase 2 stub."""
        import app.docs  # noqa: F401 — triggers registration
        from app.docs.analyze_handler import analyze_impact as real_handler
        from app.jobs.worker import _HANDLERS

        assert _HANDLERS["analyze_impact"] is real_handler
