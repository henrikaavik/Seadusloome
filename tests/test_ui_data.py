"""Smoke tests for DataTable, Column, and Pagination components."""

from fasthtml.common import to_xml

from app.ui.data import Column, DataTable, Pagination


def _rows():
    return [
        {"id": 1, "name": "Alpha", "status": "active"},
        {"id": 2, "name": "Beta", "status": "inactive"},
    ]


def _cols():
    return [
        Column("id", "ID"),
        Column("name", "Nimi"),
        Column("status", "Olek", sortable=False),
    ]


def test_data_table_renders_columns_and_rows():
    html = to_xml(DataTable(_cols(), _rows()))
    assert "data-table" in html
    assert "Nimi" in html
    assert "Alpha" in html
    assert "Beta" in html
    assert "<table" in html
    assert "<thead" in html
    assert "<tbody" in html


def test_data_table_empty_state_shows_message():
    html = to_xml(DataTable(_cols(), [], empty_message="Midagi ei leitud"))
    assert "Midagi ei leitud" in html
    assert "data-table-empty" in html
    assert 'colspan="3"' in html


def test_data_table_sortable_column_has_aria_sort():
    html = to_xml(DataTable(_cols(), _rows(), sort_by="name", sort_dir="asc"))
    # Active sorted column announces direction
    assert 'aria-sort="ascending"' in html
    # Non-sortable column and unsorted sortable columns stay "none"
    assert 'aria-sort="none"' in html
    assert "data-table-sortable" in html


def test_data_table_sort_direction_toggles():
    html = to_xml(DataTable(_cols(), _rows(), sort_by="name", sort_dir="asc"))
    # Current ascending → next click should go descending
    assert "sort=name&amp;dir=desc" in html or "sort=name&dir=desc" in html


def test_data_table_custom_render_is_used():
    cols = [
        Column("id", "ID"),
        Column("name", "Nimi", render=lambda row: f"<<{row['name']}>>"),
    ]
    html = to_xml(DataTable(cols, _rows()))
    assert "&lt;&lt;Alpha&gt;&gt;" in html or "<<Alpha>>" in html


def test_data_table_cells_have_data_label_for_responsive_layout():
    html = to_xml(DataTable(_cols(), _rows()))
    assert 'data-label="Nimi"' in html


def test_pagination_shows_current_and_total():
    html = to_xml(
        Pagination(
            current_page=2,
            total_pages=5,
            base_url="/drafts",
            page_size=10,
            total=47,
        )
    )
    assert 'aria-label="Lehtede navigatsioon"' in html
    assert 'aria-current="page"' in html
    assert "11 kuni 20 kokku 47" in html
    assert "Eelmine" in html
    assert "Järgmine" in html


def test_pagination_first_page_disables_prev():
    html = to_xml(Pagination(current_page=1, total_pages=5, base_url="/x"))
    assert "pagination-disabled" in html
    # Prev control should be a real <button disabled>, not a span (#406).
    assert "<button" in html
    assert "disabled" in html


def test_pagination_last_page_disables_next():
    html = to_xml(Pagination(current_page=5, total_pages=5, base_url="/x"))
    assert "pagination-disabled" in html
    assert "<button" in html
    assert "disabled" in html


def test_pagination_with_zero_rows():
    html = to_xml(
        Pagination(
            current_page=1,
            total_pages=0,
            base_url="/drafts",
            page_size=10,
            total=0,
        )
    )
    assert "0 kirjet" in html
    # Both prev and next should be disabled
    assert html.count("pagination-disabled") >= 2


def test_pagination_preserves_existing_query_params():
    html = to_xml(Pagination(current_page=1, total_pages=3, base_url="/drafts?sort=name&dir=asc"))
    assert "sort=name" in html
    assert "dir=asc" in html
    assert "page=2" in html


def test_pagination_does_not_double_encode_multibyte_query():
    """``_build_url`` must not re-encode already-percent-encoded values (#405)."""
    html = to_xml(Pagination(current_page=1, total_pages=3, base_url="/drafts?q=%C3%BC&page=1"))
    # The single-encoded value should survive untouched.
    assert "q=%C3%BC" in html
    # And must not be double-encoded into %25C3%25BC.
    assert "%25C3%25BC" not in html


def test_data_table_sort_link_has_no_hx_attrs():
    """Sort headers must be plain anchors after the #404 fix."""
    html = to_xml(DataTable(_cols(), _rows(), sort_by="name", sort_dir="asc"))
    assert "hx-get" not in html
    assert "hx-target" not in html


def test_pagination_links_have_no_hx_attrs():
    """Pagination links must be plain anchors after the #404 fix."""
    html = to_xml(Pagination(current_page=2, total_pages=5, base_url="/x"))
    assert "hx-get" not in html
    assert "hx-target" not in html
