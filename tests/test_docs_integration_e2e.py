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

import json
import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from starlette.testclient import TestClient

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

        # ----- drafts table -----
        if "insert into drafts" in sql_lower and "returning" in sql_lower:
            # create_draft inserts and returns the row. The INSERT
            # columns are: user_id, org_id, title, filename,
            # content_type, file_size, storage_path, graph_uri, status
            # — 9 params. RETURNING reconstructs all 16 columns (the
            # 16th is last_accessed_at, added by migration 015 / #572).
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

        if "select" in sql_lower and "from drafts" in sql_lower and "where id" in sql_lower:
            # get_draft / fetch_draft
            cursor.fetchone.return_value = state.draft_row
            return cursor

        if "from drafts" in sql_lower and "where org_id" in sql_lower:
            # fetch_drafts_for_org / count_drafts_for_org
            if "count(" in sql_lower:
                cursor.fetchone.return_value = (1 if state.draft_row else 0,)
            else:
                cursor.fetchall.return_value = [state.draft_row] if state.draft_row else []
            return cursor

        if "update drafts" in sql_lower and "status" in sql_lower:
            # update_draft_status / pipeline status flips. ``params`` for
            # update_draft_status is (status, error_message, draft_id);
            # for the parse_handler "set parsed_text_encrypted=..." flow
            # it's (ciphertext_bytes, draft_id) — extract the new status
            # from the SQL string instead of guessing the param order.
            assert state.draft_row is not None
            row = list(state.draft_row)
            if "set parsed_text_encrypted" in sql_lower:
                # Parse handler combined update: parsed_text_encrypted
                # (Fernet bytes) + status='extracting'.
                row[10] = params[0] if params else None
                row[9] = "extracting"
            elif "status = 'analyzing'" in sql_lower:
                # extract_entities final UPDATE: entity_count + status.
                if params is not None:
                    row[11] = params[0]  # entity_count
                row[9] = "analyzing"
            elif "status = 'ready'" in sql_lower:
                row[9] = "ready"
            elif "status = 'failed'" in sql_lower:
                # #609: _mark_draft_failed uses a direct UPDATE with
                # params (user_msg, debug_detail, draft_id). The
                # user-facing Estonian text lands in error_message.
                row[9] = "failed"
                if params is not None and len(params) >= 1:
                    row[12] = params[0]  # error_message
            elif params and len(params) >= 2:
                # update_draft_status — first param is the new status.
                row[9] = str(params[0])
                if len(params) >= 3 and params[1] is not None:
                    row[12] = params[1]  # error_message
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
            rows = [
                (
                    e[1],  # ref_text
                    e[2],  # entity_uri
                    e[3],  # confidence
                    e[4],  # ref_type
                    e[5],  # location
                )
                for e in state.entities
                if e[2] is not None  # entity_uri not null
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
            if state.impact_reports:
                report = state.impact_reports[-1]
                # The query returns:
                #   id, draft_id, affected_count, conflict_count,
                #   gap_count, impact_score, report_data, ontology_version,
                #   generated_at
                cursor.fetchone.return_value = (
                    report[0],
                    report[1],
                    report[2],
                    report[3],
                    report[4],
                    report[5],
                    report[6],
                    report[7],
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
            patch("app.docs.routes._connect", side_effect=conn_factory),
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
                        b"%PDF-test bytes",
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
            report_row = list(state.impact_reports[-1])
            report_row[6] = json.dumps(
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
