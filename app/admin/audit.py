"""Admin audit log page, filtering helpers, and CSV export."""

from __future__ import annotations

import csv
import io
import json
import logging
from datetime import date, datetime
from urllib.parse import urlencode

from fasthtml.common import *  # noqa: F403
from starlette.requests import Request
from starlette.responses import Response

from app.db import get_connection as _connect
from app.ui.data.data_table import Column, DataTable
from app.ui.data.pagination import Pagination
from app.ui.forms.app_form import AppForm
from app.ui.layout import PageShell
from app.ui.primitives.button import Button  # noqa: F401, F811  -- shadow guard
from app.ui.surfaces.card import Card, CardBody, CardHeader
from app.ui.theme import get_theme_from_request
from app.ui.time import format_tallinn

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


def _parse_date(value: str | None) -> date | None:
    """Parse a date string (YYYY-MM-DD) or return None."""
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _parse_actions(value: str | list[str] | None) -> list[str]:
    """Parse a multi-select action filter (CSV string or list) into list[str]."""
    if value is None:
        return []
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    return [str(v).strip() for v in value if str(v).strip()]


def _build_audit_where(
    *,
    action: str | None = None,
    actions: list[str] | None = None,
    user_id: str | None = None,
    org_id: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    query: str | None = None,
) -> tuple[str, list]:
    """Build WHERE clause and params for filtered audit queries.

    ``action`` keeps the old single-value contract; ``actions`` is the new
    multi-select list (rendered as ``IN (...)``). Both are merged so callers
    can pass either.
    """
    clauses: list[str] = []
    params: list = []

    combined_actions = list(actions or [])
    if action:
        combined_actions.append(action)
    # De-dup while preserving order so SQL placeholders match params 1:1
    seen: set[str] = set()
    deduped: list[str] = []
    for act in combined_actions:
        if act and act not in seen:
            seen.add(act)
            deduped.append(act)

    if len(deduped) == 1:
        clauses.append("a.action = %s")
        params.append(deduped[0])
    elif len(deduped) > 1:
        placeholders = ", ".join(["%s"] * len(deduped))
        clauses.append(f"a.action IN ({placeholders})")
        params.extend(deduped)

    if user_id:
        clauses.append("a.user_id = %s")
        params.append(user_id)
    if org_id:
        clauses.append("u.org_id = %s")
        params.append(org_id)
    if date_from:
        clauses.append("a.created_at >= %s")
        params.append(datetime.combine(date_from, datetime.min.time()))
    if date_to:
        clauses.append("a.created_at < %s")
        # Use day after to include all entries on the to-date
        from datetime import timedelta

        params.append(datetime.combine(date_to + timedelta(days=1), datetime.min.time()))
    if query:
        clauses.append("a.detail::text ILIKE %s")
        params.append(f"%{query}%")

    where = " AND ".join(clauses) if clauses else "TRUE"
    return where, params


def _get_audit_log_page(
    page: int = 1,
    per_page: int = 25,
    *,
    action: str | None = None,
    actions: list[str] | None = None,
    user_id: str | None = None,
    org_id: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    query: str | None = None,
) -> tuple[list[dict], int]:  # type: ignore[type-arg]
    """Return a page of audit_log entries and total count, with optional filters."""
    entries: list[dict] = []  # type: ignore[type-arg]
    total = 0
    try:
        where, params = _build_audit_where(
            action=action,
            actions=actions,
            user_id=user_id,
            org_id=org_id,
            date_from=date_from,
            date_to=date_to,
            query=query,
        )
        with _connect() as conn:
            row = conn.execute(
                f"SELECT COUNT(*) FROM audit_log a "  # type: ignore[arg-type]
                f"LEFT JOIN users u ON u.id = a.user_id WHERE {where}",
                params,
            ).fetchone()
            total = row[0] if row else 0

            offset = (page - 1) * per_page
            rows = conn.execute(
                f"SELECT a.id, a.user_id, u.full_name, a.action, a.detail, a.created_at, "  # type: ignore[arg-type]
                f"u.org_id "
                f"FROM audit_log a "
                f"LEFT JOIN users u ON u.id = a.user_id "
                f"WHERE {where} "
                f"ORDER BY a.created_at DESC LIMIT %s OFFSET %s",
                [*params, per_page, offset],
            ).fetchall()
            entries = [
                {
                    "id": r[0],
                    "user_id": str(r[1]) if r[1] else None,
                    "user_name": r[2] or "Süsteem",
                    "action": r[3],
                    "detail": r[4],
                    "created_at": r[5],
                    "org_id": str(r[6]) if r[6] else None,
                }
                for r in rows
            ]
    except Exception:
        logger.exception("Failed to fetch audit log page %d", page)
    return entries, total


