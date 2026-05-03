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
        # #618 PR-B: two UPDATEs fire — draft_versions then drafts mirror.
        assert conn.execute.call_count == 2


# ---------------------------------------------------------------------------
# update_draft_status -- SQL shape and parameter contract
# ---------------------------------------------------------------------------


class TestUpdateDraftStatusSqlShape:
    """Pin the produced SQL + params so the integration fakes (and the
    §4.2 cutover invariant) stay accurate.
    """

    def test_writes_to_both_draft_versions_and_drafts(self):
        """§4.2 cutover (#618 PR-B): the helper writes to BOTH tables.

        Post-PR-B the version-aware status update lands first (so the
        latest ``draft_versions`` row is the source of truth) and a
        defensive mirror to ``drafts.status`` keeps legacy readers
        working for one release cycle.  PR-D will drop the
        ``drafts.status`` write and the column.

        A regression that drops EITHER write breaks the cutover:

            * Missing the ``draft_versions`` UPDATE means PR-C diff /
              timeline UI sees a stale per-version status.
            * Missing the ``drafts`` UPDATE means legacy readers (every
              listing query that hasn't been pivoted yet) see a stale
              status column.

        The test asserts both UPDATE statements fire in the SAME
        ``update_draft_status`` call so the contract is checked end-to-end.
        """
        conn = MagicMock()
        conn.execute.return_value.rowcount = 1

        for status in VALID_STATUSES:
            conn.reset_mock()
            update_draft_status(conn, _DRAFT_ID, status)
            sql_calls = [c.args[0].lower() for c in conn.execute.call_args_list]
            joined = "\n".join(sql_calls)
            assert "draft_versions" in joined, (
                f"update_draft_status({status!r}) must write to draft_versions; "
                f"§4.2 cutover (#618 PR-B). SQL was:\n{joined}"
            )
            assert "update drafts" in joined, (
                f"update_draft_status({status!r}) must defensive-mirror to drafts.status; "
                f"§4.2 cutover (#618 PR-B). SQL was:\n{joined}"
            )

    def test_default_call_writes_status_and_clears_errors(self):
        conn = MagicMock()
        conn.execute.return_value.rowcount = 1

        update_draft_status(conn, _DRAFT_ID, "parsing")

        # Two UPDATEs: first to draft_versions, second to drafts.
        # The drafts UPDATE owns the error-clearing + completion-stamp logic.
        drafts_call = next(
            c for c in conn.execute.call_args_list if "update drafts" in c.args[0].lower()
        )
        sql, params = drafts_call.args
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
            drafts_call = next(
                c for c in conn.execute.call_args_list if "update drafts" in c.args[0].lower()
            )
            sql = drafts_call.args[0]
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
            drafts_call = next(
                c for c in conn.execute.call_args_list if "update drafts" in c.args[0].lower()
            )
            sql = drafts_call.args[0]
            assert "processing_completed_at = null" in sql, (
                f"Non-terminal status {spec.value!r} must clear processing_completed_at"
            )

    def test_error_message_passed_positionally(self):
        """Legacy positional shape ``(conn, id, status, msg)`` still works."""
        conn = MagicMock()
        conn.execute.return_value.rowcount = 1

        update_draft_status(conn, _DRAFT_ID, "failed", "Boom")

        drafts_call = next(
            c for c in conn.execute.call_args_list if "update drafts" in c.args[0].lower()
        )
        params = drafts_call.args[1]
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

        drafts_call = next(
            c for c in conn.execute.call_args_list if "update drafts" in c.args[0].lower()
        )
        params = drafts_call.args[1]
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

        drafts_call = next(
            c for c in conn.execute.call_args_list if "update drafts" in c.args[0].lower()
        )
        sql, params = drafts_call.args
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

        drafts_call = next(
            c for c in conn.execute.call_args_list if "update drafts" in c.args[0].lower()
        )
        sql, params = drafts_call.args
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

        drafts_call = next(
            c for c in conn.execute.call_args_list if "update drafts" in c.args[0].lower()
        )
        sql, params = drafts_call.args
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


