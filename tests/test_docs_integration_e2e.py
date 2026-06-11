"""End-to-end pipeline integration test for Phase 2 Document Upload (#462).

Drives the whole upload → parse → extract → analyze → report flow
synchronously through the four real handlers, mocking only the
*external* services (Tika HTTP, Anthropic API, Jena HTTP) and the
Postgres connection. The handlers themselves run unmodified, so any
regression in the state-machine plumbing — for example #441's
"retries stuck in 'retrying'" bug — surfaces here as a failed
``claim_next`` instead of needing a four-week production trace to
diagnose.

Test layout:

    1. POST /drafts as an authenticated user → 303 → draft row exists
       in the in-memory DB stub, parse_draft job enqueued.
    2. Manually call ``JobQueue().claim_next()`` → call ``parse_draft``
       handler → draft.status='extracting', extract_entities job in
       the queue.
    3. Claim + call ``extract_entities`` → draft.status='analyzing',
       analyze_impact job in the queue, draft_entities rows created.
    4. Claim + call ``analyze_impact`` → draft.status='ready',
       impact_reports row inserted, named graph put.
    5. GET /drafts/<id>/report → 200, summary card visible.

The DB connection mock keeps a tiny in-memory representation of just
the columns the handlers touch. It is NOT a complete Postgres
emulator — every test that touches a new column has to extend it.
"""

from __future__ import annotations

import io
import json
import uuid
import zipfile
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from starlette.testclient import TestClient