def _get_distinct_actions() -> list[str]:
    """Return sorted distinct action values from audit_log."""
    try:
        with _connect() as conn:
            rows = conn.execute("SELECT DISTINCT action FROM audit_log ORDER BY action").fetchall()
            return [r[0] for r in rows]
    except Exception:
        logger.exception("Failed to fetch distinct audit actions")
        return []


def _get_audit_users() -> list[dict]:  # type: ignore[type-arg]
    """Return users that have audit log entries (id + name)."""
    try:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT a.user_id, COALESCE(u.full_name, 'Süsteem') AS name "
                "FROM audit_log a "
                "LEFT JOIN users u ON u.id = a.user_id "
                "WHERE a.user_id IS NOT NULL "
                "ORDER BY name"
            ).fetchall()
            return [{"id": str(r[0]), "name": r[1]} for r in rows]
    except Exception:
        logger.exception("Failed to fetch audit users")
        return []


def _get_audit_orgs() -> list[dict]:  # type: ignore[type-arg]
    """Return orgs whose users have audit log entries (id + name).

    Used to populate the org-filter dropdown for admins who oversee
    multiple organisations. Empty list when the user has no orgs or the
    DB read fails.
    """
    try:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT o.id, o.name "
                "FROM audit_log a "
                "JOIN users u ON u.id = a.user_id "
                "JOIN organizations o ON o.id = u.org_id "
                "ORDER BY o.name"
            ).fetchall()
            return [{"id": str(r[0]), "name": r[1]} for r in rows]
    except Exception:
        logger.exception("Failed to fetch audit orgs")
        return []


def _get_audit_entry(entry_id: int) -> dict | None:  # type: ignore[type-arg]
    """Return a single audit entry by id, or None if not found."""
    try:
        with _connect() as conn:
            row = conn.execute(
                "SELECT a.id, a.user_id, u.full_name, a.action, a.detail, a.created_at "
                "FROM audit_log a "
                "LEFT JOIN users u ON u.id = a.user_id "
                "WHERE a.id = %s",
                (entry_id,),
            ).fetchone()
            if not row:
                return None
            return {
                "id": row[0],
                "user_id": str(row[1]) if row[1] else None,
                "user_name": row[2] or "Süsteem",
                "action": row[3],
                "detail": row[4],
                "created_at": row[5],
            }
    except Exception:
        logger.exception("Failed to fetch audit entry %s", entry_id)
        return None


def _get_all_filtered_entries(
    *,
    action: str | None = None,
    actions: list[str] | None = None,
    user_id: str | None = None,
    org_id: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    query: str | None = None,
) -> list[dict]:  # type: ignore[type-arg]
    """Return ALL matching audit entries (no pagination) for CSV export."""
    entries: list[dict] = []  # type: ignore[type-arg]
    try:
        where, params = _build_audit_where(
            action=action,
            actions=actions,
            user_id=user_id,
            org_id=org_id,
            date_from=date_from,
            date_to=date_to,
            query=query,
        )
        with _connect() as conn:
            rows = conn.execute(
                f"SELECT a.id, a.user_id, u.full_name, a.action, a.detail, a.created_at, "  # type: ignore[arg-type]
                f"u.org_id "
                f"FROM audit_log a "
                f"LEFT JOIN users u ON u.id = a.user_id "
                f"WHERE {where} "
                f"ORDER BY a.created_at DESC",
                params,
            ).fetchall()
            entries = [
                {
                    "id": r[0],
                    "user_id": str(r[1]) if r[1] else "",
                    "user_name": r[2] or "Süsteem",
                    "action": r[3],
                    "detail": r[4],
                    "created_at": r[5],
                    "org_id": str(r[6]) if r[6] else "",
                }
                for r in rows
            ]
    except Exception:
        logger.exception("Failed to fetch all audit entries for export")
    return entries


