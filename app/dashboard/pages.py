"""Rendering + route layer for the ``/dashboard`` ("Töölaud") work queue.

This module owns the FastHTML/Starlette surface of the Töölaud: the page
handler, the per-section rendering helpers, the bookmark add/remove routes,
and :func:`register_dashboard_routes`. All data access is delegated to the
framework-free :mod:`app.dashboard.service` layer (the widget loaders + the
bookmark CRUD), so this file imports ``fasthtml`` / ``starlette`` while the
service layer stays import-clean for the Phase-5 public API / MCP server.

Issue #717 (epic #714, design doc ``docs/2026-05-11-ministry-lawyer-ui-structure.md``):
the dashboard is no longer a welcome page. It is a daily work queue that answers
"what should I do next" by synthesising signals already present in the database
(see :mod:`app.dashboard.service` for the per-widget query inventory).

The widget loaders are imported by name so this module holds a module-level
reference to each — tests patch ``app.dashboard.pages.<name>`` (patch-where-used)
to render :func:`dashboard_page` without a live DB, and patch
``app.dashboard.service.list_overdue_or_upcoming_transpositions`` /
``app.dashboard.service._connect`` for the data layer itself.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from fasthtml.common import *
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response

from app.analyysikeskus.eu_transposition import TranspositionDeadlineRow
from app.auth.audit import log_action
from app.auth.policy import ROLE_REVIEWER, ROLE_SYSTEM_ADMIN
from app.dashboard.service import (
    _MAX_EU_DEADLINES,
    _add_bookmark,
    _get_active_drafter_sessions,
    _get_awaiting_review_drafts,
    _get_bookmarks,
    _get_eu_transposition_deadlines,
    _get_high_risk_reports,
    _get_recent_exports,
    _get_recent_syncs,
    _get_stale_analysis_drafts,
    _get_unresolved_annotation_drafts,
    _get_unviewed_reports,
    _get_user_org_info,
    _remove_bookmark,
)
from app.drafter.state_machine import STEP_LABELS_ET, Step
from app.impact.scoring import IMPACT_BAND_LABELS_ET, ImpactBand, impact_band
from app.ui.capabilities import CAPABILITIES, Capability
from app.ui.components.capability_card import CapabilityCard
from app.ui.data.data_table import Column, DataTable
from app.ui.forms.app_form import AppForm
from app.ui.forms.form_field import FormField
from app.ui.layout import PageShell
from app.ui.primitives.badge import Badge, BadgeVariant
from app.ui.primitives.button import Button
from app.ui.primitives.icon import Icon
from app.ui.primitives.link_button import LinkButton
from app.ui.safe_url import is_safe_http_url
from app.ui.surfaces.card import Card, CardBody, CardHeader
from app.ui.theme import get_theme_from_request
from app.ui.time import format_tallinn

# The synthesised "next action" cap lives in the page layer (it bounds the
# rendered list, not a query). Mirrors the service-layer widget caps.
_MAX_NEXT_ACTIONS = 8


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------

_ROLE_LABELS = {
    "admin": "Administraator",
    "org_admin": "Organisatsiooni admin",
    "reviewer": "Ülevaataja",
    "drafter": "Koostaja",
}

# Badge variant per impact band — restrained colour, matches the design doc's
# "clear status and risk coding" guidance.
_BAND_VARIANT: dict[ImpactBand, BadgeVariant] = {
    "low": "default",
    "medium": "warning",
    "high": "danger",
    "critical": "danger",
}


# A muted single-line "nothing here" row shared by every collapsible section.
def _empty_row(text: str):  # type: ignore[no-untyped-def]
    return P(text, cls="muted-text")


def _step_label(step_number: int) -> str:
    """Estonian label for a drafter step number, falling back to the bare number."""
    try:
        return STEP_LABELS_ET.get(Step(step_number), str(step_number))
    except ValueError:
        return str(step_number)


def _section_card(title: str, body):  # type: ignore[no-untyped-def]
    """A compact section card — ``Card(CardHeader(H3(...)), CardBody(...))``."""
    return Card(
        CardHeader(H3(title, cls="card-title")),
        CardBody(body),
    )


# ---------------------------------------------------------------------------
# Section 0: "Mida soovid teha?" — capability discovery map (B2, issue #793)
# ---------------------------------------------------------------------------
#
# Sits at the very top of /dashboard above the operational work queue. The
# point is to give a brand-new lawyer with no drafts and no findings an
# immediate answer to "what can this system do?" — the work-queue widgets
# below are useless to them on day 1.
#
# The card list is derived from the B3 capability dictionary
# (:data:`app.ui.capabilities.CAPABILITIES`). We exclude two entries to
# avoid duplicating dashboard chrome that already exists:
#
#   * ``globaalne-otsing``  → target is ``/`` (handled by the top-bar search
#     bar B1 once that lands; would just bounce back to /dashboard today).
#   * ``el-tahtajad``       → already covered by the EU transposition
#     deadlines widget (A6) further down the page.
#
# Live capabilities render first (so a user landing fresh sees real entry
# points), then planned ones with a "Tulekul" badge — matching the
# Analüüsikeskus directory's affordance for consistency. We cap at
# :data:`_MAX_CAPABILITY_CARDS` so the section fits one screen on a laptop.

_MAX_CAPABILITY_CARDS = 9

# Slugs deliberately omitted from the capability map — see commentary above.
_CAPABILITY_MAP_EXCLUDED_SLUGS: frozenset[str] = frozenset(
    {
        "globaalne-otsing",
        "el-tahtajad",
    }
)


def _dashboard_capabilities() -> list[Capability]:
    """Return the capability list rendered in the Töölaud discovery map.

    Live entries first (preserving their canonical order from
    :data:`app.ui.capabilities.CAPABILITIES`), then planned entries. Excludes
    capabilities that would duplicate existing dashboard chrome and caps the
    result at :data:`_MAX_CAPABILITY_CARDS` so the grid stays compact.
    """
    live: list[Capability] = []
    planned: list[Capability] = []
    for cap in CAPABILITIES:
        if cap.slug in _CAPABILITY_MAP_EXCLUDED_SLUGS:
            continue
        if cap.status == "live":
            live.append(cap)
        elif cap.status == "planned":
            planned.append(cap)
        # Other statuses ("deferred", …) are not advertised.
    return (live + planned)[:_MAX_CAPABILITY_CARDS]


# Tiny inline script: honour a stored open/closed preference in localStorage,
# default to open on desktop and collapsed on mobile (≤768 px). Runs once on
# DOMContentLoaded; cheap enough to inline so the dashboard doesn't need yet
# another /static file.
_CAPABILITY_MAP_SCRIPT = """
(function () {
  var STORAGE_KEY = 'dashboard.capabilityMap.open';
  function apply(el) {
    var stored = null;
    try { stored = localStorage.getItem(STORAGE_KEY); } catch (e) { /* private mode */ }
    if (stored === 'true') {
      el.open = true;
    } else if (stored === 'false') {
      el.open = false;
    } else {
      // No stored preference → default open on desktop, collapsed on mobile.
      el.open = window.matchMedia('(min-width: 769px)').matches;
    }
    el.addEventListener('toggle', function () {
      try { localStorage.setItem(STORAGE_KEY, el.open ? 'true' : 'false'); }
      catch (e) { /* private mode */ }
    });
  }
  function init() {
    var el = document.getElementById('capability-map');
    if (el) { apply(el); }
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
"""


def _capability_map_section(capabilities: list[Capability]):  # type: ignore[no-untyped-def]
    """Render the collapsible "Mida soovid teha?" discovery section.

    Uses a native ``<details>`` element wrapped in a card so the collapsible
    state works without JavaScript (progressive enhancement). The companion
    inline script then layers localStorage persistence + a smart default
    (open on desktop, collapsed on mobile) on top.

    Returns ``None`` when there are no capabilities to show (keeps the empty-
    state policy uniform with the EU deadlines widget — never render an
    empty decorative box).
    """
    if not capabilities:
        return None

    cards = [CapabilityCard(cap) for cap in capabilities]

    # ``open`` is left out so the script is the single source of truth for
    # default state; without JS, browsers render <details> as closed.
    return Card(
        Details(  # noqa: F405
            Summary(  # noqa: F405
                Span("Mida soovid teha?", cls="capability-map__title"),
                Icon("chevron-down", cls="capability-map__chevron", aria_hidden=True),
                cls="capability-map__summary",
                # aria-label is announced by VoiceOver in place of the
                # summary's children — keeps the disclosure verb explicit.
                aria_label="Mida soovid teha? Töövoogude valik.",
            ),
            Div(*cards, cls="capability-map__grid"),  # noqa: F405
            id="capability-map",
            cls="capability-map",
        ),
        Script(_CAPABILITY_MAP_SCRIPT),  # noqa: F405
        cls="capability-map-card",
    )


# ---------------------------------------------------------------------------
# Section 1: Minu järgmised tegevused
# ---------------------------------------------------------------------------


def _build_next_actions(
    sessions: list[dict],  # type: ignore[type-arg]
    high_risk: list[dict],  # type: ignore[type-arg]
    stale: list[dict],  # type: ignore[type-arg]
    unviewed: list[dict],  # type: ignore[type-arg]
) -> list[dict]:  # type: ignore[type-arg]
    """Synthesise the "what should I do next" list from existing signals.

    This is *not* a new data source — it folds four already-loaded widget
    result sets into one prioritised list of ``{text, href, link_label}``
    dicts, capped at :data:`_MAX_NEXT_ACTIONS`. Order, and one row per draft
    (a draft that qualifies for several sources gets only the most-urgent
    framing): drafter sessions first (unfinished work), then stale analyses
    (the ontology moved on — re-analyse, even if the now-outdated report
    flags high risk), then high-risk reports (conflicts to review), then
    reports you simply haven't opened yet.
    """
    actions: list[dict[str, str]] = []
    seen_drafts: set[str] = set()

    for s in sessions:
        step_num = s["current_step"]
        actions.append(
            {
                "text": f"Jätka koostajas — {step_num}. samm: {_step_label(step_num)}",
                "href": f"/drafter/{s['id']}",
                "link_label": "Ava koostaja",
            }
        )

    for st in stale:
        did = st["draft_id"]
        if did in seen_drafts:
            continue
        seen_drafts.add(did)
        actions.append(
            {
                "text": (
                    f"«{st['title']}»: ontoloogia uuenes pärast aruande koostamist. "
                    "Analüüsi uuesti."
                ),
                "href": f"/drafts/{did}/report",
                "link_label": "Ava aruanne",
            }
        )

    for r in high_risk:
        did = r["draft_id"]
        if did in seen_drafts:
            continue
        seen_drafts.add(did)
        conflicts = r["conflict_count"]
        title = r["title"]
        if conflicts > 0:
            text = f"Vaata mõjuaruannet «{title}» — {conflicts} konflikti vajavad ülevaatust"
        else:
            band_label = IMPACT_BAND_LABELS_ET[impact_band(r["impact_score"])].lower()
            text = f"Vaata mõjuaruannet «{title}» — {band_label} (skoor {r['impact_score']}/100)"
        actions.append(
            {"text": text, "href": f"/drafts/{did}/report", "link_label": "Ava aruanne"}
        )

    for u in unviewed:
        did = u["draft_id"]
        if did in seen_drafts:
            continue
        seen_drafts.add(did)
        title = u["title"]
        if u["reanalyzed"]:
            text = f"«{title}»: eelnõu analüüsiti uuesti — vaata uut mõjuaruannet."
        else:
            text = f"Mõjuaruanne valmis: «{title}». Vaata aruannet."
        actions.append(
            {"text": text, "href": f"/drafts/{did}/report", "link_label": "Ava aruanne"}
        )

    return actions[:_MAX_NEXT_ACTIONS]


def _next_actions_card(actions: list[dict]):  # type: ignore[type-arg, no-untyped-def]
    if not actions:
        return _section_card("Minu järgmised tegevused", _empty_row("Hetkel pole midagi ootel."))
    items = [
        Li(
            Span(a["text"], cls="next-action-text"),
            LinkButton(a["link_label"], href=a["href"], variant="secondary", size="sm"),
            cls="next-action-item",
        )
        for a in actions
    ]
    return _section_card("Minu järgmised tegevused", Ul(*items, cls="next-action-list"))


# ---------------------------------------------------------------------------
# Section 2: Kõrge riskiga leiud
# ---------------------------------------------------------------------------


def _high_risk_card(reports: list[dict]):  # type: ignore[type-arg, no-untyped-def]
    if not reports:
        return _section_card(
            "Kõrge riskiga leiud", _empty_row("Kõrge riskiga mõjuaruandeid hetkel pole.")
        )
    columns = [
        Column(key="title", label="Eelnõu", sortable=False),
        Column(
            key="band",
            label="Risk",
            sortable=False,
            render=lambda r: Badge(r["band_label"], variant=r["band_variant"]),
        ),
        Column(key="counts", label="Leiud", sortable=False),
        Column(key="generated_at", label="Analüüsitud", sortable=False),
        Column(
            key="actions",
            label="",
            sortable=False,
            render=lambda r: A("Vaata aruannet", href=r["href"], cls="table-link"),
        ),
    ]
    rows = []
    for r in reports:
        band = impact_band(r["impact_score"])
        rows.append(
            {
                "title": r["title"],
                "band_label": IMPACT_BAND_LABELS_ET[band],
                "band_variant": _BAND_VARIANT[band],
                "counts": (
                    f"{r['conflict_count']} konflikti · {r['affected_count']} mõjutatud · "
                    f"{r['gap_count']} lünka"
                ),
                "generated_at": format_tallinn(r["generated_at"]),
                "href": f"/drafts/{r['draft_id']}/report",
            }
        )
    return _section_card("Kõrge riskiga leiud", DataTable(columns=columns, rows=rows))


# ---------------------------------------------------------------------------
# Section 2b: EL ülevõtu tähtajad (A6)
# ---------------------------------------------------------------------------
#
# Proactive surveillance widget — surfaces EU directives whose
# transposition deadline is within the next 90 days **and** Estonia's
# transposition status is not yet ``"kaetud"``. The widget is operational,
# not decorative: every row click-throughs to the existing EL ülevõtt
# workflow pre-filled with the directive's CELEX.
#
# Empty-state policy: hide the entire card when there are no rows. The
# dashboard already runs long, and an empty "no upcoming transpositions"
# message would be noise. Caller (``dashboard_page``) only includes the
# card in the page tree when :func:`_eu_deadlines_card` returns a node.


# Map a row's days_remaining + status onto the Estonian status text +
# badge variant the table renders. Severity order (badge colour):
#
#   Tähtaeg möödunud  → danger  (red)
#   Tähtaeg läheneb   → warning (amber) — within 30 days
#   Ülevõtt puudub    → danger  (red)   — no transposing act at all
#   Ülevõtt osaline   → warning (amber)
#   Ebaselge          → default (neutral)
#
# Time-based status wins over status-bucket: an overdue row is always
# rendered as "Tähtaeg möödunud" even if its transposition status is
# only "osaline" — the message is "act now", not "this is partial".


def _deadline_badge_variant(days_remaining: int) -> BadgeVariant:
    """Pick the deadline-cell badge colour from days_remaining."""
    if days_remaining < 0:
        return "danger"
    if days_remaining < 30:
        return "warning"
    return "default"


def _deadline_badge_label(days_remaining: int) -> str:
    """Pick the deadline-cell Estonian label from days_remaining."""
    if days_remaining < 0:
        # ``abs()`` keeps the surface non-negative; the colour already
        # signals "overdue".
        return f"Tähtaeg möödunud ({abs(days_remaining)} p)"
    if days_remaining == 0:
        return "Tähtaeg täna"
    if days_remaining < 30:
        return f"Tähtaeg {days_remaining} päeva"
    return f"{days_remaining} päeva"


_STATUS_LABELS_ET: dict[str, str] = {
    "puudub": "Ülevõtt puudub",
    "osaline": "Ülevõtt osaline",
    "ebaselge": "Ebaselge",
    "kaetud": "Üle võetud",  # never rendered (filtered out) but kept for completeness
}

_STATUS_VARIANTS: dict[str, BadgeVariant] = {
    "puudub": "danger",
    "osaline": "warning",
    "ebaselge": "default",
    "kaetud": "success",
}


def _format_deadline_date(d: Any) -> str:
    """Format a deadline ``date`` as ``DD.MM.YYYY`` (matches ``format_tallinn``)."""
    try:
        return d.strftime("%d.%m.%Y")
    except Exception:
        return str(d)


def _el_ulevott_link(celex: str) -> str:
    """Build a ``/analyysikeskus/el-ulevott?sisend=<celex>`` URL."""
    return "/analyysikeskus/el-ulevott?" + urlencode({"sisend": celex})


def _eu_deadlines_card(rows: list[TranspositionDeadlineRow]):  # type: ignore[no-untyped-def]
    """Render the "EL ülevõtu tähtajad" Töölaud widget.

    Returns ``None`` when there are no rows so the caller can omit the
    section entirely (per the A6 empty-state rule). Renders the top
    :data:`_MAX_EU_DEADLINES` rows; when more exist, a "Näita kõiki (X)"
    link at the bottom points at the EL ülevõtt workflow.
    """
    if not rows:
        return None

    total = len(rows)
    top_rows = rows[:_MAX_EU_DEADLINES]

    columns = [
        Column(
            key="deadline",
            label="Tähtaeg",
            sortable=False,
            render=lambda r: Badge(
                r["deadline_label"],
                variant=r["deadline_variant"],
            ),
        ),
        Column(key="celex", label="CELEX", sortable=False),
        Column(key="directive_label", label="Direktiiv", sortable=False),
        Column(
            key="status",
            label="Staatus",
            sortable=False,
            render=lambda r: Badge(r["status_label"], variant=r["status_variant"]),
        ),
        Column(
            key="actions",
            label="",
            sortable=False,
            render=lambda r: A(
                "Vaata ülevõttu →",
                href=r["href"],
                cls="table-link el-deadlines-action",
                # Operational widget — keep the touch-target obvious.
                aria_label=f"Vaata EL ülevõttu — {r['celex']}",
            ),
        ),
    ]
    table_rows = [
        {
            "celex": row.celex,
            "directive_label": row.directive_label_et,
            "deadline_label": (
                f"{_format_deadline_date(row.deadline)} · "
                f"{_deadline_badge_label(row.days_remaining)}"
            ),
            "deadline_variant": _deadline_badge_variant(row.days_remaining),
            "status_label": _STATUS_LABELS_ET.get(row.status, row.status),
            "status_variant": _STATUS_VARIANTS.get(row.status, "default"),
            "href": _el_ulevott_link(row.celex),
        }
        for row in top_rows
    ]

    body_children: list[Any] = [DataTable(columns=columns, rows=table_rows)]
    if total > _MAX_EU_DEADLINES:
        body_children.append(
            P(
                A(
                    f"Näita kõiki ({total}) →",
                    href="/analyysikeskus/el-ulevott?vaade=tahtajad",
                    cls="el-deadlines-show-all",
                ),
                cls="el-deadlines-show-all-row",
            )
        )

    return Card(
        CardHeader(H3("EL ülevõtu tähtajad", cls="card-title")),
        CardBody(*body_children),
    )


# ---------------------------------------------------------------------------
# Section 3: Aegunud analüüsid
# ---------------------------------------------------------------------------


def _stale_card(drafts: list[dict]):  # type: ignore[type-arg, no-untyped-def]
    if not drafts:
        return _section_card("Aegunud analüüsid", _empty_row("Aegunud analüüse pole."))
    items = [
        Li(
            Span(
                f"«{d['title']}» — ontoloogia uuenes, analüüsi uuesti "
                f"({d['stale_count']} aegunud märkust).",
                cls="stale-text",
            ),
            LinkButton(
                "Ava aruanne",
                href=f"/drafts/{d['draft_id']}/report",
                variant="secondary",
                size="sm",
            ),
            cls="stale-item",
        )
        for d in drafts
    ]
    return _section_card("Aegunud analüüsid", Ul(*items, cls="stale-list"))


# ---------------------------------------------------------------------------
# Section 4: Uued ontoloogia muudatused
# ---------------------------------------------------------------------------


def _syncs_card(syncs: list[dict]):  # type: ignore[type-arg, no-untyped-def]
    if not syncs:
        return _section_card(
            "Uued ontoloogia muudatused", _empty_row("Hiljutisi ontoloogia uuendusi pole.")
        )
    columns = [
        Column(key="finished_at", label="Uuendatud", sortable=False),
        Column(key="entity_count", label="Olemeid ontoloogias", sortable=False),
    ]
    rows = [
        {
            "finished_at": format_tallinn(s["finished_at"]),
            "entity_count": (
                f"{s['entity_count']:,}".replace(",", " ")
                if s["entity_count"] is not None
                else "—"
            ),
        }
        for s in syncs
    ]
    return _section_card("Uued ontoloogia muudatused", DataTable(columns=columns, rows=rows))


# ---------------------------------------------------------------------------
# Section 5: Hiljutised ekspordid
# ---------------------------------------------------------------------------


def _exports_card(exports: list[dict]):  # type: ignore[type-arg, no-untyped-def]
    if not exports:
        return _section_card("Hiljutised ekspordid", _empty_row("Hiljutisi eksporte pole."))
    items = [
        Li(
            Span(
                f"«{e['title']}» mõjuaruanne eksporditud"
                + (f" — {format_tallinn(e['finished_at'])}" if e["finished_at"] else ""),
                cls="export-text",
            ),
            LinkButton(
                "Ava aruanne",
                href=f"/drafts/{e['draft_id']}/report",
                variant="secondary",
                size="sm",
            ),
            cls="export-item",
        )
        for e in exports
    ]
    return _section_card("Hiljutised ekspordid", Ul(*items, cls="export-list"))


# ---------------------------------------------------------------------------
# Section 6: Eelnõud lahtiste märkustega
# ---------------------------------------------------------------------------


def _awaiting_review_card(drafts: list[dict]):  # type: ignore[type-arg, no-untyped-def]
    """Render the reviewer "Ülevaatuse järgi ootavad" widget (#817).

    Only called when the caller has the reviewer role — the dashboard
    page-handler gates this card behind a role check. Renders ``None``
    when no drafts await this reviewer so the caller can omit the
    section entirely (consistent with the EU deadlines widget).
    """
    if not drafts:
        return _section_card(
            "Ülevaatuse järgi ootavad",
            _empty_row("Hetkel pole eelnõusid, mis ootaks Teie ülevaatust."),
        )
    columns = [
        Column(key="title", label="Eelnõu", sortable=False),
        Column(
            key="created_at",
            label="Üles laaditud",
            sortable=False,
            render=lambda r: r["created_at_label"],
        ),
        Column(
            key="actions",
            label="",
            sortable=False,
            render=lambda r: A("Ava ülevaatuseks", href=r["href"], cls="table-link"),
        ),
    ]
    rows = [
        {
            "title": d["title"],
            "created_at_label": format_tallinn(d["created_at"]) if d["created_at"] else "—",
            "href": f"/drafts/{d['draft_id']}",
        }
        for d in drafts
    ]
    return _section_card("Ülevaatuse järgi ootavad", DataTable(columns=columns, rows=rows))


def _unresolved_card(drafts: list[dict]):  # type: ignore[type-arg, no-untyped-def]
    if not drafts:
        return _section_card(
            "Eelnõud lahtiste märkustega", _empty_row("Lahtiste märkustega eelnõusid pole.")
        )
    columns = [
        Column(key="title", label="Eelnõu", sortable=False),
        Column(
            key="unresolved_count",
            label="Lahtisi märkusi",
            sortable=False,
            render=lambda r: Badge(str(r["unresolved_count"]), variant="warning"),
        ),
        Column(
            key="actions",
            label="",
            sortable=False,
            render=lambda r: A("Vaata aruannet", href=r["href"], cls="table-link"),
        ),
    ]
    rows = [
        {
            "title": d["title"],
            "unresolved_count": d["unresolved_count"],
            "href": f"/drafts/{d['draft_id']}/report",
        }
        for d in drafts
    ]
    return _section_card("Eelnõud lahtiste märkustega", DataTable(columns=columns, rows=rows))


# ---------------------------------------------------------------------------
# Section 7: Minu järjehoidjad — KEPT VERBATIM
# ---------------------------------------------------------------------------


def _bookmarks_card(bookmarks: list[dict]):  # type: ignore[type-arg]
    """Render the bookmarks card (list + add form)."""
    if not bookmarks:
        table: object = P("Järjehoidjaid ei leitud.", cls="muted-text")
    else:
        columns = [
            Column(key="label", label="Nimi", sortable=False),
            Column(
                key="entity_uri",
                label="URI",
                sortable=False,
                # #848: render-side guard (defense in depth). Even though the
                # POST handler now validates before insert, legacy rows may
                # carry an unsafe ``javascript:`` / ``data:`` value — render
                # those as plain text, never as a clickable ``href``.
                render=lambda r: (
                    A(r["entity_uri"], href=r["entity_uri"])
                    if is_safe_http_url(r["entity_uri"])
                    else Span(r["entity_uri"], cls="muted-text")
                ),
            ),
            Column(
                key="actions",
                label="Tegevused",
                sortable=False,
                render=lambda r: AppForm(
                    Button(
                        "Eemalda",
                        type="submit",
                        variant="secondary",
                        size="sm",
                    ),
                    method="post",
                    action=f"/api/bookmarks/{r['id']}/delete",
                    cls="inline-form",
                ),
            ),
        ]
        rows = [
            {
                "id": bm["id"],
                "label": bm["label"] or bm["entity_uri"],
                "entity_uri": bm["entity_uri"],
            }
            for bm in bookmarks
        ]
        table = DataTable(columns=columns, rows=rows)

    add_form = AppForm(
        FormField(name="entity_uri", label="URI", type="text", required=True),
        FormField(name="label", label="Nimi", type="text"),
        Button("Lisa järjehoidja", type="submit", variant="primary"),
        method="post",
        action="/api/bookmarks",
        cls="bookmark-add-form",
    )

    return Card(
        CardHeader(H3("Järjehoidjad", cls="card-title")),
        CardBody(table, add_form),
    )


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------


def dashboard_page(req: Request):
    """GET /dashboard — operational work queue for authenticated users."""
    auth = req.scope.get("auth", {})
    theme = get_theme_from_request(req)
    user_id = auth.get("id")
    org_id = auth.get("org_id")
    full_name = auth.get("full_name", "Kasutaja")
    first_name = (full_name or "Kasutaja").split()[0] if full_name else "Kasutaja"

    if user_id:
        sessions = _get_active_drafter_sessions(user_id, org_id)
        high_risk = _get_high_risk_reports(org_id)
        unviewed = _get_unviewed_reports(user_id, org_id)
        stale = _get_stale_analysis_drafts(org_id)
        syncs = _get_recent_syncs()
        exports = _get_recent_exports(org_id)
        unresolved = _get_unresolved_annotation_drafts(org_id)
        bookmarks = _get_bookmarks(user_id)
        org_info = _get_user_org_info(user_id)
        eu_deadlines = _get_eu_transposition_deadlines(org_id)
        # #817: only reviewers (and system admins) get the
        # "Ülevaatuse järgi ootavad" widget — drafters / org admins
        # don't review, so the query and section are skipped.
        user_role = (org_info or {}).get("role") if org_info else None
        awaiting_review: list[dict] = []
        if user_role in (ROLE_REVIEWER, ROLE_SYSTEM_ADMIN):
            awaiting_review = _get_awaiting_review_drafts(user_id, org_id)
    else:
        sessions = high_risk = unviewed = stale = syncs = exports = unresolved = bookmarks = []
        org_info = None
        eu_deadlines = []
        awaiting_review = []

    next_actions = _build_next_actions(sessions, high_risk, stale, unviewed)

    # Compact header — no marketing hero. H1 + a small org/role line.
    if org_info is not None:
        role_label = _ROLE_LABELS.get(org_info["role"], org_info["role"])
        subtitle = Small(f"{org_info['org_name']} · {role_label}", cls="page-subtitle")
    else:
        subtitle = Small("Te ei kuulu ühtegi organisatsiooni.", cls="page-subtitle")

    # A6: the EU deadlines widget hides entirely when there are no rows
    # (no decorative empty box), so it's spliced in only when present.
    eu_deadlines_card = _eu_deadlines_card(eu_deadlines)

    # B2: "Mida soovid teha?" capability discovery map sits above the queue
    # so first-time users see what the system can do before scanning their
    # (possibly empty) work-queue. Hidden entirely when no capabilities are
    # eligible (keeps the empty-state policy uniform).
    capability_map_card = _capability_map_section(_dashboard_capabilities())

    content_parts: list[Any] = [
        H1(f"Tere, {first_name}", cls="page-title"),
        subtitle,
    ]
    if capability_map_card is not None:
        content_parts.append(capability_map_card)
    content_parts.extend(
        [
            _next_actions_card(next_actions),
            _high_risk_card(high_risk),
        ]
    )
    # #817: reviewer-only "Ülevaatuse järgi ootavad" — surfaced when the
    # caller has the reviewer role (the gate that controls whether the
    # query runs in the first place). Drafters / org admins never see
    # the section even when the org has unreviewed drafts.
    user_role = (org_info or {}).get("role") if org_info else None
    if user_role in (ROLE_REVIEWER, ROLE_SYSTEM_ADMIN):
        content_parts.append(_awaiting_review_card(awaiting_review))
    if eu_deadlines_card is not None:
        content_parts.append(eu_deadlines_card)
    content_parts.extend(
        [
            _stale_card(stale),
            _syncs_card(syncs),
            _exports_card(exports),
            _unresolved_card(unresolved),
            _bookmarks_card(bookmarks),
        ]
    )
    content = tuple(content_parts)

    return PageShell(
        *content,
        title="Töölaud",
        user=auth or None,
        theme=theme,
        active_nav="/dashboard",
    )


def _wants_json(req: Request) -> bool:
    """True when the caller is an XHR/fetch (``X-Requested-With: XMLHttpRequest``).

    The bookmark endpoints serve two callers: a plain HTML ``<form>`` on the
    dashboard (which wants a 303 redirect so the page re-renders) and the
    Õiguskaart bookmark button (a ``fetch()`` that needs a real JSON response
    — a 303 to ``/dashboard`` vs a 303 to ``/auth/login`` are indistinguishable
    to a ``redirect: "manual"`` fetch, so an expired session looked like a
    successful save — #743).
    """
    return req.headers.get("x-requested-with", "").lower() == "xmlhttprequest"


# #848: Estonian message for a rejected bookmark URI (shown to plain-form
# callers as the 400 body and returned to XHR callers in the JSON payload).
_INVALID_URI_MSG = "Vigane URI. Lubatud on ainult http:// või https:// aadressid."


def add_bookmark(req: Request, entity_uri: str, label: str = ""):
    """POST /api/bookmarks — add a bookmark for the current user.

    XHR callers get JSON (``200 {"ok": true, ...}`` / ``401 {"ok": false,
    "error": "auth"}``); plain-form callers get the 303 redirects.
    """
    wants_json = _wants_json(req)
    auth = req.scope.get("auth", {})
    user_id = auth.get("id")
    if not user_id:
        if wants_json:
            return JSONResponse({"ok": False, "error": "auth"}, status_code=401)
        return RedirectResponse(url="/auth/login", status_code=303)

    clean_uri = entity_uri.strip()
    # #848: validate the scheme server-side BEFORE insert — only absolute
    # http(s):// URLs are allowed. Rejects javascript:/data:/protocol-relative/
    # backslash-normalised/relative/empty values so a crafted bookmark can
    # never become stored XSS on dashboard load. No row is persisted on reject.
    if not is_safe_http_url(clean_uri):
        if wants_json:
            return JSONResponse(
                {"ok": False, "error": "invalid_uri", "message": _INVALID_URI_MSG},
                status_code=400,
            )
        return Response(_INVALID_URI_MSG, status_code=400)

    actual_label = label.strip() if label else None
    bookmark = _add_bookmark(user_id, clean_uri, actual_label)
    if bookmark:
        log_action(user_id, "bookmark.add", {"entity_uri": clean_uri, "label": actual_label})
    if wants_json:
        # ``bookmark`` is None when the row already existed (ON CONFLICT DO
        # NOTHING) — still "ok" from the caller's point of view.
        return JSONResponse({"ok": True, "id": bookmark["id"] if bookmark else None})
    return RedirectResponse(url="/dashboard", status_code=303)


def remove_bookmark(req: Request, bookmark_id: str):
    """POST /api/bookmarks/{bookmark_id}/delete — remove a bookmark.

    XHR callers get JSON (``200 {"ok": <bool>}`` / ``401 {"ok": false,
    "error": "auth"}``); plain-form callers get the 303 redirects.
    """
    wants_json = _wants_json(req)
    auth = req.scope.get("auth", {})
    user_id = auth.get("id")
    if not user_id:
        if wants_json:
            return JSONResponse({"ok": False, "error": "auth"}, status_code=401)
        return RedirectResponse(url="/auth/login", status_code=303)

    success = _remove_bookmark(bookmark_id, user_id)
    if success:
        log_action(user_id, "bookmark.remove", {"bookmark_id": bookmark_id})
    if wants_json:
        return JSONResponse({"ok": bool(success)})
    return RedirectResponse(url="/dashboard", status_code=303)


def index_redirect(req: Request):
    """GET / — redirect authenticated users to the dashboard."""
    auth = req.scope.get("auth")
    if auth:
        return RedirectResponse(url="/dashboard", status_code=303)
    return RedirectResponse(url="/auth/login", status_code=303)


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------


def register_dashboard_routes(rt) -> None:  # type: ignore[no-untyped-def]
    """Register personal dashboard routes on the FastHTML route decorator *rt*."""
    rt("/dashboard", methods=["GET"])(dashboard_page)
    rt("/api/bookmarks", methods=["POST"])(add_bookmark)
    rt("/api/bookmarks/{bookmark_id}/delete", methods=["POST"])(remove_bookmark)
