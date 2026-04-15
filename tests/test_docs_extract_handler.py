"""Unit tests for ``app.docs.extract_handler.extract_entities``.

Never touches a real Postgres connection, real Jena, or a real LLM:
every external dependency is mocked out so we can verify the control
flow, state transitions, and error handling in isolation.

``parsed_text_encrypted`` is a BYTEA column (migration 006). Tests that
exercise the happy path patch ``app.docs.extract_handler.decrypt_text``
so they do not need a real Fernet key. Tests that verify the
``None``-encrypted-column guard pass ``parsed_text_encrypted=None``
directly — the handler raises before it ever calls ``decrypt_text``.
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

# Sentinel ciphertext used in tests that need a non-None bytes value.
# The actual bytes don't matter because decrypt_text is always patched
# in those tests.
_FAKE_CIPHERTEXT = b"gAAAA_fake_ciphertext_bytes_for_tests"


def _make_draft(
    draft_id: uuid.UUID | None = None,
    *,
    parsed_text_encrypted: bytes | None = _FAKE_CIPHERTEXT,
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
        parsed_text_encrypted=parsed_text_encrypted,
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
        draft = _make_draft(draft_id=draft_id)

        # #626: DELETE + INSERT + UPDATE now run in a SINGLE
        # ``with get_connection()`` block, so the happy path opens
        # three connections total:
        # 1) initial get_draft
        # 2) status→extracting
        # 3) combined delete + inserts + update
        mock_conns = [MagicMock(), MagicMock(), MagicMock()]
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
            patch(
                "app.docs.extract_handler.decrypt_text",
                return_value="§ 1. Test.",
            ),
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

        # Combined-transaction connection (index 2) receives:
        #   1 DELETE + 3 INSERTs + 1 UPDATE = 5 execute calls total.
        combined_exec = mock_conns[2].execute
        assert combined_exec.call_count == 5

        delete_calls = [
            call
            for call in combined_exec.call_args_list
            if "delete from draft_entities" in call.args[0].lower()
        ]
        assert len(delete_calls) == 1
        assert delete_calls[0].args[1] == (str(draft_id),)

        insert_calls = [
            call
            for call in combined_exec.call_args_list
            if "insert into draft_entities" in call.args[0].lower()
        ]
        assert len(insert_calls) == 3

        # The UPDATE drafts call must set entity_count=3 and status='analyzing'.
        update_calls = [
            call
            for call in combined_exec.call_args_list
            if "update drafts" in call.args[0].lower()
        ]
        assert len(update_calls) == 1
        update_sql, update_params = update_calls[0].args
        assert "status = 'analyzing'" in update_sql
        assert update_params[0] == 3  # entity_count
        assert update_params[1] == str(draft_id)

        # Exactly ONE commit for the combined transaction.
        mock_conns[2].commit.assert_called_once()

        # Ordering: the DELETE must run before the first INSERT so the
        # rollback semantics of #626 hold.
        sqls = [c.args[0].lower() for c in combined_exec.call_args_list]
        first_delete = next(i for i, s in enumerate(sqls) if "delete from draft_entities" in s)
        first_insert = next(i for i, s in enumerate(sqls) if "insert into draft_entities" in s)
        assert first_delete < first_insert

        # Next job was enqueued.
        mock_queue.enqueue.assert_called_once_with(
            "analyze_impact",
            {"draft_id": str(draft_id)},
            priority=0,
        )

    def test_json_location_is_serialised(self):
        draft_id = uuid.UUID("11111111-1111-1111-1111-111111111111")
        draft = _make_draft(draft_id=draft_id)
        # 3 connections: get_draft, status→extracting, combined tx.
        mock_conns = [MagicMock(), MagicMock(), MagicMock()]
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
                "app.docs.extract_handler.decrypt_text",
                return_value="KarS § 133 säte.",
            ),
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
            for call in mock_conns[2].execute.call_args_list
            if "insert into draft_entities" in call.args[0].lower()
        ]
        assert len(insert_calls) == 1
        params = insert_calls[0].args[1]
        # Location is the last positional param.
        assert params[-1] == json.dumps(location)

    def test_retry_cleanup_clears_prior_partial_rows(self):
        """#469 + #626: on retry, the handler must DELETE pre-existing rows.

        Simulates the scenario where a previous attempt inserted
        ``draft_entities`` rows but then failed before the final
        UPDATE. The retry should wipe those rows before the new inserts.

        #626 made DELETE + INSERT + UPDATE atomic, so the cleanup
        now lives on the same connection as the rest of the persist
        step rather than a dedicated transaction.
        """
        draft_id = uuid.UUID("11111111-1111-1111-1111-111111111111")
        draft = _make_draft(draft_id=draft_id)

        mock_conns = [MagicMock(), MagicMock(), MagicMock()]
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
                "app.docs.extract_handler.decrypt_text",
                return_value="KarS § 133 säte.",
            ),
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

        # The DELETE runs on the same connection as the inserts +
        # final UPDATE (conn index 2), before any INSERT.
        combined_exec = mock_conns[2].execute
        cleanup_sql_calls = [
            call
            for call in combined_exec.call_args_list
            if "delete from draft_entities" in call.args[0].lower()
        ]
        assert len(cleanup_sql_calls) == 1
        assert cleanup_sql_calls[0].args[1] == (str(draft_id),)

        # Exactly one commit — the combined transaction's single commit.
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
        """Encrypted column is present but decrypts to whitespace-only text."""
        draft_id = uuid.UUID("11111111-1111-1111-1111-111111111111")
        draft = _make_draft(draft_id=draft_id)  # non-None ciphertext
        with (
            patch("app.docs.extract_handler.get_connection") as mock_get_conn,
            patch("app.docs.extract_handler.get_draft", return_value=draft),
            patch(
                "app.docs.extract_handler.decrypt_text",
                return_value="   \n\t",
            ),
        ):
            mock_get_conn.return_value = _ConnectCM(MagicMock())
            with pytest.raises(ValueError, match="no parsed text"):
                extract_entities({"draft_id": str(draft_id)})

    def test_none_parsed_text_raises(self):
        """parsed_text_encrypted column is NULL — parse_draft was not run."""
        draft_id = uuid.UUID("11111111-1111-1111-1111-111111111111")
        draft = _make_draft(draft_id=draft_id, parsed_text_encrypted=None)
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
        # #626: connections are now
        #   1) get_draft
        #   2) status→extracting
        #   3) status→failed (opened inside the except branch)
        # The old #469 cleanup-DELETE connection is gone — DELETE is
        # part of the combined tx which never runs on the failure path.
        mock_conns = [MagicMock(), MagicMock(), MagicMock()]
        for c in mock_conns:
            c.execute.return_value.rowcount = 1

        with (
            patch("app.docs.extract_handler.get_connection") as mock_get_conn,
            patch("app.docs.extract_handler.get_draft", return_value=draft),
            patch(
                "app.docs.extract_handler.decrypt_text",
                return_value="§ 1. Test.",
            ),
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

        # The third connection's execute should be the failed UPDATE.
        failed_exec = mock_conns[2].execute.call_args_list
        assert any(
            "update drafts" in call.args[0].lower() and "status = 'failed'" in call.args[0].lower()
            for call in failed_exec
        )
        # #609: the first param is the user-facing Estonian message,
        # the second is the raw debug detail.
        failed_call = next(
            call for call in failed_exec if "status = 'failed'" in call.args[0].lower()
        )
        user_msg, debug_detail, draft_id_param = failed_call.args[1]
        assert isinstance(user_msg, str)
        assert "LLM boom" in debug_detail
        assert draft_id_param == str(draft_id)

    def test_extractor_failure_does_not_mark_failed_when_retry_pending(self):
        """#448: a transient extractor error on attempt 1 must not flip the draft.

        #626 removed the dedicated pre-extract cleanup connection, so
        the early-failure path now opens exactly 2 connections:
        ``get_draft`` and the status→``extracting`` transition.
        """
        draft_id = uuid.UUID("11111111-1111-1111-1111-111111111111")
        draft = _make_draft(draft_id=draft_id)
        mock_conns = [MagicMock(), MagicMock()]
        for c in mock_conns:
            c.execute.return_value.rowcount = 1

        with (
            patch("app.docs.extract_handler.get_connection") as mock_get_conn,
            patch("app.docs.extract_handler.get_draft", return_value=draft),
            patch(
                "app.docs.extract_handler.decrypt_text",
                return_value="§ 1. Test.",
            ),
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

        # get_draft + extracting transition = 2 conns.
        # No failed-status-update connection was opened.
        assert mock_get_conn.call_count == 2

    def test_resolver_failure_marks_draft_failed_on_final_attempt(self):
        draft_id = uuid.UUID("11111111-1111-1111-1111-111111111111")
        draft = _make_draft(draft_id=draft_id)
        # #626: 1) get_draft, 2) status→extracting, 3) status→failed.
        # No dedicated cleanup connection any more.
        mock_conns = [MagicMock(), MagicMock(), MagicMock()]
        for c in mock_conns:
            c.execute.return_value.rowcount = 1

        with (
            patch("app.docs.extract_handler.get_connection") as mock_get_conn,
            patch("app.docs.extract_handler.get_draft", return_value=draft),
            patch(
                "app.docs.extract_handler.decrypt_text",
                return_value="§ 1. Test.",
            ),
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

        last_exec = mock_conns[2].execute.call_args_list
        assert any(
            "update drafts" in call.args[0].lower() and "status = 'failed'" in call.args[0].lower()
            for call in last_exec
        )


# ---------------------------------------------------------------------------
# #626: DELETE + INSERT + UPDATE transaction atomicity
# ---------------------------------------------------------------------------


class TestCombinedTransaction:
    """Regression tests for #626 — ``draft_entities`` DELETE, INSERT,
    and ``drafts`` UPDATE must land atomically.

    Before #626, the DELETE ran in its own transaction before the
    extractor call. A crash between the DELETE commit and the later
    INSERT+UPDATE commit left ``drafts.entity_count`` pointing at the
    previous attempt's count while ``draft_entities`` was empty.
    """

    def test_mid_transaction_failure_does_not_commit(self):
        """Mock INSERT to raise halfway through and assert no commit fires.

        The contract the real psycopg connection would honour: no
        ``conn.commit()`` means the DELETE is rolled back too, so the
        draft_entities table is unchanged from whatever it held before
        this attempt. Here we assert that assumption at the call level —
        on a mid-transaction error the handler never calls ``commit()``
        on the combined-tx connection, which is what preserves
        atomicity in production.
        """
        draft_id = uuid.UUID("11111111-1111-1111-1111-111111111111")
        draft = _make_draft(draft_id=draft_id, status="extracting")

        combined_conn = MagicMock()
        combined_conn.execute.return_value.rowcount = 1

        # Make the INSERT fail; DELETE succeeds first (call #1).
        call_count = {"n": 0}

        def execute_side_effect(sql, *_a, **_kw):
            call_count["n"] += 1
            sql_lower = sql.lower()
            if "insert into draft_entities" in sql_lower:
                raise RuntimeError("Postgres connection dropped mid-insert")
            # DELETE and everything else return a normal mock.
            return MagicMock()

        combined_conn.execute.side_effect = execute_side_effect

        mock_conns = [MagicMock(), MagicMock(), combined_conn]
        mock_conns[1].execute.return_value.rowcount = 1

        extracted = [_ref("KarS § 133"), _ref("TsÜS § 12")]
        resolved = [
            ResolvedRef(
                extracted=extracted[0],
                entity_uri="urn:test-1",
                matched_label=None,
                match_score=1.0,
            ),
            ResolvedRef(
                extracted=extracted[1],
                entity_uri="urn:test-2",
                matched_label=None,
                match_score=1.0,
            ),
        ]

        with (
            patch("app.docs.extract_handler.get_connection") as mock_get_conn,
            patch("app.docs.extract_handler.get_draft", return_value=draft),
            patch(
                "app.docs.extract_handler.decrypt_text",
                return_value="§ 1. Test.",
            ),
            patch(
                "app.docs.extract_handler.extract_refs_from_text",
                return_value=extracted,
            ),
            patch("app.docs.extract_handler.resolve_refs", return_value=resolved),
        ):
            mock_get_conn.side_effect = [_ConnectCM(c) for c in mock_conns]

            # Earlier attempt: retry still has budget, handler must
            # re-raise without flipping the draft to ``failed``.
            with pytest.raises(RuntimeError, match="dropped mid-insert"):
                extract_entities(
                    {"draft_id": str(draft_id)},
                    attempt=1,
                    max_attempts=3,
                )

        # The combined-tx connection must NOT have committed.
        # This is the load-bearing #626 assertion: the DELETE sitting
        # in the same txn rolls back with everything else, so the
        # table is unchanged from whatever it was before the attempt.
        combined_conn.commit.assert_not_called()

        # #448 retry gating: no status='failed' UPDATE ran because
        # the retry budget was not exhausted. Only 3 connections
        # were opened (get_draft, extracting, combined-tx).
        assert mock_get_conn.call_count == 3

    def test_mid_transaction_failure_final_attempt_marks_failed(self):
        """On the last retry attempt, a mid-transaction crash still
        ends in ``status='failed'`` — but the combined-tx connection
        itself never commits, so draft_entities stays consistent.
        """
        draft_id = uuid.UUID("11111111-1111-1111-1111-111111111111")
        draft = _make_draft(draft_id=draft_id)

        combined_conn = MagicMock()
        combined_conn.execute.return_value.rowcount = 1

        def execute_side_effect(sql, *_a, **_kw):
            if "insert into draft_entities" in sql.lower():
                raise RuntimeError("mid-insert crash")
            return MagicMock()

        combined_conn.execute.side_effect = execute_side_effect

        # 1) get_draft, 2) extracting, 3) combined-tx (crashes),
        # 4) failed-status UPDATE (on final attempt).
        mock_conns = [MagicMock(), MagicMock(), combined_conn, MagicMock()]
        for c in mock_conns:
            if c is not combined_conn:
                c.execute.return_value.rowcount = 1

        extracted = [_ref("KarS § 133")]
        resolved = [
            ResolvedRef(
                extracted=extracted[0],
                entity_uri="urn:x",
                matched_label=None,
                match_score=1.0,
            )
        ]

        with (
            patch("app.docs.extract_handler.get_connection") as mock_get_conn,
            patch("app.docs.extract_handler.get_draft", return_value=draft),
            patch(
                "app.docs.extract_handler.decrypt_text",
                return_value="§ 1. Test.",
            ),
            patch(
                "app.docs.extract_handler.extract_refs_from_text",
                return_value=extracted,
            ),
            patch("app.docs.extract_handler.resolve_refs", return_value=resolved),
        ):
            mock_get_conn.side_effect = [_ConnectCM(c) for c in mock_conns]

            with pytest.raises(RuntimeError, match="mid-insert crash"):
                extract_entities(
                    {"draft_id": str(draft_id)},
                    attempt=3,
                    max_attempts=3,
                )

        # Combined-tx connection must not have committed.
        combined_conn.commit.assert_not_called()

        # Dedicated failed-status UPDATE ran on conn index 3 and
        # committed (independent transaction, doesn't touch
        # draft_entities).
        failed_exec = mock_conns[3].execute.call_args_list
        assert any("status = 'failed'" in call.args[0].lower() for call in failed_exec)
        mock_conns[3].commit.assert_called_once()