# ---------------------------------------------------------------------------
# Detail rendering helpers
# ---------------------------------------------------------------------------


# Per-action one-liner builders.  Each returns a short Estonian summary; the
# full JSON is always available in the expander.  Falling back to "Vaata
# detaile" keeps the UI scannable for action types we have not curated yet.
def _summarize_detail(action: str, detail: object) -> str:
    """Return a short Estonian summary line for the detail JSONB cell.

    The summary is deterministic and never raises; on unrecognised shapes
    it falls back to a generic "Vaata detaile" label so the expander stays
    useful.
    """
    if detail is None:
        return "—"
    if not isinstance(detail, dict):
        # JSON scalars: fall back to a short stringification.
        text = str(detail)
        return text if len(text) <= 80 else text[:77] + "…"

    d: dict = detail  # type: ignore[type-arg]

    # Action-specific shortcuts (most frequent action types first).
    if action == "user.login":
        return f"Sisselogimine ({d.get('email', d.get('user_email', '—'))})"
    if action == "user.logout":
        return "Väljalogimine"
    if action == "user.login_failed":
        return f"Sisselogimine ebaõnnestus ({d.get('email', '—')})"
    if action.startswith("doc.upload"):
        fname = d.get("filename") or d.get("file_name") or d.get("draft_title")
        return f"Üleslaadimine: {fname}" if fname else "Üleslaadimine"
    if action.startswith("doc.delete"):
        return f"Kustutamine: {d.get('draft_id') or d.get('id', '—')}"
    if action.startswith("draft."):
        title = d.get("title") or d.get("draft_title") or d.get("draft_id")
        return f"Eelnõu: {title}" if title else "Eelnõu sündmus"
    if action.startswith("chat."):
        cid = d.get("conversation_id") or d.get("id")
        return f"Vestlus: {cid}" if cid else "Vestlus"
    if action.startswith("admin.") or action.startswith("org."):
        target = d.get("target") or d.get("name") or d.get("id")
        return f"Haldussündmus: {target}" if target else "Haldussündmus"

    # Generic fallbacks: prefer a "message" / "summary" field, otherwise
    # show the first ~80 chars of the serialised JSON.
    for key in ("message", "summary", "title", "name"):
        if key in d and d[key]:
            text = str(d[key])
            return text if len(text) <= 80 else text[:77] + "…"

    text = json.dumps(d, ensure_ascii=False, default=str)
    return text if len(text) <= 80 else text[:77] + "…"


def _format_detail_json(detail: object) -> str:
    """Return pretty-printed JSON (UTF-8, 2-space indent) for the expander."""
    if detail is None:
        return ""
    if isinstance(detail, str):
        # Already-stringified JSON: try to parse + re-pretty-print.
        try:
            parsed = json.loads(detail)
            return json.dumps(parsed, ensure_ascii=False, indent=2, default=str)
        except (ValueError, TypeError):
            return detail
    try:
        return json.dumps(detail, ensure_ascii=False, indent=2, default=str)
    except (TypeError, ValueError):
        return str(detail)


def _audit_detail_cell(entry: dict) -> object:  # type: ignore[type-arg]
    """Render the ``Detailid`` table cell with a server-side expander.

    The cell shows a one-line Estonian summary by default; clicking the
    disclosure triangle fetches the formatted JSON via HTMX from
    ``/admin/audit/detail/{id}``.  Empty details render as an em-dash.
    """
    detail = entry.get("detail")
    if detail is None:
        return "—"
    summary = _summarize_detail(entry.get("action", ""), detail)
    target_id = f"audit-detail-{entry['id']}"
    return Details(  # noqa: F405
        Summary(summary),  # noqa: F405
        Div(  # noqa: F405
            P("Laen detaile…", cls="muted-text"),  # noqa: F405
            id=target_id,
            hx_get=f"/admin/audit/detail/{entry['id']}",
            hx_trigger="toggle from:closest details once",
            hx_swap="innerHTML",
        ),
        cls="audit-detail",
    )


