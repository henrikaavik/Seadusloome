"""Tests for the draft-status SSOT module (#625).

Two layers of guarantees are pinned here:

1. ``DRAFT_STATUSES`` is a closed, well-formed table -- every row has
   a non-empty Estonian label, a valid badge variant, and a successor
   pointer that either is ``None`` or names another known status. A
   regression that adds a typo'd successor or omits a label will fail
   here long before the UI breaks at runtime.

2. ``update_draft_status`` is the exclusive write path. The validation,
   error-clearing, terminal-stamp, and -- the §4.2 contract --
   "writes ONLY to ``drafts``" invariants are all asserted directly so
   the version cutover in #618 PR-B is an explicit, bisectable change.
"""

from __future__ import annotations

import uuid
from typing import get_args
from unittest.mock import MagicMock

import pytest

from app.docs.status import (
    DRAFT_STATUSES,
    PIPELINE_STAGES,
    STATUS_BY_VALUE,
    TERMINAL_STATUSES,
    VALID_STATUSES,
    DraftStatus,
    update_draft_status,
)
from app.ui.primitives.badge import BadgeVariant

_DRAFT_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")


# ---------------------------------------------------------------------------
# Table well-formedness
# ---------------------------------------------------------------------------


_BADGE_VARIANTS: frozenset[str] = frozenset(get_args(BadgeVariant))


@pytest.mark.parametrize("spec", DRAFT_STATUSES, ids=[s.value for s in DRAFT_STATUSES])
class TestDraftStatusTable:
    """Each row in :data:`DRAFT_STATUSES` must be well-formed."""

    def test_value_is_nonempty_string(self, spec: DraftStatus):
        assert isinstance(spec.value, str) and spec.value, (
            "DraftStatus.value must be a non-empty string"
        )

    def test_label_et_is_nonempty_string(self, spec: DraftStatus):
        assert isinstance(spec.label_et, str) and spec.label_et.strip(), (
            f"DraftStatus({spec.value!r}).label_et must be a non-empty Estonian string"
        )

    def test_badge_variant_is_valid(self, spec: DraftStatus):
        assert spec.badge_variant in _BADGE_VARIANTS, (
            f"DraftStatus({spec.value!r}).badge_variant={spec.badge_variant!r} "
            f"is not a known BadgeVariant ({sorted(_BADGE_VARIANTS)})"
        )

    def test_successor_is_known_status_or_none(self, spec: DraftStatus):
        if spec.successor is None:
            return
        assert spec.successor in STATUS_BY_VALUE, (
            f"DraftStatus({spec.value!r}).successor={spec.successor!r} "
            "is not a known DraftStatus value"
        )

    def test_terminal_status_has_no_successor(self, spec: DraftStatus):
        if spec.is_terminal:
            assert spec.successor is None, (
                f"DraftStatus({spec.value!r}) is terminal but has successor {spec.successor!r}"
            )

    def test_css_key_is_nonempty(self, spec: DraftStatus):
        assert isinstance(spec.css_key, str) and spec.css_key, (
            f"DraftStatus({spec.value!r}).css_key must be a non-empty string"
        )


class TestStatusTableInvariants:
    """Cross-row invariants that the per-row parametrise can't catch."""

    def test_values_are_unique(self):
        values = [s.value for s in DRAFT_STATUSES]
        assert len(values) == len(set(values)), "DraftStatus.value must be unique"

    def test_orders_are_unique(self):
        orders = [s.order for s in DRAFT_STATUSES]
        assert len(orders) == len(set(orders)), "DraftStatus.order must be unique"

    def test_status_by_value_covers_every_row(self):
        assert set(STATUS_BY_VALUE) == {s.value for s in DRAFT_STATUSES}

    def test_valid_statuses_matches_table(self):
        assert set(VALID_STATUSES) == {s.value for s in DRAFT_STATUSES}

    def test_terminal_statuses_subset(self):
        expected = {s.value for s in DRAFT_STATUSES if s.is_terminal}
        assert TERMINAL_STATUSES == expected

    def test_pipeline_stages_excludes_failed(self):
        values = {s.value for s in PIPELINE_STAGES}
        assert "failed" not in values, "failed must not appear in PIPELINE_STAGES"
        # And contains every non-failed status.
        expected = {s.value for s in DRAFT_STATUSES if s.value != "failed"}
        assert values == expected

    def test_pipeline_stages_are_in_order(self):
        orders = [s.order for s in PIPELINE_STAGES]
        assert orders == sorted(orders), "PIPELINE_STAGES must be sorted by order"

    def test_happy_path_chain_is_walkable(self):
        """Walking ``successor`` from ``uploaded`` must end at a terminal."""
        seen: list[str] = []
        cursor: str | None = "uploaded"
        while cursor is not None:
            assert cursor not in seen, "successor chain has a cycle"
            seen.append(cursor)
            cursor = STATUS_BY_VALUE[cursor].successor
        assert seen[-1] == "ready", (
            f"Walking successors from 'uploaded' must reach 'ready'; chain was {seen!r}"
        )


