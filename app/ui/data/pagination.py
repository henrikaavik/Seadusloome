"""Pagination component — page navigation controls for DataTable et al.

Follows the design system spec §4.3 and NFR §10 (accessible nav landmark,
``aria-current="page"`` on the active link, Estonian copy).

Pagination links navigate via plain ``<a href>`` anchors. The HTMX swap
behaviour was removed in Phase 1.5 because the matching handlers return
full PageShell documents, not partials; partial swap is deferred to
Phase 2 when we add table-specific HTMX endpoints.
"""

from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from fasthtml.common import *  # noqa: F403

_ELLIPSIS = "\u2026"


def _build_url(base_url: str, page: int) -> str:
    """Return ``base_url`` with the ``page`` query parameter set to ``page``.

    Preserves any existing query string on ``base_url`` so callers can pass
    URLs that already include filters such as ``?sort=name&dir=asc``.
    Uses ``parse_qsl`` so percent-encoded values are decoded once and
    re-encoded once — no double encoding of multibyte characters.
    """
    parts = urlparse(base_url)
    existing = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != "page"]
    existing.append(("page", str(page)))
    new_query = urlencode(existing)
    return urlunparse(parts._replace(query=new_query))


def _page_window(current: int, total: int) -> list[int | str]:
    """Compute which page numbers to show: first, last, current, neighbors."""
    if total <= 7:
        return list(range(1, total + 1))

    pages: list[int | str] = [1]
    left = max(2, current - 1)
    right = min(total - 1, current + 1)

    if left > 2:
        pages.append(_ELLIPSIS)
    pages.extend(range(left, right + 1))
    if right < total - 1:
        pages.append(_ELLIPSIS)
    pages.append(total)
    return pages


def _link(label: str, url: str | None, *, active: bool = False, disabled: bool = False):
    """Render a single pagination control.

    Active page numbers and reachable navigation steps are anchors so they
    keep working without JavaScript. The disabled state is rendered as a
    real ``<button disabled>`` instead of a span — ``aria-disabled`` on a
    non-focusable element is invalid and AT-confusing.
    """
    if disabled or url is None:
        return Button(  # noqa: F405
            label,
            type="button",
            cls="pagination-link pagination-disabled",
            disabled=True,
        )

    classes = "pagination-link"
    attrs: dict = {}
    if active:
        classes += " pagination-current"
        attrs["aria_current"] = "page"

    return A(  # noqa: F405
        label,
        href=url,
        cls=classes,
        **attrs,
    )


def Pagination(
    *,
    current_page: int,
    total_pages: int,
    base_url: str,
    page_size: int | None = None,
    total: int | None = None,
    cls: str = "",
    **kwargs,
):
    """Render page controls plus an optional "X kuni Y kokku Z" info line.

    Args:
        current_page: 1-indexed current page.
        total_pages: Total number of pages (0 when there are no rows).
        base_url: URL to link back to; the ``page`` query param is overwritten.
        page_size: Rows per page — required together with ``total`` to render
            the info line.
        total: Grand total row count — required together with ``page_size``.
        cls: Extra classes appended to the wrapper.
    """
    current = max(1, current_page)
    total_pages = max(0, total_pages)

    controls: list = []

    prev_url = _build_url(base_url, current - 1) if current > 1 else None
    controls.append(_link("\u2039 Eelmine", prev_url, disabled=(current <= 1 or total_pages == 0)))

    if total_pages > 0:
        for page in _page_window(current, total_pages):
            if isinstance(page, str):
                controls.append(Span(page, cls="pagination-ellipsis", aria_hidden="true"))  # noqa: F405
            else:
                controls.append(
                    _link(
                        str(page),
                        _build_url(base_url, page),
                        active=(page == current),
                    )
                )

    next_url = _build_url(base_url, current + 1) if current < total_pages else None
    controls.append(
        _link("Järgmine \u203a", next_url, disabled=(current >= total_pages or total_pages == 0))
    )

    info_nodes: list = []
    if page_size is not None and total is not None:
        if total == 0:
            info_nodes.append(Span("0 kirjet", cls="pagination-info"))  # noqa: F405
        else:
            start = (current - 1) * page_size + 1
            end = min(current * page_size, total)
            info_nodes.append(
                Span(f"{start} kuni {end} kokku {total}", cls="pagination-info")  # noqa: F405
            )

    wrapper_cls = f"pagination-wrapper {cls}".strip()
    return Nav(  # noqa: F405
        Div(*controls, cls="pagination"),  # noqa: F405
        *info_nodes,
        cls=wrapper_cls,
        aria_label="Lehtede navigatsioon",
        **kwargs,
    )
