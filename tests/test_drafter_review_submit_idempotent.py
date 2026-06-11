"""#852 E3 — ``submit_review`` claims the integrated review atomically.

Two concurrent POSTs to ``/drafter/{id}/step/6`` used to both observe
``integrated_draft_id IS NULL`` on a stale session snapshot and both run
``_trigger_integrated_review`` — duplicate draft rows, named graphs and
parse jobs. The route now serialises submitters with a per-session
advisory transaction lock, re-checks under the lock, and claims with a
conditional ``UPDATE ... WHERE integrated_draft_id IS NULL``; repeats
idempotently render the existing state.

Also covers the residual from #867: ``_trigger_integrated_review`` must
route its plaintext .docx through a request-scoped temp file (deleted in
``finally``) instead of leaving ``EXPORT_DIR/drafter-<session_id>.docx``
behind forever.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from app.drafter.session_model import DraftingSession

_ORG_ID = "11111111-1111-1111-1111-111111111111"
_USER_ID = "33333333-3333-3333-3333-333333333333"
_SESSION_ID = uuid.UUID("44444444-4444-4444-4444-444444444444")
_DRAFT_ID = uuid.UUID("55555555-5555-5555-5555-555555555555")


def _authed_user() -> dict[str, Any]:
    return {
        "id": _USER_ID,
        "email": "koostaja@seadusloome.ee",
        "full_name": "Test Koostaja",
        "role": "drafter",
        "org_id": _ORG_ID,
    }


def _make_session(*, integrated_draft_id: uuid.UUID | None = None) -> DraftingSession:
    now = datetime.now(UTC)
    return DraftingSession(
        id=_SESSION_ID,
        user_id=uuid.UUID(_USER_ID),
        org_id=uuid.UUID(_ORG_ID),
        workflow_type="full_law",
        current_step=6,
        intent="Testi ülevaadet",
        clarifications=[],
        research_data_encrypted=None,
        proposed_structure={"title": "Test", "chapters": []},
        draft_content_encrypted=None,
        integrated_draft_id=integrated_draft_id,
        status="active",
        created_at=now,
        updated_at=now,
    )


def _stub_provider() -> MagicMock:
    provider = MagicMock()
    provider.get_current_user.return_value = _authed_user()
    return provider


def _authed_client():
    from starlette.testclient import TestClient

    client = TestClient(
        __import__("app.main", fromlist=["app"]).app,
        follow_redirects=False,
    )
    client.cookies.set("access_token", "stub-token")
    return client


class _StatefulClaimConn:
    """Fake connection that models the ``integrated_draft_id`` column.

    The advisory lock is a no-op (the TestClient is sequential), but the
    SELECT/UPDATE pair behaves like Postgres: the conditional UPDATE only
    "writes" while the column is NULL, and the under-lock SELECT returns
    whatever the previous request claimed.
    """

    def __init__(self, state: dict):
        self.state = state
        self.executed: list[str] = []

    def execute(self, sql: str, params: tuple | None = None):
        sql_norm = " ".join(str(sql).split())
        self.executed.append(sql_norm)
        cursor = MagicMock()
        if "SELECT integrated_draft_id FROM drafting_sessions" in sql_norm:
            cursor.fetchone.return_value = (self.state.get("claimed"),)
            return cursor
        if "SET integrated_draft_id" in sql_norm and "IS NULL" in sql_norm:
            if self.state.get("claimed") is None:
                self.state["claimed"] = params[0]  # type: ignore[index]
                cursor.rowcount = 1
            else:
                cursor.rowcount = 0
            return cursor
        cursor.fetchone.return_value = None
        return cursor

    def commit(self) -> None:
        pass


class _ConnCM:
    def __init__(self, conn: Any):
        self.conn = conn

    def __enter__(self):
        return self.conn

    def __exit__(self, *_: Any) -> bool:
        return False


# ---------------------------------------------------------------------------
# Idempotent / atomic claim
# ---------------------------------------------------------------------------


class TestSubmitReviewIdempotent:
    @patch("app.drafter.routes._trigger_integrated_review")
    @patch("app.drafter.routes._connect")
    @patch("app.drafter.routes.fetch_session")
    @patch("app.auth.middleware._get_provider")
    def test_double_post_triggers_exactly_once(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_connect: MagicMock,
        mock_trigger: MagicMock,
    ):
        """Worst case: BOTH requests read a stale snapshot with
        ``integrated_draft_id=None``. The under-lock recheck must stop
        the second from creating a duplicate draft/graph/job."""
        mock_get_provider.return_value = _stub_provider()
        # Stale snapshot on every fetch — the pre-#852 race condition.
        mock_fetch.return_value = _make_session(integrated_draft_id=None)
        mock_trigger.return_value = _DRAFT_ID

        state: dict = {"claimed": None}
        mock_connect.side_effect = lambda: _ConnCM(_StatefulClaimConn(state))

        client = _authed_client()
        resp1 = client.post(f"/drafter/{_SESSION_ID}/step/6", data={})
        resp2 = client.post(f"/drafter/{_SESSION_ID}/step/6", data={})

        assert resp1.status_code == 200
        assert resp2.status_code == 200
        # Exactly ONE integrated draft (and hence one graph + one job —
        # both are created inside _trigger_integrated_review).
        mock_trigger.assert_called_once()
        assert state["claimed"] == str(_DRAFT_ID)

    @patch("app.drafter.routes._trigger_integrated_review")
    @patch("app.drafter.routes._connect")
    @patch("app.drafter.routes.fetch_session")
    @patch("app.auth.middleware._get_provider")
    def test_claim_is_serialised_by_advisory_lock(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_connect: MagicMock,
        mock_trigger: MagicMock,
    ):
        mock_get_provider.return_value = _stub_provider()
        mock_fetch.return_value = _make_session(integrated_draft_id=None)
        mock_trigger.return_value = _DRAFT_ID

        state: dict = {"claimed": None}
        conn = _StatefulClaimConn(state)
        mock_connect.side_effect = lambda: _ConnCM(conn)

        client = _authed_client()
        resp = client.post(f"/drafter/{_SESSION_ID}/step/6", data={})

        assert resp.status_code == 200
        joined = " ".join(conn.executed)
        # Lock taken BEFORE the recheck, claim is conditional.
        assert "pg_advisory_xact_lock" in conn.executed[0]
        assert "SELECT integrated_draft_id FROM drafting_sessions" in joined
        assert "integrated_draft_id IS NULL" in joined

    @patch("app.drafter.routes._trigger_integrated_review")
    @patch("app.drafter.routes._connect")
    @patch("app.drafter.routes.fetch_session")
    @patch("app.auth.middleware._get_provider")
    def test_fast_path_skips_lock_when_already_linked(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_connect: MagicMock,
        mock_trigger: MagicMock,
    ):
        mock_get_provider.return_value = _stub_provider()
        mock_fetch.return_value = _make_session(integrated_draft_id=_DRAFT_ID)

        client = _authed_client()
        resp = client.post(f"/drafter/{_SESSION_ID}/step/6", data={})

        assert resp.status_code == 200
        mock_trigger.assert_not_called()
        mock_connect.assert_not_called()

    @patch("app.drafter.routes._trigger_integrated_review")
    @patch("app.drafter.routes._connect")
    @patch("app.drafter.routes.fetch_session")
    @patch("app.auth.middleware._get_provider")
    def test_loser_renders_existing_state_without_triggering(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_connect: MagicMock,
        mock_trigger: MagicMock,
    ):
        """Stale snapshot + already-claimed column (the blocked-on-lock
        loser's view) → idempotent render, no second pipeline."""
        mock_get_provider.return_value = _stub_provider()
        mock_fetch.return_value = _make_session(integrated_draft_id=None)

        state: dict = {"claimed": str(_DRAFT_ID)}
        mock_connect.side_effect = lambda: _ConnCM(_StatefulClaimConn(state))

        client = _authed_client()
        resp = client.post(f"/drafter/{_SESSION_ID}/step/6", data={})

        assert resp.status_code == 200
        mock_trigger.assert_not_called()
        assert state["claimed"] == str(_DRAFT_ID)  # untouched


# ---------------------------------------------------------------------------
# Residual #867 — integrated-review docx must not persist in EXPORT_DIR
# ---------------------------------------------------------------------------


class TestIntegratedReviewTempfile:
    def _run_trigger(self, builder_side_effect=None) -> tuple[uuid.UUID | None, dict]:
        """Drive ``_trigger_integrated_review`` with everything mocked
        except the temp-file lifecycle. Returns (draft_id, captured)."""
        from app.drafter.routes import _trigger_integrated_review

        captured: dict = {}

        def fake_builder(**kwargs):
            out_path = kwargs.get("out_path")
            captured["out_path"] = out_path
            if builder_side_effect is not None:
                raise builder_side_effect
            assert out_path is not None
            Path(out_path).write_bytes(b"PK-fake-docx")
            return Path(out_path)

        stored = MagicMock()
        stored.storage_path = "stored/path.enc"
        draft = MagicMock()
        draft.id = _DRAFT_ID

        conn = MagicMock()

        with (
            patch("app.drafter.docx_builder.build_drafter_docx", side_effect=fake_builder),
            patch("app.storage.store_file", return_value=stored) as mock_store,
            patch("app.docs.draft_model.create_draft", return_value=draft),
            patch("app.docs.version_model.create_draft_version"),
            patch("app.drafter.routes._connect") as mock_connect,
            patch("app.drafter.routes.JobQueue") as mock_queue_cls,
        ):
            mock_connect.return_value.__enter__ = MagicMock(return_value=conn)
            mock_connect.return_value.__exit__ = MagicMock(return_value=False)
            session = _make_session()
            result = _trigger_integrated_review(session, _authed_user())  # type: ignore[arg-type]
            captured["store_calls"] = mock_store.call_args_list
            captured["enqueue_calls"] = mock_queue_cls.return_value.enqueue.call_args_list
        return result, captured

    def test_docx_goes_through_temp_file_and_is_removed(self):
        result, captured = self._run_trigger()

        assert result == _DRAFT_ID
        out_path = captured["out_path"]
        assert out_path is not None, "builder must receive an explicit out_path"
        # Request-scoped temp file, not the EXPORT_DIR default name.
        assert Path(out_path).name.startswith("seadusloome-integreeritud-")
        assert not Path(out_path).exists(), "plaintext temp docx must be removed"
        # The encrypted copy got the bytes the builder wrote.
        assert captured["store_calls"][0].args[0] == b"PK-fake-docx"
        # The pipeline job was still enqueued.
        assert captured["enqueue_calls"][0].args[0] == "parse_draft"

    def test_temp_file_removed_when_builder_raises(self):
        with pytest.raises(RuntimeError, match="render boom"):
            self._run_trigger(builder_side_effect=RuntimeError("render boom"))

        # The captured path was created by NamedTemporaryFile before the
        # builder ran; the finally must have removed it.
        # (captured dict is not returned on raise, so re-check via scan:
        # nothing matching the prefix may remain in the temp dir.)
        import tempfile

        leftovers = list(
            Path(tempfile.gettempdir()).glob(f"seadusloome-integreeritud-{_SESSION_ID}-*")
        )
        assert leftovers == []


# ---------------------------------------------------------------------------
# #852 review F1 — integrated-review drafts get a v1 draft_versions row
# ---------------------------------------------------------------------------
#
# ``_trigger_integrated_review`` used to create the drafts row + patch
# graph_uri but never insert the required v1 ``draft_versions`` row that
# normal uploads create (``app/docs/upload.py::_create_new_draft``, #618
# PR-B). Consequence chain: ``analyze_impact`` resolved
# ``latest_version=None`` → ``impact_reports.draft_version_id`` NULL →
# stale-annotation reconciliation skipped and the §4.2 status mirror had
# no version row, for EVERY drafter-generated draft. (Gap pre-dates #852
# — present on merged main — but fixed here as part of this PR's blast
# radius.)


class TestIntegratedReviewVersionRow:
    def _run_trigger_with_version_capture(self) -> dict:
        from app.drafter.routes import _trigger_integrated_review

        captured: dict = {}
        conn = MagicMock()

        def fake_create_draft(c, **kwargs):
            captured["draft_conn"] = c
            draft = MagicMock()
            draft.id = _DRAFT_ID
            return draft

        def fake_create_version(c, **kwargs):
            captured["version_conn"] = c
            captured["version_kwargs"] = kwargs
            # Same-transaction proof: the connection must NOT have been
            # committed between the draft insert and the version insert.
            captured["commits_before_version"] = conn.commit.call_count
            return MagicMock()

        def fake_builder(**kwargs):
            out_path = kwargs["out_path"]
            Path(out_path).write_bytes(b"PK-fake-docx")
            return Path(out_path)

        stored = MagicMock()
        stored.storage_path = "stored/path.enc"

        with (
            patch("app.drafter.docx_builder.build_drafter_docx", side_effect=fake_builder),
            patch("app.storage.store_file", return_value=stored),
            patch("app.docs.draft_model.create_draft", side_effect=fake_create_draft),
            patch(
                "app.docs.version_model.create_draft_version",
                side_effect=fake_create_version,
            ),
            patch("app.drafter.routes.log_action") as mock_log,
            patch("app.drafter.routes._connect") as mock_connect,
            patch("app.drafter.routes.JobQueue"),
        ):
            mock_connect.return_value.__enter__ = MagicMock(return_value=conn)
            mock_connect.return_value.__exit__ = MagicMock(return_value=False)
            session = _make_session()
            result = _trigger_integrated_review(session, _authed_user())  # type: ignore[arg-type]

        captured["result"] = result
        captured["total_commits"] = conn.commit.call_count
        captured["log_calls"] = mock_log.call_args_list
        return captured

    def test_v1_version_row_created_in_same_transaction(self):
        captured = self._run_trigger_with_version_capture()

        assert captured["result"] == _DRAFT_ID
        # The version insert happened — and on the SAME connection as
        # the draft insert, BEFORE the single commit (one transaction:
        # a partial commit can never leave a draft without its v1 row).
        assert captured["version_conn"] is captured["draft_conn"]
        assert captured["commits_before_version"] == 0
        assert captured["total_commits"] == 1

    def test_v1_version_row_mirrors_upload_path_fields(self):
        captured = self._run_trigger_with_version_capture()
        kwargs = captured["version_kwargs"]

        # Mirrors app/docs/upload.py::_create_new_draft exactly.
        assert kwargs["draft_id"] == _DRAFT_ID
        assert kwargs["version_number"] == 1
        assert kwargs["reading_stage"] == "vtk"
        assert kwargs["status"] == "uploaded"
        # created_by = the drafting-session OWNER (not e.g. org).
        assert kwargs["created_by"] == uuid.UUID(_USER_ID)
        assert kwargs["storage_path"] == "stored/path.enc"
        # The version points at the FINAL graph URI (with the draft id),
        # not the pending- placeholder.
        assert kwargs["graph_uri"].endswith(str(_DRAFT_ID))
        assert "pending-" not in kwargs["graph_uri"]

    def test_v1_creation_is_audited_like_uploads(self):
        captured = self._run_trigger_with_version_capture()

        version_logs = [c for c in captured["log_calls"] if c.args[1] == "draft.version.create"]
        assert len(version_logs) == 1
        assert version_logs[0].args[0] == _USER_ID
        assert version_logs[0].args[2]["draft_id"] == str(_DRAFT_ID)
        assert version_logs[0].args[2]["version_number"] == 1


class TestIntegratedDraftAnalyzeLinksVersion:
    """End of the F1 chain: once the v1 row exists, ``analyze_impact``
    must bind ``impact_reports.draft_version_id`` to it (NOT NULL).

    Uses the REAL ``get_latest_version`` reading the v1 row off the
    insert connection — no patching of the version lookup — so this
    proves the wiring an integrated-review draft now satisfies.
    """

    def test_analyze_binds_report_to_v1_version(self):
        from datetime import datetime as _dt

        from app.docs.analyze_handler import analyze_impact
        from app.docs.draft_model import Draft
        from app.impact.analyzer import ImpactFindings

        now = datetime.now(UTC)
        version_id = uuid.UUID("66666666-6666-6666-6666-666666666666")
        graph_uri = f"https://data.riik.ee/ontology/estleg/drafts/{_DRAFT_ID}"
        draft = Draft(
            id=_DRAFT_ID,
            user_id=uuid.UUID(_USER_ID),
            org_id=uuid.UUID(_ORG_ID),
            title="Integreeritud eelnõu",
            filename=f"drafter-{_SESSION_ID}.docx",
            content_type=(
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            ),
            file_size=1024,
            storage_path="stored/path.enc",
            graph_uri=graph_uri,
            status="analyzing",
            parsed_text_encrypted=None,
            entity_count=0,
            error_message=None,
            created_at=now,
            updated_at=now,
        )
        findings = ImpactFindings(
            affected_entities=[],
            conflicts=[],
            gaps=[],
            eu_compliance=[],
            affected_count=0,
            conflict_count=0,
            gap_count=0,
        )

        # Row served to the REAL get_latest_version — _VERSION_COLUMNS
        # order: id, draft_id, version_number, reading_stage,
        # parsed_text_encrypted, storage_path, graph_uri, status,
        # created_at, created_by.
        version_row = (
            str(version_id),
            str(_DRAFT_ID),
            1,
            "vtk",
            None,
            "stored/path.enc",
            graph_uri,
            "analyzing",
            now,
            _USER_ID,
        )

        load_conn = MagicMock()
        eu_cursor = MagicMock()
        eu_cursor.fetchall.return_value = []
        entity_cursor = MagicMock()
        entity_cursor.fetchall.return_value = []
        load_conn.execute.side_effect = [eu_cursor, entity_cursor]

        sync_conn = MagicMock()
        sync_conn.execute.return_value.fetchone.return_value = (
            _dt(2026, 6, 1, 12, 0, tzinfo=UTC),
            90000,
        )

        insert_conn = MagicMock()
        insert_sqls: list[tuple] = []

        def insert_execute(sql: str, params: tuple | None = None):
            insert_sqls.append((sql, params))
            cursor = MagicMock()
            cursor.rowcount = 1
            if "from draft_versions" in " ".join(str(sql).lower().split()):
                cursor.fetchone.return_value = version_row
            else:
                cursor.fetchone.return_value = None
            return cursor

        insert_conn.execute.side_effect = insert_execute

        class _CM:
            def __init__(self, c):
                self.c = c

            def __enter__(self):
                return self.c

            def __exit__(self, *_):
                return False

        mock_analyzer = MagicMock()
        mock_analyzer.analyze.return_value = findings

        with (
            patch("app.docs.analyze_handler.get_connection") as mock_get_conn,
            patch("app.docs.analyze_handler.get_draft", return_value=draft),
            patch("app.docs.analyze_handler.build_draft_graph", return_value="# t"),
            patch("app.docs.analyze_handler.put_named_graph", return_value=True),
            patch("app.docs.analyze_handler.write_doc_lineage", return_value=None),
            patch("app.docs.analyze_handler.fetch_draft", return_value=None),
            patch("app.docs.analyze_handler.ImpactAnalyzer", return_value=mock_analyzer),
            patch("app.docs.analyze_handler.calculate_impact_score", return_value=7),
        ):
            mock_get_conn.side_effect = [
                _CM(load_conn),
                _CM(sync_conn),
                _CM(insert_conn),
            ]
            analyze_impact({"draft_id": str(_DRAFT_ID)})

        report_inserts = [
            (sql, params)
            for sql, params in insert_sqls
            if "insert into impact_reports" in " ".join(str(sql).lower().split())
        ]
        assert len(report_inserts) == 1
        _sql, params = report_inserts[0]
        # Param order: report_id, draft_id, draft_version_id, ...
        assert params is not None
        assert params[2] == str(version_id), (
            "impact_reports.draft_version_id must bind to the v1 row that "
            "_trigger_integrated_review now creates — NULL means the F1 "
            "regression is back"
        )
