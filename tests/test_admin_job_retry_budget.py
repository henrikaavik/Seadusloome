"""#852 — admin job retry must respect the retry budget + confirm.

``_retry_job`` used to reset ``attempts = 0``, which made a poison job
retryable forever (each admin click restored the FULL ``max_attempts``
budget). The attempt counter is now preserved: a failed job already has
``attempts >= max_attempts``, so one click grants exactly one extra
attempt and the climbing counter doubles as the audit trail. The retry
button also gets an ``hx-confirm`` like the adjacent purge button.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch


class TestRetryPreservesBudget:
    @patch("app.admin.job_monitor._connect")
    def test_retry_does_not_reset_attempts(self, mock_connect: MagicMock):
        from app.admin.job_monitor import _retry_job

        mock_conn = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)
        mock_conn.execute.return_value.rowcount = 1

        assert _retry_job(42) is True

        sql = mock_conn.execute.call_args[0][0]
        # The poison-job hole: resetting attempts restored the whole
        # budget on every click. The counter must be left alone.
        assert "attempts" not in sql, f"retry must not touch attempts: {sql}"
        # The rest of the reset contract is unchanged.
        assert "SET status = 'pending'" in sql
        assert "error_message = NULL" in sql
        assert "claimed_by = NULL" in sql
        assert "WHERE id = %s AND status = 'failed'" in sql

    @patch("app.admin.job_monitor._connect")
    def test_retry_only_targets_failed_jobs(self, mock_connect: MagicMock):
        from app.admin.job_monitor import _retry_job

        mock_conn = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)
        mock_conn.execute.return_value.rowcount = 0

        assert _retry_job(999) is False


class TestRetryButtonConfirm:
    def _render_jobs_table(self) -> str:
        from fasthtml.common import to_xml

        from app.admin.job_monitor import _jobs_table

        job = {
            "id": 42,
            "job_type": "drafter_draft",
            "status": "failed",
            "error_message": "boom",
            "attempts": 3,
            "max_attempts": 3,
            "finished_at": None,
        }
        return to_xml(_jobs_table([job], "failed"))

    def test_retry_button_has_confirm_dialog(self):
        html = self._render_jobs_table()
        assert "/admin/jobs/42/retry" in html
        # The retry button must carry a confirm like the purge button —
        # it is a state-mutating action on a permanently-failed job.
        assert "hx-confirm" in html
        assert "Kas olete kindel?" in html

    def test_attempt_counter_is_displayed(self):
        """The preserved counter is the admin-facing audit trail."""
        html = self._render_jobs_table()
        assert "3/3" in html
