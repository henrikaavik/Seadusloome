"""#845 (B4b) — drafting_sessions join the 90-day retention sweep.

``drafting_sessions`` rows hold encrypted draft clauses + legislative
intent but were excluded from the archive-warning lifecycle (the daily
scan only covered ``drafts``). ``scan_stale_drafting_sessions`` mirrors
``scan_stale_drafts``: stale **active** sessions (by ``updated_at``)
get a ``drafting_session_archive_warning`` notification with the same
NOT-EXISTS dedup window; migration 038 allows the new type and indexes
the scan.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.drafter.session_model import DraftingSession
from app.jobs.archive_warning import scan_stale_drafting_sessions

_ORG_ID = "11111111-1111-1111-1111-111111111111"
_USER_ID = "33333333-3333-3333-3333-333333333333"
_SESSION_ID = uuid.UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")


def _make_session(
    *,
    intent: str | None = "Reguleerida droonide kasutamist",
    updated_at: datetime | None = None,
) -> DraftingSession:
    now = datetime.now(UTC)
    return DraftingSession(
        id=_SESSION_ID,
        user_id=uuid.UUID(_USER_ID),
        org_id=uuid.UUID(_ORG_ID),
        workflow_type="full_law",
        current_step=3,
        intent=intent,
        clarifications=[],
        research_data_encrypted=None,
        proposed_structure=None,
        draft_content_encrypted=b"encrypted",
        integrated_draft_id=None,
        status="active",
        created_at=now - timedelta(days=120),
        updated_at=updated_at or (now - timedelta(days=100)),
    )


def _row_from_session(session: DraftingSession) -> tuple:
    """Shape a session back into the raw row tuple ``_row_to_session``
    consumes — kept in lockstep with ``_SESSION_COLUMNS``."""
    return (
        str(session.id),
        str(session.user_id),
        str(session.org_id),
        session.workflow_type,
        session.current_step,
        session.intent,
        session.clarifications,
        session.research_data_encrypted,
        session.proposed_structure,
        session.draft_content_encrypted,
        None,
        session.status,
        session.created_at,
        session.updated_at,
    )


def _wire_connection(mock_conn: MagicMock, rows: list[tuple]) -> MagicMock:
    """Configure the patched ``get_connection`` to yield *rows* and
    return the inner connection mock (same shape as the drafts-scan
    tests in ``tests/test_archive_warning.py``)."""
    cursor = MagicMock()
    cursor.fetchall.return_value = rows
    conn = MagicMock()
    conn.execute.return_value = cursor
    mock_conn.return_value.__enter__ = MagicMock(return_value=conn)
    mock_conn.return_value.__exit__ = MagicMock(return_value=False)
    return conn


class TestScanStaleDraftingSessions:
    @patch("app.jobs.archive_warning.notify")
    @patch("app.jobs.archive_warning.get_connection")
    def test_stale_sessions_get_notified(self, mock_conn, mock_notify):
        stale = _make_session()
        conn = _wire_connection(mock_conn, [_row_from_session(stale)])

        result = scan_stale_drafting_sessions(threshold_days=90, dedupe_window_days=7)

        mock_notify.assert_called_once()
        kwargs = mock_notify.call_args.kwargs
        assert kwargs["type"] == "drafting_session_archive_warning"
        assert kwargs["user_id"] == stale.user_id
        assert kwargs["link"] == f"/drafter/{stale.id}"
        assert kwargs["metadata"]["session_id"] == str(stale.id)
        assert kwargs["metadata"]["workflow_type"] == "full_law"
        assert kwargs["metadata"]["updated_at"] is not None
        # Estonian, references the intent so the owner knows which one.
        assert "Reguleerida droonide kasutamist" in kwargs["body"]

        assert len(result) == 1
        assert result[0]["session_id"] == str(stale.id)
        assert result[0]["user_id"] == str(stale.user_id)
        assert result[0]["org_id"] == str(stale.org_id)

        # Threshold + dedup window forwarded to the SQL layer.
        assert conn.execute.call_args.args[1] == (90, 7)

    @patch("app.jobs.archive_warning.notify")
    @patch("app.jobs.archive_warning.get_connection")
    def test_sql_scopes_to_active_with_dedupe(self, mock_conn, mock_notify):
        conn = _wire_connection(mock_conn, [])

        result = scan_stale_drafting_sessions()

        assert result == []
        mock_notify.assert_not_called()
        sql = conn.execute.call_args.args[0]
        # Only active sessions are swept (completed/abandoned are terminal).
        assert "status = 'active'" in sql
        # Same NOT EXISTS dedup pattern as the drafts scan.
        assert "NOT EXISTS" in sql
        assert "drafting_session_archive_warning" in sql
        assert "metadata->>'session_id'" in sql
        # psycopg-safe interval substitution (no ``interval %s``).
        assert "make_interval(days => %s)" in sql
        assert "interval %s" not in sql

    @patch("app.jobs.archive_warning.notify")
    @patch("app.jobs.archive_warning.get_connection")
    def test_missing_intent_falls_back_to_generic_label(self, mock_conn, mock_notify):
        stale = _make_session(intent=None)
        _wire_connection(mock_conn, [_row_from_session(stale)])

        scan_stale_drafting_sessions()

        body = mock_notify.call_args.kwargs["body"]
        assert '"Eelnõu"' in body

    @patch("app.jobs.archive_warning.notify")
    @patch("app.jobs.archive_warning.get_connection")
    def test_db_error_returns_empty_list(self, mock_conn, mock_notify):
        mock_conn.side_effect = RuntimeError("boom")
        assert scan_stale_drafting_sessions() == []
        mock_notify.assert_not_called()

    @patch("app.jobs.archive_warning.notify")
    @patch("app.jobs.archive_warning.get_connection")
    def test_bad_row_skipped_others_notified(self, mock_conn, mock_notify):
        stale = _make_session()
        _wire_connection(mock_conn, [("garbage",), _row_from_session(stale)])

        result = scan_stale_drafting_sessions()

        assert len(result) == 1
        assert result[0]["session_id"] == str(stale.id)
        mock_notify.assert_called_once()


class TestSchedulerRunsBothScans:
    def test_scheduler_loop_calls_session_scan(self):
        """The daily tick must sweep BOTH tables; a drafts-scan failure
        must not starve the sessions scan."""
        import threading

        from app.jobs import archive_warning

        stop = threading.Event()
        calls: list[str] = []

        def _drafts_scan() -> list:
            calls.append("drafts")
            raise RuntimeError("drafts scan boom")

        def _sessions_scan() -> list:
            calls.append("sessions")
            stop.set()
            return []

        with (
            patch.object(archive_warning, "scan_stale_drafts", side_effect=_drafts_scan),
            patch.object(
                archive_warning,
                "scan_stale_drafting_sessions",
                side_effect=_sessions_scan,
            ),
            patch.object(archive_warning, "_INITIAL_DELAY_SECONDS", 0.0),
        ):
            archive_warning._scheduler_loop(stop, interval_seconds=3600)

        assert calls == ["drafts", "sessions"]


_MIGRATION_038 = (
    Path(__file__).parent.parent / "migrations" / "038_drafting_session_archive_warning.sql"
)


def _migration_038_allowed_types() -> set[str]:
    """Parse the type list out of migration 038's recreated CHECK."""
    match = re.search(r"check \(type in \((.*?)\)\)", _MIGRATION_038.read_text(), re.DOTALL)
    assert match, "could not locate the CHECK (type in (...)) list in migration 038"
    return set(re.findall(r"'([a-z_]+)'", match.group(1)))


