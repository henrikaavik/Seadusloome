"""Unit tests for the export progress indicator (#610).

Covers four layers:

1. ``app.docs.export_handler._publish_progress`` writes the right
   JSONB payload to ``background_jobs.progress`` and tolerates DB
   errors gracefully (best-effort contract).
2. ``app.docs.docx_export.build_impact_report_docx`` invokes the
   optional progress callback the expected number of times for a
   given report shape, and tolerates a misbehaving callback.
3. ``app.docs.ws_export_progress.ws_export_progress`` validates
   subscribe envelopes, enforces auth + cross-org guards, and pushes
   ``{"current": N, "total": M}`` frames to the client when the
   underlying ``background_jobs.progress`` column changes.
4. The HTTP polling fragment in ``app.docs.report_routes`` continues
   to emit ``hx-get`` / ``hx-trigger`` attributes so the
   graceful-degradation path keeps working when the WebSocket is
   unavailable (the issue's DoD).
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch

from app.docs import ws_export_progress as ws_module
from app.docs.docx_export import _PROGRESS_BATCH, build_impact_report_docx
from app.docs.draft_model import Draft
from app.docs.export_handler import _publish_progress

_DRAFT_ID = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_REPORT_ID = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


class _ConnectCM:
    """Context-manager wrapper for the ``get_connection`` mock — same
    shape as the helper in ``tests/test_docs_export_handler.py``."""

    def __init__(self, conn: MagicMock):
        self.conn = conn

    def __enter__(self) -> MagicMock:
        return self.conn

    def __exit__(self, *_: Any) -> bool:
        return False


def _make_draft() -> Draft:
    now = datetime.now(UTC)
    return Draft(
        id=_DRAFT_ID,
        user_id=uuid.UUID("11111111-1111-1111-1111-111111111111"),
        org_id=uuid.UUID("22222222-2222-2222-2222-222222222222"),
        title="Test eelnõu",
        filename="eelnou.docx",
        content_type=("application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
        file_size=2048,
        storage_path="/tmp/cipher.enc",
        graph_uri=f"https://data.riik.ee/ontology/estleg/drafts/{_DRAFT_ID}",
        status="ready",
        parsed_text_encrypted=None,
        entity_count=2,
        error_message=None,
        created_at=now,
        updated_at=now,
    )


def _make_report_row(report_data: dict[str, Any] | None = None) -> tuple:
    """Build a tuple matching ``_REPORT_COLUMN_INDEX`` ordering."""
    return (
        _REPORT_ID,
        _DRAFT_ID,
        2,
        1,
        0,
        42,
        report_data
        or {
            "affected_entities": [],
            "conflicts": [],
            "eu_compliance": [],
            "gaps": [],
        },
        "2026-04-09T12:00:00+00:00@1061123",
        datetime(2026, 4, 9, 12, 0, tzinfo=UTC),
    )


# ---------------------------------------------------------------------------
# 1. _publish_progress
# ---------------------------------------------------------------------------


class TestPublishProgress:
    def test_writes_jsonb_payload_to_background_jobs(self):
        """The handler must UPDATE background_jobs.progress with the
        ``{current, total}`` payload bound through psycopg's Jsonb so
        the JSONB column receives a typed value, not a stringified one."""
        conn = MagicMock()
        conn.execute = MagicMock()
        conn.commit = MagicMock()

        with patch("app.docs.export_handler.get_connection") as mock_conn:
            mock_conn.return_value = _ConnectCM(conn)
            _publish_progress(42, current=3, total=10)

        conn.execute.assert_called_once()
        conn.commit.assert_called_once()

        sql, params = conn.execute.call_args.args
        assert "UPDATE background_jobs" in sql
        assert "SET progress" in sql
        assert "WHERE id" in sql

        progress_param, job_id_param = params
        assert job_id_param == 42
        # Jsonb wraps the dict — check both .obj and the dict path so we
        # don't couple to psycopg internals.
        unwrapped = getattr(progress_param, "obj", progress_param)
        assert unwrapped == {"current": 3, "total": 10}

    def test_db_failure_is_swallowed(self):
        """A DB error must NOT propagate — the .docx render is the
        source of truth and the progress channel is best-effort UX."""

        def boom(*_: Any, **__: Any):
            raise RuntimeError("connection lost")

        with patch("app.docs.export_handler.get_connection", side_effect=boom):
            # Must not raise; the export render would abort otherwise.
            _publish_progress(1, current=1, total=1)


# ---------------------------------------------------------------------------
# 2. build_impact_report_docx — progress callback wiring
# ---------------------------------------------------------------------------


class TestDocxProgressCallback:
    def test_callback_invoked_for_empty_report(self):
        """Empty findings still yields multiple section-level publishes
        + a final ``(total, total)`` pin so the WS sees current==total
        when the .docx is done."""
        draft = _make_draft()
        report_row = _make_report_row()
        calls: list[tuple[int, int]] = []

        def cb(current: int, total: int) -> None:
            calls.append((current, total))

        with patch("app.docs.docx_export.Document") as mock_doc_cls:
            doc = MagicMock()
            mock_doc_cls.return_value = doc
            doc.sections = []  # _add_footer_page_numbers loop is no-op
            build_impact_report_docx(draft, report_row, progress_callback=cb)

        assert calls, "progress callback was never invoked"
        assert calls[0][0] == 1
        assert calls[-1][0] == calls[-1][1]

        totals = {total for _, total in calls}
        assert len(totals) == 1, f"total drifted across calls: {totals}"

        currents = [c for c, _ in calls]
        for prev, nxt in zip(currents, currents[1:], strict=False):
            assert prev <= nxt, f"current went backwards: {prev} -> {nxt}"

    def test_callback_fires_mid_table_for_large_section(self):
        """A table with > _PROGRESS_BATCH rows must publish progress
        mid-table so the user sees the bar move during a large
        affected-entities render rather than jumping section-by-section."""
        draft = _make_draft()
        big_rows = [
            {"uri": f"u-{i}", "label": f"L {i}", "type": "x"}
            for i in range(_PROGRESS_BATCH * 2 + 5)
        ]
        report_row = _make_report_row(
            {
                "affected_entities": big_rows,
                "conflicts": [],
                "eu_compliance": [],
                "gaps": [],
            }
        )
        calls: list[tuple[int, int]] = []

        def cb(current: int, total: int) -> None:
            calls.append((current, total))

        with patch("app.docs.docx_export.Document") as mock_doc_cls:
            doc = MagicMock()
            mock_doc_cls.return_value = doc
            doc.sections = []
            build_impact_report_docx(draft, report_row, progress_callback=cb)

        # Empty-report baseline = 8 publishes (cover, summary, 4 section
        # headings, footer, save + final pin). 25 rows / 10 = 2 mid-table
        # extras → at least 2 more publishes.
        assert len(calls) > 8, f"expected >8 publishes for big table, got {len(calls)}: {calls}"

    def test_callback_failure_does_not_abort_render(self):
        """A misbehaving callback (e.g. DB hiccup mid-render) must not
        abort the .docx build — every publish goes through ``_safe_publish``."""
        draft = _make_draft()
        report_row = _make_report_row()

        def cb(_current: int, _total: int) -> None:
            raise RuntimeError("DB temporarily unavailable")

        with patch("app.docs.docx_export.Document") as mock_doc_cls:
            doc = MagicMock()
            mock_doc_cls.return_value = doc
            doc.sections = []
            # Must not raise.
            build_impact_report_docx(draft, report_row, progress_callback=cb)

    def test_no_callback_keeps_existing_behaviour(self):
        """Passing ``progress_callback=None`` (the default for older
        callers + every test that pre-dates #610) must work unchanged."""
        draft = _make_draft()
        report_row = _make_report_row()

        with patch("app.docs.docx_export.Document") as mock_doc_cls:
            doc = MagicMock()
            mock_doc_cls.return_value = doc
            doc.sections = []
            # Must not raise; must still call doc.save once.
            build_impact_report_docx(draft, report_row)
        doc.save.assert_called_once()


def test_progress_batch_is_a_positive_int():
    assert isinstance(_PROGRESS_BATCH, int)
    assert _PROGRESS_BATCH > 0


# ---------------------------------------------------------------------------
# 3. WebSocket handler — message validation + auth
# ---------------------------------------------------------------------------


def _drain_messages(received: list[str]) -> list[str]:
    """Extract the human-readable ``message`` strings from a list of
    raw WS frames (skipping non-error frames). Helper to keep the test
    bodies short."""
    out: list[str] = []
    for r in received:
        try:
            payload = json.loads(r)
        except (TypeError, ValueError):
            continue
        msg = payload.get("message")
        if isinstance(msg, str):
            out.append(msg)
    return out


class TestWebSocketValidation:
    def test_invalid_json_yields_error(self):
        async def _run() -> list[str]:
            received: list[str] = []

            async def send(p: str) -> None:
                received.append(p)

            await ws_module.ws_export_progress("not-json{", send, scope={})
            return received

        received = asyncio.run(_run())
        assert any("Vigane JSON" in r for r in received)

    def test_non_dict_yields_error(self):
        async def _run() -> list[str]:
            received: list[str] = []

            async def send(p: str) -> None:
                received.append(p)

            await ws_module.ws_export_progress('["nope"]', send, scope={})
            return received

        received = asyncio.run(_run())
        assert any("Vigane sõnum" in m for m in _drain_messages(received))

    def test_unknown_type_silently_ignored(self):
        async def _run() -> list[str]:
            received: list[str] = []

            async def send(p: str) -> None:
                received.append(p)

            await ws_module.ws_export_progress(
                json.dumps({"type": "unsubscribe", "draft_id": str(uuid.uuid4())}),
                send,
                scope={},
            )
            return received

        received = asyncio.run(_run())
        assert received == []

    def test_subscribe_without_draft_id(self):
        async def _run() -> list[str]:
            received: list[str] = []

            async def send(p: str) -> None:
                received.append(p)

            await ws_module.ws_export_progress(
                json.dumps({"type": "subscribe", "job_id": 1}),
                send,
                scope={"auth": {"id": "u1"}},
            )
            return received

        received = asyncio.run(_run())
        assert any("draft_id" in m for m in _drain_messages(received))

    def test_subscribe_with_invalid_draft_id(self):
        async def _run() -> list[str]:
            received: list[str] = []

            async def send(p: str) -> None:
                received.append(p)

            await ws_module.ws_export_progress(
                json.dumps({"type": "subscribe", "draft_id": "not-a-uuid", "job_id": 1}),
                send,
                scope={"auth": {"id": "u1"}},
            )
            return received

        received = asyncio.run(_run())
        assert any("Vigane draft_id" in m for m in _drain_messages(received))

    def test_subscribe_without_job_id(self):
        async def _run() -> list[str]:
            received: list[str] = []

            async def send(p: str) -> None:
                received.append(p)

            await ws_module.ws_export_progress(
                json.dumps({"type": "subscribe", "draft_id": str(uuid.uuid4())}),
                send,
                scope={"auth": {"id": "u1"}},
            )
            return received

        received = asyncio.run(_run())
        assert any("job_id" in m for m in _drain_messages(received))

    def test_subscribe_with_invalid_job_id(self):
        async def _run() -> list[str]:
            received: list[str] = []

            async def send(p: str) -> None:
                received.append(p)

            await ws_module.ws_export_progress(
                json.dumps(
                    {
                        "type": "subscribe",
                        "draft_id": str(uuid.uuid4()),
                        "job_id": "not-an-int",
                    }
                ),
                send,
                scope={"auth": {"id": "u1"}},
            )
            return received

        received = asyncio.run(_run())
        assert any("Vigane job_id" in m for m in _drain_messages(received))

    def test_unauthenticated_subscribe_yields_error(self):
        async def _run() -> list[str]:
            received: list[str] = []

            async def send(p: str) -> None:
                received.append(p)

            await ws_module.ws_export_progress(
                json.dumps({"type": "subscribe", "draft_id": str(uuid.uuid4()), "job_id": 1}),
                send,
                scope={},
            )
            return received

        received = asyncio.run(_run())
        assert any("Autentimine" in m for m in _drain_messages(received))


def _make_draft_obj(*, org_id: str = "org-1") -> Any:
    from types import SimpleNamespace

    return SimpleNamespace(
        id=uuid.uuid4(),
        org_id=org_id,
        status="ready",
        error_message=None,
    )


class TestWebSocketAuthGuards:
    def test_subscribe_to_missing_draft_yields_404_style_error(self):
        async def _run() -> list[str]:
            received: list[str] = []

            async def send(p: str) -> None:
                received.append(p)

            with patch("app.docs.ws_export_progress.fetch_draft", return_value=None):
                await ws_module.ws_export_progress(
                    json.dumps(
                        {
                            "type": "subscribe",
                            "draft_id": str(uuid.uuid4()),
                            "job_id": 1,
                        }
                    ),
                    send,
                    scope={"auth": {"id": "u1", "org_id": "org-1"}},
                )
            return received

        received = asyncio.run(_run())
        assert any("Eelnõu ei leitud" in m for m in _drain_messages(received))

    def test_cross_org_subscribe_yields_404_style_not_403(self):
        async def _run() -> list[str]:
            received: list[str] = []

            async def send(p: str) -> None:
                received.append(p)

            draft = _make_draft_obj(org_id="other-org")
            with (
                patch("app.docs.ws_export_progress.fetch_draft", return_value=draft),
                patch("app.docs.ws_export_progress.can_view_draft", return_value=False),
            ):
                await ws_module.ws_export_progress(
                    json.dumps(
                        {
                            "type": "subscribe",
                            "draft_id": str(draft.id),
                            "job_id": 1,
                        }
                    ),
                    send,
                    scope={"auth": {"id": "u1", "org_id": "org-1"}},
                )
            return received

        received = asyncio.run(_run())
        msgs = _drain_messages(received)
        assert any("Eelnõu ei leitud" in m for m in msgs)
        assert not any("403" in m for m in msgs)

    def test_job_not_owned_by_draft_yields_error(self):
        """A leaked job_id from another draft must be rejected even
        after the draft-level auth check passes."""

        async def _run() -> list[str]:
            received: list[str] = []

            async def send(p: str) -> None:
                received.append(p)

            draft = _make_draft_obj(org_id="org-1")
            with (
                patch("app.docs.ws_export_progress.fetch_draft", return_value=draft),
                patch("app.docs.ws_export_progress.can_view_draft", return_value=True),
                patch(
                    "app.docs.ws_export_progress._validate_job_belongs_to_draft",
                    return_value=False,
                ),
            ):
                await ws_module.ws_export_progress(
                    json.dumps(
                        {
                            "type": "subscribe",
                            "draft_id": str(draft.id),
                            "job_id": 9999,
                        }
                    ),
                    send,
                    scope={"auth": {"id": "u1", "org_id": "org-1"}},
                )
            return received

        received = asyncio.run(_run())
        assert any("Eksporditööd" in m for m in _drain_messages(received))


class TestWebSocketProgressPush:
    def test_initial_event_carries_current_total_when_progress_present(self):
        """A successful subscribe must immediately push the current
        ``progress`` JSONB payload as an ``initial`` event so the bar
        paints before the first poll tick. Job in success state closes
        the socket immediately after."""
        draft = _make_draft_obj()

        async def _run() -> list[str]:
            received: list[Any] = []

            async def send(p: Any) -> None:
                received.append(p)

            with (
                patch("app.docs.ws_export_progress.fetch_draft", return_value=draft),
                patch("app.docs.ws_export_progress.can_view_draft", return_value=True),
                patch(
                    "app.docs.ws_export_progress._validate_job_belongs_to_draft",
                    return_value=True,
                ),
                patch(
                    "app.docs.ws_export_progress._read_job_progress",
                    return_value=("success", {"current": 8, "total": 12}),
                ),
            ):
                await ws_module.ws_export_progress(
                    json.dumps(
                        {
                            "type": "subscribe",
                            "draft_id": str(draft.id),
                            "job_id": 7,
                        }
                    ),
                    send,
                    scope={"auth": {"id": "u1", "org_id": "org-1"}},
                )
            return received

        received = asyncio.run(_run())
        # Filter out the ASGI close envelope (a dict, not a JSON string).
        json_frames = [json.loads(r) for r in received if isinstance(r, str) and r.startswith("{")]
        first = next(e for e in json_frames if e.get("type") == "initial")
        assert first["current"] == 8
        assert first["total"] == 12
        assert first["status"] == "success"

    def test_progress_change_pushes_progress_event(self):
        """When the polled ``progress`` value changes, the handler
        must push a ``progress`` frame to the client and a final
        ``terminal`` frame when the job hits success."""
        draft = _make_draft_obj()
        reads = [
            ("running", {"current": 3, "total": 10}),
            ("running", {"current": 6, "total": 10}),
            ("success", {"current": 10, "total": 10}),
        ]
        read_idx = {"i": 0}

        def fake_read(_job_id: int):
            i = min(read_idx["i"], len(reads) - 1)
            read_idx["i"] += 1
            return reads[i]

        async def _run() -> list[Any]:
            received: list[Any] = []

            async def send(p: Any) -> None:
                received.append(p)

            with (
                patch("app.docs.ws_export_progress._POLL_INTERVAL_SECONDS", 0.01),
                patch("app.docs.ws_export_progress._WS_MAX_LIFETIME_SECONDS", 1.0),
                patch("app.docs.ws_export_progress.fetch_draft", return_value=draft),
                patch("app.docs.ws_export_progress.can_view_draft", return_value=True),
                patch(
                    "app.docs.ws_export_progress._validate_job_belongs_to_draft",
                    return_value=True,
                ),
                patch(
                    "app.docs.ws_export_progress._read_job_progress",
                    side_effect=fake_read,
                ),
            ):
                await ws_module.ws_export_progress(
                    json.dumps(
                        {
                            "type": "subscribe",
                            "draft_id": str(draft.id),
                            "job_id": 11,
                        }
                    ),
                    send,
                    scope={"auth": {"id": "u1", "org_id": "org-1"}},
                )
            return received

        received = asyncio.run(_run())
        json_frames = [json.loads(r) for r in received if isinstance(r, str) and r.startswith("{")]
        types = [e.get("type") for e in json_frames]
        assert "initial" in types
        assert "progress" in types
        assert "terminal" in types

        progress_events = [e for e in json_frames if e.get("type") == "progress"]
        assert any(e.get("current") == 6 and e.get("total") == 10 for e in progress_events)


# ---------------------------------------------------------------------------
# 4. Polling fallback still works
# ---------------------------------------------------------------------------


class TestPollingFallbackPreserved:
    def _render(self, ft_obj: Any) -> str:
        """Render an FT object to its raw HTML string via FastHTML's
        ``to_xml`` helper (the canonical way to materialise FT trees in
        tests — see ``tests/test_ui_layout.py``)."""
        from fasthtml.common import to_xml

        return to_xml(ft_obj)

    def test_export_status_spinner_keeps_htmx_polling_attributes(self):
        """The graceful-degradation contract: the spinner fragment
        must keep its ``hx-get`` / ``hx-trigger`` / ``hx-swap`` so the
        UI still drives to the success/failed terminal state when the
        WebSocket is unavailable. This is the issue's DoD."""
        from app.docs.report_routes import _export_status_spinner

        draft_id = uuid.uuid4()
        spinner = _export_status_spinner(draft_id, job_id=42, job_created=None)
        rendered = self._render(spinner)

        assert f"/drafts/{draft_id}/export-status/42" in rendered
        assert "every" in rendered  # hx-trigger="every Ns"
        assert "outerHTML" in rendered  # hx-swap

    def test_export_status_spinner_includes_progress_marker(self):
        """The spinner fragment must expose the
        ``data-export-progress-ws`` marker the JS shim looks for, plus
        the ``<progress>`` element + label so the WS push has somewhere
        to write the bar value."""
        from app.docs.report_routes import _export_status_spinner

        draft_id = uuid.uuid4()
        spinner = _export_status_spinner(draft_id, job_id=42, job_created=None)
        rendered = self._render(spinner)

        assert "data-export-progress-ws" in rendered
        assert "data-job-id" in rendered
        assert "data-draft-id" in rendered
        assert "<progress" in rendered
        assert "export-progress-label" in rendered


# ---------------------------------------------------------------------------
# Smoke: WS route registration
# ---------------------------------------------------------------------------


class TestRouteRegistration:
    def test_register_mounts_export_progress_path(self):
        """``register_export_progress_ws_routes`` must register a
        handler on ``/ws/drafts/export-progress`` so the FastHTML app
        can route incoming connections to it."""
        recorded: list[str] = []

        class _StubApp:
            def ws(self, path: str, *_args: Any, **_kwargs: Any):
                recorded.append(path)

                def _decorator(handler: Any) -> Any:
                    return handler

                return _decorator

        ws_module.register_export_progress_ws_routes(_StubApp())
        assert recorded == ["/ws/drafts/export-progress"]