# ---------------------------------------------------------------------------
# Filter form component
# ---------------------------------------------------------------------------


def _extract_filters(req: Request) -> dict:  # type: ignore[type-arg]
    """Extract filter values from query params.

    ``actions`` (plural) accepts multi-select values via repeated
    ``actions=...`` query params; the legacy ``action`` (singular) is
    still honoured for backward compatibility and merged in.
    """
    actions = req.query_params.getlist("actions") if hasattr(req.query_params, "getlist") else []
    return {
        "action": req.query_params.get("action", ""),
        "actions": [a for a in actions if a],
        "user": req.query_params.get("user", ""),
        "org": req.query_params.get("org", ""),
        "from": req.query_params.get("from", ""),
        "to": req.query_params.get("to", ""),
        "query": req.query_params.get("query", ""),
    }


def _filter_querystring(filters: dict) -> str:  # type: ignore[type-arg]
    """Encode filters (incl. repeated ``actions``) into a stable query string.

    Used both for pagination links and the CSV export href so a filtered
    view round-trips across page navigation and downloads.
    """
    parts: list[tuple[str, str]] = []
    for key in ("action", "user", "org", "from", "to", "query"):
        val = filters.get(key)
        if val:
            parts.append((key, str(val)))
    for act in filters.get("actions", []) or []:
        if act:
            parts.append(("actions", act))
    return urlencode(parts)


def _audit_filter_form(
    filters: dict,  # type: ignore[type-arg]
    actions: list[str],
    users: list[dict],  # type: ignore[type-arg]
    orgs: list[dict] | None = None,  # type: ignore[type-arg]
) -> object:
    """Render the audit log filter controls.

    The action picker is a native multi-select so admins can OR several
    action types together; the org dropdown is rendered only when more
    than one org has audit traffic (single-org admins do not need it).
    """
    selected_actions = set(filters.get("actions") or [])
    if filters.get("action"):
        selected_actions.add(filters["action"])

    # Action multi-select
    action_options = []
    for act in actions:
        selected = "selected" if act in selected_actions else None
        action_options.append(Option(act, value=act, selected=selected))  # noqa: F405

    # User selector
    user_options = [Option("Kõik kasutajad", value="")]  # noqa: F405
    for u in users:
        selected = "selected" if filters["user"] == u["id"] else None
        user_options.append(Option(u["name"], value=u["id"], selected=selected))  # noqa: F405

    fields: list = [
        Div(  # noqa: F405
            Label("Tegevus", fr="filter-action"),  # noqa: F405
            Select(  # noqa: F405
                *action_options,
                name="actions",
                id="filter-action",
                multiple="multiple",
                size="4",
            ),
            P(  # noqa: F405
                "Hoia Ctrl/Cmd mitme valimiseks.",
                cls="muted-text filter-hint",
            ),
            cls="filter-field",
        ),
        Div(  # noqa: F405
            Label("Kasutaja", fr="filter-user"),  # noqa: F405
            Select(*user_options, name="user", id="filter-user"),  # noqa: F405
            cls="filter-field",
        ),
    ]

    # Org dropdown only when >1 org has audit traffic — single-org
    # deployments would just see a useless one-item filter.
    org_list = orgs or []
    if len(org_list) > 1:
        org_options = [Option("Kõik organisatsioonid", value="")]  # noqa: F405
        for o in org_list:
            selected = "selected" if filters.get("org") == o["id"] else None
            org_options.append(Option(o["name"], value=o["id"], selected=selected))  # noqa: F405
        fields.append(
            Div(  # noqa: F405
                Label("Organisatsioon", fr="filter-org"),  # noqa: F405
                Select(*org_options, name="org", id="filter-org"),  # noqa: F405
                cls="filter-field",
            )
        )

    fields.extend(
        [
            Div(  # noqa: F405
                Label("Alguskuupäev", fr="filter-from"),  # noqa: F405
                Input(  # noqa: F405
                    type="date", name="from", id="filter-from", value=filters["from"]
                ),
                cls="filter-field",
            ),
            Div(  # noqa: F405
                Label("Lõppkuupäev", fr="filter-to"),  # noqa: F405
                Input(type="date", name="to", id="filter-to", value=filters["to"]),  # noqa: F405
                cls="filter-field",
            ),
            Div(  # noqa: F405
                Label("Otsing detailides", fr="filter-query"),  # noqa: F405
                Input(  # noqa: F405
                    type="text",
                    name="query",
                    id="filter-query",
                    value=filters["query"],
                    placeholder="Otsi detailidest...",
                ),
                cls="filter-field",
            ),
        ]
    )

    return AppForm(
        Div(*fields, cls="filter-row"),  # noqa: F405
        Div(  # noqa: F405
            Button("Filtreeri", type="submit", variant="primary", size="sm"),
            A(  # noqa: F405
                "Tühjenda",
                href="/admin/audit",
                cls="btn btn-secondary btn-sm",
            ),
            cls="filter-actions",
        ),
        method="get",
        action="/admin/audit",
        cls="audit-filter-form",
    )