def _emitted_notification_types() -> set[str]:
    """Every ``type="..."`` literal in modules that call the notify layer.

    Scans all of ``app/`` but only extracts from files that actually
    invoke ``notify(`` / ``create_notification(`` — factory *wrappers*
    like ``notify_draft_shared(...)`` don't match the call pattern and
    carry no type literals, so UI ``type="checkbox"``-style attributes
    never leak in. Self-extending: a future module that emits a new
    notification type is picked up automatically.
    """
    app_dir = Path(__file__).parent.parent / "app"
    emitted: set[str] = set()
    for path in sorted(app_dir.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if "notify(" not in text and "create_notification(" not in text:
            continue
        emitted |= set(re.findall(r'\btype="([a-z_]+)"', text))
    return emitted


class TestMigration038Contract:
    """The code emits ``drafting_session_archive_warning``; the CHECK
    constraint must allow it or ``notify()`` silently drops every
    warning (the exact regression migration 036 fixed for #572)."""

    def test_migration_allows_new_type_and_indexes_scan(self):
        migration = _MIGRATION_038.read_text()
        assert "drafting_session_archive_warning" in migration
        assert "notifications_type_check" in migration
        # 036 regression guard: BOTH historical constraint names dropped.
        assert "chk_notifications_type" in migration
        # Partial index for the daily scan.
        assert "drafting_sessions(updated_at)" in migration
        assert "status = 'active'" in migration

    def test_check_list_is_superset_of_every_emitted_type(self):
        """#845 review finding 1: rebuilding the canonical CHECK from a
        stale snapshot can silently drop an in-use type — exactly what
        happened to ``annotation_mention`` (emitted by wire.py since the
        annotations feature, never in any constraint, every insert
        silently swallowed by ``notify()``). The recreated list must be
        a superset of every type literal the codebase emits, so the
        next constraint rebuild cannot regress one."""
        emitted = _emitted_notification_types()
        allowed = _migration_038_allowed_types()
        # Extraction sanity: the patterns must keep finding real usage.
        assert "drafting_session_archive_warning" in emitted
        assert "annotation_mention" in emitted
        assert len(allowed) >= 9
        missing = emitted - allowed
        assert not missing, (
            f"migration 038 CHECK omits emitted notification types: {sorted(missing)} — "
            "notify() swallows the CHECK violation, so these would be dropped silently"
        )

    def test_annotation_mention_explicitly_allowed(self):
        """Pin the finding-1 fix itself: annotation_mention inserts have
        been silently dropped since the feature shipped (no prior
        migration ever allowed the type); 038 fixes that."""
        assert "annotation_mention" in _migration_038_allowed_types()