# ---------------------------------------------------------------------------
# update_draft_status -- validation
# ---------------------------------------------------------------------------


class TestUpdateDraftStatusValidation:
    def test_unknown_status_raises_value_error(self):
        conn = MagicMock()
        with pytest.raises(ValueError, match="Unknown draft status"):
            update_draft_status(conn, _DRAFT_ID, "bogus")
        # No SQL must have run -- the validation gate is upfront.
        conn.execute.assert_not_called()

    def test_unknown_expected_status_raises_value_error(self):
        conn = MagicMock()
        with pytest.raises(ValueError, match="Unknown expected draft status"):
            update_draft_status(conn, _DRAFT_ID, "uploaded", expected_status="zzz")
        conn.execute.assert_not_called()

    @pytest.mark.parametrize("status", VALID_STATUSES)
    def test_every_valid_status_is_accepted(self, status: str):
        conn = MagicMock()
        conn.execute.return_value.rowcount = 1
        # Must not raise.
        update_draft_status(conn, _DRAFT_ID, status)
        conn.execute.assert_called_once()


# ---------------------------------------------------------------------------
# update_draft_status -- SQL shape and parameter contract
# ---------------------------------------------------------------------------


class TestUpdateDraftStatusSqlShape:
    """Pin the produced SQL + params so the integration fakes (and the
    §4.2 cutover invariant) stay accurate.
    """

    def test_writes_to_drafts_only(self):
        """§4.2 contract: this helper must NOT touch ``draft_versions``.

        The version-aware write lands in #618 PR-B. Until then, every
        UPDATE produced by the helper must reference only the legacy
        ``drafts`` table -- a regression that adds a stray
        ``draft_versions`` write would defeat the bisect that pins the
        cutover sequence.
        """
        conn = MagicMock()
        conn.execute.return_value.rowcount = 1

        for status in VALID_STATUSES:
            conn.reset_mock()
            update_draft_status(conn, _DRAFT_ID, status)
            sql = conn.execute.call_args.args[0].lower()
            assert "draft_versions" not in sql, (
                f"update_draft_status({status!r}) must not write to draft_versions; "
                f"§4.2 cutover lives in #618 PR-B. SQL was:\n{sql}"
            )
            assert "update drafts" in sql

    def test_default_call_writes_status_and_clears_errors(self):
        conn = MagicMock()
        conn.execute.return_value.rowcount = 1

        update_draft_status(conn, _DRAFT_ID, "parsing")

        sql, params = conn.execute.call_args.args
        assert "status = %s" in sql
        assert "error_message = %s" in sql
        assert "error_debug = %s" in sql
        assert "updated_at = now()" in sql
        # status, error_message=None, error_debug=None, draft_id
        assert params == ("parsing", None, None, str(_DRAFT_ID))

    def test_terminal_transition_stamps_completion_now(self):
        for terminal in TERMINAL_STATUSES:
            conn = MagicMock()
            conn.execute.return_value.rowcount = 1
            update_draft_status(conn, _DRAFT_ID, terminal)
            sql = conn.execute.call_args.args[0]
            assert "processing_completed_at = now()" in sql, (
                f"Terminal status {terminal!r} must stamp processing_completed_at = now()"
            )

    def test_non_terminal_transition_clears_completion(self):
        for spec in DRAFT_STATUSES:
            if spec.is_terminal:
                continue
            conn = MagicMock()
            conn.execute.return_value.rowcount = 1
            update_draft_status(conn, _DRAFT_ID, spec.value)
            sql = conn.execute.call_args.args[0]
            assert "processing_completed_at = null" in sql, (
                f"Non-terminal status {spec.value!r} must clear processing_completed_at"
            )

    def test_error_message_passed_positionally(self):
        """Legacy positional shape ``(conn, id, status, msg)`` still works."""
        conn = MagicMock()
        conn.execute.return_value.rowcount = 1

        update_draft_status(conn, _DRAFT_ID, "failed", "Boom")

        params = conn.execute.call_args.args[1]
        assert params[0] == "failed"
        assert params[1] == "Boom"
        assert params[2] is None  # error_debug

    def test_error_debug_kwarg_lands_in_third_param_slot(self):
        conn = MagicMock()
        conn.execute.return_value.rowcount = 1

        update_draft_status(
            conn,
            _DRAFT_ID,
            "failed",
            "User-facing",
            error_debug="raw stack trace",
        )

        params = conn.execute.call_args.args[1]
        assert params == (
            "failed",
            "User-facing",
            "raw stack trace",
            str(_DRAFT_ID),
        )

    def test_extras_land_in_sorted_column_order(self):
        """The ``extras`` columns are interpolated in sorted alphabetic
        order so the param tuple's layout stays deterministic.
        """
        conn = MagicMock()
        conn.execute.return_value.rowcount = 1

        update_draft_status(
            conn,
            _DRAFT_ID,
            "extracting",
            extras={"parsed_text_encrypted": b"ciphertext"},
        )

        sql, params = conn.execute.call_args.args
        assert "parsed_text_encrypted = %s" in sql
        # status, error_message=None, error_debug=None, parsed_text_encrypted, draft_id
        assert params == (
            "extracting",
            None,
            None,
            b"ciphertext",
            str(_DRAFT_ID),
        )

    def test_extras_multiple_columns_are_sorted(self):
        """Two extras must land in alphabetic order regardless of dict
        insertion order so the integration fakes stay deterministic.
        """
        conn = MagicMock()
        conn.execute.return_value.rowcount = 1

        update_draft_status(
            conn,
            _DRAFT_ID,
            "ready",
            extras={"parsed_text_encrypted": b"x", "entity_count": 3},
        )

        sql, params = conn.execute.call_args.args
        # entity_count comes before parsed_text_encrypted alphabetically.
        ec_idx = sql.find("entity_count = %s")
        pte_idx = sql.find("parsed_text_encrypted = %s")
        assert 0 < ec_idx < pte_idx, "extras must be interpolated in sorted alphabetic order"
        assert params == ("ready", None, None, 3, b"x", str(_DRAFT_ID))

    def test_expected_status_adds_optimistic_where_predicate(self):
        conn = MagicMock()
        conn.execute.return_value.rowcount = 1

        update_draft_status(
            conn,
            _DRAFT_ID,
            "uploaded",
            expected_status="failed",
        )

        sql, params = conn.execute.call_args.args
        assert "where id = %s and status = %s" in sql.lower()
        # status=uploaded, error_message=None, error_debug=None, id, expected=failed
        assert params == ("uploaded", None, None, str(_DRAFT_ID), "failed")

    def test_returns_true_when_row_was_updated(self):
        conn = MagicMock()
        conn.execute.return_value.rowcount = 1
        assert update_draft_status(conn, _DRAFT_ID, "parsing") is True

    def test_returns_false_when_no_rows_match(self):
        conn = MagicMock()
        conn.execute.return_value.rowcount = 0
        assert update_draft_status(conn, _DRAFT_ID, "parsing") is False

    def test_returns_false_when_rowcount_is_none(self):
        conn = MagicMock()
        conn.execute.return_value.rowcount = None
        assert update_draft_status(conn, _DRAFT_ID, "parsing") is False
