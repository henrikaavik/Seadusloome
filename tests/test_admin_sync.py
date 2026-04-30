"""Tests for admin sync card rendering — focused on the Veateade column.

The previous implementation rendered the full ``error_message`` text inline,
which broke layout when the message was a multi-KB SHACL warning report.
``_sync_error_cell`` now truncates long messages and exposes the full text
through a ``<details>`` disclosure.
"""

from __future__ import annotations

from datetime import UTC, datetime

# ---------------------------------------------------------------------------
# _sync_error_cell — render helper
# ---------------------------------------------------------------------------


class TestSyncErrorCell:
    def test_returns_dash_for_empty_message(self):
        from app.admin.sync import _sync_error_cell

        assert _sync_error_cell({"error_message": None}) == "—"
        assert _sync_error_cell({"error_message": ""}) == "—"
        # Already-rendered placeholder must pass through unchanged so the
        # row dict produced by ``_sync_card`` doesn't get double-wrapped.
        assert _sync_error_cell({"error_message": "—"}) == "—"

    def test_short_message_renders_inline(self):
        """Messages at or below 80 chars are returned as plain strings."""
        from app.admin.sync import _sync_error_cell

        msg = "Connection timeout"
        result = _sync_error_cell({"error_message": msg})
        assert result == msg

    def test_boundary_at_80_chars_renders_inline(self):
        from app.admin.sync import _sync_error_cell

        msg = "x" * 80
        result = _sync_error_cell({"error_message": msg})
        assert result == msg

    def test_long_message_renders_disclosure(self):
        """Messages longer than 80 chars are wrapped in <details>."""
        from fasthtml.common import to_xml

        from app.admin.sync import _sync_error_cell

        msg = (
            "SHACL validation produced 142 warnings: "
            "ConstraintComponent#MinCountConstraintComponent at sh:path "
            "ex:hasJurisdiction expected at least 1, got 0 on entity "
            "https://example.org/legal/draft/abc-123 (and 141 more)"
        )
        cell = _sync_error_cell({"error_message": msg})
        html = to_xml(cell)
        assert "<details" in html
        assert "sync-error" in html
        assert "<summary>" in html
        # Truncated preview ends with ellipsis
        assert msg[:80] in html
        assert "…" in html
        # Full text inside <pre>
        assert "<pre" in html
        assert "sync-error-full" in html
        # Last few chars of the long message must be present in the
        # <pre> block — confirms the FULL text is preserved.
        assert msg[-10:] in html


# ---------------------------------------------------------------------------
# Integration: _sync_card uses the new render helper for long errors.
# ---------------------------------------------------------------------------


class TestSyncCardErrorColumn:
    def _failed_log(self, error: str) -> dict:  # type: ignore[type-arg]
        return {
            "id": 1,
            "started_at": datetime(2026, 4, 29, 9, 30, tzinfo=UTC),
            "finished_at": datetime(2026, 4, 29, 9, 35, tzinfo=UTC),
            "status": "failed",
            "entity_count": None,
            "error_message": error,
            "current_step": None,
        }

    def test_card_does_not_dump_long_error_inline(self):
        """The pre-fix behaviour dumped the entire SHACL report inline.
        After the fix the cell must wrap long messages in <details>."""
        from fasthtml.common import to_xml

        from app.admin.sync import _sync_card

        long_error = "SHACL validation: " + ("warning text " * 50)
        html = to_xml(_sync_card([self._failed_log(long_error)]))
        # Disclosure markers present
        assert "<details" in html
        assert "sync-error" in html
        # Truncated preview present, full message available in <pre>
        assert long_error[:80] in html
        assert "<pre" in html

    def test_card_keeps_short_error_inline(self):
        """Short errors should NOT get a disclosure — the noise isn't worth it."""
        from fasthtml.common import to_xml

        from app.admin.sync import _sync_card

        html = to_xml(_sync_card([self._failed_log("Connection refused")]))
        # No details element introduced for the short error
        assert "<details" not in html
        assert "Connection refused" in html

    def test_card_with_no_error_renders_dash(self):
        """Successful runs have no error message — render the em-dash."""
        from fasthtml.common import to_xml

        from app.admin.sync import _sync_card

        success_log = {
            "id": 2,
            "started_at": datetime(2026, 4, 29, 9, 30, tzinfo=UTC),
            "finished_at": datetime(2026, 4, 29, 9, 35, tzinfo=UTC),
            "status": "success",
            "entity_count": 5_000_000,
            "error_message": None,
            "current_step": None,
        }
        html = to_xml(_sync_card([success_log]))
        # No details disclosure for a clean run
        assert "<details" not in html
        # The em-dash placeholder is rendered (data-label="Veateade" cell)
        assert "—" in html
