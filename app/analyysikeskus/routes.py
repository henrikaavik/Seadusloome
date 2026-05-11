"""Analüüsikeskus routes (#714).

The Analüüsikeskus is the legal-analysis workflow hub — the design
rationale lives in ``docs/2026-05-11-ministry-lawyer-ui-structure.md``.
This module hosts:

    GET  /analyysikeskus                         — workflow directory (#720)
    GET  /analyysikeskus/normi-mojuahel          — Normi mõjuahel (#722)
    GET  /analyysikeskus/el-ulevott              — EL ülevõtt stub (#723 fills it in)

Only the two workflows with backing ontology data today (``Normi
mõjuahel`` and ``EL ülevõtt ja harmoneerimine``) are wired here; the
other six Section-7 workflows are deferred to a follow-up epic and get
no placeholder cards in the meantime.

Auth is handled by the global ``auth_before`` middleware — none of these
paths are in ``SKIP_PATHS`` so an unauthenticated request is redirected
to ``/auth/login`` before any handler runs.

**#722 — Normi mõjuahel.** The workflow resolves the user's free-text
input to one ontology entity URI, runs the existing impact analyser
against an *ephemeral synthetic named graph* (see
:mod:`app.analyysikeskus.adhoc_analysis`), and renders the findings
through :func:`app.analyysikeskus.result_shell.analysis_result_shell`.
A UUID matching a draft the caller's org owns short-circuits to that
draft's persisted ``impact_reports`` row instead. Ad-hoc analyses are
ephemeral — recomputed on every GET, never persisted (C-lite). Nothing
on the page uses SPARQL / RDF / named-graph / "graph URI" language —
the ``Ulatus`` controls read purely as legal/policy scope.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from fasthtml.common import *  # noqa: F403
from starlette.requests import Request
from starlette.responses import RedirectResponse

from app.analyysikeskus.adhoc_analysis import run_adhoc_impact_analysis
from app.analyysikeskus.input_parser import parse_user_reference
from app.analyysikeskus.result_shell import analysis_result_shell
from app.db import get_connection as _connect
from app.docs.entity_extractor import ExtractedRef
from app.docs.impact.analyzer import ImpactFindings
from app.docs.impact.scoring import IMPACT_BAND_LABELS_ET, ImpactBand, impact_band
from app.docs.labels import TYPE_LABELS_ET as _TYPE_LABELS_ET
from app.docs.reference_resolver import ReferenceResolver
from app.docs.report_routes import explorer_focus_url
from app.drafter.state_machine import STEP_LABELS_ET, Step
from app.ui.data.data_table import Column, DataTable
from app.ui.layout import PageShell
from app.ui.primitives.badge import Badge, BadgeVariant  # noqa: E402  (re-import after wildcard)
from app.ui.primitives.button import Button  # noqa: E402  (re-import after wildcard)
from app.ui.primitives.input import Checkbox, Input, Select
from app.ui.surfaces.alert import Alert
from app.ui.surfaces.card import Card, CardBody, CardHeader
from app.ui.theme import get_theme_from_request
from app.ui.time import format_tallinn

logger = logging.getLogger(__name__)

# How many "Hiljutised analüüsid" rows to surface (newest first, merged
# across impact reports + drafter sessions). Kept small so the directory
# page stays dense-but-calm.
_MAX_RECENT_ANALYSES = 10

# How many RAG candidates to surface when no structured ref is recognised.
_MAX_RAG_CANDIDATES = 5

# Cap how many rows we render inline in each result sub-section — purely
# page-weight control (the underlying findings can be 100s of rows).
_MAX_RESULT_ROWS = 30

# Entity types we treat as "drafts" / "court practice" when partitioning
# the affected + conflicting entity sets into the result sub-sections.
_DRAFT_TYPE_LOCALNAMES = frozenset({"DraftLegislation", "DraftingIntent"})
_COURT_TYPE_LOCALNAMES = frozenset({"CourtDecision", "EUCourtDecision"})

# Map the four impact bands to the legal-language risk labels the design
# note asks for on the result page. ``high`` / ``critical`` already read
# as risk in ``IMPACT_BAND_LABELS_ET`` ("Kõrge risk" / "Kriitiline");
# ``low`` / ``medium`` get re-labelled here so the result page speaks the
# "Väike mõju" / "Vajab kontrolli" / "Taustateave" / "Kõrge risk" vocabulary.
_RISK_BAND_LABELS_ET: dict[ImpactBand, str] = {
    "low": "Väike mõju",
    "medium": "Vajab kontrolli",
    "high": "Kõrge risk",
    "critical": "Kõrge risk",
}

_RISK_BAND_BADGE_VARIANT: dict[ImpactBand, BadgeVariant] = {
    "low": "success",
    "medium": "warning",
    "high": "danger",
    "critical": "danger",
}

# ---------------------------------------------------------------------------
# DB helper — "Hiljutised analüüsid"
# ---------------------------------------------------------------------------


def _step_label(step_number: int) -> str:
    """Estonian label for a drafter step number, falling back to the bare number."""
    try:
        return STEP_LABELS_ET.get(Step(step_number), str(step_number))
    except (ValueError, TypeError):
        return str(step_number)


def _get_recent_analyses(user_id: str | None, org_id: str | None) -> list[dict[str, Any]]:
    """Return recent analysis activity for the directory page, newest first.

    Two small org-scoped raw-SQL queries — mirroring the try/except → log
    + return ``[]`` pattern from ``app/templates/dashboard.py``'s widget
    loaders — merged into one list capped at :data:`_MAX_RECENT_ANALYSES`:

    * **Impact reports** — the latest report per draft for the org (via a
      ``DISTINCT ON (ir.draft_id)`` CTE, then re-sorted by ``generated_at``
      DESC). Each row links to ``/drafts/{id}/report`` and carries the
      draft title, the risk-band label, and the ``generated_at`` timestamp.
    * **Drafter sessions** — the current user's active/completed sessions
      for the org. Each row links to ``/drafter/{id}`` and carries the
      "Koostaja — {N}. samm" label and ``updated_at``.

    Returns a list of dicts shaped::

        {"kind": "report"|"session", "href": str, "title": str,
         "detail": str, "when": datetime|None, "_sort": datetime}

    ``[]`` on any DB error so the directory page degrades to the empty
    state rather than 500.
    """
    out: list[dict[str, Any]] = []

    # --- recent impact reports (org-scoped) ---------------------------------
    if org_id:
        try:
            with _connect() as conn:
                rows = conn.execute(
                    """
                    WITH latest_report AS (
                        SELECT DISTINCT ON (ir.draft_id)
                               ir.draft_id, ir.impact_score, ir.generated_at
                        FROM impact_reports ir
                        JOIN drafts d ON d.id = ir.draft_id
                        WHERE d.org_id = %s
                        ORDER BY ir.draft_id, ir.generated_at DESC
                    )
                    SELECT lr.draft_id, d.title, lr.impact_score, lr.generated_at
                    FROM latest_report lr
                    JOIN drafts d ON d.id = lr.draft_id
                    ORDER BY lr.generated_at DESC
                    LIMIT %s
                    """,
                    (org_id, _MAX_RECENT_ANALYSES),
                ).fetchall()
            for r in rows:
                score = int(r[2] or 0)
                band_label = IMPACT_BAND_LABELS_ET[impact_band(score)]
                out.append(
                    {
                        "kind": "report",
                        "href": f"/drafts/{r[0]}/report",
                        "title": r[1] or "Pealkirjata eelnõu",
                        "detail": f"Mõjuaruanne — {band_label}",
                        "when": r[3],
                        "_sort": r[3],
                    }
                )
        except Exception:
            logger.exception("Failed to fetch recent impact reports for org %s", org_id)

    # --- recent drafter sessions (user + org scoped) ------------------------
    if user_id and org_id:
        try:
            with _connect() as conn:
                rows = conn.execute(
                    """
                    SELECT id, current_step, updated_at
                    FROM drafting_sessions
                    WHERE user_id = %s AND org_id = %s
                      AND status IN ('active', 'completed')
                    ORDER BY updated_at DESC
                    LIMIT %s
                    """,
                    (user_id, org_id, _MAX_RECENT_ANALYSES),
                ).fetchall()
            for r in rows:
                step_num = int(r[1] or 1)
                out.append(
                    {
                        "kind": "session",
                        "href": f"/drafter/{r[0]}",
                        "title": "Koostaja eelnõu",
                        "detail": f"Koostaja — {step_num}. samm: {_step_label(step_num)}",
                        "when": r[2],
                        "_sort": r[2],
                    }
                )
        except Exception:
            logger.exception("Failed to fetch recent drafter sessions for user %s", user_id)

    # Merge newest-first; rows with a missing timestamp sink to the bottom.
    def _key(item: dict[str, Any]) -> datetime:
        ts = item.get("_sort")
        if isinstance(ts, datetime):
            return ts if ts.tzinfo is not None else ts.replace(tzinfo=UTC)
        return datetime.min.replace(tzinfo=UTC)

    out.sort(key=_key, reverse=True)
    return out[:_MAX_RECENT_ANALYSES]


# ---------------------------------------------------------------------------
# Directory page (#720)
# ---------------------------------------------------------------------------


def _workflow_card(
    *,
    title: str,
    purpose: str,
    action: str,
    input_name: str,
    input_placeholder: str,
    input_aria_label: str,
    examples: str,
) -> Any:
    """One compact workflow entry — a card with a one-line purpose + a GET form.

    Not a marketing card: dense, scan-friendly, task-first (per the design
    doc's "Analüüsikeskus" section). The form does a plain ``GET`` to the
    workflow route with a single ``sisend`` text input + an "Alusta
    analüüsi" submit; example inputs sit below in muted text.
    """
    return Card(
        CardHeader(H3(title, cls="card-title")),  # noqa: F405
        CardBody(
            P(purpose),  # noqa: F405
            Form(  # noqa: F405
                Input(
                    input_name,
                    type="text",
                    placeholder=input_placeholder,
                    aria_label=input_aria_label,
                    cls="analyysikeskus-input",
                ),
                Button("Alusta analüüsi", type="submit", variant="primary"),
                method="get",
                action=action,
                cls="analyysikeskus-workflow-form",
            ),
            Small(examples, cls="muted-text"),  # noqa: F405
        ),
    )


def _recent_analyses_card(items: list[dict[str, Any]]) -> Any:
    """The "Hiljutised analüüsid" card — a DataTable of recent activity.

    Empty → a single muted "Veel pole analüüse." row (consistent with how
    ``app/templates/dashboard.py`` renders its empty section bodies).
    """
    if not items:
        return Card(
            CardHeader(H3("Hiljutised analüüsid", cls="card-title")),  # noqa: F405
            CardBody(P("Veel pole analüüse.", cls="muted-text")),  # noqa: F405
        )

    columns = [
        Column(
            key="title",
            label="Analüüs",
            sortable=False,
            render=lambda r: A(r["title"], href=r["href"], cls="table-link"),  # noqa: F405
        ),
        Column(key="detail", label="Tüüp", sortable=False),
        Column(
            key="when",
            label="Muudetud",
            sortable=False,
            render=lambda r: format_tallinn(r["when"]),
        ),
    ]
    rows = [
        {"title": it["title"], "href": it["href"], "detail": it["detail"], "when": it["when"]}
        for it in items
    ]
    return Card(
        CardHeader(H3("Hiljutised analüüsid", cls="card-title")),  # noqa: F405
        CardBody(DataTable(columns=columns, rows=rows)),
    )


def analyysikeskus_page(req: Request):
    """GET /analyysikeskus — the legal-analysis workflow directory."""
    auth = req.scope.get("auth") or None
    theme = get_theme_from_request(req)
    user_id = auth.get("id") if auth else None
    org_id = auth.get("org_id") if auth else None

    recent = _get_recent_analyses(user_id, org_id)

    # Compact header — no marketing hero, no InfoBox banner.
    header = (
        H1("Analüüsikeskus", cls="page-title"),  # noqa: F405
        P(  # noqa: F405
            "Õigusliku analüüsi töövood ühes kohas. Vali töövoog, sisesta "
            "õiguslik viide või küsimus.",
            cls="page-subtitle",
        ),
    )

    normi_card = _workflow_card(
        title="Normi mõjuahel",
        purpose=(
            "Vaata, mida muudatus mõjutab — millised sätted viitavad muudetavale "
            "paragrahvile, millised eelnõud puudutavad sama teemat ja milline "
            "Riigikohtu praktika on seotud."
        ),
        action="/analyysikeskus/normi-mojuahel",
        input_name="sisend",
        input_placeholder=(
            "Nt: AvTS § 35 · CELEX-number · eelnõu pealkiri · või kirjeldage muudatust"
        ),
        input_aria_label="Õiguslik viide või kirjeldus",
        examples=("Näited: «Muudame AvTS § 35.» · «Kontrolli karistusseadustiku § 133 mõju.»"),
    )

    el_card = _workflow_card(
        title="EL ülevõtt ja harmoneerimine",
        purpose=(
            "Kontrolli, kas Eesti õigus katab EL kohustuse — millised Eesti sätted "
            "on EL aktiga seotud ja kus on katmata kohad."
        ),
        action="/analyysikeskus/el-ulevott",
        input_name="sisend",
        input_placeholder="Nt: CELEX-number · EL akti pealkiri · poliitikavaldkond",
        input_aria_label="CELEX-number, EL akti pealkiri või valdkond",
        examples="Näited: «Kontrolli AI määruse ülevõttu.» · «32016R0679»",
    )

    return PageShell(
        *header,
        normi_card,
        el_card,
        _recent_analyses_card(recent),
        title="Analüüsikeskus",
        user=auth,
        theme=theme,
        active_nav="/analyysikeskus",
    )


# ---------------------------------------------------------------------------
# EL ülevõtt — still a stub (#723 fills it in)
# ---------------------------------------------------------------------------

# Stub copy reused by the EL ülevõtt result page until #723 lands the
# real computation.
_RESULTS_STUB_TEXT = "Selle töövoo tulemuste arvutus on koostamisel — tulekul."
_EVIDENCE_STUB_TEXT = (
    "Tõendid (allikad, seosed, kuupäevad, lingid) kuvatakse siin, kui tulemused on arvutatud."
)
_STUB_ACTIONS: list[dict[str, str]] = [
    {"label": "Küsi nõustajalt", "href": "/chat/new"},
    {"label": "Tagasi analüüsikeskusesse", "href": "/analyysikeskus"},
]


def el_ulevott_page(req: Request):
    """GET /analyysikeskus/el-ulevott?sisend=<text> — EL ülevõtt ja harmoneerimine (stub)."""
    sisend = (req.query_params.get("sisend") or "").strip()
    if not sisend:
        return RedirectResponse(url="/analyysikeskus", status_code=303)

    auth = req.scope.get("auth") or None
    theme = get_theme_from_request(req)
    return analysis_result_shell(
        workflow_title="EL ülevõtt ja harmoneerimine",
        input_summary=P(f"Sisestasite: «{sisend}»"),  # noqa: F405
        results_block=Alert(_RESULTS_STUB_TEXT, variant="info"),
        evidence_block=P(_EVIDENCE_STUB_TEXT, cls="muted-text"),  # noqa: F405
        actions=_STUB_ACTIONS,
        user=auth,
        theme=theme,
    )


# ---------------------------------------------------------------------------
# Normi mõjuahel (#722) — helpers
# ---------------------------------------------------------------------------


# Scope state read off the query string. Booleans default per the design
# note: EU + court practice on, org-wide drafts off.
class _Scope:
    """Parsed ``Ulatus`` scope from the GET query params.

    ``oigus`` is informational only (temporal redactions aren't wired —
    the second select option is disabled). ``include_eu`` /
    ``include_court`` actually filter the analysis output;
    ``org_wide_drafts`` only changes the ``Seotud eelnõud`` framing
    (the underlying query returns what it returns).
    """

    def __init__(self, params: Any) -> None:
        # Checkboxes: present in the query string ⇒ checked. The default
        # form has EU + court checked, so on a *first* GET (no scope
        # params at all) we want both ON; we detect "the form was
        # submitted" by the presence of the marker hidden input
        # ``ulatus_submitted``.
        submitted = params.get("ulatus_submitted") == "1"
        if submitted:
            self.include_eu = params.get("kaasa_el") is not None
            self.include_court = params.get("kaasa_kohtupraktika") is not None
            self.org_wide_drafts = params.get("kogu_organisatsioon") is not None
        else:
            self.include_eu = True
            self.include_court = True
            self.org_wide_drafts = False
        self.oigus = params.get("oigus") or "current"
        self.ajavahemik_algus = params.get("ajavahemik_algus") or ""
        self.ajavahemik_lopp = params.get("ajavahemik_lopp") or ""

    def query_pairs(self, sisend: str) -> list[tuple[str, str]]:
        """Return the ``(key, value)`` pairs to carry the scope through links."""
        pairs: list[tuple[str, str]] = [("sisend", sisend), ("ulatus_submitted", "1")]
        if self.include_eu:
            pairs.append(("kaasa_el", "1"))
        if self.include_court:
            pairs.append(("kaasa_kohtupraktika", "1"))
        if self.org_wide_drafts:
            pairs.append(("kogu_organisatsioon", "1"))
        if self.oigus and self.oigus != "current":
            pairs.append(("oigus", self.oigus))
        if self.ajavahemik_algus:
            pairs.append(("ajavahemik_algus", self.ajavahemik_algus))
        if self.ajavahemik_lopp:
            pairs.append(("ajavahemik_lopp", self.ajavahemik_lopp))
        return pairs


def _type_localname(uri: str) -> str:
    """Return the local name of a type URI (after ``#`` or last ``/``)."""
    if not uri:
        return ""
    return uri.rsplit("#", 1)[-1] if "#" in uri else uri.rsplit("/", 1)[-1]


def _type_label(uri: str) -> str:
    """Estonian label for a type URI, falling back to the bare local name."""
    if not uri:
        return "—"
    return _TYPE_LABELS_ET.get(_type_localname(uri), _type_localname(uri))


def _entity_display_label(row: dict[str, Any]) -> str:
    """Best human label for an entity row — its ``label`` or the URI tail."""
    label = str(row.get("label") or "").strip()
    if label:
        return label
    uri = str(row.get("uri") or "").strip()
    return _type_localname(uri) or uri or "—"


def _split_link_query(pairs: list[tuple[str, str]]) -> str:
    """URL-encode ``(key, value)`` pairs into an ``a=b&c=d`` query string."""
    from urllib.parse import urlencode

    return urlencode(pairs)


def _normi_link(sisend: str, *, scope: _Scope | None = None) -> str:
    """Build a ``/analyysikeskus/normi-mojuahel?sisend=…`` link (scope-carrying)."""
    pairs: list[tuple[str, str]] = (
        scope.query_pairs(sisend) if scope is not None else [("sisend", sisend)]
    )
    return f"/analyysikeskus/normi-mojuahel?{_split_link_query(pairs)}"


# ---------------------------------------------------------------------------
# Normi mõjuahel — scope form (the design note overrides result_shell's stub)
# ---------------------------------------------------------------------------

_LAW_SCOPE_OPTIONS: list[tuple[str, str]] = [
    ("current", "Kehtiv õigus"),
    ("current_plus_history", "Kehtiv + varasemad redaktsioonid (tulekul)"),
]


def _normi_scope_block(sisend: str, scope: _Scope) -> Any:
    """The enabled ``Ulatus`` scope form for Normi mõjuahel.

    A ``GET`` form back to ``/analyysikeskus/normi-mojuahel`` carrying
    ``sisend`` as a hidden input + the legal-language scope controls.
    Submitting re-runs the analysis with the chosen scope. All copy is
    legal/policy language — there is no SPARQL / RDF / named-graph
    vocabulary anywhere (these are *scope* words).
    """
    return Form(  # noqa: F405
        P(  # noqa: F405
            "Analüüsin vaikimisi kehtivat õigust, seotud eelnõusid, Riigikohtu "
            "praktikat ja EL õigusakte. Võite ulatust muuta ja analüüsi uuesti käivitada.",
            cls="muted-text",
        ),
        # Carry the analysed input through unchanged.
        Hidden(name="sisend", value=sisend),  # noqa: F405
        # Marker so the handler can tell "form submitted" from "first GET".
        Hidden(name="ulatus_submitted", value="1"),  # noqa: F405
        # "Õigus" — which redactions of the law count. Second option is
        # disabled ("tulekul") because temporal versions aren't wired.
        Div(  # noqa: F405
            Label("Õigus", fr="analyysikeskus-scope-law"),  # noqa: F405
            Select(
                "oigus",
                _LAW_SCOPE_OPTIONS,
                # Reflect the chosen value, but the second ("varasemad
                # redaktsioonid") option is "tulekul" — temporal versions
                # aren't wired, so we always fall back to "current".
                value="current",
                id="analyysikeskus-scope-law",
            ),
            cls="form-field",
        ),
        # Toggles that actually affect the analysis output.
        Checkbox("kaasa_el", checked=scope.include_eu, label="Kaasa EL õigus"),
        Checkbox(
            "kaasa_kohtupraktika",
            checked=scope.include_court,
            label="Kaasa kohtupraktika",
        ),
        Checkbox(
            "kogu_organisatsioon",
            checked=scope.org_wide_drafts,
            label="Kaasa kogu organisatsiooni eelnõud",
        ),
        Small(  # noqa: F405
            "Vaikimisi näitan otseseid eelnõuseoseid; märkige see, et "
            "kaasata kogu organisatsiooni eelnõud.",
            cls="muted-text",
        ),
        # KOV regulations — not wired yet; disabled with a "Tulekul" tooltip.
        Checkbox(
            "kaasa_kov",
            checked=False,
            label="Kaasa KOV regulatsioonid",
            disabled=True,
            title="Tulekul",
        ),
        # Optional time range — disabled ("tulekul"); temporal scoping
        # isn't backed by data yet.
        Div(  # noqa: F405
            Label("Ajavahemik (tulekul)"),  # noqa: F405
            Span(  # noqa: F405
                Input(
                    "ajavahemik_algus",
                    type="date",
                    aria_label="Alguskuupäev",
                    disabled=True,
                ),
                Span(" – ", cls="muted-text"),  # noqa: F405
                Input(
                    "ajavahemik_lopp",
                    type="date",
                    aria_label="Lõppkuupäev",
                    disabled=True,
                ),
                cls="analyysikeskus-date-range",
            ),
            cls="form-field",
        ),
        Button("Uuenda ulatust", type="submit", variant="secondary", size="sm"),
        method="get",
        action="/analyysikeskus/normi-mojuahel",
        cls="analyysikeskus-scope-form",
    )


# ---------------------------------------------------------------------------
# Normi mõjuahel — Tulemused sub-sections
# ---------------------------------------------------------------------------


def _sub_section(heading: str, *content: Any) -> Any:
    """One ``Tulemused`` sub-section: a small ``H4`` + its content."""
    return Div(H4(heading, cls="analyysikeskus-subsection-title"), *content)  # noqa: F405


def _missing_row(text: str) -> Any:
    """A one-line muted "…ei leitud" row standing in for an empty sub-section body."""
    return P(text, cls="muted-text")  # noqa: F405


def _entity_link(row: dict[str, Any]) -> Any:
    """Render an entity row's label as an "open on the legal map" link.

    Affected/conflicting entities are clickable: clicking opens the
    entity on the legal map (``/explorer?focus=…``, URL-encoded by
    :func:`explorer_focus_url`) — the "drill into this" affordance the
    design note asks for. Rows without a URI render as plain text.
    """
    uri = str(row.get("uri") or row.get("conflicting_entity") or "").strip()
    label = _entity_display_label(row)
    if not uri:
        return Span(label)  # noqa: F405
    return A(label, href=explorer_focus_url(uri), cls="data-table-link")  # noqa: F405


def _peamised_mojud_section(
    affected: list[dict[str, Any]],
    *,
    n_provisions: int,
    n_drafts: int,
    n_courts: int,
) -> Any:
    """``Peamised mõjud`` — the top affected entities + a one-line lead."""
    lead = P(  # noqa: F405
        f"Kavandatav muudatus mõjutab vähemalt {n_provisions} sätet, "
        f"{n_drafts} eelnõu ja {n_courts} Riigikohtu lahendit.",
        cls="muted-text",
    )
    if not affected:
        return _sub_section("Peamised mõjud", lead, _missing_row("Otseseid mõjusid ei leitud."))
    rows = affected[:_MAX_RESULT_ROWS]
    columns = [
        Column(key="label", label="Üksus", sortable=False, render=lambda r: _entity_link(r)),
        Column(
            key="type",
            label="Tüüp",
            sortable=False,
            render=lambda r: _type_label(str(r.get("type") or "")),
        ),
    ]
    return _sub_section(
        "Peamised mõjud",
        lead,
        DataTable(columns=columns, rows=rows, empty_message="Otseseid mõjusid ei leitud."),
    )


def _korge_riskiga_section(conflicts: list[dict[str, Any]]) -> Any:
    """``Kõrge riskiga seosed`` — the conflict rows with a legal-language reason."""
    if not conflicts:
        return _sub_section(
            "Kõrge riskiga seosed", _missing_row("Kõrge riskiga seoseid ei leitud.")
        )
    rows = conflicts[:_MAX_RESULT_ROWS]
    columns = [
        Column(
            key="conflicting_label",
            label="Konflikti üksus",
            sortable=False,
            render=lambda r: _entity_link(
                {"uri": r.get("conflicting_entity"), "label": r.get("conflicting_label")}
            ),
        ),
        Column(
            key="reason",
            label="Põhjus",
            sortable=False,
            render=lambda r: str(r.get("reason") or "—"),
        ),
    ]
    return _sub_section(
        "Kõrge riskiga seosed",
        DataTable(columns=columns, rows=rows, empty_message="Kõrge riskiga seoseid ei leitud."),
    )


def _seotud_eelnoud_section(draft_rows: list[dict[str, Any]], *, org_wide: bool) -> Any:
    """``Seotud eelnõud`` — affected entities that are drafts."""
    framing = P(  # noqa: F405
        "Näitan kõiki seotud eelnõusid." if org_wide else "Näitan otseselt seotud eelnõusid.",
        cls="muted-text",
    )
    if not draft_rows:
        return _sub_section("Seotud eelnõud", framing, _missing_row("Seotud eelnõusid ei leitud."))
    rows = draft_rows[:_MAX_RESULT_ROWS]
    columns = [
        Column(key="label", label="Eelnõu", sortable=False, render=lambda r: _entity_link(r)),
    ]
    return _sub_section(
        "Seotud eelnõud",
        framing,
        DataTable(columns=columns, rows=rows, empty_message="Seotud eelnõusid ei leitud."),
    )


def _riigikohtu_section(court_rows: list[dict[str, Any]]) -> Any:
    """``Riigikohtu praktika`` — affected/conflicting entities that are court decisions."""
    if not court_rows:
        return _sub_section(
            "Riigikohtu praktika", _missing_row("Seotud kohtulahendeid ei leitud.")
        )
    rows = court_rows[:_MAX_RESULT_ROWS]
    columns = [
        Column(key="label", label="Lahend", sortable=False, render=lambda r: _entity_link(r)),
        Column(
            key="type",
            label="Tüüp",
            sortable=False,
            render=lambda r: _type_label(str(r.get("type") or "")),
        ),
    ]
    return _sub_section(
        "Riigikohtu praktika",
        DataTable(columns=columns, rows=rows, empty_message="Seotud kohtulahendeid ei leitud."),
    )


def _el_seosed_section(eu_rows: list[dict[str, Any]], *, included: bool) -> Any:
    """``EL seosed`` — the EU-compliance rows (EU act ↔ linking Estonian provision)."""
    if not included:
        return _sub_section("EL seosed", _missing_row("EL õigus on ulatusest välja jäetud."))
    if not eu_rows:
        return _sub_section("EL seosed", _missing_row("EL õiguse seoseid ei leitud."))
    rows = eu_rows[:_MAX_RESULT_ROWS]
    columns = [
        Column(
            key="eu_label",
            label="EL õigusakt",
            sortable=False,
            render=lambda r: _entity_link({"uri": r.get("eu_act"), "label": r.get("eu_label")}),
        ),
        Column(
            key="provision_label",
            label="Seotud Eesti säte",
            sortable=False,
            render=lambda r: str(r.get("provision_label") or r.get("estonian_provision") or "—"),
        ),
    ]
    return _sub_section(
        "EL seosed",
        DataTable(columns=columns, rows=rows, empty_message="EL õiguse seoseid ei leitud."),
    )


def _risk_and_recommendation(
    score: int,
    *,
    n_conflicts: int,
    n_provisions: int,
    n_eu: int,
) -> Any:
    """The risk-band Badge + a templated recommended-next-action sentence."""
    band = impact_band(score)
    label = _RISK_BAND_LABELS_ET[band]
    variant = _RISK_BAND_BADGE_VARIANT[band]

    # Template the recommendation from the counts/conflicts — no LLM.
    if n_conflicts > 0:
        recommendation = (
            "Soovitus: vaadata üle vastuolud teiste eelnõude / kohtupraktikaga "
            "ja kaaluda üleminekusätet."
        )
    elif n_eu > 0:
        recommendation = (
            "Soovitus: kontrollida, kas muudatus mõjutab EL õiguse ülevõtmist, "
            "ja vajadusel kooskõlastada."
        )
    elif n_provisions > 0:
        recommendation = (
            "Soovitus: vaadata üle viidatud sätted ja veenduda, et muudatus on nendega kooskõlas."
        )
    else:
        recommendation = "Soovitus: täiendavat tegevust ei tuvastatud — kasuta seda taustateabena."

    return _sub_section(
        "Riskihinnang ja soovitus",
        P(Span("Riskitase: ", cls="muted-text"), Badge(label, variant=variant)),  # noqa: F405
        P(recommendation),  # noqa: F405
    )


def _is_court_conflict(row: dict[str, Any]) -> bool:
    """True when a conflict row is a case-law conflict (the analyzer's reason phrasing)."""
    reason = str(row.get("reason") or "").lower()
    return "tõlgendab" in reason or "kohtulahend" in reason


def _build_results_block(findings: ImpactFindings, score: int, scope: _Scope) -> list[Any]:
    """Assemble the ``Tulemused`` sub-sections from the (scope-filtered) findings.

    Scope wiring (only what has backing data):

    * ``Kaasa kohtupraktika`` off → ``CourtDecision`` / ``EUCourtDecision``
      rows are filtered out of the affected set, and case-law conflict
      rows out of the conflict set, *before* rendering.
    * ``Kaasa EL õigus`` off → the EU-compliance rows are dropped.
    * ``Kaasa kogu organisatsiooni eelnõud`` only re-frames the
      ``Seotud eelnõud`` block ("näitan kõiki" vs "näitan otseseid
      seoseid") — the underlying query returns what it returns, so we
      don't pretend to filter what we can't.
    """
    affected = list(findings.affected_entities or [])
    conflicts = list(findings.conflicts or [])
    eu_rows = list(findings.eu_compliance or [])

    if not scope.include_court:
        affected = [
            r
            for r in affected
            if _type_localname(str(r.get("type") or "")) not in _COURT_TYPE_LOCALNAMES
        ]
        conflicts = [r for r in conflicts if not _is_court_conflict(r)]
    if not scope.include_eu:
        eu_rows = []

    draft_rows = [
        r for r in affected if _type_localname(str(r.get("type") or "")) in _DRAFT_TYPE_LOCALNAMES
    ]
    court_rows = [
        r for r in affected if _type_localname(str(r.get("type") or "")) in _COURT_TYPE_LOCALNAMES
    ]
    provision_rows = [
        r
        for r in affected
        if _type_localname(str(r.get("type") or "")) not in _DRAFT_TYPE_LOCALNAMES
        and _type_localname(str(r.get("type") or "")) not in _COURT_TYPE_LOCALNAMES
    ]

    return [
        _peamised_mojud_section(
            affected,
            n_provisions=len(provision_rows),
            n_drafts=len(draft_rows),
            n_courts=len(court_rows),
        ),
        _korge_riskiga_section(conflicts),
        _seotud_eelnoud_section(draft_rows, org_wide=scope.org_wide_drafts),
        _riigikohtu_section(court_rows),
        _el_seosed_section(eu_rows, included=scope.include_eu),
        _risk_and_recommendation(
            score,
            n_conflicts=len(conflicts),
            n_provisions=len(provision_rows),
            n_eu=len(eu_rows),
        ),
    ]


# ---------------------------------------------------------------------------
# Normi mõjuahel — Tõendid block
# ---------------------------------------------------------------------------


def _evidence_row(
    *,
    source_label: str,
    relation: str,
    target_label: str,
    uri: str,
    why: str,
    snippet: str = "",
    when: str = "",
) -> Any:
    """One row in the ``Tõendid`` card.

    Carries the source label, the relation **in legal language**, an
    optional snippet/date, an "Ava allikas" link, a "miks see on oluline"
    line, and an "Ava õiguskaardil →" deep link (URL-encoded by
    :func:`explorer_focus_url`).
    """
    bits: list[Any] = [
        P(  # noqa: F405
            Strong(source_label),  # noqa: F405
            f" {relation} ",
            Strong(target_label) if target_label else "",  # noqa: F405
        ),
    ]
    if snippet:
        bits.append(P(snippet, cls="muted-text"))  # noqa: F405
    if when:
        bits.append(P(f"Kuupäev / redaktsioon: {when}", cls="muted-text"))  # noqa: F405
    if why:
        bits.append(P(why, cls="muted-text"))  # noqa: F405
    link_bits: list[Any] = []
    if uri:
        link_bits.append(A("Ava allikas", href=uri, cls="data-table-link"))  # noqa: F405
        link_bits.append(Span(" · ", cls="muted-text"))  # noqa: F405
        link_bits.append(
            A("Ava õiguskaardil →", href=explorer_focus_url(uri), cls="data-table-link")  # noqa: F405
        )
    if link_bits:
        bits.append(P(*link_bits))  # noqa: F405
    return Div(*bits, cls="analyysikeskus-evidence-row")  # noqa: F405


def _build_evidence_block(
    findings: ImpactFindings,
    *,
    analysed_label: str,
    scope: _Scope,
) -> list[Any]:
    """Assemble the ``Tõendid`` rows from the findings.

    One row per affected entity (the affected-entities pass doesn't
    return the linking predicate, so the relation reads as the neutral
    "on seotud üksusega"), plus one row per conflict (relation = the
    analyzer's reason string, which is already legal language) and one
    row per EU link ("võtab üle direktiivi"). Court rows are filtered
    out when ``Kaasa kohtupraktika`` is off; EU rows when ``Kaasa EL
    õigus`` is off — same scope wiring as :func:`_build_results_block`.
    """
    rows: list[Any] = []

    for r in list(findings.affected_entities or []):
        type_ln = _type_localname(str(r.get("type") or ""))
        if not scope.include_court and type_ln in _COURT_TYPE_LOCALNAMES:
            continue
        uri = str(r.get("uri") or "").strip()
        if not uri:
            continue
        type_word = _type_label(str(r.get("type") or "")).lower()
        rows.append(
            _evidence_row(
                source_label=analysed_label,
                relation="on seotud üksusega",
                target_label=_entity_display_label(r),
                uri=uri,
                why=(
                    f"See {type_word} on analüüsitava üksusega otseses seoses, "
                    "seega võib muudatus seda mõjutada."
                ),
            )
        )

    for r in list(findings.conflicts or []):
        if not scope.include_court and _is_court_conflict(r):
            continue
        uri = str(r.get("conflicting_entity") or "").strip()
        label = str(r.get("conflicting_label") or uri or "—")
        rows.append(
            _evidence_row(
                source_label=label,
                relation="—",
                target_label="",
                uri=uri,
                why=str(r.get("reason") or "Seotud üksus, mis võib põhjustada vastuolu."),
            )
        )

    if scope.include_eu:
        for r in list(findings.eu_compliance or []):
            uri = str(r.get("eu_act") or "").strip()
            label = str(r.get("eu_label") or uri or "EL õigusakt")
            provision = str(r.get("provision_label") or r.get("estonian_provision") or "")
            rows.append(
                _evidence_row(
                    source_label=provision or "Eesti säte",
                    relation="võtab üle direktiivi",
                    target_label=label,
                    uri=uri,
                    why=(
                        "Muudatus võib mõjutada EL õiguse ülevõtmist — kontrolli "
                        "vastavust enne menetlust."
                    ),
                )
            )

    return rows


# ---------------------------------------------------------------------------
# Normi mõjuahel — draft-backed path
# ---------------------------------------------------------------------------


def _load_owned_draft_report(draft_uuid: uuid.UUID, org_id: str | None) -> tuple | None:
    """Return ``(draft_id, draft_title, draft_version_id, report_data, impact_score)``.

    Only when *draft_uuid* is a draft the caller's org owns **and** that
    draft has an ``impact_reports`` row. ``None`` (→ fall through to the
    parse path) when the UUID doesn't resolve to an owned draft or there
    is no report yet. Best-effort: any DB error also returns ``None``.
    """
    if not org_id:
        return None
    try:
        with _connect() as conn:
            row = conn.execute(
                """
                SELECT d.id, d.title, ir.draft_version_id, ir.report_data, ir.impact_score
                FROM drafts d
                JOIN impact_reports ir ON ir.draft_id = d.id
                WHERE d.id = %s AND d.org_id = %s
                ORDER BY ir.generated_at DESC
                LIMIT 1
                """,
                (str(draft_uuid), str(org_id)),
            ).fetchone()
    except Exception:
        logger.warning("Failed to load owned-draft report for draft=%s", draft_uuid, exc_info=True)
        return None
    return row


def _parse_report_data(raw: Any) -> dict[str, Any]:
    """Normalise a JSONB ``report_data`` value into a dict (mirrors report_routes)."""
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, (bytes, bytearray)):
        try:
            return json.loads(raw.decode())
        except (TypeError, ValueError, UnicodeDecodeError):
            return {}
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return {}
    return {}


def _findings_from_report_data(data: dict[str, Any]) -> ImpactFindings:
    """Rebuild an :class:`ImpactFindings` from a persisted ``report_data`` dict."""
    affected = list(data.get("affected_entities") or [])
    conflicts = list(data.get("conflicts") or [])
    gaps = list(data.get("gaps") or [])
    eu = list(data.get("eu_compliance") or [])
    return ImpactFindings(
        affected_entities=affected,
        conflicts=conflicts,
        gaps=gaps,
        eu_compliance=eu,
        affected_count=int(data.get("affected_count") or len(affected)),
        conflict_count=int(data.get("conflict_count") or len(conflicts)),
        gap_count=int(data.get("gap_count") or len(gaps)),
    )


# ---------------------------------------------------------------------------
# Normi mõjuahel — resolution / RAG helpers
# ---------------------------------------------------------------------------


def _resolve_refs(refs: list[ExtractedRef]) -> list[Any]:
    """Resolve parsed refs to ontology URIs; an unreachable Jena yields ``[]``.

    Wrapped so a dead Jena (or any resolver crash) degrades to "nothing
    resolved" rather than 500 — the route then shows the "no structured
    ref" branch, which is the right fallback.
    """
    if not refs:
        return []
    try:
        return ReferenceResolver().resolve(refs)
    except Exception:
        logger.warning("Normi mõjuahel: reference resolution failed", exc_info=True)
        return []


def _rag_candidates(sisend: str, org_id: str | None) -> list[dict[str, str]]:
    """Light RAG fallback: top provision-ish chunks for *sisend*.

    Returns a list of ``{"label": str, "ref": str}`` dicts — ``ref`` is
    the search-box text a click should re-submit. Best-effort: if the
    RAG retriever isn't wired / errors / returns nothing, returns ``[]``
    and the route simply omits the candidates (no crash). ``ref`` is
    derived from the chunk metadata's provision/law fields when present,
    else the first line of the chunk content trimmed.
    """
    try:
        from app.rag.retriever import Retriever
    except Exception:
        return []
    try:
        import asyncio

        retriever = Retriever()

        async def _run() -> list[Any]:
            return await retriever.retrieve(
                sisend,
                k=_MAX_RAG_CANDIDATES,
                org_id=org_id,
            )

        try:
            chunks = asyncio.run(_run())
        except RuntimeError:
            # Already inside an event loop (shouldn't happen in a sync
            # route, but be defensive) — skip the RAG fallback.
            return []
    except Exception:
        logger.debug("Normi mõjuahel: RAG candidate lookup failed", exc_info=True)
        return []

    out: list[dict[str, str]] = []
    for ch in chunks or []:
        meta = getattr(ch, "metadata", None) or {}
        ref_text = ""
        for key in ("provision_ref", "provision", "section_ref", "law_short", "law"):
            val = meta.get(key) if isinstance(meta, dict) else None
            if val:
                ref_text = str(val).strip()
                break
        label = ref_text or (str(getattr(ch, "content", "") or "").strip().splitlines() or [""])[0]
        label = label[:120].strip()
        if not label:
            continue
        out.append({"label": label, "ref": ref_text or label})
        if len(out) >= _MAX_RAG_CANDIDATES:
            break
    return out


def _candidate_links(candidates: list[dict[str, str]], *, scope: _Scope | None = None) -> Any:
    """Render RAG / disambiguation candidates as clickable workflow links.

    Each candidate becomes ``A(label, href="/analyysikeskus/normi-mojuahel?sisend=<ref>")``
    so a click re-runs the workflow with that candidate's reference text;
    when *scope* is supplied the chosen scope params ride along so a
    disambiguation pick keeps the user's scope selection. Empty /
    ref-less candidates are skipped; an empty list renders nothing.
    """
    if not candidates:
        return ""
    items = []
    for c in candidates:
        ref = (c.get("ref") or c.get("label") or "").strip()
        if not ref:
            continue
        items.append(Li(A(c.get("label") or ref, href=_normi_link(ref, scope=scope))))  # noqa: F405
    if not items:
        return ""
    return Ul(*items, cls="analyysikeskus-candidates")  # noqa: F405


# ---------------------------------------------------------------------------
# GET /analyysikeskus/normi-mojuahel
# ---------------------------------------------------------------------------


def normi_mojuahel_page(req: Request):
    """GET /analyysikeskus/normi-mojuahel?sisend=<text> — the Normi mõjuahel workflow.

    Flow (per the epic #714 design note):

    1. Blank ``sisend`` → 303 back to ``/analyysikeskus``.
    2. ``sisend`` is a UUID of a draft the caller's org owns *and* that
       draft has an ``impact_reports`` row → render that persisted report
       through the result shell (no synthetic graph). ``Lisa märkus`` is
       enabled here (links to ``/drafts/{id}/report`` where the row-
       annotation flow lives).
    3. Else parse ``sisend`` → resolve via :class:`ReferenceResolver`:
       * exactly one resolved entity → run the ephemeral-graph impact
         analysis (:func:`run_adhoc_impact_analysis`), score it, render
         the result;
       * nothing resolved → render a friendly "no structured reference"
         warning + (optionally) RAG candidate links;
       * multiple plausible resolutions → render them as clickable
         disambiguation links.
    """
    auth = req.scope.get("auth") or None
    theme = get_theme_from_request(req)
    org_id = auth.get("org_id") if auth else None

    sisend = (req.query_params.get("sisend") or "").strip()
    if not sisend:
        return RedirectResponse(url="/analyysikeskus", status_code=303)

    scope = _Scope(req.query_params)

    # --- 2. UUID → owned-draft report short-circuit -------------------------
    maybe_uuid = _try_parse_uuid(sisend)
    if maybe_uuid is not None:
        report_row = _load_owned_draft_report(maybe_uuid, org_id)
        if report_row is not None:
            return _render_draft_backed_result(
                req,
                auth=auth,
                theme=theme,
                draft_id=str(report_row[0]),
                draft_title=str(report_row[1] or "Pealkirjata eelnõu"),
                report_data=_parse_report_data(report_row[3]),
                impact_score=int(report_row[4] or 0),
                sisend=sisend,
                scope=scope,
            )
        # UUID that isn't an owned draft with a report → fall through to
        # the parse path (it may still be a recognisable reference).

    # --- 3. parse + resolve -------------------------------------------------
    parsed_refs = parse_user_reference(sisend)
    resolved = _resolve_refs(parsed_refs)
    resolved_with_uri = [
        r for r in resolved if getattr(r, "entity_uri", None) and str(r.entity_uri).strip()
    ]
    # Dedupe by entity URI so "AvTS § 35" + the "AvTS" law ref both
    # resolving don't count as two distinct entities.
    seen: set[str] = set()
    unique_resolved: list[Any] = []
    for r in resolved_with_uri:
        uri = str(r.entity_uri)
        if uri in seen:
            continue
        seen.add(uri)
        unique_resolved.append(r)

    if len(unique_resolved) == 1:
        return _render_adhoc_result(
            req,
            auth=auth,
            theme=theme,
            resolved=unique_resolved[0],
            sisend=sisend,
            scope=scope,
        )

    if len(unique_resolved) > 1:
        return _render_disambiguation(
            req,
            auth=auth,
            theme=theme,
            resolved=unique_resolved,
            sisend=sisend,
            scope=scope,
        )

    # Nothing resolved.
    return _render_unresolved(
        req,
        auth=auth,
        theme=theme,
        sisend=sisend,
        scope=scope,
        org_id=org_id,
    )


# ---------------------------------------------------------------------------
# Render branches
# ---------------------------------------------------------------------------


def _try_parse_uuid(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(value)
    except (ValueError, TypeError):
        return None


def _resolved_label(resolved: Any, fallback: str) -> str:
    """Best human label for a resolved ref."""
    label = getattr(resolved, "matched_label", None)
    if label and str(label).strip():
        return str(label).strip()
    extracted = getattr(resolved, "extracted", None)
    if extracted is not None and getattr(extracted, "ref_text", None):
        return str(extracted.ref_text).strip()
    return fallback


def _resolved_type_label(resolved: Any) -> str:
    """Estonian type label for a resolved ref's ref_type, or "" if unknown."""
    extracted = getattr(resolved, "extracted", None)
    ref_type = getattr(extracted, "ref_type", "") if extracted is not None else ""
    return {
        "law": "seadus",
        "provision": "säte",
        "eu_act": "EL õigusakt",
        "court_decision": "kohtulahend",
        "concept": "õigusmõiste",
    }.get(str(ref_type), "")


def _result_actions(
    *,
    focus_uri: str | None,
    draft_id: str | None = None,
    adhoc: bool,
) -> list[dict[str, str]]:
    """Build the ``Soovitatud tegevused`` action set.

    ``Lisa märkus`` only on the draft-backed path (links to the row-
    annotation flow at ``/drafts/{id}/report``); it is *omitted* on an
    ad-hoc result, which has no ``draft_version_id`` + no row-annotation
    flow. ``Ekspordi memo`` is omitted for ad-hoc (no export machinery
    yet) — on the draft-backed path the user can export from the report
    page itself, so we don't duplicate it here either; the design note
    allows omitting it.
    """
    actions: list[dict[str, str]] = []
    if focus_uri:
        actions.append({"label": "Ava õiguskaardil", "href": explorer_focus_url(focus_uri)})
    actions.append({"label": "Küsi nõustajalt", "href": "/chat/new"})
    if draft_id and not adhoc:
        actions.append({"label": "Lisa märkus", "href": f"/drafts/{draft_id}/report"})
    actions.append({"label": "Tagasi analüüsikeskusesse", "href": "/analyysikeskus"})
    return actions


def _render_adhoc_result(
    req: Request,
    *,
    auth: Any,
    theme: str,
    resolved: Any,
    sisend: str,
    scope: _Scope,
) -> Any:
    """Render the result page for a single resolved entity (ephemeral-graph path).

    Runs :func:`run_adhoc_impact_analysis` (which mints + PUTs + analyses
    + **always deletes** an ephemeral named graph), scores the findings,
    and lays them out through :func:`analysis_result_shell`. Because the
    synthetic graph is already torn down by the time control returns
    here, a render error below cannot leave a graph behind.
    """
    entity_uri = str(resolved.entity_uri)
    label = _resolved_label(resolved, sisend)
    type_label = _resolved_type_label(resolved)

    result = run_adhoc_impact_analysis(entity_uri)
    findings = result.findings
    score = result.score

    input_summary = P(  # noqa: F405
        "Analüüsisin: ",
        Strong(label),
        (f" — {type_label}" if type_label else ""),
    )

    results_block = _build_results_block(findings, score, scope)
    evidence_block = _build_evidence_block(findings, analysed_label=label, scope=scope)
    actions = _result_actions(focus_uri=entity_uri, adhoc=True)

    return analysis_result_shell(
        workflow_title="Normi mõjuahel",
        input_summary=input_summary,
        results_block=results_block,
        evidence_block=evidence_block if evidence_block else _missing_row("Tõendeid ei leitud."),
        actions=actions,
        user=auth,
        theme=theme,
        scope_block=_normi_scope_block(sisend, scope),
    )


def _render_draft_backed_result(
    req: Request,
    *,
    auth: Any,
    theme: str,
    draft_id: str,
    draft_title: str,
    report_data: dict[str, Any],
    impact_score: int,
    sisend: str,
    scope: _Scope,
) -> Any:
    """Render the result page from a draft's persisted ``impact_reports`` row.

    No synthetic graph here — the findings come straight from the
    ``impact_reports`` row. ``Lisa märkus`` is enabled (links to
    ``/drafts/{id}/report`` where the row-annotation flow lives).
    """
    findings = _findings_from_report_data(report_data)

    input_summary = Div(  # noqa: F405
        P("Analüüsisin eelnõu: ", Strong(draft_title)),  # noqa: F405
        P(  # noqa: F405
            A(
                "Ava eelnõu mõjuaruanne →",
                href=f"/drafts/{draft_id}/report",
                cls="data-table-link",
            ),
            cls="muted-text",
        ),
    )

    results_block = _build_results_block(findings, impact_score, scope)
    evidence_block = _build_evidence_block(findings, analysed_label=draft_title, scope=scope)
    actions = _result_actions(focus_uri=None, draft_id=draft_id, adhoc=False)
    # Draft-backed: also offer the explorer view of the whole draft.
    actions.insert(0, {"label": "Ava õiguskaardil", "href": f"/explorer?draft={draft_id}"})

    return analysis_result_shell(
        workflow_title="Normi mõjuahel",
        input_summary=input_summary,
        results_block=results_block,
        evidence_block=evidence_block if evidence_block else _missing_row("Tõendeid ei leitud."),
        actions=actions,
        user=auth,
        theme=theme,
        scope_block=_normi_scope_block(sisend, scope),
    )


def _render_disambiguation(
    req: Request,
    *,
    auth: Any,
    theme: str,
    resolved: list[Any],
    sisend: str,
    scope: _Scope,
) -> Any:
    """Render a disambiguation page listing the plausible resolutions as links."""
    candidates: list[dict[str, str]] = []
    for r in resolved:
        label = _resolved_label(r, sisend)
        extracted = getattr(r, "extracted", None)
        ref_text = str(getattr(extracted, "ref_text", "") or label)
        candidates.append({"label": label, "ref": ref_text})

    results_block = [
        Alert(
            "Sisend võib viidata mitmele üksusele. Vali, millist analüüsida:",
            variant="info",
        ),
        _candidate_links(candidates, scope=scope),
    ]
    return analysis_result_shell(
        workflow_title="Normi mõjuahel",
        input_summary=P(f"Sisestasite: «{sisend}»"),  # noqa: F405
        results_block=results_block,
        evidence_block=_missing_row("—"),
        actions=[
            {"label": "Küsi nõustajalt", "href": "/chat/new"},
            {"label": "Tagasi analüüsikeskusesse", "href": "/analyysikeskus"},
        ],
        user=auth,
        theme=theme,
        scope_block=_normi_scope_block(sisend, scope),
    )


def _render_unresolved(
    req: Request,
    *,
    auth: Any,
    theme: str,
    sisend: str,
    scope: _Scope,
    org_id: str | None,
) -> Any:
    """Render the "no structured reference recognised" page (+ optional RAG candidates)."""
    warning = Alert(
        "Ei tuvastanud õiguslikku viidet. Proovige nt «AvTS § 35», CELEX-numbrit "
        "(32016R0679) või kohtulahendi numbrit.",
        variant="warning",
    )
    candidates = _rag_candidates(sisend, org_id)
    results_children: list[Any] = [warning]
    if candidates:
        results_children.append(
            P("Võimalikud sätted, mida võisite mõelda:", cls="muted-text")  # noqa: F405
        )
        results_children.append(_candidate_links(candidates, scope=scope))

    return analysis_result_shell(
        workflow_title="Normi mõjuahel",
        input_summary=P(f"Sisestasite: «{sisend}»"),  # noqa: F405
        results_block=results_children,
        evidence_block=_missing_row("—"),
        actions=[
            {"label": "Küsi nõustajalt", "href": "/chat/new"},
            {"label": "Tagasi analüüsikeskusesse", "href": "/analyysikeskus"},
        ],
        user=auth,
        theme=theme,
        scope_block=_normi_scope_block(sisend, scope),
    )


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------


def register_analyysikeskus_routes(rt) -> None:  # type: ignore[no-untyped-def]
    """Register Analüüsikeskus routes on the FastHTML route decorator *rt*."""
    rt("/analyysikeskus", methods=["GET"])(analyysikeskus_page)
    rt("/analyysikeskus/normi-mojuahel", methods=["GET"])(normi_mojuahel_page)
    rt("/analyysikeskus/el-ulevott", methods=["GET"])(el_ulevott_page)