def _docx_bytes() -> bytes:
    """Minimal structurally-valid .docx bytes (#858 magic/zip checks)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr("word/document.xml", "<w:document>E2E test</w:document>")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Shared identities + helpers
# ---------------------------------------------------------------------------


_USER_ID = "33333333-3333-3333-3333-333333333333"
_ORG_ID = "11111111-1111-1111-1111-111111111111"


def _authed_user() -> dict[str, Any]:
    return {
        "id": _USER_ID,
        "email": "koostaja@seadusloome.ee",
        "full_name": "Test Koostaja",
        "role": "drafter",
        "org_id": _ORG_ID,
    }


def _stub_provider() -> MagicMock:
    """Return a JWTAuthProvider stub that always authenticates."""
    provider = MagicMock()
    provider.get_current_user.return_value = _authed_user()
    return provider


def _authed_client():
    """Return an authenticated TestClient against the live FastHTML app."""
    from app.main import app

    client = TestClient(app, follow_redirects=False)
    client.cookies.set("access_token", "stub-token")
    return client


# ---------------------------------------------------------------------------
# Tiny in-memory Postgres stand-in
# ---------------------------------------------------------------------------


class _State:
    """In-memory state shared across the mocked Postgres connections."""

    def __init__(self) -> None:
        self.draft_row: tuple | None = None
        self.draft_id: uuid.UUID | None = None
        self.entities: list[tuple] = []
        self.impact_reports: list[tuple] = []
        self.background_jobs: list[dict] = []
        self.next_job_id = 1

    def insert_job(self, job_type: str, payload: Any, priority: int = 0) -> int:
        # ``payload`` arrives wrapped in ``Jsonb(...)`` from the real
        # enqueue path; unwrap it so downstream assertions can treat
        # it as a plain dict.
        if hasattr(payload, "obj"):  # psycopg Jsonb exposes .obj
            payload_dict = payload.obj  # type: ignore[attr-defined]
        elif hasattr(payload, "adapted"):
            payload_dict = payload.adapted  # type: ignore[attr-defined]
        else:
            payload_dict = payload
        job_id = self.next_job_id
        self.next_job_id += 1
        self.background_jobs.append(
            {
                "id": job_id,
                "job_type": job_type,
                "payload": payload_dict,
                "status": "pending",
                "priority": priority,
                "attempts": 0,
                "max_attempts": 3,
                "claimed_by": None,
                "claimed_at": None,
                "started_at": None,
                "finished_at": None,
                "error_message": None,
                "result": None,
                "scheduled_for": datetime.now(UTC),
                "created_at": datetime.now(UTC),
            }
        )
        return job_id

    def claim_next_pending(self) -> dict | None:
        for job in self.background_jobs:
            if job["status"] == "pending":
                job["status"] = "claimed"
                return job
        return None


# ---------------------------------------------------------------------------
# Connection mock factory
# ---------------------------------------------------------------------------


def _make_conn_factory(state: _State):
    """Build a context-managed connection mock that interprets SQL by keywords.

    Only the queries the handlers actually run are recognised — anything
    else returns an empty cursor. This is *not* a real Postgres emulator;
    it tracks just enough state to let the four handlers thread through
    end-to-end.
    """

    def _execute(sql: str, params: tuple | None = None):
        sql_lower = " ".join(sql.split()).lower()
        cursor = MagicMock()
        # Default rowcount=0 so callers using ``(rowcount or 0) > 0``
        # get a usable boolean back.  Branches that simulate a real
        # UPDATE override this to 1.
        cursor.rowcount = 0

        # ----- drafts table -----
        if "insert into drafts" in sql_lower and "returning" in sql_lower:
            # create_draft inserts and returns the row. The INSERT
            # columns are: user_id, org_id, title, filename,
            # content_type, file_size, storage_path, graph_uri, status,
            # doc_type, parent_vtk_id -- 11 params. RETURNING reconstructs
            # all 19 columns (19th is processing_completed_at, migration
            # 023 / #670; 18th is parent_vtk_id, migration 019 / #639;
            # 16th is last_accessed_at, migration 015 / #572).
            assert params is not None
            (
                user_id,
                org_id,
                title,
                filename,
                content_type,
                file_size,
                storage_path,
                graph_uri,
                _status,
            ) = params[:9]
            doc_type = params[9] if len(params) > 9 else "eelnou"
            parent_vtk_id = params[10] if len(params) > 10 else None
            new_id = state.draft_id or uuid.uuid4()
            state.draft_id = new_id
            now = datetime.now(UTC)
            row = (
                new_id,
                user_id,
                org_id,
                title,
                filename,
                content_type,
                file_size,
                storage_path,
                graph_uri,
                "uploaded",
                None,  # parsed_text_encrypted
                None,  # entity_count
                None,  # error_message
                now,
                now,
                now,  # last_accessed_at (#572)
                doc_type,  # doc_type (#639)
                parent_vtk_id,  # parent_vtk_id (#639)
                None,  # processing_completed_at (#670)
            )
            state.draft_row = row
            cursor.fetchone.return_value = row
            return cursor

        if "update drafts set graph_uri" in sql_lower:
            # The upload handler patches graph_uri after insert.
            assert params is not None and state.draft_row is not None
            new_uri = params[0]
            row = list(state.draft_row)
            row[8] = new_uri
            state.draft_row = tuple(row)
            return cursor

        if (
            "select" in sql_lower
            and "from drafts" in sql_lower
            and ("where id" in sql_lower or "where d.id" in sql_lower)
        ):
            # get_draft / fetch_draft.  Post #618 PR-B the get_draft SELECT
            # JOINs through ``draft_versions`` and aliases drafts as ``d``,
            # so the WHERE predicate is ``where d.id`` rather than the
            # legacy ``where id``.  Both shapes return the same draft row
            # because the COALESCE in the new query falls back to the
            # legacy ``drafts.*`` columns when no version row exists --
            # which is the case in this in-memory stub since we never
            # write a v1 row here.
            cursor.fetchone.return_value = state.draft_row
            return cursor

        if "from drafts" in sql_lower and "where org_id" in sql_lower:
            # fetch_drafts_for_org / count_drafts_for_org
            if "count(" in sql_lower:
                cursor.fetchone.return_value = (1 if state.draft_row else 0,)
            else:
                cursor.fetchall.return_value = [state.draft_row] if state.draft_row else []
            return cursor

        if "insert into draft_versions" in sql_lower and "returning" in sql_lower:
            # #618 PR-B: the upload handler now creates an explicit v1
            # draft_versions row alongside the drafts INSERT.  Mint a
            # synthetic version row so create_draft_version's
            # RETURNING ... fetchone gets a usable tuple.  This stub
            # doesn't otherwise track version state because
            # update_draft_status's version write is a no-op below.
            assert params is not None
            (
                v_draft_id,
                version_number,
                reading_stage,
                _parsed_text_encrypted,
                v_storage_path,
                v_graph_uri,
                v_status,
                v_created_by,
            ) = params
            v_id = uuid.uuid4()
            now = datetime.now(UTC)
            cursor.fetchone.return_value = (
                v_id,
                uuid.UUID(v_draft_id) if isinstance(v_draft_id, str) else v_draft_id,
                int(version_number),
                str(reading_stage),
                None,
                str(v_storage_path),
                str(v_graph_uri),
                str(v_status),
                now,
                uuid.UUID(v_created_by) if isinstance(v_created_by, str) else v_created_by,
            )
            return cursor

        if "update draft_versions" in sql_lower:
            # #618 PR-B cutover: update_draft_status now writes to BOTH
            # draft_versions AND drafts.  This stub doesn't model the
            # draft_versions rows so the version UPDATE is a no-op --
            # the legacy ``drafts`` mirror immediately following carries
            # the actual state mutation that the test asserts on.
            cursor.rowcount = 1
            return cursor

        if "select" in sql_lower and "max(version_number)" in sql_lower:
            # get_next_version_number: handle the COALESCE(MAX, 0) query
            # used by the v2+ upload branch.  We don't model real version
            # rows here so always return 0 (caller adds 1).
            cursor.fetchone.return_value = (0,)
            return cursor

        if "update drafts" in sql_lower and "status" in sql_lower:
            # Pipeline status flips. Post-#625 §4.2 every transition
            # routes through ``app.docs.status.update_draft_status`` so
            # the SQL is parameterised: the param tuple always starts
            # with ``[status, error_message, error_debug, ...extras...,
            # draft_id]``. Extras are sorted by column name so
            # ``entity_count`` and ``parsed_text_encrypted`` (the only
            # extras used in this package) land at known positions.
            assert state.draft_row is not None and params is not None
            row = list(state.draft_row)
            new_status = str(params[0])
            row[9] = new_status
            error_message = params[1]
            if error_message is not None:
                row[12] = error_message
            else:
                row[12] = None  # successful transition clears stale error
            # ``params[2]`` is error_debug (not mirrored on Draft).
            # ``params[3:-1]`` are extras in sorted column-name order:
            #   - extracting transition: ``parsed_text_encrypted``
            #   - analyzing transition:  ``entity_count``
            if "parsed_text_encrypted = %s" in sql_lower:
                # Find the parsed_text_encrypted slot. Sorted alpha
                # order with no other extras puts it at index 3.
                row[10] = params[3]
            if "entity_count = %s" in sql_lower:
                # Sorted alpha order with no other extras puts entity_count
                # at index 3.
                row[11] = params[3]
            cursor.rowcount = 1
            state.draft_row = tuple(row)
            return cursor

        # ----- draft_entities -----
        if "insert into draft_entities" in sql_lower:
            assert params is not None
            state.entities.append(params)
            return cursor

        if "from draft_entities" in sql_lower and "where draft_id" in sql_lower:
            # analyze_handler reads back resolved refs.
            # Wave 2 Step 5 widened the SELECT to include partial_match
            # (jsonb, migration 034) AND broadened the WHERE clause to
            # include rows where partial_match is non-null even if
            # entity_uri is null. The mock mirrors both shifts:
            #   - row index 6 is the partial_match jsonb blob (None for
            #     fully-resolved or fully-unresolved rows).
            #   - the WHERE filter accepts EITHER entity_uri or
            #     partial_match being non-null.
            rows = [
                (
                    e[1],  # ref_text
                    e[2],  # entity_uri
                    e[3],  # confidence
                    e[4],  # ref_type
                    e[5],  # location
                    e[6] if len(e) > 6 else None,  # partial_match (migration 034)
                )
                for e in state.entities
                if e[2] is not None or (len(e) > 6 and e[6] is not None)
            ]
            cursor.fetchall.return_value = rows
            return cursor

        # ----- impact_reports -----
        if "insert into impact_reports" in sql_lower:
            assert params is not None
            state.impact_reports.append(params)
            return cursor

        if "from impact_reports" in sql_lower:
            # report routes look up the latest report by draft_id.
            # Post #618 PR-B the INSERT params layout is:
            #   (id, draft_id, draft_version_id, affected_count,
            #    conflict_count, gap_count, impact_score, report_data,
            #    ontology_version)
            # The legacy SELECT used by the report routes still
            # expects the old shape (no draft_version_id), so we skip
            # index 2 (draft_version_id) when reconstructing the row.
            if state.impact_reports:
                report = state.impact_reports[-1]
                cursor.fetchone.return_value = (
                    report[0],  # id
                    report[1],  # draft_id
                    report[3],  # affected_count (params[2] is draft_version_id)
                    report[4],  # conflict_count
                    report[5],  # gap_count
                    report[6],  # impact_score
                    report[7],  # report_data
                    report[8],  # ontology_version
                    datetime.now(UTC),
                )
            else:
                cursor.fetchone.return_value = None
            return cursor

        # ----- background_jobs -----
        if "insert into background_jobs" in sql_lower:
            assert params is not None
            job_type = params[0]
            payload_raw = params[1]
            priority = params[2]
            new_id = state.insert_job(job_type, payload_raw, priority)
            cursor.fetchone.return_value = (new_id,)
            return cursor

        if "from background_jobs" in sql_lower:
            # The handlers don't read background_jobs directly; only
            # the e2e test driver does, and it goes through JobQueue
            # which we patch separately.
            cursor.fetchone.return_value = None
            cursor.fetchall.return_value = []
            return cursor

        # ----- sync_log (analyze_handler reads it for ontology version) -----
        if "from sync_log" in sql_lower:
            cursor.fetchone.return_value = (
                datetime(2026, 4, 9, 12, 0, tzinfo=UTC),
                1061123,
            )
            return cursor

        # ----- audit_log inserts (auth audit) -----
        if "insert into audit_log" in sql_lower:
            return cursor

        # Default: empty result so any unexpected query at least
        # doesn't blow up the test runner.
        cursor.fetchone.return_value = None
        cursor.fetchall.return_value = []
        return cursor

    class _ConnCM:
        def __init__(self):
            self.conn = MagicMock()
            self.conn.execute.side_effect = _execute
            self.conn.commit = MagicMock()

        def __enter__(self):
            return self.conn

        def __exit__(self, *_a):
            return False

    def _factory(*_a, **_kw):
        return _ConnCM()

    return _factory


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


class TestDocsPipelineE2E:
    def test_full_pipeline_upload_to_report(self, monkeypatch):
        """Drive the full Phase 2 pipeline end-to-end (#462).

        Mocks the three external services (Tika, Claude, Jena) and the
        DB layer; the four real handlers run unmodified.
        """
        # Suppress the ephemeral-key warning + don't enforce production.
        monkeypatch.setenv("APP_ENV", "development")
        monkeypatch.delenv("STORAGE_ENCRYPTION_KEY", raising=False)
        monkeypatch.delenv("TIKA_URL", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        state = _State()
        # Pre-mint the draft id so the test can refer to it without
        # racing the INSERT.
        state.draft_id = uuid.UUID("99999999-9999-9999-9999-999999999999")
        conn_factory = _make_conn_factory(state)

        # Mock the three external services.
        fake_tika = MagicMock()
        fake_tika.extract_text.return_value = "§ 1. Test säte. KarS § 133 alusel kohaldatav."

        fake_resolved = MagicMock()
        fake_resolved.entity_uri = "https://data.riik.ee/ontology/estleg#KarS_Par_133"
        fake_resolved.matched_label = "KarS § 133"
        fake_resolved.match_score = 1.0
        # #801/#803: extract_handler now reads ``partial_match`` and json-dumps
        # it when present. MagicMock auto-creates attributes on access, so
        # leaving this unset would give us a MagicMock instance that isn't
        # ``None`` and isn't JSON-serialisable. Pin it explicitly to None
        # for the resolved-with-URI happy path.
        fake_resolved.partial_match = None
        fake_resolved.extracted = MagicMock(
            ref_text="KarS § 133",
            ref_type="provision",
            confidence=0.9,
            location={"chunk": 0},
        )

        from app.docs.impact.analyzer import ImpactFindings

        fake_findings = ImpactFindings(
            affected_entities=[{"uri": "urn:x", "label": "X", "type": "urn:t"}],
            conflicts=[],
            gaps=[],
            eu_compliance=[],
            affected_count=1,
            conflict_count=0,
            gap_count=0,
        )
        fake_analyzer_instance = MagicMock()
        fake_analyzer_instance.analyze.return_value = fake_findings

        # Storage round-trip is patched at the encrypted module level so
        # the upload can encrypt+write without touching the real disk.
        with (
            patch("app.auth.middleware._get_provider", return_value=_stub_provider()),
            patch("app.docs.upload._connect", side_effect=conn_factory),
            patch("app.docs.routes._detail._connect", side_effect=conn_factory),
            patch("app.docs.report_routes._connect", side_effect=conn_factory),
            patch("app.docs.parse_handler.get_connection", side_effect=conn_factory),
            patch("app.docs.parse_handler.get_default_tika_client", return_value=fake_tika),
            patch("app.docs.extract_handler.get_connection", side_effect=conn_factory),
            patch(
                "app.docs.extract_handler.extract_refs_from_text",
                return_value=[fake_resolved.extracted],
            ),
            patch(
                "app.docs.extract_handler.resolve_refs",
                return_value=[fake_resolved],
            ),
            patch("app.docs.analyze_handler.get_connection", side_effect=conn_factory),
            patch("app.docs.analyze_handler.build_draft_graph", return_value="# turtle"),
            patch("app.docs.analyze_handler.put_named_graph", return_value=True) as mock_put,
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
                return_value=fake_analyzer_instance,
            ),
            patch(
                "app.docs.analyze_handler.calculate_impact_score",
                return_value=42,
            ),
            patch("app.jobs.queue.get_connection", side_effect=conn_factory),
            patch("app.auth.audit.get_connection", side_effect=conn_factory),
            patch("app.docs.draft_model._connect", side_effect=conn_factory),
            patch("app.docs.upload.store_file") as mock_store,
        ):
            mock_store.return_value = MagicMock(
                storage_path="/tmp/fake.enc",
                size_bytes=20,
                filename="eelnou.docx",
            )

            # ----- Step 1: POST /drafts -----
            client = _authed_client()
            resp = client.post(
                "/drafts",
                data={"title": "E2E test eelnõu"},
                files={
                    "file": (
                        "eelnou.docx",
                        _docx_bytes(),
                        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    )
                },
            )
            assert resp.status_code == 303, (
                f"Upload should 303 to detail page, got {resp.status_code}: {resp.text[:200]}"
            )
            assert state.draft_row is not None, "Upload did not insert a draft row"
            assert state.draft_row[9] == "uploaded"
            # parse_draft job in the queue.
            parse_jobs = [j for j in state.background_jobs if j["job_type"] == "parse_draft"]
            assert len(parse_jobs) == 1
            assert parse_jobs[0]["payload"] == {"draft_id": str(state.draft_id)}

            # ----- Step 2: claim + run parse_draft -----
            from app.docs.parse_handler import parse_draft

            job = state.claim_next_pending()
            assert job is not None and job["job_type"] == "parse_draft"
            # Patch read_file at the parse_handler level so we don't
            # touch the disk.
            with patch("app.docs.parse_handler.read_file", return_value=b"plaintext bytes"):
                parse_result = parse_draft(
                    job["payload"],
                    attempt=1,
                    max_attempts=3,
                )
            assert parse_result["next_job"] == "extract_entities"
            assert state.draft_row[9] == "extracting"
            extract_jobs = [
                j for j in state.background_jobs if j["job_type"] == "extract_entities"
            ]
            assert len(extract_jobs) == 1

            # ----- Step 3: claim + run extract_entities -----
            from app.docs.extract_handler import extract_entities

            job = state.claim_next_pending()
            assert job is not None and job["job_type"] == "extract_entities"
            extract_result = extract_entities(
                job["payload"],
                attempt=1,
                max_attempts=3,
            )
            assert extract_result is not None
            assert extract_result["next_job"] == "analyze_impact"
            assert state.draft_row[9] == "analyzing"
            assert len(state.entities) == 1
            analyze_jobs = [j for j in state.background_jobs if j["job_type"] == "analyze_impact"]
            assert len(analyze_jobs) == 1

            # ----- Step 4: claim + run analyze_impact -----
            from app.docs.analyze_handler import analyze_impact

            job = state.claim_next_pending()
            assert job is not None and job["job_type"] == "analyze_impact"
            analyze_result = analyze_impact(
                job["payload"],
                attempt=1,
                max_attempts=3,
            )
            assert analyze_result["impact_score"] == 42
            assert state.draft_row[9] == "ready"
            mock_put.assert_called_once()
            assert len(state.impact_reports) == 1

            # Patch the report's report_data column so the report page
            # has a valid JSON dict to deserialise — the analyzer wrote
            # the dataclass via dataclasses.asdict, but our state mock
            # stored the raw params tuple.
            # #618 PR-B: the INSERT params now carry draft_version_id at
            # index 2, so report_data lives at index 7 (was 6 pre-PR-B).
            report_row = list(state.impact_reports[-1])
            report_row[7] = json.dumps(
                {
                    "affected_entities": [],
                    "conflicts": [],
                    "gaps": [],
                    "eu_compliance": [],
                }
            )
            state.impact_reports[-1] = tuple(report_row)

            # ----- Step 5: GET /drafts/<id>/report -----
            report_resp = client.get(f"/drafts/{state.draft_id}/report")
            assert report_resp.status_code == 200, (
                f"Report page should 200; got {report_resp.status_code}: {report_resp.text[:200]}"
            )
            assert "Kokkuvõte" in report_resp.text
            assert "42/100" in report_resp.text

    def test_parse_draft_retry_gating(self, monkeypatch):
        """Exercise the #448 retry gate on parse_draft.

        Scenario:
            * A draft exists and is parked at ``status='parsing'``.
            * Attempts 1 and 2 fail transiently (Tika raises).
            * Attempt 3 (the final attempt) fails again.

        Assertions:
            * Attempts 1 and 2 re-raise AND leave the draft row in
              ``status='parsing'`` — the UI must not flash a misleading
              ``Ebaõnnestus`` state while a retry is still pending.
            * Only attempt 3 flips the row to ``failed``.
        """
        monkeypatch.setenv("APP_ENV", "development")
        monkeypatch.delenv("STORAGE_ENCRYPTION_KEY", raising=False)
        monkeypatch.delenv("TIKA_URL", raising=False)

        state = _State()
        # Pre-mint the draft id and seed a draft row already in the
        # ``parsing`` state so the retry harness doesn't have to walk
        # through the upload flow.
        draft_id = uuid.UUID("88888888-8888-8888-8888-888888888888")
        state.draft_id = draft_id
        now = datetime.now(UTC)
        state.draft_row = (
            draft_id,
            uuid.UUID(_USER_ID),
            uuid.UUID(_ORG_ID),
            "Retry test eelnõu",
            "eelnou.docx",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            20,
            "/tmp/fake.enc",
            f"https://data.riik.ee/ontology/estleg/drafts/{draft_id}",
            "parsing",  # index 9: already parked in parsing
            None,  # parsed_text_encrypted
            None,  # entity_count
            None,  # error_message
            now,
            now,
            now,  # last_accessed_at (#572)
            "eelnou",  # doc_type (#639)
            None,  # parent_vtk_id (#639)
            None,  # processing_completed_at (#670)
        )
        conn_factory = _make_conn_factory(state)

        from app.docs.parse_handler import parse_draft

        # Tika will raise every time the handler calls it. We patch
        # ``read_file`` so the handler reaches the Tika call without
        # touching the disk.
        fake_tika = MagicMock()
        fake_tika.extract_text.side_effect = RuntimeError("Tika down")

        with (
            patch("app.docs.parse_handler.get_connection", side_effect=conn_factory),
            patch("app.docs.parse_handler.get_default_tika_client", return_value=fake_tika),
            patch("app.docs.parse_handler.read_file", return_value=b"plaintext bytes"),
            patch("app.jobs.queue.get_connection", side_effect=conn_factory),
        ):
            payload = {"draft_id": str(draft_id)}

            # Attempt 1: transient failure. Handler must re-raise and
            # leave the draft in ``parsing`` (NOT ``failed``).
            with pytest.raises(RuntimeError, match="Tika down"):
                parse_draft(payload, attempt=1, max_attempts=3)
            assert state.draft_row[9] == "parsing", (
                "attempt 1 must NOT flip the draft to failed — retry is pending"
            )

            # Attempt 2: still transient. Same guarantees.
            with pytest.raises(RuntimeError, match="Tika down"):
                parse_draft(payload, attempt=2, max_attempts=3)
            assert state.draft_row[9] == "parsing", (
                "attempt 2 must NOT flip the draft to failed — retry is pending"
            )

            # Attempt 3: final attempt. Handler still re-raises (the
            # worker depends on the exception to consume the retry
            # budget) BUT the draft row is now marked ``failed`` so
            # the UI surfaces the permanent error.
            with pytest.raises(RuntimeError, match="Tika down"):
                parse_draft(payload, attempt=3, max_attempts=3)
            assert state.draft_row[9] == "failed", (
                "final attempt must flip the draft to failed so the UI surfaces the error"
            )
            # #609: error_message now holds the actionable Estonian
            # user-facing string (the raw "Tika down" text lives in the
            # separate ``error_debug`` column, which the fake DB doesn't
            # model because routes.py never reads it).
            assert state.draft_row[12] is not None
            from app.docs.error_mapping import MSG_UNKNOWN

            assert state.draft_row[12] == MSG_UNKNOWN