def _csv_export_link(filters: dict) -> object:  # type: ignore[type-arg]
    """Render an export-to-CSV link that respects the current filter set.

    Labelled "Ekspordi filtreeritud vaade" when any filter is active so it
    is obvious the download is scoped, not a full dump.
    """
    qs = _filter_querystring(filters)
    href = f"/admin/audit/export?{qs}" if qs else "/admin/audit/export"
    label = "Ekspordi filtreeritud vaade" if qs else "Ekspordi CSV"
    return A(  # noqa: F405
        label,
        href=href,
        cls="btn btn-secondary btn-sm",
        download="auditilogi.csv",
    )


def _filter_summary_text(filters: dict, total: int) -> str:  # type: ignore[type-arg]
    """Render the "Leitud X kirjet ..." count + active filter description."""
    parts: list[str] = [f"Leitud {total} kirjet"]
    active: list[str] = []
    actions = list(filters.get("actions") or [])
    if filters.get("action") and filters["action"] not in actions:
        actions.append(filters["action"])
    if actions:
        active.append("tegevus: " + ", ".join(actions))
    if filters.get("user"):
        active.append("kasutaja valitud")
    if filters.get("org"):
        active.append("organisatsioon valitud")
    if filters.get("from") or filters.get("to"):
        rng = f"{filters.get('from') or '…'}–{filters.get('to') or '…'}"
        active.append(f"kuupäev: {rng}")
    if filters.get("query"):
        active.append(f"otsing: '{filters['query']}'")
    if active:
        parts.append("Filtreerimisel: " + "; ".join(active))
    return ". ".join(parts) + "."


# ---------------------------------------------------------------------------
# Page handler
# ---------------------------------------------------------------------------


