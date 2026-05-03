"""Side-by-side diff renderer for draft versions (#618 PR-C).

Two public functions:

* :func:`compute_diff` — pure-Python diff between two text blobs that
  returns a flat list of :class:`DiffRow` records (one per visible
  line on either side).  Backed by :class:`difflib.SequenceMatcher`
  so we get clean ``unchanged`` / ``added`` / ``removed`` /
  ``changed`` blocks instead of the +/- noise that
  :func:`difflib.unified_diff` produces.

* :func:`render_diff_table` — render the rows as a side-by-side
  ``<table class="diff-table">``.  Emits stable ``diff-row diff-row-{kind}``
  classes so :file:`app/static/css/ui.css` can pick up the colors
  without further coupling.

Both functions are deliberately framework-agnostic on the
``compute_diff`` side (no FastHTML imports) so the diff engine can
be exercised in unit tests without spinning up the full app.

Design notes:
    * Rows are flat — even ``changed`` blocks emit a single row carrying
      both the old and the new text.  This keeps the side-by-side table
      naturally aligned: the number of rows on the left always equals
      the number of rows on the right (None-padding for pure additions
      / removals).
    * ``replace`` opcodes are aligned line-by-line up to the shorter
      side and the leftover lines fall through as a tail of pure
      additions or removals.  This mirrors what GitHub's split diff
      does for unequal-size blocks and avoids a second n×m alignment
      pass that would dominate the cost for large files.
    * Line numbers are 1-based per side and ``None`` when the row has
      no counterpart on that side (typical for ``added`` / ``removed``).
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from typing import Any, Literal

from fasthtml.common import *  # noqa: F403

DiffKind = Literal["unchanged", "added", "removed", "changed"]


@dataclass(frozen=True)
class DiffRow:
    """One visible row in the side-by-side diff table.

    Attributes:
        kind: One of ``unchanged`` / ``added`` / ``removed`` /
            ``changed``.  Drives the row CSS class.
        left_lineno: 1-based line number on the *left* (older) side,
            or ``None`` when this row has no counterpart on the left
            (i.e. a pure addition).
        left_text: The text content of the left line.  ``None`` when
            ``left_lineno`` is ``None``.
        right_lineno: 1-based line number on the *right* (newer) side,
            or ``None`` for pure removals.
        right_text: The text content of the right line.  ``None`` when
            ``right_lineno`` is ``None``.
    """

    kind: DiffKind
    left_lineno: int | None
    left_text: str | None
    right_lineno: int | None
    right_text: str | None


def _split_lines(text: str) -> list[str]:
    """Split *text* into lines without trailing newline characters.

    ``str.splitlines`` matches Python's universal-newline handling,
    which means we treat ``\\r\\n``, ``\\n``, and ``\\r`` interchangeably
    — useful when one version was uploaded from Windows and the next
    from macOS or Linux.  Empty strings split to an empty list, which
    naturally renders as "no rows" in the table.
    """
    return text.splitlines()


def compute_diff(left_text: str, right_text: str) -> list[DiffRow]:
    """Return a side-by-side diff of *left_text* vs *right_text*.

    Identical inputs yield a list of ``unchanged`` rows (one per
    line).  An empty input on either side yields a list of pure
    ``added`` / ``removed`` rows for the non-empty side.

    The implementation uses :class:`difflib.SequenceMatcher` so
    callers get explicit ``unchanged`` / ``added`` / ``removed`` /
    ``changed`` opcodes rather than the +/- noise of
    :func:`difflib.unified_diff`.
    """
    left_lines = _split_lines(left_text)
    right_lines = _split_lines(right_text)
    matcher = difflib.SequenceMatcher(a=left_lines, b=right_lines, autojunk=False)

    rows: list[DiffRow] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for offset in range(i2 - i1):
                rows.append(
                    DiffRow(
                        kind="unchanged",
                        left_lineno=i1 + offset + 1,
                        left_text=left_lines[i1 + offset],
                        right_lineno=j1 + offset + 1,
                        right_text=right_lines[j1 + offset],
                    )
                )
        elif tag == "delete":
            for offset in range(i2 - i1):
                rows.append(
                    DiffRow(
                        kind="removed",
                        left_lineno=i1 + offset + 1,
                        left_text=left_lines[i1 + offset],
                        right_lineno=None,
                        right_text=None,
                    )
                )
        elif tag == "insert":
            for offset in range(j2 - j1):
                rows.append(
                    DiffRow(
                        kind="added",
                        left_lineno=None,
                        left_text=None,
                        right_lineno=j1 + offset + 1,
                        right_text=right_lines[j1 + offset],
                    )
                )
        elif tag == "replace":
            # Align the two sides line-by-line up to the shorter
            # length; the tail (if any) falls through as a pure add
            # or pure remove.  Mirrors GitHub's split-diff behaviour
            # for unequal blocks and keeps the per-row alignment
            # cost O(min(n, m)) rather than O(n*m).
            left_block = left_lines[i1:i2]
            right_block = right_lines[j1:j2]
            common = min(len(left_block), len(right_block))
            for offset in range(common):
                rows.append(
                    DiffRow(
                        kind="changed",
                        left_lineno=i1 + offset + 1,
                        left_text=left_block[offset],
                        right_lineno=j1 + offset + 1,
                        right_text=right_block[offset],
                    )
                )
            for offset in range(common, len(left_block)):
                rows.append(
                    DiffRow(
                        kind="removed",
                        left_lineno=i1 + offset + 1,
                        left_text=left_block[offset],
                        right_lineno=None,
                        right_text=None,
                    )
                )
            for offset in range(common, len(right_block)):
                rows.append(
                    DiffRow(
                        kind="added",
                        left_lineno=None,
                        left_text=None,
                        right_lineno=j1 + offset + 1,
                        right_text=right_block[offset],
                    )
                )
        else:  # pragma: no cover — SequenceMatcher only emits the four tags above.
            raise AssertionError(f"Unexpected SequenceMatcher tag: {tag!r}")

    return rows


def _format_lineno(lineno: int | None) -> str:
    """Render a 1-based line number, or a non-breaking space placeholder.

    A non-breaking space (``\\u00a0``) keeps the gutter cell from
    collapsing on rows where one side has no counterpart, which would
    otherwise leave the row visibly off-grid.
    """
    return str(lineno) if lineno is not None else " "


def _format_cell_text(text: str | None) -> str:
    """Render a cell's text content; empty strings become a non-breaking space.

    This guarantees the ``<td>`` always has visible content (so the
    cell height matches its sibling row) while still rendering the
    real text verbatim when present.
    """
    if text is None or text == "":
        return " "
    return text


def render_diff_table(rows: list[DiffRow]) -> Any:
    """Render *rows* as a side-by-side ``<table class="diff-table">``.

    Each row carries a ``diff-row diff-row-{kind}`` class so the CSS
    in :file:`app/static/css/ui.css` can highlight added / removed /
    changed lines.  The line-number gutters are stamped with
    ``diff-lineno`` so they can be visually de-emphasised independent
    of the row colour.

    Empty input yields a table with a single placeholder row so we
    never render a totally empty ``<tbody>`` (which screen readers
    report as "no data" — confusing when the real meaning is "no
    differences").
    """
    if not rows:
        return Table(  # noqa: F405
            Tbody(  # noqa: F405
                Tr(  # noqa: F405
                    Td(  # noqa: F405
                        "Versioonide vahel erinevusi ei leitud.",
                        colspan="4",
                        cls="diff-empty",
                    ),
                ),
            ),
            cls="diff-table",
            role="table",
            aria_label="Versioonide erinevus",
        )

    body_rows: list[Any] = []
    for row in rows:
        body_rows.append(
            Tr(  # noqa: F405
                Td(  # noqa: F405
                    _format_lineno(row.left_lineno),
                    cls="diff-lineno diff-lineno-left",
                ),
                Td(  # noqa: F405
                    _format_cell_text(row.left_text),
                    cls="diff-text diff-text-left",
                ),
                Td(  # noqa: F405
                    _format_lineno(row.right_lineno),
                    cls="diff-lineno diff-lineno-right",
                ),
                Td(  # noqa: F405
                    _format_cell_text(row.right_text),
                    cls="diff-text diff-text-right",
                ),
                cls=f"diff-row diff-row-{row.kind}",
            )
        )

    return Table(  # noqa: F405
        Thead(  # noqa: F405
            Tr(  # noqa: F405
                Th("Rida", cls="diff-lineno-header", scope="col"),  # noqa: F405
                Th("Vana versioon", scope="col"),  # noqa: F405
                Th("Rida", cls="diff-lineno-header", scope="col"),  # noqa: F405
                Th("Uus versioon", scope="col"),  # noqa: F405
            ),
        ),
        Tbody(*body_rows),  # noqa: F405
        cls="diff-table",
        role="table",
        aria_label="Versioonide erinevus",
    )
