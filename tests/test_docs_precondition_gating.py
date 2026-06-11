"""#852 E6 — pipeline preconditions are inside the retry-gated region.

``extract_entities`` and ``analyze_impact`` used to run their
draft-load/decrypt/empty-text preconditions BEFORE the retry-gated
``try``, so a ``DecryptionError`` (or a DB hiccup during the load)
never flipped the draft to ``failed`` even on the final attempt — the
draft sat in ``extracting``/``analyzing`` forever with no retry button.

These tests prove precondition failures now consume the retry budget
like any other handler error and mark the draft ``failed`` on the last
attempt (which is exactly the state the ``POST /drafts/{id}/retry``
button requires — see ``app.docs.retry_handler``).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from app.docs.draft_model import Draft
from app.storage import DecryptionError

_DRAFT_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
_FAKE_CIPHERTEXT = b"gAAAA_fake_ciphertext_bytes_for_tests"


def _make_draft(
    *,
    parsed_text_encrypted: bytes | None = _FAKE_CIPHERTEXT,
    status: str = "uploaded",
) -> Draft:
    now = datetime.now(UTC)
    return Draft(
        id=_DRAFT_ID,
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
    def __init__(self, conn: MagicMock):
        self.conn = conn

    def __enter__(self) -> MagicMock:
        return self.conn

    def __exit__(self, *_: Any) -> bool:
        return False


# ---------------------------------------------------------------------------
# extract_entities
# ---------------------------------------------------------------------------


class TestExtractPreconditionGating:
    def _run(self, *, attempt: int, max_attempts: int = 3):
        from app.docs.extract_handler import extract_entities

        return extract_entities(
            {"draft_id": str(_DRAFT_ID)}, attempt=attempt, max_attempts=max_attempts
        )

    def test_decrypt_failure_final_attempt_marks_draft_failed(self):
        """The headline E6 case: DecryptionError → draft failed, retryable."""
        draft = _make_draft()
        with (
            patch("app.docs.extract_handler.get_connection") as mock_get_conn,
            patch("app.docs.extract_handler.get_draft", return_value=draft),
            patch(
                "app.docs.extract_handler.decrypt_text",
                side_effect=DecryptionError("bad key"),
            ),
            patch("app.docs.extract_handler.update_draft_status") as mock_update,
        ):
            mock_get_conn.return_value = _ConnectCM(MagicMock())

            with pytest.raises(DecryptionError):
                self._run(attempt=3, max_attempts=3)

        mock_update.assert_called_once()
        args = mock_update.call_args
        assert args.args[2] == "failed"
        # The user-facing message column is populated (retry button needs
        # a failed status; the message tells the user what to do).
        assert args.args[3]

    def test_decrypt_failure_earlier_attempt_does_not_flip(self):
        """#448 gating still holds: budget remaining → no failed flip."""
        draft = _make_draft()
        with (
            patch("app.docs.extract_handler.get_connection") as mock_get_conn,
            patch("app.docs.extract_handler.get_draft", return_value=draft),
            patch(
                "app.docs.extract_handler.decrypt_text",
                side_effect=DecryptionError("bad key"),
            ),
            patch("app.docs.extract_handler.update_draft_status") as mock_update,
        ):
            mock_get_conn.return_value = _ConnectCM(MagicMock())

            with pytest.raises(DecryptionError):
                self._run(attempt=1, max_attempts=3)

        mock_update.assert_not_called()

    def test_missing_parsed_text_final_attempt_marks_draft_failed(self):
        draft = _make_draft(parsed_text_encrypted=None)
        with (
            patch("app.docs.extract_handler.get_connection") as mock_get_conn,
            patch("app.docs.extract_handler.get_draft", return_value=draft),
            patch("app.docs.extract_handler.update_draft_status") as mock_update,
        ):
            mock_get_conn.return_value = _ConnectCM(MagicMock())

            with pytest.raises(ValueError, match="no parsed text"):
                self._run(attempt=3, max_attempts=3)

        mock_update.assert_called_once()
        assert mock_update.call_args.args[2] == "failed"

    def test_missing_draft_final_attempt_raises_without_exploding(self):
        """No draft row → the failure UPDATE matches zero rows; the
        handler still raises so the JOB is marked failed."""
        with (
            patch("app.docs.extract_handler.get_connection") as mock_get_conn,
            patch("app.docs.extract_handler.get_draft", return_value=None),
            patch("app.docs.extract_handler.update_draft_status") as mock_update,
        ):
            mock_get_conn.return_value = _ConnectCM(MagicMock())

            with pytest.raises(ValueError, match="not found"):
                self._run(attempt=3, max_attempts=3)

        # Best-effort flip attempted (harmless zero-row UPDATE).
        mock_update.assert_called_once()


# ---------------------------------------------------------------------------
# analyze_impact
# ---------------------------------------------------------------------------


class TestAnalyzePreconditionGating:
    def _run(self, *, attempt: int, max_attempts: int = 3):
        from app.docs.analyze_handler import analyze_impact

        return analyze_impact(
            {"draft_id": str(_DRAFT_ID)}, attempt=attempt, max_attempts=max_attempts
        )

    def test_load_failure_final_attempt_marks_draft_failed(self):
        """A DB error during the draft/entity load (pre-#852 it ran
        before the gated try) must flip the draft on the final attempt."""
        draft = _make_draft(status="analyzing")
        load_conn = MagicMock()
        load_conn.execute.side_effect = RuntimeError("db hiccup during load")
        fail_conn = MagicMock()

        with (
            patch("app.docs.analyze_handler.get_connection") as mock_get_conn,
            patch("app.docs.analyze_handler.get_draft", return_value=draft),
            patch("app.docs.analyze_handler.update_draft_status") as mock_update,
        ):
            mock_get_conn.side_effect = [
                _ConnectCM(load_conn),
                _ConnectCM(fail_conn),  # opened by _mark_draft_failed
            ]

            with pytest.raises(RuntimeError, match="db hiccup"):
                self._run(attempt=3, max_attempts=3)

        mock_update.assert_called_once()
        assert mock_update.call_args.args[2] == "failed"

    def test_load_failure_earlier_attempt_does_not_flip(self):
        draft = _make_draft(status="analyzing")
        load_conn = MagicMock()
        load_conn.execute.side_effect = RuntimeError("db hiccup during load")

        with (
            patch("app.docs.analyze_handler.get_connection") as mock_get_conn,
            patch("app.docs.analyze_handler.get_draft", return_value=draft),
            patch("app.docs.analyze_handler.update_draft_status") as mock_update,
        ):
            mock_get_conn.side_effect = [_ConnectCM(load_conn)]

            with pytest.raises(RuntimeError, match="db hiccup"):
                self._run(attempt=1, max_attempts=3)

        mock_update.assert_not_called()

    def test_missing_draft_final_attempt_marks_failed_best_effort(self):
        with (
            patch("app.docs.analyze_handler.get_connection") as mock_get_conn,
            patch("app.docs.analyze_handler.get_draft", return_value=None),
            patch("app.docs.analyze_handler.update_draft_status") as mock_update,
        ):
            mock_get_conn.return_value = _ConnectCM(MagicMock())

            with pytest.raises(ValueError, match="not found"):
                self._run(attempt=3, max_attempts=3)

        # Zero-row UPDATE — harmless, but the attempt is made so a
        # still-existing row can never be left stuck.
        mock_update.assert_called_once()
