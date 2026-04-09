"""Unit tests for ``app.docs.extract_handler.extract_entities``.

Never touches a real Postgres connection, real Jena, or a real LLM:
every external dependency is mocked out so we can verify the control
flow, state transitions, and error handling in isolation.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from app.docs.draft_model import Draft
from app.docs.entity_extractor import ExtractedRef
from app.docs.extract_handler import extract_entities
from app.docs.reference_resolver import ResolvedRef


def _make_draft(
    draft_id: uuid.UUID | None = None,
    *,
    parsed_text: str | None = "§ 1. Test. KarS § 133.",
    status: str = "uploaded",
) -> Draft:
    now = datetime.now(UTC)
    return Draft(
        id=draft_id or uuid.UUID("11111111-1111-1111-1111-111111111111"),
        user_id=uuid.UUID("22222222-2222-2222-2222-222222222222"),
        org_id=uuid.UUID("33333333-3333-3333-3333-333333333333"),
        title="Test eelnõu",
        filename="eelnou.docx",
        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        file_size=1024,
        storage_path="/tmp/ciphertext.enc",
        graph_uri="https://data.riik.ee/ontology/estleg/drafts/test",
        status=status,
        parsed_text=parsed_text,
        entity_count=None,
        error_message=None,
        created_at=now,
        updated_at=now,
    )


class _ConnectCM:
    """Context-manager wrapper around a cursor-ish mock."""

    def __init__(self, conn: MagicMock):
        self.conn = conn

    def __enter__(self) -> MagicMock:
        return self.conn

    def __exit__(self, *_: Any) -> bool:
        return False


def _ref(text: str = "KarS § 133", rtype: str = "provision") -> ExtractedRef:
    return ExtractedRef(
        ref_text=text,
        ref_type=rtype,
        confidence=0.9,
        location={"chunk": 0, "offset": 0},
    )


class TestHappyPath:
    def test_happy_path_inserts_entities_and_enqueues_next_job(self):
        draft_id = uuid.UUID("11111111-1111-1111-1111-111111111111")
        draft = _make_draft(draft_id=draft_id, parsed_text="§ 1. Test.")

        # Four conn mocks for four ``with get_connection()`` blocks:
        # 1) initial get_draft
        # 2) status→extracting
        # 3) #469: DELETE FROM draft_entities for retry cleanup
        # 4) inserts + commit
        mock_conns = [MagicMock(), MagicMock(), MagicMock(), MagicMock()]
        # Status update (block 2) must report rowcount > 0 so
        # update_draft_status returns True.
        mock_conns[1].execute.return_value.rowcount = 1

        extracted = [_ref("KarS § 133"), _ref("TsÜS § 12"), _ref("KarS § 1")]
        resolved = [
            ResolvedRef(
                extracted=extracted[0],
                entity_uri="https://data.riik.ee/ontology/estleg#KarS_Par_133",
                matched_label="KarS § 133",
                match_score=1.0,
            ),
            ResolvedRef(
                extracted=extracted[1],
                entity_uri="https://data.riik.ee/ontology/estleg#TsUS_Par_12",
                matched_label="TsÜS § 12",
                match_score=1.0,
            ),
            ResolvedRef(
                extracted=extracted[2],
                entity_uri=None,
                matched_label=None,
                match_score=0.0,
            ),
        ]

        mock_queue = MagicMock()

        with (
            patch("app.docs.extract_handler.get_connection") as mock_get_conn,
            patch("app.docs.extract_handler.get_draft") as mock_get_draft,
            patch("app.docs.extract_handler.extract_refs_from_text") as mock_extract,
            patch("app.docs.extract_handler.resolve_refs") as mock_resolve,
            patch("app.docs.extract_handler.JobQueue") as mock_queue_cls,
        ):
            mock_get_conn.side_effect = [_ConnectCM(c) for c in mock_conns]
            mock_get_draft.return_value = draft
            mock_extract.return_value = extracted
            mock_resolve.return_value = resolved
            mock_queue_cls.return_value = mock_queue

            result = extract_entities({"draft_id": str(draft_id)})

        # Return summary is correct.
        assert result == {
            "draft_id": str(draft_id),
            "extracted": 3,
            "resolved": 2,
            "next_job": "analyze_impact",
        }

        # Three INSERT ... draft_entities calls + one UPDATE drafts
        # on the fourth connection.
        insert_exec = mock_conns[3].execute
        # 3 inserts + 1 update = 4 calls
        assert insert_exec.call_count == 4
        insert_calls = [
            call
            for call in insert_exec.call_args_list
            if "insert into draft_entities" in call.args[0].lower()
        ]
        assert len(insert_calls) == 3

        # The UPDATE drafts call must set entity_count=3 and status='analyzing'.
        update_calls = [
            call for call in insert_exec.call_args_list if "update drafts" in call.args[0].lower()
        ]
        assert len(update_calls) == 1
        update_sql, update_params = update_calls[0].args
        assert "status = 'analyzing'" in update_sql
        assert update_params[0] == 3  # entity_count
        assert update_params[1] == str(draft_id)

        # Commit happened on the inserts connection.
        mock_conns[3].commit.assert_called_once()

        # #469: the retry-cleanup DELETE ran on conn 3 (the dedicated
        # pre-extract cleanup block). Verify it used the draft id.
        cleanup_calls = [
            call
            for call in mock_conns[2].execute.call_args_list
            if "delete from draft_entities" in call.args[0].lower()
        ]
        assert len(cleanup_calls) == 1
        assert cleanup_calls[0].args[1] == (str(draft_id),)

        # Next job was enqueued.
        mock_queue.enqueue.assert_called_once_with(
            "analyze_impact",
            {"draft_id": str(draft_id)},
            priority=0,
        )

    def test_json_location_is_serialised(self):
        draft_id = uuid.UUID("11111111-1111-1111-1111-111111111111")
        draft = _make_draft(draft_id=draft_id)
        mock_conns = [MagicMock(), MagicMock(), MagicMock(), MagicMock()]
        mock_conns[1].execute.return_value.rowcount = 1

        location = {"chunk": 5, "offset": 1234, "extra": "meta"}
        extracted = [
            ExtractedRef(
                ref_text="KarS § 133",
                ref_type="provision",
                confidence=0.8,
                location=location,
            )
        ]
        resolved = [
            ResolvedRef(
                extracted=extracted[0],
                entity_uri="urn:test",
                matched_label=None,
                match_score=1.0,
            )
        ]

        with (
            patch("app.docs.extract_handler.get_connection") as mock_get_conn,
            patch("app.docs.extract_handler.get_draft") as mock_get_draft,
            patch(
                "app.docs.extract_handler.extract_refs_from_text",
                return_value=extracted,
            ),
            patch("app.docs.extract_handler.resolve_refs", return_value=resolved),
            patch("app.docs.extract_handler.JobQueue"),
        ):
            mock_get_conn.side_effect = [_ConnectCM(c) for c in mock_conns]
            mock_get_draft.return_value = draft

            extract_entities({"draft_id": str(draft_id)})

        # Find the insert call and make sure location was json.dumps'd.
        insert_calls = [
            call
            for call in mock_conns[3].execute.call_args_list
            if "insert into draft_entities" in call.args[0].lower()
        ]
        assert len(insert_calls) == 1
        params = insert_calls[0].args[1]
        # Location is the last positional param.
        assert params[-1] == json.dumps(location)

    def test_retry_cleanup_clears_prior_partial_rows(self):
        """#469: on retry, the handler must DELETE pre-existing rows.

        Simulates the scenario where a previous attempt inserted
        ``draft_entities`` rows but then failed before the final
        UPDATE. The retry should wipe those rows before the extractor
        runs so duplicates never reach the database.
        """
        draft_id = uuid.UUID("11111111-1111-1111-1111-111111111111")
        draft = _make_draft(draft_id=draft_id)

        mock_conns = [MagicMock(), MagicMock(), MagicMock(), MagicMock()]
        mock_conns[1].execute.return_value.rowcount = 1

        extracted = [_ref("KarS § 133")]
        resolved = [
            ResolvedRef(
                extracted=extracted[0],
                entity_uri="urn:test",
                matched_label=None,
                match_score=1.0,
            )
        ]

        with (
            patch("app.docs.extract_handler.get_connection") as mock_get_conn,
            patch("app.docs.extract_handler.get_draft", return_value=draft),
            patch(
                "app.docs.extract_handler.extract_refs_from_text",
                return_value=extracted,
            ),
            patch("app.docs.extract_handler.resolve_refs", return_value=resolved),
            patch("app.docs.extract_handler.JobQueue"),
        ):
            mock_get_conn.side_effect = [_ConnectCM(c) for c in mock_conns]
            # Retry attempt number 2 — the cleanup DELETE must still
            # run unconditionally regardless of the attempt counter.
            extract_entities(
                {"draft_id": str(draft_id)},
                attempt=2,
                max_attempts=3,
            )

        # The DELETE must have run on the dedicated cleanup connection
        # (conn 3), BEFORE any INSERTs on conn 4.
        cleanup_sql_calls = [
            call
            for call in mock_conns[2].execute.call_args_list
            if "delete from draft_entities" in call.args[0].lower()
        ]
        assert len(cleanup_sql_calls) == 1
        assert cleanup_sql_calls[0].args[1] == (str(draft_id),)
        # The cleanup connection must have committed.
        mock_conns[2].commit.assert_called_once()


class TestFailurePaths:
    def test_missing_draft_raises(self):
        draft_id = uuid.UUID("11111111-1111-1111-1111-111111111111")
        with (
            patch("app.docs.extract_handler.get_connection") as mock_get_conn,
            patch("app.docs.extract_handler.get_draft", return_value=None),
        ):
            mock_get_conn.return_value = _ConnectCM(MagicMock())
            with pytest.raises(ValueError, match="not found"):
                extract_entities({"draft_id": str(draft_id)})

    def test_empty_parsed_text_raises(self):
        draft_id = uuid.UUID("11111111-1111-1111-1111-111111111111")
        draft = _make_draft(draft_id=draft_id, parsed_text="   \n\t")
        with (
            patch("app.docs.extract_handler.get_connection") as mock_get_conn,
            patch("app.docs.extract_handler.get_draft", return_value=draft),
        ):
            mock_get_conn.return_value = _ConnectCM(MagicMock())
            with pytest.raises(ValueError, match="no parsed text"):
                extract_entities({"draft_id": str(draft_id)})

    def test_none_parsed_text_raises(self):
        draft_id = uuid.UUID("11111111-1111-1111-1111-111111111111")
        draft = _make_draft(draft_id=draft_id, parsed_text=None)
        with (
            patch("app.docs.extract_handler.get_connection") as mock_get_conn,
            patch("app.docs.extract_handler.get_draft", return_value=draft),
        ):
            mock_get_conn.return_value = _ConnectCM(MagicMock())
            with pytest.raises(ValueError, match="no parsed text"):
                extract_entities({"draft_id": str(draft_id)})

    def test_missing_draft_id_payload(self):
        with pytest.raises(ValueError, match="draft_id"):
            extract_entities({})

    def test_extractor_failure_marks_draft_failed_on_final_attempt(self):
        draft_id = uuid.UUID("11111111-1111-1111-1111-111111111111")
        draft = _make_draft(draft_id=draft_id)
        # Connections: 1) get_draft, 2) status→extracting,
        # 3) #469 cleanup DELETE, 4) status→failed
        mock_conns = [MagicMock(), MagicMock(), MagicMock(), MagicMock()]
        for c in mock_conns:
            c.execute.return_value.rowcount = 1

        with (
            patch("app.docs.extract_handler.get_connection") as mock_get_conn,
            patch("app.docs.extract_handler.get_draft", return_value=draft),
            patch(
                "app.docs.extract_handler.extract_refs_from_text",
                side_effect=RuntimeError("LLM boom"),
            ),
            patch("app.docs.extract_handler.resolve_refs") as mock_resolve,
        ):
            mock_get_conn.side_effect = [_ConnectCM(c) for c in mock_conns]

            with pytest.raises(RuntimeError, match="LLM boom"):
                extract_entities(
                    {"draft_id": str(draft_id)},
                    attempt=3,
                    max_attempts=3,
                )

            # Resolver must not have been called — extractor died first.
            mock_resolve.assert_not_called()

        # The fourth connection's execute should be the status=failed UPDATE.
        last_exec = mock_conns[3].execute.call_args_list
        # update_draft_status runs one UPDATE and commits.
        assert any("update drafts" in call.args[0].lower() for call in last_exec)
        assert any("failed" in str(call.args[1]) for call in last_exec if len(call.args) > 1)

    def test_extractor_failure_does_not_mark_failed_when_retry_pending(self):
        """#448: a transient extractor error on attempt 1 must not flip the draft."""
        draft_id = uuid.UUID("11111111-1111-1111-1111-111111111111")
        draft = _make_draft(draft_id=draft_id)
        # Three connections: 1) get_draft, 2) status→extracting,
        # 3) #469 cleanup DELETE. No fourth conn since the handler
        # should bail before the failed-status-update path.
        mock_conns = [MagicMock(), MagicMock(), MagicMock()]
        for c in mock_conns:
            c.execute.return_value.rowcount = 1

        with (
            patch("app.docs.extract_handler.get_connection") as mock_get_conn,
            patch("app.docs.extract_handler.get_draft", return_value=draft),
            patch(
                "app.docs.extract_handler.extract_refs_from_text",
                side_effect=RuntimeError("LLM boom"),
            ),
        ):
            mock_get_conn.side_effect = [_ConnectCM(c) for c in mock_conns]

            with pytest.raises(RuntimeError, match="LLM boom"):
                extract_entities(
                    {"draft_id": str(draft_id)},
                    attempt=1,
                    max_attempts=3,
                )

        # get_draft + extracting transition + cleanup DELETE = 3 conns.
        # No fourth connection was opened for a failed-status update.
        assert mock_get_conn.call_count == 3

    def test_resolver_failure_marks_draft_failed_on_final_attempt(self):
        draft_id = uuid.UUID("11111111-1111-1111-1111-111111111111")
        draft = _make_draft(draft_id=draft_id)
        # 1) get_draft, 2) status→extracting, 3) cleanup, 4) status→failed
        mock_conns = [MagicMock(), MagicMock(), MagicMock(), MagicMock()]
        for c in mock_conns:
            c.execute.return_value.rowcount = 1

        with (
            patch("app.docs.extract_handler.get_connection") as mock_get_conn,
            patch("app.docs.extract_handler.get_draft", return_value=draft),
            patch(
                "app.docs.extract_handler.extract_refs_from_text",
                return_value=[_ref()],
            ),
            patch(
                "app.docs.extract_handler.resolve_refs",
                side_effect=RuntimeError("resolver down"),
            ),
        ):
            mock_get_conn.side_effect = [_ConnectCM(c) for c in mock_conns]

            with pytest.raises(RuntimeError, match="resolver down"):
                extract_entities(
                    {"draft_id": str(draft_id)},
                    attempt=3,
                    max_attempts=3,
                )

        last_exec = mock_conns[3].execute.call_args_list
        assert any("update drafts" in call.args[0].lower() for call in last_exec)
