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
from app.docs.version_model import DraftVersion

_DRAFT_ID = uuid.UUID("77777777-7777-7777-7777-777777777777")
_VERSION_ID = uuid.UUID("88888888-8888-8888-8888-888888888888")
_GRAPH_URI = f"https://data.riik.ee/ontology/estleg/drafts/{_DRAFT_ID}"


def _make_version() -> DraftVersion:
    """Build a v1 :class:`DraftVersion` for analyze_handler patches."""
    return DraftVersion(
        id=_VERSION_ID,
        draft_id=_DRAFT_ID,
        version_number=1,
        reading_stage="vtk",
        parsed_text_encrypted=None,
        storage_path="/tmp/cipher.enc",
        graph_uri=_GRAPH_URI,
        status="analyzing",
        created_at=datetime.now(UTC),
        created_by=uuid.UUID("55555555-5555-5555-5555-555555555555"),
    )


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
        parsed_text_encrypted=None,
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
        # Row shape post-Wave-2-Step-5: (ref_text, entity_uri,
        # confidence, ref_type, location, partial_match). Both rows
        # below are fully-resolved provision matches → partial_match
        # is NULL.
        entity_rows = [
            ("KarS § 133", "urn:kars-133", 0.9, "provision", json.dumps({}), None),
            ("TsÜS § 12", "urn:tsus-12", 0.85, "provision", json.dumps({}), None),
        ]

        load_conn = _make_load_conn(entity_rows=entity_rows)
        insert_conn = _make_insert_conn()
        sync_conn = _make_sync_conn((datetime(2026, 4, 9, 12, 0, tzinfo=UTC), 1061123))

        mock_analyzer = MagicMock()
        mock_analyzer.analyze.return_value = _findings(affected=2, conflicts=1, gaps=1)

        with (
            patch("app.docs.analyze_handler.get_connection") as mock_get_conn,
            patch("app.docs.analyze_handler.get_draft", return_value=draft),
            patch("app.docs.analyze_handler.get_latest_version", return_value=_make_version()),
            patch(
                "app.docs.analyze_handler.build_draft_graph",
                return_value="# turtle",
            ) as mock_build,
            patch(
                "app.docs.analyze_handler.put_named_graph",
                return_value=True,
            ) as mock_put,
            patch(
                "app.docs.analyze_handler.write_doc_lineage",
                return_value=None,
            ),
            patch(
                "app.docs.analyze_handler.fetch_draft",
                return_value=None,
            ),
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
        # and one UPDATE drafts. Post-#625 the SSOT helper writes
        # parameterised SQL so we look for the "ready" status in the
        # bound params, not the SQL string.
        calls = insert_conn.execute.call_args_list
        sql_texts = [c.args[0].lower() for c in calls]
        assert any("insert into impact_reports" in s for s in sql_texts)
        # #618 PR-B: the impact_reports INSERT must carry a
        # ``draft_version_id`` column bound to the latest version.
        insert_call = next(c for c in calls if "insert into impact_reports" in c.args[0].lower())
        assert "draft_version_id" in insert_call.args[0]
        # Param order: report_id, draft_id, draft_version_id, ...
        assert insert_call.args[1][2] == str(_VERSION_ID)
        # §4.2 cutover (#618 PR-B): update_draft_status now writes to BOTH
        # tables, so we expect an UPDATE drafts AND an UPDATE draft_versions.
        update_drafts_calls = [c for c in calls if "update drafts" in c.args[0].lower()]
        update_versions_calls = [c for c in calls if "update draft_versions" in c.args[0].lower()]
        assert len(update_drafts_calls) == 1
        assert update_versions_calls, (
            "analyze_handler must write status='ready' to draft_versions via the "
            "version-aware update_draft_status (§4.2 cutover, #618 PR-B)"
        )
        assert update_drafts_calls[0].args[1][0] == "ready"
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
            patch("app.docs.analyze_handler.get_latest_version", return_value=_make_version()),
            patch(
                "app.docs.analyze_handler.build_draft_graph",
                return_value="# ttl",
            ),
            patch(
                "app.docs.analyze_handler.put_named_graph",
                return_value=True,
            ),
            patch(
                "app.docs.analyze_handler.write_doc_lineage",
                return_value=None,
            ),
            patch(
                "app.docs.analyze_handler.fetch_draft",
                return_value=None,
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
            patch("app.docs.analyze_handler.get_latest_version", return_value=_make_version()),
            patch(
                "app.docs.analyze_handler.build_draft_graph",
                return_value="# ttl",
            ),
            patch(
                "app.docs.analyze_handler.put_named_graph",
                return_value=True,
            ),
            patch(
                "app.docs.analyze_handler.write_doc_lineage",
                return_value=None,
            ),
            patch(
                "app.docs.analyze_handler.fetch_draft",
                return_value=None,
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
            patch("app.docs.analyze_handler.get_latest_version", return_value=_make_version()),
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
            patch("app.docs.analyze_handler.get_latest_version", return_value=_make_version()),
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
            patch("app.docs.analyze_handler.get_latest_version", return_value=_make_version()),
            patch("app.docs.analyze_handler.build_draft_graph", return_value="# t"),
            patch(
                "app.docs.analyze_handler.put_named_graph",
                return_value=True,
            ),
            patch(
                "app.docs.analyze_handler.write_doc_lineage",
                return_value=None,
            ),
            patch(
                "app.docs.analyze_handler.fetch_draft",
                return_value=None,
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


class TestAnalyzeImpactPartialMatch:
    """Wave 2 Step 5 of docs/2026-05-18-bugfix-plan.md.

    The entity-load SELECT widened to include rows where
    ``partial_match`` is non-null even though ``entity_uri`` is null.
    These rows must flow through ``_row_to_resolved_ref`` carrying the
    JSON-decoded partial_match dict so the graph builder can emit an
    act-level annotation triple instead of a fake provision URI.
    """

    def test_partial_match_row_threads_through_to_resolved_ref(self):
        """A draft_entities row with partial_match set must surface as
        a ResolvedRef with the dict populated and entity_uri=None.
        """
        draft = _make_draft()
        # Row shape: (ref_text, entity_uri, confidence, ref_type, location, partial_match).
        # partial_match is the jsonb column from migration 034.
        entity_rows = [
            (
                "riigieelarve seaduse § 20 lõike 5",
                None,
                0.85,
                "provision",
                json.dumps({}),
                json.dumps(
                    {
                        "act_token": "REELS",
                        "act_title": "Riigieelarve seadus",
                        "section": "20",
                    }
                ),
            ),
        ]
        load_conn = _make_load_conn(entity_rows=entity_rows)
        insert_conn = _make_insert_conn()
        sync_conn = _make_sync_conn((datetime(2026, 4, 9, 12, 0, tzinfo=UTC), 1))

        mock_analyzer = MagicMock()
        mock_analyzer.analyze.return_value = _findings(affected=0)

        captured_refs: list[Any] = []

        def _capture_refs(_draft: Any, refs: Any) -> str:
            captured_refs.extend(refs)
            return "# turtle"

        with (
            patch("app.docs.analyze_handler.get_connection") as mock_get_conn,
            patch("app.docs.analyze_handler.get_draft", return_value=draft),
            patch("app.docs.analyze_handler.get_latest_version", return_value=_make_version()),
            patch(
                "app.docs.analyze_handler.build_draft_graph",
                side_effect=_capture_refs,
            ),
            patch("app.docs.analyze_handler.put_named_graph", return_value=True),
            patch("app.docs.analyze_handler.write_doc_lineage", return_value=None),
            patch("app.docs.analyze_handler.fetch_draft", return_value=None),
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

        # The build_draft_graph mock saw exactly one ResolvedRef with
        # partial_match populated (and entity_uri=None).
        assert len(captured_refs) == 1
        ref = captured_refs[0]
        assert ref.entity_uri is None
        assert ref.partial_match is not None
        assert ref.partial_match["act_title"] == "Riigieelarve seadus"
        assert ref.partial_match["section"] == "20"
        assert ref.partial_match["act_token"] == "REELS"
        # match_score is the resolver's partial-match marker (0.5).
        assert ref.match_score == 0.5

    def test_select_includes_partial_match_column_and_widened_where(self):
        """The SELECT must read partial_match AND the WHERE clause must
        no longer require entity_uri to be non-null. Together these
        ensure act-level partial matches surface in the impact pipeline.
        """
        draft = _make_draft()
        load_conn = _make_load_conn(entity_rows=[])
        insert_conn = _make_insert_conn()
        sync_conn = _make_sync_conn(None)

        mock_analyzer = MagicMock()
        mock_analyzer.analyze.return_value = _findings(affected=0)

        with (
            patch("app.docs.analyze_handler.get_connection") as mock_get_conn,
            patch("app.docs.analyze_handler.get_draft", return_value=draft),
            patch("app.docs.analyze_handler.get_latest_version", return_value=_make_version()),
            patch("app.docs.analyze_handler.build_draft_graph", return_value="# t"),
            patch("app.docs.analyze_handler.put_named_graph", return_value=True),
            patch("app.docs.analyze_handler.write_doc_lineage", return_value=None),
            patch("app.docs.analyze_handler.fetch_draft", return_value=None),
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

        # The load-connection's SELECT must contain ``partial_match``
        # (widened column projection) and must NOT have the old
        # ``entity_uri is not null`` filter that hid act-level rows.
        select_calls = [
            c.args[0]
            for c in load_conn.execute.call_args_list
            if "from draft_entities" in c.args[0].lower()
        ]
        assert len(select_calls) == 1
        select_sql = select_calls[0].lower()
        assert "partial_match" in select_sql, (
            "The entity-load SELECT must project partial_match — "
            "see Wave 2 Step 5 of docs/2026-05-18-bugfix-plan.md"
        )
        # Allow either ``or partial_match is not null`` or
        # any equivalent broadening — we just want to be sure the
        # bare ``entity_uri is not null`` filter is gone in isolation.
        # Specifically: the WHERE must NOT be exactly
        # ``and entity_uri is not null`` followed by a newline.
        import re

        old_filter = re.search(
            r"and\s+entity_uri\s+is\s+not\s+null\s*[\n)]",
            select_sql,
        )
        if old_filter:
            assert "or partial_match is not null" in select_sql, (
                "If entity_uri is not null is still in the WHERE, "
                "it must be paired with OR partial_match is not null "
                "so act-level rows still flow through"
            )


class TestRegistration:
    def test_handler_is_registered_in_worker_registry(self):
        """The import side effect must replace the Phase 2 stub."""
        import app.docs  # noqa: F401 — triggers registration
        from app.docs.analyze_handler import analyze_impact as real_handler
        from app.jobs.worker import _HANDLERS

        assert _HANDLERS["analyze_impact"] is real_handler


class TestAnalyzeImpactLineageHook:
    """#641 — the analyze pipeline must call ``write_doc_lineage`` after
    the Turtle PUT so the doc_type class + optional ``estleg:basedOn``
    edge land on the named graph before the ImpactAnalyzer runs.
    """

    def _run_with_draft(self, draft: Draft) -> tuple[MagicMock, MagicMock]:
        """Execute ``analyze_impact`` with all externals mocked and
        return ``(mock_write_doc_lineage, mock_fetch_draft)`` for
        assertion in the calling test.
        """
        load_conn = _make_load_conn(entity_rows=[])
        insert_conn = _make_insert_conn()
        sync_conn = _make_sync_conn((datetime(2026, 4, 9, 12, 0, tzinfo=UTC), 1))

        mock_analyzer = MagicMock()
        mock_analyzer.analyze.return_value = _findings(affected=0)

        with (
            patch("app.docs.analyze_handler.get_connection") as mock_get_conn,
            patch("app.docs.analyze_handler.get_draft", return_value=draft),
            patch("app.docs.analyze_handler.get_latest_version", return_value=_make_version()),
            patch("app.docs.analyze_handler.build_draft_graph", return_value="# t"),
            patch("app.docs.analyze_handler.put_named_graph", return_value=True),
            patch(
                "app.docs.analyze_handler.write_doc_lineage",
                return_value=None,
            ) as mock_write_lineage,
            patch(
                "app.docs.analyze_handler.fetch_draft",
                return_value=None,
            ) as mock_fetch_draft,
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

        return mock_write_lineage, mock_fetch_draft

    def test_lineage_called_with_none_when_no_parent_vtk(self):
        draft = _make_draft()  # parent_vtk_id = None by default
        mock_write_lineage, mock_fetch_draft = self._run_with_draft(draft)

        mock_fetch_draft.assert_not_called()
        mock_write_lineage.assert_called_once()
        args = mock_write_lineage.call_args.args
        assert args[0] is draft
        assert args[1] is None

    def test_lineage_called_with_fetched_parent_when_parent_vtk_id_set(self):
        vtk_id = uuid.UUID("88888888-8888-8888-8888-888888888888")
        draft = _make_draft()
        draft.parent_vtk_id = vtk_id

        load_conn = _make_load_conn(entity_rows=[])
        insert_conn = _make_insert_conn()
        sync_conn = _make_sync_conn((datetime(2026, 4, 9, 12, 0, tzinfo=UTC), 1))
        vtk_draft = _make_draft()
        vtk_draft.id = vtk_id

        mock_analyzer = MagicMock()
        mock_analyzer.analyze.return_value = _findings(affected=0)

        with (
            patch("app.docs.analyze_handler.get_connection") as mock_get_conn,
            patch("app.docs.analyze_handler.get_draft", return_value=draft),
            patch("app.docs.analyze_handler.get_latest_version", return_value=_make_version()),
            patch("app.docs.analyze_handler.build_draft_graph", return_value="# t"),
            patch("app.docs.analyze_handler.put_named_graph", return_value=True),
            patch(
                "app.docs.analyze_handler.write_doc_lineage",
                return_value=None,
            ) as mock_write_lineage,
            patch(
                "app.docs.analyze_handler.fetch_draft",
                return_value=vtk_draft,
            ) as mock_fetch_draft,
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

        mock_fetch_draft.assert_called_once_with(vtk_id)
        mock_write_lineage.assert_called_once_with(draft, vtk_draft)

    def test_lineage_runs_before_analyzer(self):
        """The ImpactAnalyzer must see the lineage triples, so
        ``write_doc_lineage`` must fire before ``ImpactAnalyzer.analyze``.
        """
        draft = _make_draft()
        load_conn = _make_load_conn(entity_rows=[])
        insert_conn = _make_insert_conn()
        sync_conn = _make_sync_conn(None)

        call_order: list[str] = []

        def _lineage_side_effect(*_args: Any, **_kwargs: Any) -> None:
            call_order.append("lineage")

        mock_analyzer = MagicMock()

        def _analyzer_side_effect(*_args: Any, **_kwargs: Any) -> Any:
            call_order.append("analyze")
            return _findings(affected=0)

        mock_analyzer.analyze.side_effect = _analyzer_side_effect

        with (
            patch("app.docs.analyze_handler.get_connection") as mock_get_conn,
            patch("app.docs.analyze_handler.get_draft", return_value=draft),
            patch("app.docs.analyze_handler.get_latest_version", return_value=_make_version()),
            patch("app.docs.analyze_handler.build_draft_graph", return_value="# t"),
            patch("app.docs.analyze_handler.put_named_graph", return_value=True),
            patch(
                "app.docs.analyze_handler.write_doc_lineage",
                side_effect=_lineage_side_effect,
            ),
            patch(
                "app.docs.analyze_handler.fetch_draft",
                return_value=None,
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

        assert call_order == ["lineage", "analyze"]