def _audit_results_content(
    entries: list[dict],  # type: ignore[type-arg]
    page: int,
    total_pages: int,
    per_page: int,
    total: int,
    filters: dict,  # type: ignore[type-arg]
) -> tuple:
    """Render the audit table + pagination (the swappable content)."""
    if not entries:
        # Empty state — distinguish "no audit traffic at all" from "no
        # match for current filters" so the admin knows whether to widen
        # the search or move on.
        has_active_filter = bool(_filter_querystring(filters))
        if has_active_filter:
            empty_msg = "Praeguste filtritega ei leitud kirjeid."
            empty_hint = A(  # noqa: F405
                "Tühjenda filtrid",
                href="/admin/audit",
                cls="btn btn-secondary btn-sm",
            )
            body: object = Div(  # noqa: F405
                P(empty_msg, cls="muted-text"),  # noqa: F405
                empty_hint,
                cls="empty-state",
            )
        else:
            body = P("Auditilogis kirjeid ei leitud.", cls="muted-text")  # noqa: F405
    else:
        columns = [
            Column(key="time", label="Aeg", sortable=False),
            Column(key="user_name", label="Kasutaja", sortable=False),
            Column(key="action", label="Tegevus", sortable=False),
            Column(key="detail", label="Detailid", sortable=False),
        ]
        rows = []
        for entry in entries:
            ts = entry["created_at"]
            rows.append(
                {
                    "time": format_tallinn(ts),
                    "user_name": entry["user_name"],
                    "action": entry["action"],
                    "detail": _audit_detail_cell(entry),
                }
            )
        body = DataTable(columns=columns, rows=rows)

    # Preserve current filters across pagination links.
    qs = _filter_querystring(filters)
    base_url = f"/admin/audit?{qs}" if qs else "/admin/audit"

    pagination = Pagination(
        current_page=page,
        total_pages=total_pages,
        base_url=base_url,
        page_size=per_page,
        total=total,
    )

    return body, pagination


