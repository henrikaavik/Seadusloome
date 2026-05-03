"""Unit tests for ``app.docs.version_diff`` (#618 PR-C).

These tests are pure-Python — no DB, no FastHTML server.  The
:func:`compute_diff` function is data-in / data-out and the
:func:`render_diff_table` function returns an FT element whose
serialised XML we string-search for the expected CSS classes.

Coverage targets (from the sprint plan §6 Days 8-9 acceptance):

* identical text -> all rows ``unchanged``
* pure addition  -> row carries ``kind="added"``, ``left_lineno=None``
* pure removal   -> row carries ``kind="removed"``, ``right_lineno=None``
* modified line  -> row carries ``kind="changed"``
* render output  -> table contains ``diff-row-added`` for added rows
"""

from __future__ import annotations

from fasthtml.common import to_xml

from app.docs.version_diff import (
    DiffRow,
    compute_diff,
    render_diff_table,
)

# ---------------------------------------------------------------------------
# compute_diff — opcodes from difflib.SequenceMatcher
# ---------------------------------------------------------------------------


class TestComputeDiffIdentical:
    def test_identical_text_yields_only_unchanged_rows(self):
        text = "esimene rida\nteine rida\nkolmas rida"
        rows = compute_diff(text, text)
        assert len(rows) == 3
        assert all(row.kind == "unchanged" for row in rows)

    def test_identical_rows_carry_matching_lineno_pairs(self):
        text = "rida 1\nrida 2"
        rows = compute_diff(text, text)
        assert rows[0].left_lineno == 1
        assert rows[0].right_lineno == 1
        assert rows[1].left_lineno == 2
        assert rows[1].right_lineno == 2

    def test_identical_rows_carry_matching_text_pairs(self):
        text = "ainus rida"
        rows = compute_diff(text, text)
        assert rows[0].left_text == "ainus rida"
        assert rows[0].right_text == "ainus rida"

    def test_two_empty_inputs_yield_zero_rows(self):
        assert compute_diff("", "") == []


# ---------------------------------------------------------------------------
# compute_diff — pure additions
# ---------------------------------------------------------------------------


class TestComputeDiffPureAddition:
    def test_addition_emits_added_row_with_no_left_lineno(self):
        rows = compute_diff("", "uus rida")
        assert len(rows) == 1
        assert rows[0].kind == "added"
        assert rows[0].left_lineno is None
        assert rows[0].left_text is None

    def test_addition_carries_right_lineno_starting_at_one(self):
        rows = compute_diff("", "esimene\nteine")
        assert rows[0].right_lineno == 1
        assert rows[0].right_text == "esimene"
        assert rows[1].right_lineno == 2
        assert rows[1].right_text == "teine"

    def test_appended_line_preserves_existing_unchanged_block(self):
        # left = "alpha"; right = "alpha\nbeta" — alpha stays unchanged,
        # beta is a pure addition with no left counterpart.
        rows = compute_diff("alpha", "alpha\nbeta")
        kinds = [row.kind for row in rows]
        assert kinds == ["unchanged", "added"]
        assert rows[1].left_lineno is None
        assert rows[1].right_text == "beta"


# ---------------------------------------------------------------------------
# compute_diff — pure removals
# ---------------------------------------------------------------------------


class TestComputeDiffPureRemoval:
    def test_removal_emits_removed_row_with_no_right_lineno(self):
        rows = compute_diff("kustutatav rida", "")
        assert len(rows) == 1
        assert rows[0].kind == "removed"
        assert rows[0].right_lineno is None
        assert rows[0].right_text is None

    def test_removal_carries_left_lineno_starting_at_one(self):
        rows = compute_diff("eemaldatud 1\neemaldatud 2", "")
        assert rows[0].left_lineno == 1
        assert rows[0].left_text == "eemaldatud 1"
        assert rows[1].left_lineno == 2
        assert rows[1].left_text == "eemaldatud 2"

    def test_dropped_line_preserves_remaining_unchanged_block(self):
        # left = "a\nb"; right = "a" — a stays unchanged, b is removed.
        rows = compute_diff("a\nb", "a")
        kinds = [row.kind for row in rows]
        assert kinds == ["unchanged", "removed"]
        assert rows[1].right_lineno is None
        assert rows[1].left_text == "b"


# ---------------------------------------------------------------------------
# compute_diff — modified line emits ``changed`` rather than add+remove pair
# ---------------------------------------------------------------------------