# ---------------------------------------------------------------------------
# update_draft_status -- §4.2 cutover (#618 PR-B) version-aware write
# ---------------------------------------------------------------------------


class TestUpdateDraftStatusVersionCutover:
    """The PR-B cutover splits the UPDATE into two statements: first the
    latest ``draft_versions`` row, then the defensive ``drafts`` mirror.

    These tests pin the per-statement shape so a future PR-D can safely
    drop the second UPDATE without losing the per-version semantics.
    """

    def test_version_update_targets_latest_version_row(self):
        """The version UPDATE must use ``ORDER BY version_number DESC LIMIT 1``
        so a v3 status flip never bleeds into v2.
        """
        conn = MagicMock()
        conn.execute.return_value.rowcount = 1

        update_draft_status(conn, _DRAFT_ID, "ready")

        version_call = next(
            c for c in conn.execute.call_args_list if "draft_versions" in c.args[0].lower()
        )
        sql = version_call.args[0].lower()
        assert "update draft_versions" in sql
        assert "order by version_number desc" in sql
        assert "limit 1" in sql

    def test_version_update_runs_before_drafts_mirror(self):
        """Order matters: version write first, drafts mirror second.

        If the drafts mirror runs first and then version fails (e.g. a
        never-backfilled draft has no v1 row), the rowcount-based
        return value would still be True even though no version state
        was actually persisted.  Test that the call order matches the
        documented contract.
        """
        conn = MagicMock()
        conn.execute.return_value.rowcount = 1

        update_draft_status(conn, _DRAFT_ID, "parsing")

        sql_calls = [c.args[0].lower() for c in conn.execute.call_args_list]
        version_idx = next(i for i, s in enumerate(sql_calls) if "draft_versions" in s)
        drafts_idx = next(i for i, s in enumerate(sql_calls) if "update drafts" in s)
        assert version_idx < drafts_idx, (
            f"draft_versions UPDATE must run before drafts mirror; order was {sql_calls!r}"
        )

    def test_parsed_text_encrypted_extra_mirrors_to_version(self):
        """``parsed_text_encrypted`` lives on BOTH tables and must mirror.

        The parse handler writes ``parsed_text_encrypted`` via ``extras``
        as part of the ``parsing -> extracting`` transition; the version
        row needs the same payload so per-version reads pick up the
        decrypted text.
        """
        conn = MagicMock()
        conn.execute.return_value.rowcount = 1

        update_draft_status(
            conn,
            _DRAFT_ID,
            "extracting",
            extras={"parsed_text_encrypted": b"ciphertext"},
        )

        version_call = next(
            c for c in conn.execute.call_args_list if "draft_versions" in c.args[0].lower()
        )
        sql, params = version_call.args
        assert "parsed_text_encrypted = %s" in sql
        # status, parsed_text_encrypted, draft_id
        assert params == ("extracting", b"ciphertext", str(_DRAFT_ID))

    def test_entity_count_extra_does_not_mirror_to_version(self):
        """``entity_count`` lives only on ``drafts`` -- the version row
        does not have this column and must NOT receive an UPDATE for it.
        """
        conn = MagicMock()
        conn.execute.return_value.rowcount = 1

        update_draft_status(
            conn,
            _DRAFT_ID,
            "analyzing",
            extras={"entity_count": 12},
        )

        version_call = next(
            c for c in conn.execute.call_args_list if "draft_versions" in c.args[0].lower()
        )
        sql = version_call.args[0]
        assert "entity_count" not in sql, (
            "entity_count must not appear in the draft_versions UPDATE; "
            "the column does not exist on draft_versions."
        )

        # And the drafts mirror DOES include it.
        drafts_call = next(
            c for c in conn.execute.call_args_list if "update drafts" in c.args[0].lower()
        )
        assert "entity_count = %s" in drafts_call.args[0]