def admin_audit_page(req: Request):
    """GET /admin/audit -- paginated, filterable audit log viewer.

    The body is wrapped in a top-level try/except so any backend failure
    (missing materialized view, transient DB error, malformed row) renders
    a styled error banner instead of bubbling up as a raw 500.

    Helpers are imported as locals inside the function body so the page
    works correctly when the function is rebound by the
    ``app.templates.admin_dashboard`` shim — that shim swaps
    ``__globals__`` to its own module dict, which means private helpers
    that live in this module cannot be resolved via the function's
    global namespace.
    """
    auth = req.scope.get("auth")
    theme = get_theme_from_request(req)
    try:
        # Bind module-private helpers as locals so __globals__ rebinding
        # by the admin_dashboard shim does not break name resolution.
        from app.admin.audit import (
            _audit_filter_form,
            _audit_results_content,
            _csv_export_link,
            _extract_filters,
            _filter_summary_text,
            _get_audit_log_page,
            _get_audit_orgs,
            _get_audit_users,
            _get_distinct_actions,
            _parse_date,
        )

        filters = _extract_filters(req)

        page_str = req.query_params.get("page", "1")
        try:
            page = max(1, int(page_str))
        except ValueError:
            page = 1

        per_page = 25
        action_filter = filters["action"] or None
        actions_multi = filters["actions"] or None
        user_filter = filters["user"] or None
        org_filter = filters["org"] or None
        date_from = _parse_date(filters["from"])
        date_to = _parse_date(filters["to"])
        query_filter = filters["query"] or None

        entries, total = _get_audit_log_page(
            page,
            per_page,
            action=action_filter,
            actions=actions_multi,
            user_id=user_filter,
            org_id=org_filter,
            date_from=date_from,
            date_to=date_to,
            query=query_filter,
        )
        total_pages = max(1, (total + per_page - 1) // per_page)

        # Fetch filter options
        actions = _get_distinct_actions()
        users = _get_audit_users()
        orgs = _get_audit_orgs()

        body, pagination = _audit_results_content(
            entries, page, total_pages, per_page, total, filters
        )

        filter_form = _audit_filter_form(filters, actions, users, orgs)
        export_link = _csv_export_link(filters)
        summary_text = _filter_summary_text(filters, total)

        content = (
            H1("Auditilogi", cls="page-title"),  # noqa: F405
            P(A("← Tagasi adminipaneelile", href="/admin"), cls="back-link"),  # noqa: F405
            Card(
                CardHeader(
                    Div(  # noqa: F405
                        H3("Filtrid", cls="card-title"),  # noqa: F405
                        export_link,
                        cls="card-header-row",
                    ),
                ),
                CardBody(filter_form),
            ),
            Card(
                CardHeader(
                    Div(  # noqa: F405
                        H3("Kirjed", cls="card-title"),  # noqa: F405
                        P(summary_text, cls="muted-text audit-result-summary"),  # noqa: F405
                        cls="card-header-stack",
                    ),
                ),
                CardBody(Div(body, pagination, id="audit-results")),  # noqa: F405
            ),
        )

        return PageShell(
            *content,
            title="Auditilogi",
            user=auth,
            theme=theme,
            active_nav="/admin",
        )
    except Exception:
        logger.exception("Failed to render admin audit page")
        from app.admin._shared import _render_admin_error_page

        return _render_admin_error_page(title="Auditilogi", user=auth, theme=theme)


def admin_audit_detail(req: Request):
    """GET /admin/audit/detail/{id} -- HTMX fragment with formatted JSON.

    Returns a small ``<pre>`` block (or a one-line muted message when the
    entry is missing or has no detail).  Used by the inline expander on
    the audit table; not a full PageShell.
    """
    try:
        from app.admin.audit import _format_detail_json, _get_audit_entry

        raw_id = req.path_params.get("id", "")
        try:
            entry_id = int(raw_id)
        except (TypeError, ValueError):
            return Response(
                content="Vigane ID.", status_code=400, media_type="text/plain; charset=utf-8"
            )

        entry = _get_audit_entry(entry_id)
        if entry is None:
            return P("Kirjet ei leitud.", cls="muted-text")  # noqa: F405

        detail = entry.get("detail")
        if detail is None:
            return P("Lisadetaile pole.", cls="muted-text")  # noqa: F405

        formatted = _format_detail_json(detail)
        return Pre(formatted, cls="audit-detail-json")  # noqa: F405
    except Exception:
        logger.exception("Failed to render audit detail fragment")
        return P("Detailide laadimine ebaõnnestus.", cls="muted-text")  # noqa: F405


# ---------------------------------------------------------------------------
# CSV export handler
# ---------------------------------------------------------------------------


def admin_audit_export(req: Request):
    """GET /admin/audit/export -- download filtered audit log as CSV.

    Helpers are imported as locals so this handler works correctly when
    rebound by the admin_dashboard shim. On error returns a styled
    error response rather than a raw 500.
    """
    try:
        # Bind module-private helpers as locals so __globals__ rebinding
        # by the admin_dashboard shim does not break name resolution.
        from app.admin.audit import (
            _extract_filters,
            _get_all_filtered_entries,
            _parse_date,
        )
        from app.ui.time import format_tallinn

        filters = _extract_filters(req)

        action_filter = filters["action"] or None
        actions_multi = filters["actions"] or None
        user_filter = filters["user"] or None
        org_filter = filters["org"] or None
        date_from = _parse_date(filters["from"])
        date_to = _parse_date(filters["to"])
        query_filter = filters["query"] or None

        entries = _get_all_filtered_entries(
            action=action_filter,
            actions=actions_multi,
            user_id=user_filter,
            org_id=org_filter,
            date_from=date_from,
            date_to=date_to,
            query=query_filter,
        )

        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(
            [
                "ID",
                "Kasutaja ID",
                "Kasutaja",
                "Organisatsioon ID",
                "Tegevus",
                "Detailid (JSON)",
                "Kuupäev",
            ]
        )

        for entry in entries:
            ts = entry["created_at"]
            raw_detail = entry.get("detail")
            if raw_detail is None:
                detail_str = ""
            elif isinstance(raw_detail, (dict, list)):
                detail_str = json.dumps(raw_detail, ensure_ascii=False, default=str)
            else:
                detail_str = str(raw_detail)
            writer.writerow(
                [
                    entry["id"],
                    entry["user_id"],
                    entry["user_name"],
                    entry.get("org_id", ""),
                    entry["action"],
                    detail_str,
                    format_tallinn(ts) if ts else "",
                ]
            )

        csv_content = output.getvalue()
        return Response(
            content=csv_content,
            media_type="text/csv",
            headers={
                "Content-Disposition": "attachment; filename=auditilogi.csv",
            },
        )
    except Exception:
        logger.exception("Failed to export admin audit log")
        return Response(
            content="Andmete eksportimine ebaõnnestus.",
            status_code=500,
            media_type="text/plain; charset=utf-8",
        )