class TestComputeDiffModified:
    def test_single_line_replacement_emits_changed_row(self):
        rows = compute_diff("vana rida", "uus rida")
        assert len(rows) == 1
        assert rows[0].kind == "changed"
        assert rows[0].left_lineno == 1
        assert rows[0].right_lineno == 1
        assert rows[0].left_text == "vana rida"
        assert rows[0].right_text == "uus rida"

    def test_replacement_in_middle_keeps_surrounding_unchanged(self):
        left = "alpha\nvana\ngamma"
        right = "alpha\nuus\ngamma"
        rows = compute_diff(left, right)
        kinds = [row.kind for row in rows]
        assert kinds == ["unchanged", "changed", "unchanged"]
        assert rows[1].left_text == "vana"
        assert rows[1].right_text == "uus"

    def test_uneven_replacement_tails_fall_through_as_pure_ops(self):
        # Two-line block becomes a three-line block: the first two
        # lines pair up as ``changed``, the leftover third right-line
        # falls through as a pure ``added``.
        left = "AAA\nBBB"
        right = "ZZZ\nYYY\nXXX"
        rows = compute_diff(left, right)
        kinds = [row.kind for row in rows]
        assert kinds == ["changed", "changed", "added"]
        assert rows[2].left_lineno is None
        assert rows[2].right_text == "XXX"

    def test_uneven_replacement_with_extra_left_lines_falls_through_as_removed(self):
        # Three-line block becomes a one-line block: the first lines
        # pair up as ``changed``, the leftover left-lines fall through
        # as pure ``removed``.
        left = "AAA\nBBB\nCCC"
        right = "ZZZ"
        rows = compute_diff(left, right)
        kinds = [row.kind for row in rows]
        assert kinds == ["changed", "removed", "removed"]
        assert rows[1].right_lineno is None
        assert rows[2].right_lineno is None


# ---------------------------------------------------------------------------
# render_diff_table — CSS class contract for the side-by-side view
# ---------------------------------------------------------------------------


class TestRenderDiffTable:
    def test_added_row_carries_diff_row_added_class(self):
        rows = [
            DiffRow(
                kind="added",
                left_lineno=None,
                left_text=None,
                right_lineno=1,
                right_text="uus",
            ),
        ]
        markup = to_xml(render_diff_table(rows))
        assert "diff-row-added" in markup
        # Generic row class always present so CSS hooks both selectors.
        assert "diff-row " in markup or 'class="diff-row diff-row-added"' in markup

    def test_removed_row_carries_diff_row_removed_class(self):
        rows = [
            DiffRow(
                kind="removed",
                left_lineno=1,
                left_text="vana",
                right_lineno=None,
                right_text=None,
            ),
        ]
        markup = to_xml(render_diff_table(rows))
        assert "diff-row-removed" in markup

    def test_changed_row_carries_diff_row_changed_class(self):
        rows = [
            DiffRow(
                kind="changed",
                left_lineno=1,
                left_text="enne",
                right_lineno=1,
                right_text="pärast",
            ),
        ]
        markup = to_xml(render_diff_table(rows))
        assert "diff-row-changed" in markup

    def test_unchanged_row_carries_diff_row_unchanged_class(self):
        rows = [
            DiffRow(
                kind="unchanged",
                left_lineno=1,
                left_text="sama",
                right_lineno=1,
                right_text="sama",
            ),
        ]
        markup = to_xml(render_diff_table(rows))
        assert "diff-row-unchanged" in markup

    def test_empty_input_renders_friendly_placeholder_row(self):
        markup = to_xml(render_diff_table([]))
        # Defensive: an empty diff is the success-no-difference case.
        # We still want a visible row so screen readers don't report
        # "no data" — that copy lives in version_diff.py and must
        # match here so a copy edit can't drift unnoticed.
        assert "Versioonide vahel erinevusi ei leitud." in markup
        assert "diff-table" in markup

    def test_lineno_gutter_renders_when_present(self):
        rows = [
            DiffRow(
                kind="unchanged",
                left_lineno=42,
                left_text="rida 42",
                right_lineno=42,
                right_text="rida 42",
            ),
        ]
        markup = to_xml(render_diff_table(rows))
        # Both gutters carry the diff-lineno class so the CSS picks them up.
        assert "diff-lineno" in markup
        assert ">42<" in markup

    def test_table_carries_aria_label_for_assistive_tech(self):
        rows = [
            DiffRow(
                kind="unchanged",
                left_lineno=1,
                left_text="x",
                right_lineno=1,
                right_text="x",
            ),
        ]
        markup = to_xml(render_diff_table(rows))
        # Estonian aria-label so screen readers announce the table
        # purpose in the user's language.
        assert "Versioonide erinevus" in markup


# ---------------------------------------------------------------------------
# Compose: end-to-end smoke that compute -> render survives FastHTML serialisation
# ---------------------------------------------------------------------------


class TestEndToEnd:
    def test_compute_then_render_yields_valid_table_markup(self):
        left = "alpha\nbeta\ngamma"
        right = "alpha\nBETA\ngamma\ndelta"
        rows = compute_diff(left, right)
        markup = to_xml(render_diff_table(rows))
        # Expect: unchanged, changed, unchanged, added — verify each
        # kind appears at least once in the rendered markup.
        assert "diff-row-unchanged" in markup
        assert "diff-row-changed" in markup
        assert "diff-row-added" in markup
        # Side-by-side: both old and new column headers in Estonian.
        assert "Vana versioon" in markup
        assert "Uus versioon" in markup
