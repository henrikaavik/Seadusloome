"""DataTable component — sortable, responsive HTML table.

Follows the design system spec §4.3 (DataTable/Pagination catalog) and
NFR §10.2 (sortable columns announce sort state via ``aria-sort``).

Design notes:
    - Columns are declared as ``Column`` dataclasses with optional custom
      ``render`` callables for per-cell markup.
    - Sortable headers toggle direction via an HTMX ``hx-get`` to the same
      URL with ``?sort=<key>&dir=<asc|desc>`` query params. The server
      re-renders the table and HTMX swaps it in place.
    - On viewports narrower than 768px the table collapses to a stacked
      card layout via CSS alone (``data-label`` attributes drive the
      pseudo-element labels — no JS needed).
    - Empty tables render a single centered "no data" row. EmptyState is
      intentionally not imported to avoid a circular dependency with the
      wider feedback module.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from fasthtml.common import *  # noqa: F403

Align = Literal["left", "center", "right"]
SortDir = Literal["asc", "desc"]


@dataclass(frozen=True)
class Column:
    """Declarative column definition for :func:`DataTable`.

    Attributes:
        key: Dict key used to look up the raw value from each row.
        label: Human-readable header text (Estonian).
        sortable: When True the header becomes a clickable sort toggle.
        align: Horizontal alignment for both the header and its cells.
        render: Optional callable ``(row) -> FT`` for custom cell markup.
            When omitted the raw ``row[key]`` value is stringified.
    """

    key: str
    label: str
    sortable: bool = True
    align: Align = "left"
    render: Callable[[dict[str, Any]], Any] | None = None


def _sort_indicator(col: Column, sort_by: str | None, sort_dir: SortDir) -> str:
    """Return a unicode arrow reflecting the current sort state."""
    if not col.sortable:
        return ""
    if sort_by != col.key:
        return " \u2195"  # up-down arrow — sortable but not active
    return " \u25b2" if sort_dir == "asc" else " \u25bc"


def _aria_sort(col: Column, sort_by: str | None, sort_dir: SortDir) -> str:
    """Map column state to the correct ``aria-sort`` token (NFR §10.2)."""
    if not col.sortable:
        return "none"
    if sort_by != col.key:
        return "none"
    return "ascending" if sort_dir == "asc" else "descending"


def _next_dir(col: Column, sort_by: str | None, sort_dir: SortDir) -> SortDir:
    """Toggle direction when re-sorting by the currently active column."""
    if sort_by == col.key and sort_dir == "asc":
        return "desc"
    return "asc"


def _header_cell(col: Column, sort_by: str | None, sort_dir: SortDir):
    """Render a ``<th>`` — plain label or an HTMX sort toggle link."""
    align_cls = f"text-{col.align}" if col.align != "left" else ""
    aria_sort = _aria_sort(col, sort_by, sort_dir)

    if not col.sortable:
        classes = f"data-table-th {align_cls}".strip()
        return Th(col.label, cls=classes, scope="col", aria_sort=aria_sort)  # noqa: F405

    next_dir = _next_dir(col, sort_by, sort_dir)
    indicator = _sort_indicator(col, sort_by, sort_dir)
    classes = f"data-table-th data-table-sortable {align_cls}".strip()
    link = A(  # noqa: F405
        f"{col.label}{indicator}",
        href=f"?sort={col.key}&dir={next_dir}",
        hx_get=f"?sort={col.key}&dir={next_dir}",
        hx_target="closest .data-table-wrapper",
        hx_swap="outerHTML",
        cls="data-table-sort-link",
    )
    return Th(link, cls=classes, scope="col", aria_sort=aria_sort)  # noqa: F405


def _cell(col: Column, row: dict[str, Any]):
    """Render a ``<td>`` using the column's custom ``render`` if provided."""
    content = col.render(row) if col.render is not None else str(row.get(col.key, ""))
    align_cls = f"text-{col.align}" if col.align != "left" else ""
    classes = f"data-table-td {align_cls}".strip()
    return Td(content, cls=classes, data_label=col.label)  # noqa: F405


def DataTable(
    columns: list[Column],
    rows: list[dict[str, Any]],
    *,
    sort_by: str | None = None,
    sort_dir: SortDir = "asc",
    empty_message: str = "Andmed puuduvad",
    cls: str = "",
    **kwargs,
):
    """Render a sortable, responsive data table.

    Args:
        columns: Column definitions in display order.
        rows: Sequence of row dicts. Missing keys render as empty strings.
        sort_by: Key of the currently-sorted column, or ``None`` for unsorted.
        sort_dir: Current sort direction (``asc`` / ``desc``).
        empty_message: Estonian message shown when ``rows`` is empty.
        cls: Extra classes appended to the wrapper div.

    Returns:
        An ``FT`` tree: ``<div class="data-table-wrapper"> <table>...</table> </div>``.
    """
    header = Thead(  # noqa: F405
        Tr(*[_header_cell(c, sort_by, sort_dir) for c in columns])  # noqa: F405
    )

    if rows:
        body_rows = [Tr(*[_cell(c, row) for c in columns]) for row in rows]  # noqa: F405
    else:
        body_rows = [
            Tr(  # noqa: F405
                Td(  # noqa: F405
                    empty_message,
                    colspan=str(len(columns)),
                    cls="data-table-empty",
                ),
            )
        ]
    body = Tbody(*body_rows)  # noqa: F405

    wrapper_cls = f"data-table-wrapper {cls}".strip()
    return Div(  # noqa: F405
        Table(header, body, cls="data-table", role="table"),  # noqa: F405
        cls=wrapper_cls,
        **kwargs,
    )
