"""Analüüsikeskus routes (#714).

The Analüüsikeskus is the legal-analysis workflow hub — the design
rationale lives in ``docs/2026-05-11-ministry-lawyer-ui-structure.md``.
This module hosts:

    GET  /analyysikeskus                         — workflow directory (#720)
    GET  /analyysikeskus/normi-mojuahel          — Normi mõjuahel (#722)
    GET  /analyysikeskus/el-ulevott              — EL ülevõtt ja harmoneerimine (#723)

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

**#723 — EL ülevõtt ja harmoneerimine.** The workflow resolves the
user's input to one ``estleg:EULegislation`` URI — a CELEX number via
:class:`app.docs.reference_resolver.ReferenceResolver`, or a free-text
title / policy area via a label search
(:func:`app.analyysikeskus.eu_lookup.search_eu_acts_by_label`); several
matches are surfaced as clickable candidates, never silently picked —
then runs an **entity-centered, act/provision-level transposition
query** (:func:`app.docs.impact.eu_transposition.run_eu_transposition`,
no synthetic graph) and renders the transposition table + risk band
through the same result shell. There is no EU Article/Obligation entity
model in the ontology, so the article-by-article matrix is a separate
ontology-enrichment ticket. Same legal/policy-only ``Ulatus`` framing
as Normi.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import UTC, date, datetime
from typing import Any

from fasthtml.common import *  # noqa: F403
from starlette.requests import Request
from starlette.responses import RedirectResponse

from app.analyysikeskus.adhoc_analysis import run_adhoc_impact_analysis
from app.analyysikeskus.burden import (
    BurdenDelta,
    BurdenRow,
    BurdenSummary,
    burden_delta_for_draft,
    burden_description,
    burden_key_order,
    burden_label,
    list_burden_for_act,
    list_burden_for_provision,
)
from app.analyysikeskus.competency import (
    InstitutionCompetences,
    gather_institution_competences,
    get_institution_label,
    search_institutions_by_label,
)
from app.analyysikeskus.court_practice import (
    CourtDecisionRow,
    CourtPracticeGroup,
    group_by_court,
    list_decisions_for_act,
    list_decisions_for_provision,
)
from app.analyysikeskus.eu_lookup import is_canonical_celex_shape, search_eu_acts_by_label
from app.analyysikeskus.history import get_history_bundle, temporal_status_label
from app.analyysikeskus.input_parser import parse_user_reference
from app.analyysikeskus.result_shell import analysis_result_shell
from app.analyysikeskus.sanctions import (
    SanctionRow,
    find_similar_sanctions,
    list_sanctions_for_act,
    list_sanctions_for_provision,
    sanction_type_label,
    sanction_unit_label,
)
from app.analyysikeskus.similarity import (
    SimilarityRow,
    find_similar,
    reason_labels_et,
)
from app.db import get_connection as _connect
from app.docs.entity_extractor import ExtractedRef
from app.docs.impact.analyzer import ImpactFindings
from app.docs.impact.eu_transposition import run_eu_transposition
from app.docs.impact.scoring import IMPACT_BAND_LABELS_ET, ImpactBand, impact_band
from app.docs.labels import TYPE_LABELS_ET as _TYPE_LABELS_ET
from app.docs.reference_resolver import ReferenceResolver
from app.docs.report_routes import explorer_focus_url
from app.drafter.state_machine import STEP_LABELS_ET, Step
from app.ui.capabilities import CAPABILITIES, Capability
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


# ---------------------------------------------------------------------------
# Directory page — workflow card builders driven by the B3 capability dict
# ---------------------------------------------------------------------------
#
# The directory shows every capability whose ``target_url`` lives under
# ``/analyysikeskus`` (these are the legal-analysis workflows). Live ones
# render as full ``_workflow_card`` rows with their input form; planned
# ones render as a smaller ``_planned_workflow_card`` with a "Tulekul"
# badge and no input — clicking is disabled but the card still describes
# what's coming so the directory stays an honest map of what the system
# *will* do.
#
# A small overlay dict keeps per-workflow input metadata (placeholder /
# aria label / example queries) that doesn't belong on every Capability
# (most consumers don't need it). When a planned workflow ships, its entry
# is added here at the same time its ``status`` flips to ``"live"``.
_ANALYYSIKESKUS_INPUTS: dict[str, dict[str, str]] = {
    "normi-mojuahel": {
        "placeholder": (
            "Nt: AvTS § 35 · CELEX-number · eelnõu pealkiri · või kirjeldage muudatust"
        ),
        "aria_label": "Õiguslik viide või kirjeldus",
        "examples": "Näited: «Muudame AvTS § 35.» · «Kontrolli karistusseadustiku § 133 mõju.»",
    },
    "el-ulevott": {
        "placeholder": "Nt: CELEX-number · EL akti pealkiri · poliitikavaldkond",
        "aria_label": "CELEX-number, EL akti pealkiri või valdkond",
        "examples": "Näited: «Kontrolli AI määruse ülevõttu.» · «32016R0679»",
    },
    "sanktsioonid": {
        "placeholder": "Nt: KarS § 211 · KMS · CELEX-number · või kirjeldage säte",
        "aria_label": "Õiguslik viide või kirjeldus",
        "examples": "Näited: «KarS § 211» · «KMS § 30» · «32016R0679»",
    },
    "kohtupraktika": {
        "placeholder": "Nt: AvTS § 35 · KarS § 211 · CELEX-number · 3-1-1-63-15",
        "aria_label": "Säte, akt, CELEX-number või kohtuasja number",
        "examples": ("Näited: «AvTS § 35» · «KarS § 211» · «32016R0679» · «3-1-1-63-15»"),
    },
    "halduskoormus": {
        "placeholder": "Nt: KMS · TLS § 12 · eelnõu pealkiri · või kirjeldage akti",
        "aria_label": "Õigusakt, säte või eelnõu",
        "examples": ("Näited: «Töölepingu seadus» · «KMS» · «TLS § 12» · «32016R0679»"),
    },
    "padevused": {
        "placeholder": "Nt: Andmekaitse Inspektsioon · Maksu- ja Tolliamet",
        "aria_label": "Asutuse nimi",
        "examples": (
            "Näited: «Andmekaitse Inspektsioon» · «Tarbijakaitse ja Tehnilise Järelevalve Amet»"
        ),
    },
    "ajalugu": {
        "placeholder": ("Nt: AvTS § 35 · KMS · CELEX-number · või lahendinumber 3-2-1-100-15"),
        "aria_label": "Õiguslik viide või kirjeldus",
        "examples": "Näited: «AvTS § 35» · «KMS» · «3-2-1-100-15»",
    },
    "sarnasus": {
        "placeholder": "Nt: AvTS § 35 · CELEX-number · või kirjeldage sätte sisu",
        "aria_label": "Õiguslik viide või vaba tekst",
        "examples": "Näited: «AvTS § 35» · «menetlustähtaegade pikendamine»",
    },
}


def _planned_workflow_card(cap: Capability) -> Any:
    """A directory entry for a not-yet-wired Analüüsikeskus workflow.

    Renders with a ``Tulekul`` badge in the header and no input form — the
    point is to advertise the workflow's existence (so it appears in the
    sidebar of the directory and is discoverable), not to expose a dead form.
    """
    return Card(
        CardHeader(
            H3(  # noqa: F405
                cap.canonical_name_et,
                " ",
                Badge("Tulekul", variant="warning"),
                cls="card-title",
            )
        ),
        CardBody(
            P(cap.one_line_description_et),  # noqa: F405
            Small(  # noqa: F405
                "See töövoog avaneb peagi.",
                cls="muted-text",
            ),
        ),
        cls="analyysikeskus-card analyysikeskus-card--planned",
    )


def _capability_card(cap: Capability) -> Any:
    """Render a Capability as a directory entry — full card or "Tulekul" card."""
    if cap.status != "live":
        return _planned_workflow_card(cap)
    inputs = _ANALYYSIKESKUS_INPUTS.get(cap.slug)
    if inputs is None:
        # A live capability that's wired but has no input-form metadata — fall
        # back to the planned-card layout so the user can still see it (and the
        # developer notices the missing entry).
        return _planned_workflow_card(cap)
    return _workflow_card(
        title=cap.canonical_name_et,
        purpose=cap.one_line_description_et,
        action=cap.target_url,
        input_name="sisend",
        input_placeholder=inputs["placeholder"],
        input_aria_label=inputs["aria_label"],
        examples=inputs["examples"],
    )


def analyysikeskus_page(req: Request):
    """GET /analyysikeskus — the legal-analysis workflow directory.

    The card list is generated from :data:`app.ui.capabilities.CAPABILITIES`,
    filtered to entries whose ``target_url`` lives under ``/analyysikeskus``.
    Live capabilities (``Normi mõjuahel``, ``EL ülevõtt``) render with their
    input form; planned ones (``Kohtupraktika``, ``Sanktsioonid``, ...) render
    as a compact "Tulekul" card. The order matches the dict's order — see
    ``app/ui/capabilities.py`` for the canonical ordering rules.
    """
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

    # Pull every capability whose target lives under /analyysikeskus —
    # preserves the canonical order from the dict (live first within each
    # use case).
    workflow_caps = [c for c in CAPABILITIES if c.target_url.startswith("/analyysikeskus")]
    workflow_cards = [_capability_card(c) for c in workflow_caps]

    return PageShell(
        *header,
        *workflow_cards,
        _recent_analyses_card(recent),
        title="Analüüsikeskus",
        user=auth,
        theme=theme,
        active_nav="/analyysikeskus",
    )


# ---------------------------------------------------------------------------
# Shared between Normi mõjuahel (#722) and EL ülevõtt (#723)
# ---------------------------------------------------------------------------

# Workflow identifiers — drive which ``Ulatus`` checkboxes/defaults apply
# in :class:`_Scope` and which action URL the scope form posts back to.
_WORKFLOW_NORMI = "normi"
_WORKFLOW_EL = "el_ulevott"

_WORKFLOW_ACTION: dict[str, str] = {
    _WORKFLOW_NORMI: "/analyysikeskus/normi-mojuahel",
    _WORKFLOW_EL: "/analyysikeskus/el-ulevott",
}

# CELEX shape — mirrors :data:`app.docs.reference_resolver._CELEX_RE`
# (``32016R0679``-style). Used by the EL ülevõtt route to pick the best
# ``sisend`` value for a candidate's deep-link (CELEX if known, else
# the exact label).
_CELEX_TOKEN_RE = re.compile(r"^\d{5}[A-Z]\d{1,4}$", re.IGNORECASE)


# Scope state read off the query string. The set of boolean controls —
# and their defaults — depends on the workflow (the design note: Normi
# defaults EU + court practice on / org-wide drafts off; EL ülevõtt
# defaults court practice on / transposing-provision drafts off, and
# treats EU law as always in scope since the whole workflow is
# EU-centric, so it has no "Kaasa EL õigus" toggle).
class _Scope:
    """Parsed ``Ulatus`` scope from the GET query params.

    ``oigus`` is informational only (temporal redactions aren't wired —
    the second select option is disabled). The boolean flags are
    workflow-specific:

    * **Normi mõjuahel** — ``include_eu`` / ``include_court`` actually
      filter the analysis output; ``org_wide_drafts`` only re-frames the
      ``Seotud eelnõud`` block.
    * **EL ülevõtt** — ``include_eu`` is always ``True`` (no toggle);
      ``include_court`` toggles the "Kaasa kohtupraktika" control and
      ``include_transposing_drafts`` the "Kaasa eelnõud, mis puudutavad
      ülevõtvaid sätteid" control. In the act/provision-level MVP
      neither currently *filters* the transposition table (the data to
      do so honestly isn't surfaced by the act-level query yet — see
      :func:`el_ulevott_page`); the controls are kept for UX parity with
      Normi and the params ride through so the page stays shareable.
    """

    def __init__(self, params: Any, *, workflow: str = _WORKFLOW_NORMI) -> None:
        # Checkboxes: present in the query string ⇒ checked. We detect
        # "the form was submitted" by the marker hidden input
        # ``ulatus_submitted`` — on a *first* GET (no scope params) we
        # apply the workflow's defaults instead of treating every box as
        # unchecked.
        self.workflow = workflow
        submitted = params.get("ulatus_submitted") == "1"
        if workflow == _WORKFLOW_EL:
            self.include_eu = True  # EU-centric workflow — always in scope, no toggle
            if submitted:
                self.include_court = params.get("kaasa_kohtupraktika") is not None
                self.include_transposing_drafts = params.get("kaasa_eelnoud") is not None
            else:
                self.include_court = True
                self.include_transposing_drafts = False
            # ``org_wide_drafts`` is unused by EL ülevõtt; kept as a
            # harmless alias so any shared helper that reads it doesn't
            # need to special-case the workflow.
            self.org_wide_drafts = False
        else:
            self.include_transposing_drafts = False
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
        if self.workflow == _WORKFLOW_EL:
            if self.include_court:
                pairs.append(("kaasa_kohtupraktika", "1"))
            if self.include_transposing_drafts:
                pairs.append(("kaasa_eelnoud", "1"))
        else:
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

    def workflow_link(self, sisend: str) -> str:
        """Build the workflow's ``…?sisend=…`` URL with the scope params attached."""
        action = _WORKFLOW_ACTION.get(self.workflow, _WORKFLOW_ACTION[_WORKFLOW_NORMI])
        return f"{action}?{_split_link_query(self.query_pairs(sisend))}"


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


def _workflow_link(sisend: str, *, workflow: str, scope: _Scope | None = None) -> str:
    """Build a ``/analyysikeskus/<workflow>?sisend=…`` link (scope-carrying when given).

    Shared by Normi mõjuahel (#722) and EL ülevõtt (#723) — both render
    clickable candidate / disambiguation links that re-run the workflow.
    """
    if scope is not None:
        return scope.workflow_link(sisend)
    action = _WORKFLOW_ACTION.get(workflow, _WORKFLOW_ACTION[_WORKFLOW_NORMI])
    return f"{action}?{_split_link_query([('sisend', sisend)])}"


def _normi_link(sisend: str, *, scope: _Scope | None = None) -> str:
    """Build a ``/analyysikeskus/normi-mojuahel?sisend=…`` link (scope-carrying)."""
    return _workflow_link(sisend, workflow=_WORKFLOW_NORMI, scope=scope)


# ---------------------------------------------------------------------------
# Shared ``Ulatus`` scope form (overrides result_shell's disabled stub)
# ---------------------------------------------------------------------------
#
# Both workflows render the same shape of GET form back to their own
# route, carrying ``sisend`` + ``ulatus_submitted=1`` hidden + a set of
# legal-language scope checkboxes + the disabled "tulekul" controls
# (temporal redactions, KOV regulations, time range) + an enabled
# "Uuenda ulatust" submit. Only the per-workflow checkbox set differs,
# so the builder below takes that as a parameter — Normi and EL ülevõtt
# share everything else. **All copy is legal/policy language — no
# SPARQL / RDF / named-graph / embedding vocabulary anywhere; these are
# *scope* words.**

_LAW_SCOPE_OPTIONS: list[tuple[str, str]] = [
    ("current", "Kehtiv õigus"),
    ("current_plus_history", "Kehtiv + varasemad redaktsioonid (tulekul)"),
]


def _scope_form(
    *,
    sisend: str,
    workflow: str,
    intro_text: str,
    checkboxes: list[Any],
    checkbox_help: str | None = None,
) -> Any:
    """Render the enabled ``Ulatus`` scope form for a workflow.

    Args:
        sisend: The analysed input — carried through as a hidden field.
        workflow: One of :data:`_WORKFLOW_NORMI` / :data:`_WORKFLOW_EL`;
            selects the form's ``action`` URL.
        intro_text: The muted explanatory sentence above the controls
            (states the default scope in legal terms).
        checkboxes: The workflow-specific scope ``Checkbox`` controls,
            already constructed with their checked state. Disabled
            "tulekul" controls (temporal redactions, KOV, time range)
            are appended by this builder, so callers pass only the
            enabled ones.
        checkbox_help: Optional muted ``Small`` note rendered right
            after the workflow checkboxes (e.g. what an opt-in does).
    """
    action = _WORKFLOW_ACTION.get(workflow, _WORKFLOW_ACTION[_WORKFLOW_NORMI])
    parts: list[Any] = [
        P(intro_text, cls="muted-text"),  # noqa: F405
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
        *checkboxes,
    ]
    if checkbox_help:
        parts.append(Small(checkbox_help, cls="muted-text"))  # noqa: F405
    parts.extend(
        [
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
        ]
    )
    return Form(  # noqa: F405
        *parts,
        method="get",
        action=action,
        cls="analyysikeskus-scope-form",
    )


def _normi_scope_block(sisend: str, scope: _Scope) -> Any:
    """The enabled ``Ulatus`` scope form for Normi mõjuahel (#722)."""
    return _scope_form(
        sisend=sisend,
        workflow=_WORKFLOW_NORMI,
        intro_text=(
            "Analüüsin vaikimisi kehtivat õigust, seotud eelnõusid, Riigikohtu "
            "praktikat ja EL õigusakte. Võite ulatust muuta ja analüüsi uuesti käivitada."
        ),
        checkboxes=[
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
        ],
        checkbox_help=(
            "Vaikimisi näitan otseseid eelnõuseoseid; märkige see, et "
            "kaasata kogu organisatsiooni eelnõud."
        ),
    )


def _el_ulevott_scope_block(sisend: str, scope: _Scope) -> Any:
    """The enabled ``Ulatus`` scope form for EL ülevõtt ja harmoneerimine (#723).

    Legal/policy language only — the controls read as "what to include in
    the transposition overview", never as query configuration. In the
    act/provision-level MVP the two enabled checkboxes are kept for UX
    parity with Normi and carry through for shareability, but neither
    currently filters the transposition table (see :func:`el_ulevott_page`
    for the honesty note).
    """
    return _scope_form(
        sisend=sisend,
        workflow=_WORKFLOW_EL,
        intro_text=(
            "Vaatan vaikimisi, millised Eesti õigusaktid ja sätted on selle "
            "EL õigusaktiga seotud, ning seotud Riigikohtu praktikat. Võite "
            "ulatust muuta ja analüüsi uuesti käivitada."
        ),
        checkboxes=[
            Checkbox(
                "kaasa_kohtupraktika",
                checked=scope.include_court,
                label="Kaasa kohtupraktika",
            ),
            Checkbox(
                "kaasa_eelnoud",
                checked=scope.include_transposing_drafts,
                label="Kaasa eelnõud, mis puudutavad ülevõtvaid sätteid",
            ),
        ],
        checkbox_help=(
            "Kohtupraktika ja eelnõude sidumine ülevõtvate sätetega on "
            "täiendamisel — praegu kuvan akti- ja sättetasandi ülevaate."
        ),
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

# #724: cap the phrased-finding seed length so a pathological label/relation
# can't bloat the hidden input. The seed goes through the server-side token
# table (POST /chat/seed) so this is purely a sanity bound on the form body.
_EVIDENCE_SEED_MAX_LEN = 600


def _evidence_seed_text(*, source_label: str, relation: str, target_label: str) -> str:
    """Phrase a single ``Tõendid`` finding as a question for the Nõustaja.

    Deliberately short and URL-free — the chat-seed travels through a
    server-side token (POST /chat/seed), never the URL, but keeping it terse
    keeps the textarea readable. The «…» quoting mirrors the input-summary
    style used elsewhere on the result page.
    """
    src = (source_label or "").strip()
    rel = (relation or "").strip()
    tgt = (target_label or "").strip()
    if rel and rel != "—" and tgt:
        finding = f"«{src}» {rel} «{tgt}»"
    elif rel and rel != "—":
        finding = f"«{src}» {rel}"
    else:
        finding = f"«{src}»"
    text = f"Selgita seda mõjuanalüüsi leidu: {finding}. Mida peaksin selle puhul tähele panema?"
    return text[:_EVIDENCE_SEED_MAX_LEN]


def _evidence_seed_form(seed_text: str, *, draft_id: str | None) -> Any:
    """Render the inline "Küsi nõustajalt" form for one ``Tõendid`` row (#724).

    A tiny ``<form method="post" action="/chat/seed">`` with the phrased
    finding in a ``seed_text`` hidden input plus, on a draft-backed analysis,
    a ``draft_id`` hidden input so the new conversation gets the draft's
    impact context. The submit is rendered as a ghost/small button so it sits
    quietly alongside the row's other affordances ("Ava allikas", "Ava
    õiguskaardil →"). The seed text never travels through the URL — it's
    stashed server-side and the redirect carries only an opaque token.
    """
    # Use FastHTML's ``Hidden`` helper (from the wildcard import) — the
    # project's ``Input`` primitive's ``InputType`` literal doesn't include
    # ``"hidden"``, and the rest of this module already uses ``Hidden(...)``.
    inputs: list[Any] = [
        Hidden(name="seed_text", value=seed_text),  # noqa: F405
    ]
    if draft_id:
        inputs.append(Hidden(name="draft_id", value=str(draft_id)))  # noqa: F405
    return Form(  # noqa: F405
        *inputs,
        Button(
            "Küsi nõustajalt",
            type="submit",
            variant="ghost",
            size="sm",
        ),
        method="post",
        action="/chat/seed",
        cls="analyysikeskus-evidence-seed-form inline-form",
    )


def _evidence_row(
    *,
    source_label: str,
    relation: str,
    target_label: str,
    uri: str,
    why: str,
    snippet: str = "",
    when: str = "",
    draft_id: str | None = None,
) -> Any:
    """One row in the ``Tõendid`` card.

    Carries the source label, the relation **in legal language**, an
    optional snippet/date, an "Ava allikas" link, a "miks see on oluline"
    line, an "Ava õiguskaardil →" deep link (URL-encoded by
    :func:`explorer_focus_url`), and — #724 — a small "Küsi nõustajalt"
    form that pre-fills the chat input with this finding phrased as a
    question. ``draft_id`` (when this is a draft-backed analysis) is threaded
    into that form's hidden input so the chat picks up the draft context.
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
    # #724: the "Küsi nõustajalt" affordance sits at the end of the row's
    # action line. Rendered as a sibling block (it's a <form>, not an <a>)
    # so it can't nest inside the link <p>.
    seed_text = _evidence_seed_text(
        source_label=source_label, relation=relation, target_label=target_label
    )
    seed_form = _evidence_seed_form(seed_text, draft_id=draft_id)
    if link_bits:
        bits.append(P(*link_bits))  # noqa: F405
    bits.append(seed_form)
    return Div(*bits, cls="analyysikeskus-evidence-row")  # noqa: F405


def _build_evidence_block(
    findings: ImpactFindings,
    *,
    analysed_label: str,
    scope: _Scope,
    draft_id: str | None = None,
) -> list[Any]:
    """Assemble the ``Tõendid`` rows from the findings.

    One row per affected entity (the affected-entities pass doesn't
    return the linking predicate, so the relation reads as the neutral
    "on seotud üksusega"), plus one row per conflict (relation = the
    analyzer's reason string, which is already legal language) and one
    row per EU link ("võtab üle direktiivi"). Court rows are filtered
    out when ``Kaasa kohtupraktika`` is off; EU rows when ``Kaasa EL
    õigus`` is off — same scope wiring as :func:`_build_results_block`.

    #724: ``draft_id`` (when this is a draft-backed analysis) is threaded
    into every row's "Küsi nõustajalt" form so the chat picks up the draft
    context. ``None`` on an ad-hoc analysis — the form then carries only the
    phrased finding.
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
                draft_id=draft_id,
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
                draft_id=draft_id,
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
                    draft_id=draft_id,
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


def _partial_act_title(resolved: Any) -> str | None:
    """Return the literal act title from a partial-match :class:`ResolvedRef`.

    The Wave 2 Step 2 resolver returns ``entity_uri=None`` for law-only
    references — the prod corpus has no act-level URIs (Step 1 spike).
    Instead, the canonical act title literal rides along on
    ``partial_match["act_title"]``. Routes use that title as the key
    for the ``list_*_for_act(act_title=…)`` helpers.

    Returns ``None`` when the ref has no partial-match payload or
    ``act_title`` is empty. The resolver guarantees the dict shape
    ``{"act_token", "act_title", "section"}`` when ``partial_match``
    is set, but we defensively handle missing keys.
    """
    partial = getattr(resolved, "partial_match", None)
    if not isinstance(partial, dict):
        return None
    title = partial.get("act_title")
    if title is None:
        return None
    title_str = str(title).strip()
    return title_str or None


def _has_resolved_target(resolved: Any) -> bool:
    """Return True when *resolved* has a usable target — URI OR partial-match.

    The routes used to filter on ``entity_uri is not None`` only,
    which dropped law-only refs returned by the Wave 2 Step 2
    resolver (``entity_uri=None``, ``partial_match={"act_title": …}``).
    The combined check brings those refs back into the dispatch flow
    so the routes can call the ``list_*_for_act`` helpers with the
    literal title.
    """
    entity_uri = getattr(resolved, "entity_uri", None)
    if entity_uri and str(entity_uri).strip():
        return True
    return _partial_act_title(resolved) is not None


def _resolved_key(resolved: Any) -> str:
    """Return a dedupe key for *resolved* — entity URI or partial-match title.

    Used to dedupe a ``[provision_ref, law_ref]`` pair that resolved to
    the same act. The provision branch sets ``entity_uri``; the law
    branch may set only ``partial_match["act_title"]``. We dedupe on
    whichever is set so a ``"AvTS § 35"`` input doesn't render both
    a provision view and a law view side-by-side.
    """
    entity_uri = getattr(resolved, "entity_uri", None)
    if entity_uri and str(entity_uri).strip():
        return f"uri:{str(entity_uri).strip()}"
    title = _partial_act_title(resolved)
    if title is not None:
        return f"title:{title}"
    return ""


def _select_dispatchable(resolved: list[Any]) -> list[Any]:
    """Filter & dedupe a resolver output for route-level dispatch.

    Prefers URI-resolved refs over partial-match-only refs: when at
    least one ref carries a real ``entity_uri``, only those are kept
    (so a ``"AvTS § 35"`` input — which resolves to a provision URI
    plus a law partial-match for the same act — picks the provision
    view, not a two-row disambiguation card). When no URI refs are
    present, falls back to partial-match refs (the law-only input
    flow).

    Returns a deduped list (by :func:`_resolved_key`) preserving
    input order.
    """
    uri_refs = [
        r for r in resolved if getattr(r, "entity_uri", None) and str(r.entity_uri).strip()
    ]
    if uri_refs:
        candidates = uri_refs
    else:
        candidates = [r for r in resolved if _partial_act_title(r) is not None]

    seen: set[str] = set()
    out: list[Any] = []
    for r in candidates:
        key = _resolved_key(r)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


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
    # #724: ad-hoc analysis has no backing draft → the per-row "Küsi
    # nõustajalt" forms carry only the phrased finding (draft_id=None).
    evidence_block = _build_evidence_block(
        findings, analysed_label=label, scope=scope, draft_id=None
    )
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
    # #724: draft-backed analysis → thread the draft_id into the per-row
    # "Küsi nõustajalt" forms so the chat picks up the draft context.
    evidence_block = _build_evidence_block(
        findings, analysed_label=draft_title, scope=scope, draft_id=draft_id
    )
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
# EL ülevõtt ja harmoneerimine (#723)
# ---------------------------------------------------------------------------
#
# Workflow flow (per the epic #714 design note, "Workflow 2"):
#
#   1. parse_user_reference(sisend) → if it contains a CELEX-bearing
#      ``eu_act`` ref, resolve it via ReferenceResolver. Exactly one EU
#      act URI ⇒ run_eu_transposition(uri) and render the result.
#   2. No CELEX (a free-text title / policy area) ⇒ search_eu_acts_by_label.
#      Exactly one candidate ⇒ treat as resolved (re-run with its CELEX
#      or exact label). Several ⇒ render a "Mitu vastet — valige üks:"
#      disambiguation card with clickable candidate links. None ⇒ a
#      friendly "Ei tuvastanud EL õigusakti" warning.
#   3. A dead Jena (resolver / runner / label-search all returning
#      empty) ⇒ a graceful "ei õnnestunud" message inside the result
#      shell — never a 500.
#
# The result page is the standard 5-block ``analysis_result_shell``:
# Sisend / Ulatus / Tulemused (one-line summary + the transposition
# table + a risk band) / Tõendid / Soovitatud tegevused. The MVP is an
# **act/provision-level transposition table** — there is no EU
# Article/Obligation entity model in the ontology, so the article-by-
# article matrix sketched under "Workflow 2 / UI Output" in the design
# note is a separate ontology-enrichment ticket.
#
# Honesty note (per the design note's "honesty over coverage"): the two
# enabled ``Ulatus`` checkboxes ("Kaasa kohtupraktika", "Kaasa eelnõud,
# mis puudutavad ülevõtvaid sätteid") are kept for UX parity with Normi
# and carry through for shareability, but in this act-level MVP neither
# *filters* the transposition table — wiring court practice / drafts to
# the transposing provisions cleanly needs ontology + SPARQL work that's
# out of scope here. Nothing on the page uses SPARQL / RDF / named-graph
# / embedding language — legal/policy words only.

# Status → Badge variant. Consistent with the impact-report / dashboard
# badge usage: covered → success, partial → warning, missing → danger,
# unclear → the neutral default.
_TRANSPOSITION_STATUS_BADGE: dict[str, BadgeVariant] = {
    "kaetud": "success",
    "osaline": "warning",
    "puudub": "danger",
    "ebaselge": "default",
}

# Status → a short Estonian label for the ``Staatus`` column.
_TRANSPOSITION_STATUS_LABEL_ET: dict[str, str] = {
    "kaetud": "Kaetud",
    "osaline": "Osaline",
    "puudub": "Puudub",
    "ebaselge": "Ebaselge",
}

# Status → the recommended next action shown in the ``Soovitatud
# tegevus`` column (a short Estonian phrase, no LLM).
_TRANSPOSITION_STATUS_ACTION_ET: dict[str, str] = {
    "kaetud": "—",
    "osaline": "Täpsusta ülevõttu",
    "puudub": "Lisa puuduv säte",
    "ebaselge": "Kontrolli ülevõtu staatust",
}

# Status → the relation phrase used in the ``Tõendid`` rows.
_TRANSPOSITION_RELATION_ET: dict[str, str] = {
    "with_provision": "on harmoneeritud aktiga",
    "act_only": "võtab üle direktiivi",
}


def _eu_celex_or_label(ref_text: str) -> str:
    """Return *ref_text* unchanged — used to build a candidate deep-link's ``sisend``.

    A candidate's best ``sisend`` is its CELEX (a clean, unambiguous
    re-resolve) if it has one, else its exact label. The caller picks
    which to pass; this helper just keeps the call sites tidy.
    """
    return ref_text


def _eu_summary_line(rows: list[dict[str, Any]], *, eu_label: str) -> Any:
    """The one-line ``Tulemused`` lead — N linked Estonian acts, M uncovered rows."""
    linked_acts = {str(r.get("ee_act")) for r in rows if r.get("ee_act")}
    n_acts = len(linked_acts)
    n_missing = sum(1 for r in rows if str(r.get("status")) != "kaetud")
    return P(  # noqa: F405
        Strong(eu_label),  # noqa: F405
        f" ülevõte: {n_acts} Eesti õigusakti seotud, {n_missing} kohustust/akti katmata.",
    )


def _eu_link(uri: str | None, label: str, *, scope: _Scope | None = None) -> Any:
    """Render *label* linked to the explorer focus view, or plain text without a URI.

    *scope* is accepted (and ignored) for call-site symmetry — the
    explorer link doesn't carry the workflow scope; only the
    workflow's own candidate / scope-form links do.
    """
    text = label or (uri or "—")
    if not uri:
        return Span(text)  # noqa: F405
    return A(text, href=explorer_focus_url(uri), cls="data-table-link")  # noqa: F405


def _eu_act_cell(row: dict[str, Any]) -> Any:
    """``EL õigusakt`` cell — label + CELEX, linked to the explorer."""
    label = str(row.get("eu_label") or "EL õigusakt")
    celex = str(row.get("celex") or "").strip()
    text = f"{label} ({celex})" if celex else label
    return _eu_link(str(row.get("eu_act") or "") or None, text)


def _ee_act_cell(row: dict[str, Any]) -> Any:
    """``Eesti õigusakt(id)`` cell — label linked to the explorer, or "—"."""
    uri = str(row.get("ee_act") or "").strip()
    if not uri:
        return "—"
    label = str(row.get("ee_act_label") or "").strip() or uri
    return _eu_link(uri, label)


def _ee_provision_cell(row: dict[str, Any]) -> Any:
    """``Eesti säte(d)`` cell — the harmonised provision's label, or "—"."""
    uri = str(row.get("ee_provision") or "").strip()
    if not uri:
        return "—"
    label = str(row.get("ee_provision_label") or "").strip() or uri
    return _eu_link(uri, label)


def _status_badge_cell(row: dict[str, Any]) -> Any:
    """``Staatus`` cell — a colour-coded Badge."""
    status = str(row.get("status") or "ebaselge")
    variant = _TRANSPOSITION_STATUS_BADGE.get(status, "default")
    label = _TRANSPOSITION_STATUS_LABEL_ET.get(status, status)
    return Badge(label, variant=variant)


def _status_action_cell(row: dict[str, Any]) -> Any:
    """``Soovitatud tegevus`` cell — a short Estonian phrase keyed by status."""
    status = str(row.get("status") or "ebaselge")
    return _TRANSPOSITION_STATUS_ACTION_ET.get(status, "Kontrolli ülevõtu staatust")


def _eu_risk_band(rows: list[dict[str, Any]]) -> tuple[str, BadgeVariant]:
    """Derive a risk band + Badge variant from the row statuses.

    Any ``puudub`` → ``Kõrge risk``; any ``osaline`` / ``ebaselge`` but
    no ``puudub`` → ``Vajab kontrolli``; all ``kaetud`` → ``Väike mõju``.
    Mirrors the band vocabulary the Normi workflow's result page speaks.
    """
    statuses = {str(r.get("status") or "ebaselge") for r in rows}
    if "puudub" in statuses:
        return "Kõrge risk", "danger"
    if statuses & {"osaline", "ebaselge"}:
        return "Vajab kontrolli", "warning"
    return "Väike mõju", "success"


def _eu_recommendation(rows: list[dict[str, Any]]) -> str:
    """A one-line recommended-next-action sentence templated from the statuses."""
    n_missing = sum(1 for r in rows if str(r.get("status")) == "puudub")
    n_partial = sum(1 for r in rows if str(r.get("status")) in {"osaline", "ebaselge"})
    if n_missing > 0:
        return (
            "Soovitus: lisada puuduvad ülevõtvad sätted ja vajadusel "
            "kaasata EL koordinaator enne menetlust."
        )
    if n_partial > 0:
        return (
            "Soovitus: täpsustada osalist või ebaselget ülevõttu ning "
            "kontrollida vastavust EL õigusaktile."
        )
    return "Soovitus: ülevõte näib kaetud — kasuta seda kinnitusena kooskõlastamisel."


def _eu_results_block(rows: list[dict[str, Any]], *, eu_label: str) -> list[Any]:
    """Assemble the ``Tulemused`` content: summary + transposition table + risk band.

    Empty *rows* (shouldn't normally happen — :func:`run_eu_transposition`
    synthesises a ``puudub`` row when the act has no transposing acts —
    but a dead-Jena ``[]`` reaches here) renders a one-line muted row.
    The ``Vastutav asutus`` column is intentionally absent: no
    competent-authority predicate is wired in the app yet (see
    :mod:`app.docs.impact.eu_transposition`), and shipping a column of
    "—" would be noise.
    """
    if not rows:
        return [_missing_row("Ülevõtu seoseid ei leitud.")]

    capped = rows[:_MAX_RESULT_ROWS]
    columns = [
        Column(key="eu_act", label="EL õigusakt", sortable=False, render=_eu_act_cell),
        Column(key="ee_act", label="Eesti õigusakt(id)", sortable=False, render=_ee_act_cell),
        Column(
            key="ee_provision",
            label="Eesti säte(d)",
            sortable=False,
            render=_ee_provision_cell,
        ),
        Column(key="status", label="Staatus", sortable=False, render=_status_badge_cell),
        Column(
            key="action",
            label="Soovitatud tegevus",
            sortable=False,
            render=_status_action_cell,
        ),
    ]
    band_label, band_variant = _eu_risk_band(rows)
    return [
        _eu_summary_line(rows, eu_label=eu_label),
        DataTable(columns=columns, rows=capped, empty_message="Ülevõtu seoseid ei leitud."),
        _sub_section(
            "Riskihinnang ja soovitus",
            P(Span("Riskitase: ", cls="muted-text"), Badge(band_label, variant=band_variant)),  # noqa: F405
            P(_eu_recommendation(rows)),  # noqa: F405
        ),
    ]


def _eu_evidence_block(rows: list[dict[str, Any]]) -> list[Any]:
    """Assemble the ``Tõendid`` rows — one per transposition / harmonisation link.

    Source = the Estonian act/provision label; relation = "võtab üle
    direktiivi" (act-level) or "on harmoneeritud aktiga" (provision-
    level); target = the EU act label + CELEX. Includes the raw
    ``transpositionStatus`` literal where present (the row's mapped
    bucket label), an "Ava allikas" link to the EE act/provision plus an
    "Ava õiguskaardil →" deep link, a "miks see on oluline" line, and —
    #724 — a small "Küsi nõustajalt" form. A ``puudub`` row (no transposing
    act) becomes one evidence row flagging the gap. Empty → a muted "—".

    The EL ülevõtt workflow is entity-centered on an EU act with no backing
    draft, so the per-row forms carry only the phrased finding (no
    ``draft_id``).
    """
    out: list[Any] = []
    for r in rows or []:
        status = str(r.get("status") or "ebaselge")
        eu_label = str(r.get("eu_label") or "EL õigusakt")
        celex = str(r.get("celex") or "").strip()
        target_label = f"{eu_label} ({celex})" if celex else eu_label
        provision_uri = str(r.get("ee_provision") or "").strip()
        provision_label = str(r.get("ee_provision_label") or "").strip()
        act_uri = str(r.get("ee_act") or "").strip()
        act_label = str(r.get("ee_act_label") or "").strip()

        if status == "puudub":
            out.append(
                _evidence_row(
                    source_label=target_label,
                    relation="ei ole veel üle võetud ühegi Eesti õigusaktiga",
                    target_label="",
                    uri=act_uri or "",
                    why=(
                        "Selle EL õigusakti ülevõtmiseks ei leitud Eesti sätet — "
                        "kontrolli, kas ülevõte on vajalik või veel pooleli."
                    ),
                    when=_TRANSPOSITION_STATUS_LABEL_ET.get(status, status),
                    draft_id=None,
                )
            )
            continue

        if provision_uri:
            source_label = provision_label or act_label or "Eesti säte"
            relation = _TRANSPOSITION_RELATION_ET["with_provision"]
            link_uri = provision_uri
            why = (
                "See Eesti säte on EL õigusaktiga harmoneeritud — "
                "muudatus võib mõjutada vastavust."
            )
        else:
            source_label = act_label or "Eesti õigusakt"
            relation = _TRANSPOSITION_RELATION_ET["act_only"]
            link_uri = act_uri
            why = (
                "See Eesti õigusakt võtab EL õigusakti üle — "
                "kontrolli ülevõtu täielikkust enne menetlust."
            )
        out.append(
            _evidence_row(
                source_label=source_label,
                relation=relation,
                target_label=target_label,
                uri=link_uri,
                why=why,
                when=_TRANSPOSITION_STATUS_LABEL_ET.get(status, status),
                draft_id=None,
            )
        )
    return out


def _eu_actions(eu_act_uri: str | None) -> list[dict[str, str]]:
    """The ``Soovitatud tegevused`` action set for an EL ülevõtt result page.

    ``Ava õiguskaardil`` (the explorer focus view of the EU act),
    ``Küsi nõustajalt`` (→ ``/chat/new``), and ``Tagasi
    analüüsikeskusesse``. ``Koosta kooskõla memo`` has no machinery yet
    so it's omitted (a 500-ing button would be worse than a missing one).
    """
    actions: list[dict[str, str]] = []
    if eu_act_uri:
        actions.append({"label": "Ava õiguskaardil", "href": explorer_focus_url(eu_act_uri)})
    actions.append({"label": "Küsi nõustajalt", "href": "/chat/new"})
    actions.append({"label": "Tagasi analüüsikeskusesse", "href": "/analyysikeskus"})
    return actions


def _eu_input_summary(eu_label: str, celex: str | None) -> Any:
    """The ``Sisend`` card body — which EU act we analysed + its CELEX."""
    return P(  # noqa: F405
        "Analüüsisin EL õigusakti: ",
        Strong(eu_label),  # noqa: F405
        " — ",
        celex or "(CELEX puudub)",
    )


def _render_eu_transposition_result(
    *,
    auth: Any,
    theme: str,
    eu_act_uri: str,
    eu_label: str,
    celex: str | None,
    sisend: str,
    scope: _Scope,
) -> Any:
    """Render the EL ülevõtt result page for a resolved EU act URI.

    Runs :func:`app.docs.impact.eu_transposition.run_eu_transposition`
    (entity-centered, no synthetic graph) and lays the rows out through
    :func:`analysis_result_shell`. A dead Jena ⇒ ``run_eu_transposition``
    returns ``[]`` ⇒ the ``Tulemused`` block shows a graceful "ei
    õnnestunud" line rather than 500.
    """
    rows = run_eu_transposition(eu_act_uri)
    # Prefer the label/CELEX the runner saw on the act itself (it may be
    # richer than what the resolver / label-search handed us).
    if rows:
        eu_label = str(rows[0].get("eu_label") or eu_label) or eu_label
        celex = (str(rows[0].get("celex") or "").strip() or celex) or celex

    if rows:
        results_block: Any = _eu_results_block(rows, eu_label=eu_label)
        evidence_rows = _eu_evidence_block(rows)
        evidence_block: Any = evidence_rows if evidence_rows else _missing_row("—")
    else:
        results_block = Alert(
            "Ülevõtu andmete päring ei õnnestunud. Proovige hiljem uuesti.",
            variant="warning",
        )
        evidence_block = _missing_row("—")

    return analysis_result_shell(
        workflow_title="EL ülevõtt ja harmoneerimine",
        input_summary=_eu_input_summary(eu_label, celex),
        results_block=results_block,
        evidence_block=evidence_block,
        actions=_eu_actions(eu_act_uri),
        user=auth,
        theme=theme,
        scope_block=_el_ulevott_scope_block(sisend, scope),
    )


def _render_eu_disambiguation(
    *,
    auth: Any,
    theme: str,
    candidates: list[dict[str, Any]],
    sisend: str,
    scope: _Scope,
) -> Any:
    """Render the "Mitu vastet — valige üks:" card for several label matches.

    Each candidate becomes a link that re-runs the workflow with that
    candidate's CELEX (preferred — clean re-resolve) or, lacking one,
    its exact label. The scope params ride along so the user's scope
    selection survives the pick.
    """
    items: list[Any] = []
    for c in candidates:
        uri = str(c.get("uri") or "").strip()
        label = str(c.get("label") or uri or "EL õigusakt").strip()
        celex = str(c.get("celex") or "").strip()
        ref_for_link = _eu_celex_or_label(celex or label)
        if not ref_for_link:
            continue
        shown = f"{label} ({celex})" if celex else label
        items.append(
            Li(A(shown, href=_workflow_link(ref_for_link, workflow=_WORKFLOW_EL, scope=scope)))  # noqa: F405
        )
    results_block: list[Any] = [
        Alert("Mitu vastet — valige üks:", variant="info"),
    ]
    if items:
        results_block.append(Ul(*items, cls="analyysikeskus-candidates"))  # noqa: F405
    return analysis_result_shell(
        workflow_title="EL ülevõtt ja harmoneerimine",
        input_summary=P(f"Sisestasite: «{sisend}»"),  # noqa: F405
        results_block=results_block,
        evidence_block=_missing_row("—"),
        actions=[
            {"label": "Küsi nõustajalt", "href": "/chat/new"},
            {"label": "Tagasi analüüsikeskusesse", "href": "/analyysikeskus"},
        ],
        user=auth,
        theme=theme,
        scope_block=_el_ulevott_scope_block(sisend, scope),
    )


def _render_eu_unresolved(
    *,
    auth: Any,
    theme: str,
    sisend: str,
    scope: _Scope,
) -> Any:
    """Render the "Ei tuvastanud EL õigusakti" warning page.

    Two message variants (#805):

    * **Canonical-CELEX shape** (e.g. ``32016R0679`` for GDPR,
      ``32019L1152`` for Working Conditions): the user typed a
      well-formed CELEX that simply hasn't been imported into the
      ontology yet. Tell them *which* CELEX is missing so they know
      to check the act manually rather than wondering if they
      mistyped.
    * **Anything else** (free prose, garbage, malformed CELEX): keep
      the generic "Ei tuvastatud" hint with an example to nudge the
      user toward a structured input.

    The same ``Alert`` warning variant is used in both branches so the
    surrounding scope block / actions / result-shell layout is
    identical — only the copy changes.
    """
    if is_canonical_celex_shape(sisend):
        message = (
            f"EL õigusakt CELEX-numbriga {sisend.strip()} ei ole veel "
            "ontoloogias kaardistatud. Kontrollige käsitsi või "
            "proovige akti pealkirja."
        )
    else:
        message = (
            "Ei tuvastanud EL õigusakti. Proovige CELEX-numbrit "
            "(nt 32016R0679) või akti pealkirja."
        )
    return analysis_result_shell(
        workflow_title="EL ülevõtt ja harmoneerimine",
        input_summary=P(f"Sisestasite: «{sisend}»"),  # noqa: F405
        results_block=Alert(message, variant="warning"),
        evidence_block=_missing_row("—"),
        actions=[
            {"label": "Küsi nõustajalt", "href": "/chat/new"},
            {"label": "Tagasi analüüsikeskusesse", "href": "/analyysikeskus"},
        ],
        user=auth,
        theme=theme,
        scope_block=_el_ulevott_scope_block(sisend, scope),
    )


def _resolve_eu_act_from_celex(refs: list[ExtractedRef]) -> Any | None:
    """Resolve the first ``eu_act`` ref via :class:`ReferenceResolver`.

    Returns the single resolved ref (with ``entity_uri`` set) when
    exactly one EU act resolves, else ``None``. Wrapped so a dead Jena
    (or any resolver crash) degrades to ``None`` — the route then falls
    through to the label-search path. ``parse_user_reference`` only ever
    emits **one** ``eu_act`` ref per input (a single CELEX token), so
    "exactly one" is the normal case.
    """
    eu_refs = [r for r in refs if getattr(r, "ref_type", "") == "eu_act"]
    if not eu_refs:
        return None
    try:
        resolved = ReferenceResolver().resolve(eu_refs)
    except Exception:
        logger.warning("EL ülevõtt: CELEX resolution failed", exc_info=True)
        return None
    with_uri = [
        r for r in resolved if getattr(r, "entity_uri", None) and str(r.entity_uri).strip()
    ]
    if len(with_uri) == 1:
        return with_uri[0]
    return None


def _eu_label_search(sisend: str) -> list[dict[str, Any]]:
    """Free-text → EU-act candidates; an unreachable Jena yields ``[]``.

    Thin wrapper around
    :func:`app.analyysikeskus.eu_lookup.search_eu_acts_by_label` so the
    route has one obvious patch point (and the search-box text is bound
    safely inside the SPARQL ``FILTER(CONTAINS(...))`` — never
    interpolated).
    """
    try:
        return search_eu_acts_by_label(sisend)
    except Exception:
        logger.warning("EL ülevõtt: EU-act label search failed", exc_info=True)
        return []


def el_ulevott_page(req: Request):
    """GET /analyysikeskus/el-ulevott?sisend=<text> — EL ülevõtt ja harmoneerimine (#723).

    Flow (see the section header above for the design rationale):

    1. Blank ``sisend`` → 303 back to ``/analyysikeskus``.
    2. ``parse_user_reference(sisend)`` → if a CELEX-bearing ``eu_act``
       ref resolves to exactly one EU act URI → ``run_eu_transposition``
       → render the act/provision-level transposition table.
    3. No CELEX → ``search_eu_acts_by_label`` → exactly one candidate →
       treat as resolved; several → a "Mitu vastet — valige üks:"
       disambiguation card; none → an "Ei tuvastanud EL õigusakti"
       warning.

    A dead Jena anywhere ⇒ a graceful "ei õnnestunud" message inside the
    result shell, never a 500.
    """
    auth = req.scope.get("auth") or None
    theme = get_theme_from_request(req)

    sisend = (req.query_params.get("sisend") or "").strip()
    if not sisend:
        return RedirectResponse(url="/analyysikeskus", status_code=303)

    scope = _Scope(req.query_params, workflow=_WORKFLOW_EL)

    parsed_refs = parse_user_reference(sisend)

    # --- 1. CELEX path -----------------------------------------------------
    resolved_eu = _resolve_eu_act_from_celex(parsed_refs)
    if resolved_eu is not None:
        eu_uri = str(resolved_eu.entity_uri)
        matched_label = str(getattr(resolved_eu, "matched_label", "") or "").strip()
        extracted = getattr(resolved_eu, "extracted", None)
        celex = str(getattr(extracted, "ref_text", "") or "").strip() or None
        return _render_eu_transposition_result(
            auth=auth,
            theme=theme,
            eu_act_uri=eu_uri,
            eu_label=matched_label or celex or "EL õigusakt",
            celex=celex,
            sisend=sisend,
            scope=scope,
        )

    # --- 2. label-search path ---------------------------------------------
    candidates = _eu_label_search(sisend)
    if len(candidates) == 1:
        only = candidates[0]
        return _render_eu_transposition_result(
            auth=auth,
            theme=theme,
            eu_act_uri=str(only.get("uri") or ""),
            eu_label=str(only.get("label") or "EL õigusakt"),
            celex=str(only.get("celex") or "").strip() or None,
            sisend=sisend,
            scope=scope,
        )
    if len(candidates) > 1:
        return _render_eu_disambiguation(
            auth=auth,
            theme=theme,
            candidates=candidates,
            sisend=sisend,
            scope=scope,
        )

    # --- 3. nothing recognised --------------------------------------------
    return _render_eu_unresolved(auth=auth, theme=theme, sisend=sisend, scope=scope)


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Sanktsioonide indeks (A1 v1 standalone — plan section 5 row A1)
# ---------------------------------------------------------------------------
#
# Workflow flow:
#
#   1. Blank ``sisend`` → render the workflow shell with the input form
#      (no sanctions yet, just the 5 cards waiting for input).
#   2. ``parse_user_reference(sisend)`` → if a §-reference / CELEX /
#      case number resolves to exactly one entity URI, branch on the
#      entity's RDF type:
#        * Provision → run :func:`list_sanctions_for_provision`
#        * Act (anything else with a URI) → run :func:`list_sanctions_for_act`
#      Several plausible resolutions → render a disambiguation card.
#      Nothing recognised → friendly "no structured ref" warning + RAG
#      candidates (matches the Normi mõjuahel pattern from #722).
#   3. With sanctions in hand and the ``vordle`` query param set, also
#      run :func:`find_similar_sanctions` on the first sanction row to
#      surface comparison data from other acts.
#
# All copy is legal/policy language — no SPARQL / RDF / named-graph
# vocabulary on the page (same #714 rule the other two workflows
# follow). A dead Jena anywhere ⇒ the route degrades to the "no
# sanctions" branch rather than 500.

# Workflow identifier — drives the ``Ulatus`` scope wiring and the
# evidence form's action URL. (The :class:`_Scope` class currently
# special-cases ``_WORKFLOW_NORMI`` vs ``_WORKFLOW_EL``; the
# Sanktsioonide indeks workflow reuses the Normi defaults — EU + court
# practice on / org-wide drafts off — since the scope wiring isn't
# what's distinctive about this workflow yet.)
_WORKFLOW_SANCTIONS = "sanktsioonid"
_WORKFLOW_ACTION[_WORKFLOW_SANCTIONS] = "/analyysikeskus/sanktsioonid"


def _sanctions_link(sisend: str, *, scope: _Scope | None = None) -> str:
    """Build a ``/analyysikeskus/sanktsioonid?sisend=…`` link (scope-carrying).

    When *scope* is provided we reuse its ``query_pairs`` so the user's
    scope selection rides through disambiguation / candidate clicks.
    The sanctions-specific ``vordle_sarnaste_aktidega`` flag is **not**
    carried through ``_Scope`` (which only knows the Normi/EL scope
    vocabulary); callers that want to preserve it build their links
    inline (see :func:`_sanctions_actions`).
    """
    from urllib.parse import urlencode

    if scope is not None:
        pairs = list(scope.query_pairs(sisend))
        return f"/analyysikeskus/sanktsioonid?{urlencode(pairs)}"
    return f"/analyysikeskus/sanktsioonid?{urlencode([('sisend', sisend)])}"


def _sanctions_scope_block(sisend: str, scope: _Scope, *, include_comparison: bool) -> Any:
    """The enabled ``Ulatus`` scope form for the Sanktsioonide indeks workflow.

    Legal-language only — the toggles read as "what to include in the
    sanctions index", never as query configuration. The
    ``vordle_sarnaste_aktidega`` checkbox is the only workflow-specific
    control (everything else mirrors Normi). When checked, the route
    also runs :func:`find_similar_sanctions` and renders the comparison
    section.
    """
    # ``scope`` accepted for call-site symmetry with the sibling scope
    # blocks even though Sanktsioonide indeks has just the one toggle
    # — keeps the result-shell wiring identical across workflows.
    _ = scope
    return Form(  # noqa: F405
        P(  # noqa: F405
            "Vaikimisi näitan ainult valitud sätte/akti sanktsioone. Märkige, "
            "et võrrelda ka sarnaste aktide sanktsioonidega.",
            cls="muted-text",
        ),
        Hidden(name="sisend", value=sisend),  # noqa: F405
        Hidden(name="ulatus_submitted", value="1"),  # noqa: F405
        Checkbox(
            "vordle_sarnaste_aktidega",
            checked=include_comparison,
            label="Võrdle sarnaste aktide sanktsioonidega",
        ),
        Small(  # noqa: F405
            "Võrdluseks otsin sama liiki sanktsioone teistest aktidest, "
            "mille karistusvahemik kattub.",
            cls="muted-text",
        ),
        Button("Uuenda ulatust", type="submit", variant="secondary", size="sm"),
        method="get",
        action="/analyysikeskus/sanktsioonid",
        cls="analyysikeskus-scope-form",
    )


def _sanction_penalty_phrase(row: SanctionRow) -> str:
    """Render a one-line Estonian summary of a SanctionRow's penalty range.

    Examples:
        ``"2–5 aastat"``      (imprisonment, same unit on both bounds)
        ``"kuni 32 000 EUR"`` (max-only fine)
        ``"alates 10 päevamäära"`` (min-only daily-rate fine)
        ``"—"``               (no numeric bounds at all)
    """
    min_a = row.min_amount
    max_a = row.max_amount
    min_unit = sanction_unit_label(row.min_unit, row.min_currency)
    max_unit = sanction_unit_label(row.max_unit, row.max_currency)
    # Prefer a single unit string when both ends agree; otherwise show both.
    if min_a is not None and max_a is not None:
        unit_str = max_unit or min_unit
        return f"{_fmt_amount(min_a)}–{_fmt_amount(max_a)} {unit_str}".strip()
    if max_a is not None:
        return f"kuni {_fmt_amount(max_a)} {max_unit}".strip()
    if min_a is not None:
        return f"alates {_fmt_amount(min_a)} {min_unit}".strip()
    return "—"


def _fmt_amount(value: float) -> str:
    """Format a sanction amount — whole numbers without trailing ``.0``."""
    # Whole-number amounts are by far the common case (years, days,
    # currency totals); only show the decimal when non-zero.
    if value == int(value):
        return str(int(value))
    # Estonian thousands separator is a non-breaking space, but our
    # corpus values rarely exceed three digits in amount-bearing rows;
    # keep formatting simple to avoid locale surprises.
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _sanction_summary_line(rows: list[SanctionRow]) -> Any:
    """The one-line ``Tulemused`` lead — "X sanktsiooni; Y rahatrahv, Z vangistus".

    Counts every Sanction row by Estonian display label. Counts of 0
    are omitted so the line stays short on a small result set.
    """
    from collections import Counter

    total = len(rows)
    if total == 0:
        return P("Sanktsioone ei leitud.", cls="muted-text")  # noqa: F405

    counter: Counter[str] = Counter()
    for r in rows:
        counter[sanction_type_label(r.sanction_type)] += 1
    by_type_parts = [f"{count} {label.lower()}" for label, count in counter.most_common()]
    by_type = ", ".join(by_type_parts)
    return P(  # noqa: F405
        Strong(f"{total} sanktsiooni"),  # noqa: F405
        f" — {by_type}." if by_type else ".",
    )


# The DataTable component is typed for ``list[dict[str, Any]]`` rows
# + render callables of shape ``(dict[str, Any]) -> Any``. To keep the
# rest of this module typed on :class:`SanctionRow`, each cell render
# callback below pulls the row object out of a known dict key and
# delegates to a Sanction-typed helper. The helper is the readable
# part — the dict-fetching shim keeps pyright happy without losing
# type-safety inside the helpers.
_SANCTION_ROW_KEY = "_sanction_row"


def _sr(row: dict[str, Any]) -> SanctionRow:
    """Pull the wrapped :class:`SanctionRow` out of a DataTable row dict."""
    return row[_SANCTION_ROW_KEY]


def _sanction_act_cell(row: dict[str, Any]) -> Any:
    """``Akt`` table cell — clickable to Õiguskaart focus on the act when URI present."""
    sr = _sr(row)
    label = sr.act_label or "—"
    if not sr.act_uri:
        return Span(label)  # noqa: F405
    return A(label, href=explorer_focus_url(sr.act_uri), cls="data-table-link")  # noqa: F405


def _sanction_provision_cell(row: dict[str, Any]) -> Any:
    """``Säte`` table cell — clickable to Õiguskaart focus on the provision."""
    sr = _sr(row)
    label = sr.provision_label or "—"
    if not sr.provision_uri:
        return Span(label)  # noqa: F405
    return A(label, href=explorer_focus_url(sr.provision_uri), cls="data-table-link")  # noqa: F405


def _sanction_type_cell(row: dict[str, Any]) -> Any:
    """``Sanktsiooni liik`` table cell — Estonian label of the sanctionType."""
    return Span(sanction_type_label(_sr(row).sanction_type))  # noqa: F405


def _sanction_penalty_cell(row: dict[str, Any]) -> Any:
    """``Karistusvahemik`` table cell — one-line penalty range."""
    return Span(_sanction_penalty_phrase(_sr(row)))  # noqa: F405


def _sanction_enforcement_cell(row: dict[str, Any]) -> Any:
    """``Jõustamise tase`` table cell — Estonian label for the enforcement level."""
    level = (_sr(row).enforced_at_level or "").strip().lower()
    label = {
        "act": "Seadus",
        "minister": "Minister",
        "parliament": "Parlament",
        "government": "Valitsus",
    }.get(level, level or "—")
    return Span(label)  # noqa: F405


def _sanction_default_cell(row: dict[str, Any]) -> Any:
    """``Vaikereegel`` table cell — yes/no badge for ``isStatutoryDefault``."""
    flag = _sr(row).is_statutory_default
    if flag is True:
        return Badge("Jah", variant="success")
    if flag is False:
        return Badge("Ei", variant="default")
    return Span("—")  # noqa: F405


def _sanction_results_block(
    rows: list[SanctionRow],
    *,
    similar_rows: list[SanctionRow] | None = None,
) -> list[Any]:
    """Assemble the ``Tulemused`` content: summary + sanctions table + (optional) comparison.

    Empty *rows* renders a one-line muted "Sanktsioone ei leitud." row.
    *similar_rows* is omitted unless the user toggled
    ``vordle_sarnaste_aktidega`` — keeping the page short by default.
    """
    blocks: list[Any] = [_sanction_summary_line(rows)]
    if not rows:
        return blocks

    columns = [
        Column(
            key="provision",
            label="Säte",
            sortable=False,
            render=_sanction_provision_cell,
        ),
        Column(key="act", label="Akt", sortable=False, render=_sanction_act_cell),
        Column(
            key="type",
            label="Sanktsiooni liik",
            sortable=False,
            render=_sanction_type_cell,
        ),
        Column(
            key="penalty",
            label="Karistusvahemik",
            sortable=False,
            render=_sanction_penalty_cell,
        ),
        Column(
            key="enforcement",
            label="Jõustamise tase",
            sortable=False,
            render=_sanction_enforcement_cell,
        ),
        Column(
            key="default",
            label="Vaikereegel",
            sortable=False,
            render=_sanction_default_cell,
        ),
    ]
    capped = [{_SANCTION_ROW_KEY: r} for r in rows[:_MAX_RESULT_ROWS]]
    blocks.append(
        DataTable(
            columns=columns,
            rows=capped,
            empty_message="Sanktsioone ei leitud.",
        )
    )

    if similar_rows is not None:
        sim_capped_rows = similar_rows[:_MAX_RESULT_ROWS]
        if not sim_capped_rows:
            blocks.append(
                _sub_section(
                    "Sarnaste aktide sanktsioonid",
                    _missing_row("Sarnaseid sanktsioone teistest aktidest ei leitud."),
                )
            )
        else:
            sim_capped = [{_SANCTION_ROW_KEY: r} for r in sim_capped_rows]
            blocks.append(
                _sub_section(
                    "Sarnaste aktide sanktsioonid",
                    DataTable(
                        columns=columns,
                        rows=sim_capped,
                        empty_message="Sarnaseid sanktsioone ei leitud.",
                    ),
                )
            )

    return blocks


def _sanctions_evidence_block(rows: list[SanctionRow]) -> list[Any]:
    """Assemble the ``Tõendid`` rows from the sanctions list.

    Each row has the Õiguskaart deep link and the "Küsi nõustajalt"
    seed button (#724) — pattern matches the Normi mõjuahel evidence
    rows. The chat seed text references the provision label so the
    advisor knows which finding the user is asking about. The link
    target is the Sanction URI when present, falling back to the
    provision URI (Sanction nodes can be reified blank-node-ish in
    some serialisations). Empty input ⇒ ``[]`` so the caller can
    swap in a muted "—" via the result shell's `_missing_row` helper.
    """
    out: list[Any] = []
    for r in rows or []:
        # Prefer the Sanction URI as the link target (so the explorer
        # can centre on the reified sanction node); fall back to the
        # provision URI when the Sanction node has no resolvable URI.
        link_uri = r.sanction_uri or r.provision_uri
        provision_label = r.provision_label or "Säte"
        sanction_label = sanction_type_label(r.sanction_type)
        penalty = _sanction_penalty_phrase(r)
        # Build the per-row "why this matters" line. Currency-bearing
        # rows show the amount + unit pair; non-numeric rows degrade to
        # the bare type label.
        target = f"{sanction_label} ({penalty})" if penalty and penalty != "—" else sanction_label
        out.append(
            _evidence_row(
                source_label=provision_label,
                relation="näeb ette sanktsiooni",
                target_label=target,
                uri=link_uri,
                why=(
                    "See säte sätestab struktureeritud karistuse — "
                    "muudatus peaks arvestama olemasoleva karistusulatusega."
                ),
                draft_id=None,
            )
        )
    return out


def _sanctions_actions(
    *,
    focus_uri: str | None,
    sisend: str,
    include_comparison: bool,
) -> list[dict[str, str]]:
    """The ``Soovitatud tegevused`` action set for a Sanktsioonide indeks result page.

    Static — no LLM advice yet (per the design doc). Always offers:

    * ``Vaata sätte kohtupraktikat`` — for now seeds the chat with a
      court-practice question (the dedicated workflow lives in C3 and
      is not yet available).
    * ``Võrdle sarnaste aktide sanktsioonidega`` — toggles the
      comparison scope by re-running the workflow with the
      ``vordle_sarnaste_aktidega`` flag.
    * ``Ava õiguskaardil`` — when we have a focus URI.
    * ``Küsi nõustajalt`` — always.
    * ``Tagasi analüüsikeskusesse``.
    """
    from urllib.parse import urlencode

    actions: list[dict[str, str]] = []
    if focus_uri:
        actions.append({"label": "Ava õiguskaardil", "href": explorer_focus_url(focus_uri)})
    # Toggle the comparison scope.
    new_pairs = [("sisend", sisend), ("ulatus_submitted", "1")]
    if not include_comparison:
        new_pairs.append(("vordle_sarnaste_aktidega", "1"))
    actions.append(
        {
            "label": (
                "Peida võrdlus sarnaste aktide sanktsioonidega"
                if include_comparison
                else "Võrdle sarnaste aktide sanktsioonidega"
            ),
            "href": f"/analyysikeskus/sanktsioonid?{urlencode(new_pairs)}",
        }
    )
    # Court-practice "verb" — landing on /chat for now since the C3
    # dedicated workflow doesn't exist yet. When it lands, swap to
    # `/analyysikeskus/kohtupraktika?sisend=<sisend>`.
    actions.append({"label": "Vaata sätte kohtupraktikat", "href": "/chat/new"})
    actions.append({"label": "Küsi nõustajalt", "href": "/chat/new"})
    actions.append({"label": "Tagasi analüüsikeskusesse", "href": "/analyysikeskus"})
    return actions


def _render_sanctions_landing(
    *,
    auth: Any,
    theme: str,
) -> Any:
    """Render the workflow shell with no input yet (the empty-form landing).

    The page still uses the 5-card result shell so the user lands on a
    visually consistent page even before they search. The Sisend card
    explains the workflow + holds the search input.
    """
    landing_input = Div(  # noqa: F405
        P(  # noqa: F405
            "Sisestage säte, akt, CELEX-number või vaba viide — kuvatakse "
            "kõik sätte juures kehtivad sanktsioonid struktureeritud kujul."
        ),
        Form(  # noqa: F405
            Input(
                "sisend",
                type="text",
                placeholder=("Nt: KarS § 211 · KMS · CELEX-number · või kirjeldage säte"),
                aria_label="Õiguslik viide või kirjeldus",
                cls="analyysikeskus-input",
            ),
            Button("Otsi sanktsioone", type="submit", variant="primary"),
            method="get",
            action="/analyysikeskus/sanktsioonid",
            cls="analyysikeskus-workflow-form",
        ),
        Small(  # noqa: F405
            "Näited: «KarS § 211» · «KMS § 30» · «32016R0679»",
            cls="muted-text",
        ),
    )
    return analysis_result_shell(
        workflow_title="Sanktsioonide indeks",
        input_summary=landing_input,
        results_block=P(  # noqa: F405
            "Sisestage päring, et näha sanktsioone.",
            cls="muted-text",
        ),
        evidence_block=_missing_row("—"),
        actions=[
            {"label": "Küsi nõustajalt", "href": "/chat/new"},
            {"label": "Tagasi analüüsikeskusesse", "href": "/analyysikeskus"},
        ],
        user=auth,
        theme=theme,
        scope_block=Span("Sisestage päring, et muuta ulatust.", cls="muted-text"),  # noqa: F405
    )


def _is_provision_resolved(resolved: Any) -> bool:
    """Return True when *resolved* came from a ``provision`` ExtractedRef.

    The :class:`ReferenceResolver` keeps the original
    :class:`ExtractedRef` on the resolved row; the ref_type field tells
    us whether the user input named a specific paragraph (``provision``)
    or only a law / act (``law`` / ``eu_act``). Branches the route
    between :func:`list_sanctions_for_provision` and
    :func:`list_sanctions_for_act`.
    """
    extracted = getattr(resolved, "extracted", None)
    if extracted is None:
        return False
    return getattr(extracted, "ref_type", "") == "provision"


def _render_sanctions_result(
    *,
    auth: Any,
    theme: str,
    resolved: Any,
    sisend: str,
    include_comparison: bool,
    scope: _Scope,
) -> Any:
    """Render the result page for a single resolved entity.

    Branches on the resolved ref shape:
    * URI + provision ref_type → :func:`list_sanctions_for_provision`
    * URI + any other ref_type → :func:`list_sanctions_for_act` (literal title)
    * No URI but ``partial_match["act_title"]`` set (bare law input
      like ``KarS`` / ``Karistusseadustik``) → :func:`list_sanctions_for_act`
      against that title. The Wave 2 Step 2 resolver returns this
      shape for any law-only reference because the corpus has no
      act-level URIs (Step 1 spike).
    """
    entity_uri_raw = getattr(resolved, "entity_uri", None)
    entity_uri = str(entity_uri_raw) if entity_uri_raw else ""
    label = _resolved_label(resolved, sisend)
    type_label = _resolved_type_label(resolved)
    partial_title = _partial_act_title(resolved)

    if entity_uri and _is_provision_resolved(resolved):
        rows = list_sanctions_for_provision(entity_uri)
    elif partial_title is not None and not entity_uri:
        # Bare law input — Wave 2 Step 2 resolver returns
        # entity_uri=None, partial_match.act_title=<literal title>.
        # Route directly to the act-level helper with that title.
        rows = list_sanctions_for_act(partial_title)
    else:
        # The act join is on the ``estleg:sourceAct`` literal title in
        # prod (no act URIs exist on provisions — see the Wave 2 spike
        # in ``docs/2026-05-18-bugfix-plan.md``). The best title we have
        # for a resolved law ref is the human label the resolver
        # surfaced; pass that to the SPARQL helper.
        rows = list_sanctions_for_act(label)

    similar_rows: list[Any] | None = None
    if include_comparison and rows:
        # Seed on the first row — keeps the comparison deterministic.
        # Future iterations may surface a "pick which sanction to
        # compare" affordance for multi-sanction provisions.
        similar_rows = find_similar_sanctions(rows[0], limit=_MAX_RESULT_ROWS)

    input_summary = P(  # noqa: F405
        "Analüüsisin: ",
        Strong(label),  # noqa: F405
        (f" — {type_label}" if type_label else ""),
    )
    results_block = _sanction_results_block(rows, similar_rows=similar_rows)
    evidence_rows = _sanctions_evidence_block(rows)
    evidence_block: Any = evidence_rows if evidence_rows else _missing_row("Tõendeid ei leitud.")
    actions = _sanctions_actions(
        focus_uri=entity_uri,
        sisend=sisend,
        include_comparison=include_comparison,
    )

    return analysis_result_shell(
        workflow_title="Sanktsioonide indeks",
        input_summary=input_summary,
        results_block=results_block,
        evidence_block=evidence_block,
        actions=actions,
        user=auth,
        theme=theme,
        scope_block=_sanctions_scope_block(sisend, scope, include_comparison=include_comparison),
    )


def _render_sanctions_disambiguation(
    *,
    auth: Any,
    theme: str,
    resolved: list[Any],
    sisend: str,
    include_comparison: bool,
    scope: _Scope,
) -> Any:
    """Render a disambiguation page listing plausible resolutions as links."""
    candidates: list[dict[str, str]] = []
    for r in resolved:
        label = _resolved_label(r, sisend)
        extracted = getattr(r, "extracted", None)
        ref_text = str(getattr(extracted, "ref_text", "") or label)
        candidates.append({"label": label, "ref": ref_text})

    items: list[Any] = []
    for c in candidates:
        ref = (c.get("ref") or c.get("label") or "").strip()
        if not ref:
            continue
        items.append(
            Li(A(c.get("label") or ref, href=_sanctions_link(ref, scope=scope)))  # noqa: F405
        )

    results_block: list[Any] = [
        Alert("Sisend võib viidata mitmele üksusele. Vali, millist analüüsida:", variant="info"),
    ]
    if items:
        results_block.append(Ul(*items, cls="analyysikeskus-candidates"))  # noqa: F405

    return analysis_result_shell(
        workflow_title="Sanktsioonide indeks",
        input_summary=P(f"Sisestasite: «{sisend}»"),  # noqa: F405
        results_block=results_block,
        evidence_block=_missing_row("—"),
        actions=[
            {"label": "Küsi nõustajalt", "href": "/chat/new"},
            {"label": "Tagasi analüüsikeskusesse", "href": "/analyysikeskus"},
        ],
        user=auth,
        theme=theme,
        scope_block=_sanctions_scope_block(sisend, scope, include_comparison=include_comparison),
    )


def _render_sanctions_unresolved(
    *,
    auth: Any,
    theme: str,
    sisend: str,
    include_comparison: bool,
    org_id: str | None,
    scope: _Scope,
) -> Any:
    """Render the "no structured reference recognised" page (+ optional RAG candidates)."""
    warning = Alert(
        "Ei tuvastanud õiguslikku viidet. Proovige nt «KarS § 211», CELEX-numbrit "
        "(32016R0679) või akti lühinime (KMS, AvTS).",
        variant="warning",
    )
    candidates = _rag_candidates(sisend, org_id)
    items: list[Any] = []
    for c in candidates:
        ref = (c.get("ref") or c.get("label") or "").strip()
        if not ref:
            continue
        items.append(Li(A(c.get("label") or ref, href=_sanctions_link(ref))))  # noqa: F405

    results_children: list[Any] = [warning]
    if items:
        results_children.append(
            P("Võimalikud sätted, mida võisite mõelda:", cls="muted-text")  # noqa: F405
        )
        results_children.append(Ul(*items, cls="analyysikeskus-candidates"))  # noqa: F405

    return analysis_result_shell(
        workflow_title="Sanktsioonide indeks",
        input_summary=P(f"Sisestasite: «{sisend}»"),  # noqa: F405
        results_block=results_children,
        evidence_block=_missing_row("—"),
        actions=[
            {"label": "Küsi nõustajalt", "href": "/chat/new"},
            {"label": "Tagasi analüüsikeskusesse", "href": "/analyysikeskus"},
        ],
        user=auth,
        theme=theme,
        scope_block=_sanctions_scope_block(sisend, scope, include_comparison=include_comparison),
    )


def sanktsioonid_page(req: Request):
    """GET /analyysikeskus/sanktsioonid?sisend=<text> — Sanktsioonide indeks (A1).

    Flow:

    1. Blank ``sisend`` → render the workflow shell with the input form
       (no sanctions yet — the 5-card result shell with a search-form
       Sisend card).
    2. Else parse ``sisend`` → resolve via :class:`ReferenceResolver`:
       * exactly one resolved entity → run
         :func:`list_sanctions_for_provision` (when the input named a
         specific §) or :func:`list_sanctions_for_act` (a law / act).
         Render the result through the 5-card shell.
       * multiple plausible resolutions → render disambiguation links.
       * nothing resolved → friendly "no structured ref" warning + RAG
         candidates (same pattern as Normi mõjuahel).
    3. When ``vordle_sarnaste_aktidega=1`` is set, also run
       :func:`find_similar_sanctions` against the first sanction row
       and render the comparison sub-section.
    """
    auth = req.scope.get("auth") or None
    theme = get_theme_from_request(req)
    org_id = auth.get("org_id") if auth else None

    sisend = (req.query_params.get("sisend") or "").strip()
    # ``Ulatus`` toggle — re-runs the workflow with comparison data.
    include_comparison = req.query_params.get("vordle_sarnaste_aktidega") is not None

    scope = _Scope(req.query_params, workflow=_WORKFLOW_SANCTIONS)

    if not sisend:
        return _render_sanctions_landing(auth=auth, theme=theme)

    parsed_refs = parse_user_reference(sisend)
    resolved = _resolve_refs(parsed_refs)
    # Wave 2 Step 5 (#801 follow-up): include partial-match refs so a
    # bare law input (``KarS``, ``Karistusseadustik``) reaches the
    # ``list_sanctions_for_act(act_title)`` branch in the renderer.
    # The selector prefers URI-resolved refs when both are present so
    # ``"KarS § 211"`` still picks the provision view, not a two-row
    # disambiguation card.
    unique_resolved = _select_dispatchable(resolved)

    if len(unique_resolved) == 1:
        return _render_sanctions_result(
            auth=auth,
            theme=theme,
            resolved=unique_resolved[0],
            sisend=sisend,
            include_comparison=include_comparison,
            scope=scope,
        )

    if len(unique_resolved) > 1:
        return _render_sanctions_disambiguation(
            auth=auth,
            theme=theme,
            resolved=unique_resolved,
            sisend=sisend,
            include_comparison=include_comparison,
            scope=scope,
        )

    return _render_sanctions_unresolved(
        auth=auth,
        theme=theme,
        sisend=sisend,
        include_comparison=include_comparison,
        org_id=org_id,
        scope=scope,
    )


# ---------------------------------------------------------------------------
# Kohtupraktika sätte kohta (C3 — plan section 5)
# ---------------------------------------------------------------------------
#
# Workflow flow:
#
#   1. Blank ``sisend`` → render the workflow shell with the input form
#      (the 5-card shell, no decisions yet).
#   2. ``parse_user_reference(sisend)`` → if a §-reference / CELEX /
#      case number resolves to exactly one entity URI, branch on the
#      ref type:
#        * provision → :func:`list_decisions_for_provision`
#        * anything else with a URI (law/eu_act) → :func:`list_decisions_for_act`
#      Group results via :func:`group_by_court` and render per-bucket
#      sections (Riigikohus / Euroopa Kohus / ringkonnakohus / muu) with
#      citation counts and year-bucket trends.
#      Several plausible resolutions → render a disambiguation card.
#      Nothing recognised → friendly "no structured ref" warning + RAG
#      candidates (same pattern as Normi mõjuahel / Sanktsioonid).
#
# All copy is legal/policy language — no SPARQL / RDF / named-graph
# vocabulary on the page. A dead Jena anywhere ⇒ the route degrades to
# the "no decisions" branch rather than 500.

_WORKFLOW_KOHTUPRAKTIKA = "kohtupraktika"
_WORKFLOW_ACTION[_WORKFLOW_KOHTUPRAKTIKA] = "/analyysikeskus/kohtupraktika"


def _kohtupraktika_link(sisend: str, *, scope: _Scope | None = None) -> str:
    """Build a ``/analyysikeskus/kohtupraktika?sisend=…`` link (scope-carrying).

    When *scope* is provided we reuse its ``query_pairs`` so the user's
    scope selection rides through disambiguation / candidate clicks.
    """
    from urllib.parse import urlencode

    if scope is not None:
        pairs = list(scope.query_pairs(sisend))
        return f"/analyysikeskus/kohtupraktika?{urlencode(pairs)}"
    return f"/analyysikeskus/kohtupraktika?{urlencode([('sisend', sisend)])}"


def _kohtupraktika_scope_block(sisend: str, scope: _Scope) -> Any:
    """The enabled ``Ulatus`` scope form for the Kohtupraktika workflow.

    Legal-language only. The workflow scope is essentially "which courts
    do you want to see" — but in v1 we always show every bucket and rely
    on Python-side grouping (no SPARQL filter on court type yet, since
    the corpus' court vocabulary is uneven). The scope form is kept for
    UX parity with the other workflows; the toggles ride through so URLs
    stay shareable.
    """
    _ = scope  # call-site symmetry
    return Form(  # noqa: F405
        P(  # noqa: F405
            "Vaatan kõiki kohtuid, mis on seda sätet või akti tõlgendanud. "
            "Tulemused on rühmitatud kohtu järgi.",
            cls="muted-text",
        ),
        Hidden(name="sisend", value=sisend),  # noqa: F405
        Hidden(name="ulatus_submitted", value="1"),  # noqa: F405
        Small(  # noqa: F405
            "Praeguses versioonis kuvan Riigikohtu, Euroopa Kohtu ja "
            "ringkonnakohtute lahendid eraldi sektsioonides; täpsemad "
            "filtrid on tulekul.",
            cls="muted-text",
        ),
        Button(
            "Uuenda ulatust",
            type="submit",
            variant="secondary",
            size="sm",
            disabled=True,
            title="Tulekul",
        ),
        method="get",
        action="/analyysikeskus/kohtupraktika",
        cls="analyysikeskus-scope-form",
    )


def _is_court_practice_provision(resolved: Any) -> bool:
    """Return True when *resolved* came from a ``provision`` ExtractedRef."""
    extracted = getattr(resolved, "extracted", None)
    if extracted is None:
        return False
    return getattr(extracted, "ref_type", "") == "provision"


def _format_decision_label(row: CourtDecisionRow) -> str:
    """Best human label for a decision row.

    Falls back through ``decision_label`` → ``"Kohtuasi nr <N>"`` → the
    decision URI tail so the cell is never empty.
    """
    if row.decision_label:
        return row.decision_label
    if row.case_number:
        return f"Kohtuasi nr {row.case_number}"
    tail = row.decision_uri.rsplit("#", 1)[-1].rsplit("/", 1)[-1]
    return tail or "Kohtulahend"


def _format_year_trend(year_trend: dict[int, int]) -> str:
    """Render a year-bucket trend as a compact ``"2018: 2 · 2020: 1"`` string.

    Empty ⇒ ``"—"``. Sorted ascending by year (the grouper produces it
    that way; we re-sort here defensively).
    """
    if not year_trend:
        return "—"
    parts = [f"{year}: {count}" for year, count in sorted(year_trend.items())]
    return " · ".join(parts)


def _court_practice_summary_line(groups: list[CourtPracticeGroup]) -> Any:
    """The one-line ``Tulemused`` lead — "N lahendit; X Riigikohtus, Y EL Kohtus".

    Empty groups ⇒ a friendly muted "Kohtupraktikat ei leitud" row.
    """
    total = sum(g.citation_count for g in groups)
    if total == 0:
        return P("Kohtupraktikat ei leitud.", cls="muted-text")  # noqa: F405
    by_bucket_parts = [
        f"{g.citation_count} {g.label_et.lower()}" for g in groups if g.citation_count
    ]
    by_bucket = ", ".join(by_bucket_parts)
    word = "lahend" if total == 1 else "lahendit"
    return P(  # noqa: F405
        Strong(f"{total} {word}"),  # noqa: F405
        f" — {by_bucket}." if by_bucket else ".",
    )


def _court_practice_group_section(group: CourtPracticeGroup) -> Any:
    """Render one court-bucket section: heading, count, year trend, decision list."""
    items: list[Any] = []
    for row in group.rows[:_MAX_RESULT_ROWS]:
        label = _format_decision_label(row)
        meta_parts: list[str] = []
        if row.case_number:
            meta_parts.append(f"Kohtuasi nr {row.case_number}")
        if row.decision_date:
            meta_parts.append(row.decision_date)
        if row.provision_label:
            meta_parts.append(f"Tõlgendab: {row.provision_label}")
        meta = " · ".join(meta_parts)
        if row.decision_uri:
            link: Any = A(  # noqa: F405
                label,
                href=explorer_focus_url(row.decision_uri),
                cls="data-table-link",
            )
        else:
            link = Span(label)  # noqa: F405
        bits: list[Any] = [link]
        if meta:
            bits.append(Span(f" — {meta}", cls="muted-text"))  # noqa: F405
        items.append(Li(*bits))  # noqa: F405

    body: list[Any] = [
        P(  # noqa: F405
            Strong(f"{group.citation_count} lahendit"),  # noqa: F405
            f" · Aastate kaupa: {_format_year_trend(group.year_trend)}",
            cls="muted-text",
        ),
    ]
    if items:
        body.append(Ul(*items, cls="analyysikeskus-court-decisions"))  # noqa: F405
    else:
        body.append(_missing_row("Lahendeid ei leitud."))
    return _sub_section(group.label_et, *body)


def _court_practice_results_block(groups: list[CourtPracticeGroup]) -> list[Any]:
    """Assemble the ``Tulemused`` content: summary line + per-court sections."""
    blocks: list[Any] = [_court_practice_summary_line(groups)]
    if not groups:
        return blocks
    for g in groups:
        blocks.append(_court_practice_group_section(g))
    return blocks


def _court_practice_evidence_block(
    rows: list[CourtDecisionRow],
    *,
    analysed_label: str,
) -> list[Any]:
    """Assemble the ``Tõendid`` rows from the decision list.

    Each row uses the canonical "tõlgendab" legal-language phrase from
    :mod:`app.ontology.relations` so a future predicate rename only
    happens in one place. The link target is the decision URI when
    present, falling back to the provision URI.
    """
    out: list[Any] = []
    seen: set[str] = set()
    for r in rows:
        key = r.decision_uri or f"_blank_{id(r)}"
        if key in seen:
            continue
        seen.add(key)
        decision_label = _format_decision_label(r)
        target_label = r.provision_label or analysed_label
        when = r.decision_date or ""
        link_uri = r.decision_uri or r.provision_uri
        out.append(
            _evidence_row(
                source_label=decision_label,
                relation="tõlgendab",
                target_label=target_label,
                uri=link_uri,
                why=(
                    "See lahend tõlgendab analüüsitavat sätet ja võib "
                    "kujundada selle praktilist tähendust."
                ),
                when=when,
                draft_id=None,
            )
        )
    return out


def _court_practice_actions(
    *,
    focus_uri: str | None,
    sisend: str,
) -> list[dict[str, str]]:
    """The ``Soovitatud tegevused`` action set for the Kohtupraktika workflow.

    Static — no LLM advice yet. Always offers:

    * ``Ava õiguskaardil`` — when there is a focus URI.
    * ``Vaata kohtupraktikat Õiguskaardil`` — opens the explorer with
      the court-practice view preset on the focused entity.
    * ``Küsi nõustajalt`` — link to the chat.
    * ``Tagasi analüüsikeskusesse``.
    """
    actions: list[dict[str, str]] = []
    if focus_uri:
        actions.append({"label": "Ava õiguskaardil", "href": explorer_focus_url(focus_uri)})
        # Court-practice preset on the explorer (?vaade=kohtupraktika).
        actions.append(
            {
                "label": "Vaata kohtupraktikat Õiguskaardil",
                "href": f"{explorer_focus_url(focus_uri)}&vaade=kohtupraktika",
            }
        )
    actions.append({"label": "Küsi nõustajalt", "href": "/chat/new"})
    # Sanktsioonide indeks shares the input vocabulary so a sanctions
    # check is a natural next step.
    actions.append(
        {
            "label": "Vaata sätte sanktsioone",
            "href": _sanctions_link(sisend),
        }
    )
    actions.append({"label": "Tagasi analüüsikeskusesse", "href": "/analyysikeskus"})
    return actions


def _render_court_practice_landing(
    *,
    auth: Any,
    theme: str,
) -> Any:
    """Render the workflow shell with no input yet (the empty-form landing)."""
    landing_input = Div(  # noqa: F405
        P(  # noqa: F405
            "Sisestage säte, akt, CELEX-number või kohtuasja number — "
            "kuvatakse kõik lahendid, mis seda tõlgendavad, rühmitatud kohtu järgi."
        ),
        Form(  # noqa: F405
            Input(
                "sisend",
                type="text",
                placeholder="Nt: AvTS § 35 · KarS § 211 · CELEX-number · 3-1-1-63-15",
                aria_label="Säte, akt, CELEX-number või kohtuasja number",
                cls="analyysikeskus-input",
            ),
            Button("Otsi kohtupraktikat", type="submit", variant="primary"),
            method="get",
            action="/analyysikeskus/kohtupraktika",
            cls="analyysikeskus-workflow-form",
        ),
        Small(  # noqa: F405
            "Näited: «AvTS § 35» · «KarS § 211» · «32016R0679» · «3-1-1-63-15»",
            cls="muted-text",
        ),
    )
    return analysis_result_shell(
        workflow_title="Kohtupraktika sätte kohta",
        input_summary=landing_input,
        results_block=P(  # noqa: F405
            "Sisestage päring, et näha kohtupraktikat.",
            cls="muted-text",
        ),
        evidence_block=_missing_row("—"),
        actions=[
            {"label": "Küsi nõustajalt", "href": "/chat/new"},
            {"label": "Tagasi analüüsikeskusesse", "href": "/analyysikeskus"},
        ],
        user=auth,
        theme=theme,
        scope_block=Span(  # noqa: F405
            "Sisestage päring, et muuta ulatust.",
            cls="muted-text",
        ),
    )


def _render_court_practice_result(
    *,
    auth: Any,
    theme: str,
    resolved: Any,
    sisend: str,
    scope: _Scope,
) -> Any:
    """Render the result page for a single resolved entity.

    Branches on the resolved ref shape:
    * URI + provision → :func:`list_decisions_for_provision`
    * URI + non-provision → :func:`list_decisions_for_act` (URI; the
      helper reverse-looks-up the label internally).
    * No URI but ``partial_match["act_title"]`` set (bare law input
      like ``KarS``) → :func:`list_decisions_for_act` against that
      literal title. Wave 2 Step 5 (#801 follow-up).
    """
    entity_uri_raw = getattr(resolved, "entity_uri", None)
    entity_uri = str(entity_uri_raw) if entity_uri_raw else ""
    label = _resolved_label(resolved, sisend)
    type_label = _resolved_type_label(resolved)
    partial_title = _partial_act_title(resolved)

    if entity_uri and _is_court_practice_provision(resolved):
        rows = list_decisions_for_provision(entity_uri)
    elif partial_title is not None and not entity_uri:
        # Bare law input — the act-level helper accepts a literal title.
        rows = list_decisions_for_act(partial_title)
    else:
        rows = list_decisions_for_act(entity_uri)

    groups = group_by_court(rows)

    input_summary = P(  # noqa: F405
        "Analüüsisin: ",
        Strong(label),  # noqa: F405
        (f" — {type_label}" if type_label else ""),
    )
    results_block = _court_practice_results_block(groups)
    evidence_rows = _court_practice_evidence_block(rows, analysed_label=label)
    evidence_block: Any = evidence_rows if evidence_rows else _missing_row("Tõendeid ei leitud.")
    actions = _court_practice_actions(focus_uri=entity_uri, sisend=sisend)

    return analysis_result_shell(
        workflow_title="Kohtupraktika sätte kohta",
        input_summary=input_summary,
        results_block=results_block,
        evidence_block=evidence_block,
        actions=actions,
        user=auth,
        theme=theme,
        scope_block=_kohtupraktika_scope_block(sisend, scope),
    )


def _render_court_practice_disambiguation(
    *,
    auth: Any,
    theme: str,
    resolved: list[Any],
    sisend: str,
    scope: _Scope,
) -> Any:
    """Render a disambiguation page listing plausible resolutions as links."""
    candidates: list[dict[str, str]] = []
    for r in resolved:
        label = _resolved_label(r, sisend)
        extracted = getattr(r, "extracted", None)
        ref_text = str(getattr(extracted, "ref_text", "") or label)
        candidates.append({"label": label, "ref": ref_text})

    items: list[Any] = []
    for c in candidates:
        ref = (c.get("ref") or c.get("label") or "").strip()
        if not ref:
            continue
        items.append(
            Li(  # noqa: F405
                A(  # noqa: F405
                    c.get("label") or ref,
                    href=_kohtupraktika_link(ref, scope=scope),
                )
            )
        )

    results_block: list[Any] = [
        Alert(
            "Sisend võib viidata mitmele üksusele. Vali, millist analüüsida:",
            variant="info",
        ),
    ]
    if items:
        results_block.append(Ul(*items, cls="analyysikeskus-candidates"))  # noqa: F405

    return analysis_result_shell(
        workflow_title="Kohtupraktika sätte kohta",
        input_summary=P(f"Sisestasite: «{sisend}»"),  # noqa: F405
        results_block=results_block,
        evidence_block=_missing_row("—"),
        actions=[
            {"label": "Küsi nõustajalt", "href": "/chat/new"},
            {"label": "Tagasi analüüsikeskusesse", "href": "/analyysikeskus"},
        ],
        user=auth,
        theme=theme,
        scope_block=_kohtupraktika_scope_block(sisend, scope),
    )


def _render_court_practice_unresolved(
    *,
    auth: Any,
    theme: str,
    sisend: str,
    org_id: str | None,
    scope: _Scope,
) -> Any:
    """Render the "no structured reference recognised" page (+ optional RAG candidates)."""
    warning = Alert(
        "Ei tuvastanud õiguslikku viidet. Proovige nt «AvTS § 35», CELEX-numbrit "
        "(32016R0679) või kohtuasja numbrit (3-1-1-63-15).",
        variant="warning",
    )
    candidates = _rag_candidates(sisend, org_id)
    items: list[Any] = []
    for c in candidates:
        ref = (c.get("ref") or c.get("label") or "").strip()
        if not ref:
            continue
        items.append(
            Li(  # noqa: F405
                A(  # noqa: F405
                    c.get("label") or ref,
                    href=_kohtupraktika_link(ref),
                )
            )
        )

    results_children: list[Any] = [warning]
    if items:
        results_children.append(
            P("Võimalikud sätted, mida võisite mõelda:", cls="muted-text")  # noqa: F405
        )
        results_children.append(Ul(*items, cls="analyysikeskus-candidates"))  # noqa: F405

    return analysis_result_shell(
        workflow_title="Kohtupraktika sätte kohta",
        input_summary=P(f"Sisestasite: «{sisend}»"),  # noqa: F405
        results_block=results_children,
        evidence_block=_missing_row("—"),
        actions=[
            {"label": "Küsi nõustajalt", "href": "/chat/new"},
            {"label": "Tagasi analüüsikeskusesse", "href": "/analyysikeskus"},
        ],
        user=auth,
        theme=theme,
        scope_block=_kohtupraktika_scope_block(sisend, scope),
    )


def kohtupraktika_page(req: Request):
    """GET /analyysikeskus/kohtupraktika?sisend=<text> — Kohtupraktika sätte kohta (C3).

    Flow:

    1. Blank ``sisend`` → render the workflow shell with the input form
       (the 5-card shell with a search-form Sisend card).
    2. Else parse ``sisend`` → resolve via :class:`ReferenceResolver`:
       * exactly one resolved entity → run
         :func:`list_decisions_for_provision` (when the input named a
         specific §) or :func:`list_decisions_for_act` (a law / act /
         EU act). Group via :func:`group_by_court` and render the
         per-bucket sections through the 5-card shell.
       * multiple plausible resolutions → render disambiguation links.
       * nothing resolved → friendly "no structured ref" warning + RAG
         candidates (same pattern as Normi mõjuahel / Sanktsioonid).

    A dead Jena anywhere ⇒ the route degrades to the "no decisions"
    branch rather than 500 (the SPARQL helpers all return ``[]`` on
    failure).
    """
    auth = req.scope.get("auth") or None
    theme = get_theme_from_request(req)
    org_id = auth.get("org_id") if auth else None

    sisend = (req.query_params.get("sisend") or "").strip()
    scope = _Scope(req.query_params, workflow=_WORKFLOW_NORMI)

    if not sisend:
        return _render_court_practice_landing(auth=auth, theme=theme)

    parsed_refs = parse_user_reference(sisend)
    resolved = _resolve_refs(parsed_refs)
    # Wave 2 Step 5 (#801 follow-up): include partial-match refs so a
    # bare law input (``KarS``, ``Karistusseadustik``) reaches the
    # ``list_decisions_for_act(act_title)`` branch in the renderer.
    unique_resolved = _select_dispatchable(resolved)

    if len(unique_resolved) == 1:
        return _render_court_practice_result(
            auth=auth,
            theme=theme,
            resolved=unique_resolved[0],
            sisend=sisend,
            scope=scope,
        )

    if len(unique_resolved) > 1:
        return _render_court_practice_disambiguation(
            auth=auth,
            theme=theme,
            resolved=unique_resolved,
            sisend=sisend,
            scope=scope,
        )

    return _render_court_practice_unresolved(
        auth=auth,
        theme=theme,
        sisend=sisend,
        org_id=org_id,
        scope=scope,
    )


# ---------------------------------------------------------------------------
# Halduskoormus (A2 v1 — plan section 5)
# ---------------------------------------------------------------------------
#
# Workflow flow:
#
#   1. Blank ``sisend`` → render the workflow shell with the input form
#      (the 5-card shell, no burden grid yet).
#   2. ``parse_user_reference(sisend)`` → if a §-reference / CELEX /
#      law shortname resolves to exactly one entity URI, branch on the
#      ref type:
#        * provision → :func:`list_burden_for_provision`
#        * draft URI (``DraftLegislation``) → :func:`burden_delta_for_draft`
#        * anything else with a URI (law / eu_act) → :func:`list_burden_for_act`
#      Render the count grid + per-bucket tables + the v1 "Kohustatud
#      isik (esialgne, vt #214)" fallback column. The v2 target-group
#      grouping is deferred until ontology issue #214 lands.
#
# All copy is legal/policy language — no SPARQL / RDF vocabulary on
# the page. A dead Jena anywhere ⇒ the route degrades to the empty-grid
# branch rather than 500.

_WORKFLOW_BURDEN = "halduskoormus"
_WORKFLOW_ACTION[_WORKFLOW_BURDEN] = "/analyysikeskus/halduskoormus"

# Cap how many rows we render in each per-bucket detail table — purely
# page-weight control (the underlying BurdenSummary keeps the full list).
_MAX_BURDEN_DISPLAY_ROWS = 30


def _burden_link(sisend: str, *, scope: _Scope | None = None) -> str:
    """Build a ``/analyysikeskus/halduskoormus?sisend=…`` link (scope-carrying)."""
    from urllib.parse import urlencode

    if scope is not None:
        return f"/analyysikeskus/halduskoormus?{urlencode(scope.query_pairs(sisend))}"
    return f"/analyysikeskus/halduskoormus?{urlencode([('sisend', sisend)])}"


def _burden_count_grid(summary: BurdenSummary) -> Any:
    """Render the four-bucket count grid (Kohustused / Keelud / Load / Õigused).

    ``unknown`` is only rendered when its count is non-zero — keeps the
    grid clean for fully-classified acts.
    """
    cards: list[Any] = []
    for key in burden_key_order():
        count = summary.counts.get(key, 0)
        if key == "unknown" and count == 0:
            continue
        cards.append(
            Card(
                CardHeader(H4(burden_label(key), cls="card-title")),  # noqa: F405
                CardBody(
                    P(  # noqa: F405
                        Strong(str(count), cls="burden-count"),  # noqa: F405
                        cls="burden-count-line",
                    ),
                    Small(burden_description(key), cls="muted-text"),  # noqa: F405
                ),
                cls="burden-count-card",
            )
        )
    return Div(*cards, cls="burden-count-grid")  # noqa: F405


def _burden_summary_line(summary: BurdenSummary) -> Any:
    """One-line lead — "N sätet; X kohustust, Y keeldu, Z luba, W õigust"."""
    total = summary.total
    if total == 0:
        return P("Sätteid ei leitud.", cls="muted-text")  # noqa: F405
    parts: list[str] = []
    for key in burden_key_order():
        count = summary.counts.get(key, 0)
        if count == 0:
            continue
        parts.append(f"{count} {burden_label(key).lower()}")
    detail = ", ".join(parts) if parts else ""
    extras: list[Any] = []
    if summary.truncated:
        extras.append(
            Small(  # noqa: F405
                f" — näidatud {total} esimest sätet (täielik tulemus on lühendatud).",
                cls="muted-text",
            )
        )
    return P(  # noqa: F405
        Strong(f"{total} sätet"),  # noqa: F405
        (f" — {detail}." if detail else "."),
        *extras,
    )


def _burden_duty_holder_section(summary: BurdenSummary) -> Any:
    """The v1 "Kohustatud isik (esialgne, vt #214)" fallback table.

    Surfaces the top-N most frequent dutyHolder literals + their row
    counts. Explicitly labelled as the pre-enum fallback so the user
    knows ontology issue #214 (multi-valued ``estleg:targetGroup``) is
    the v2 plan. Empty / no-dutyHolder data ⇒ a muted one-liner.
    """
    if not summary.duty_holder_counts:
        return _sub_section(
            "Kohustatud isik (esialgne, vt #214)",
            _missing_row("Kohustatud isikute andmed puuduvad."),
        )

    rows: list[dict[str, Any]] = []
    for actor, count in summary.duty_holder_counts.items():
        rows.append(
            {
                "actor": actor if actor else "Märkimata",
                "count": count,
            }
        )
    columns = [
        Column(key="actor", label="Kohustatud isik", sortable=False),
        Column(key="count", label="Sätete arv", sortable=False),
    ]
    return _sub_section(
        "Kohustatud isik (esialgne, vt #214)",
        P(  # noqa: F405
            "Esialgne grupeering vaba teksti väljal «dutyHolder» — täpsem "
            "rühmade liigitus (kodanik / ettevõtja / avalik asutus / ametnik / "
            "MTÜ) tuleb pärast ontoloogia muudatust #214.",
            cls="muted-text",
        ),
        DataTable(columns=columns, rows=rows),
    )


def _burden_per_bucket_table(summary: BurdenSummary, *, key: str) -> Any:
    """Render the per-bucket detail table (sätted by deontic key).

    Empty bucket ⇒ a muted one-liner. Otherwise the table lists each
    provision with its label + dutyHolder literal (the v1 fallback).
    """
    bucket_rows = [r for r in summary.rows if r.burden_key == key][:_MAX_BURDEN_DISPLAY_ROWS]
    if not bucket_rows:
        return _sub_section(burden_label(key), _missing_row("Sätteid ei leitud."))

    def _provision_cell(row: dict[str, Any]) -> Any:
        br: BurdenRow = row["_burden"]
        label = br.provision_label or br.provision_uri.rsplit("#", 1)[-1] or "Säte"
        if not br.provision_uri:
            return Span(label)  # noqa: F405
        return A(label, href=explorer_focus_url(br.provision_uri), cls="data-table-link")  # noqa: F405

    def _duty_holder_cell(row: dict[str, Any]) -> Any:
        br: BurdenRow = row["_burden"]
        return Span(br.duty_holder or "—")  # noqa: F405

    def _act_cell(row: dict[str, Any]) -> Any:
        br: BurdenRow = row["_burden"]
        label = br.act_label or "—"
        if not br.act_uri:
            return Span(label)  # noqa: F405
        return A(label, href=explorer_focus_url(br.act_uri), cls="data-table-link")  # noqa: F405

    columns = [
        Column(key="provision", label="Säte", sortable=False, render=_provision_cell),
        Column(key="act", label="Akt", sortable=False, render=_act_cell),
        Column(
            key="duty_holder",
            label="Kohustatud isik (esialgne, vt #214)",
            sortable=False,
            render=_duty_holder_cell,
        ),
    ]
    table_rows = [{"_burden": r} for r in bucket_rows]
    return _sub_section(
        burden_label(key),
        DataTable(columns=columns, rows=table_rows, empty_message="Sätteid ei leitud."),
    )


def _burden_results_block(summary: BurdenSummary) -> list[Any]:
    """Compose the ``Tulemused`` content: summary line + count grid + per-bucket tables."""
    blocks: list[Any] = [
        _burden_summary_line(summary),
        _burden_count_grid(summary),
    ]
    # Render per-bucket detail tables only for buckets with a non-zero
    # count — keeps the page short on a tiny act / single provision.
    for key in burden_key_order():
        if summary.counts.get(key, 0) == 0:
            continue
        blocks.append(_burden_per_bucket_table(summary, key=key))
    blocks.append(_burden_duty_holder_section(summary))
    return blocks


def _burden_delta_block(delta: BurdenDelta) -> list[Any]:
    """Compose the ``Tulemused`` content for a draft-backed (delta) page."""
    affected_msg = P(  # noqa: F405
        f"Eelnõu puudutab {delta.affected_count} kehtivat sätet. "
        "Allpool on nende sätete praegune halduskoormus.",
        cls="muted-text",
    )
    note = Alert(
        "Eelnõu enda deontiline klassifikatsioon (uued/muudetud sätted) tuleb "
        "pärast ontoloogia muudatust #214 ja sellega seotud andmevoogu — "
        "praegu kuvatakse ainult muudetavate sätete kehtiv halduskoormus.",
        variant="info",
    )
    return [note, affected_msg, *_burden_results_block(delta.before)]


def _burden_evidence_block(summary: BurdenSummary) -> list[Any]:
    """Assemble the ``Tõendid`` rows from the burden list.

    One row per provision capped at :data:`_MAX_BURDEN_DISPLAY_ROWS`,
    surfacing the deontic bucket as the legal-language relation and the
    dutyHolder literal as the target. Empty input ⇒ ``[]`` so the caller
    swaps in a muted "—" via :func:`_missing_row`.
    """
    out: list[Any] = []
    for r in (summary.rows or [])[:_MAX_BURDEN_DISPLAY_ROWS]:
        provision_label = r.provision_label or r.provision_uri.rsplit("#", 1)[-1] or "Säte"
        bucket_label = burden_label(r.burden_key)
        duty_holder = r.duty_holder or "märkimata"
        why = (
            f"Säte on liigitatud kategooriasse «{bucket_label.lower()}» — "
            "deontiline liigitus mõjutab VTK halduskoormuse hinnangut."
        )
        out.append(
            _evidence_row(
                source_label=provision_label,
                relation="on liigitatud kui",
                target_label=f"{bucket_label} (kohustatud isik: {duty_holder})",
                uri=r.provision_uri,
                why=why,
                draft_id=None,
            )
        )
    return out


def _burden_actions(*, focus_uri: str | None) -> list[dict[str, str]]:
    """The static ``Soovitatud tegevused`` action set for the Halduskoormus page."""
    actions: list[dict[str, str]] = []
    if focus_uri:
        actions.append({"label": "Ava õiguskaardil", "href": explorer_focus_url(focus_uri)})
    actions.append({"label": "Käivita Normi mõjuahel", "href": "/analyysikeskus/normi-mojuahel"})
    actions.append({"label": "Küsi nõustajalt", "href": "/chat/new"})
    actions.append({"label": "Tagasi analüüsikeskusesse", "href": "/analyysikeskus"})
    return actions


def _burden_scope_block(sisend: str) -> Any:
    """A minimal ``Ulatus`` form — v1 has no toggles, just a "Uuenda" submit + intro."""
    return Form(  # noqa: F405
        P(  # noqa: F405
            "Vaikimisi loendatakse kõik sätte või akti deontilised liigitused. "
            "Sihtgrupi (kodanik / ettevõtja / avalik asutus) grupeering tuleb "
            "pärast ontoloogia muudatust #214.",
            cls="muted-text",
        ),
        Hidden(name="sisend", value=sisend),  # noqa: F405
        Hidden(name="ulatus_submitted", value="1"),  # noqa: F405
        Button("Uuenda ulatust", type="submit", variant="secondary", size="sm"),
        method="get",
        action="/analyysikeskus/halduskoormus",
        cls="analyysikeskus-scope-form",
    )


def _render_burden_landing(*, auth: Any, theme: str) -> Any:
    """Render the workflow shell with no input yet — the landing 5-card page."""
    landing_input = Div(  # noqa: F405
        P(  # noqa: F405
            "Sisestage õigusakt, säte või eelnõu — kuvatakse selle deontiline "
            "halduskoormus (kohustused, keelud, õigused ja load)."
        ),
        Form(  # noqa: F405
            Input(
                "sisend",
                type="text",
                placeholder=("Nt: Töölepingu seadus · KMS · TLS § 12 · eelnõu pealkiri"),
                aria_label="Õigusakt, säte või eelnõu",
                cls="analyysikeskus-input",
            ),
            Button("Hinda halduskoormust", type="submit", variant="primary"),
            method="get",
            action="/analyysikeskus/halduskoormus",
            cls="analyysikeskus-workflow-form",
        ),
        Small(  # noqa: F405
            "Näited: «Töölepingu seadus» · «KMS» · «TLS § 12» · «32016R0679»",
            cls="muted-text",
        ),
    )
    return analysis_result_shell(
        workflow_title="Halduskoormus",
        input_summary=landing_input,
        results_block=P(  # noqa: F405
            "Sisestage päring, et näha halduskoormust.", cls="muted-text"
        ),
        evidence_block=_missing_row("—"),
        actions=[
            {"label": "Küsi nõustajalt", "href": "/chat/new"},
            {"label": "Tagasi analüüsikeskusesse", "href": "/analyysikeskus"},
        ],
        user=auth,
        theme=theme,
        scope_block=Span(  # noqa: F405
            "Sisestage päring, et muuta ulatust.", cls="muted-text"
        ),
    )


def _is_draft_resolved(resolved: Any) -> bool:
    """Return True when *resolved* came from a draft-style ExtractedRef."""
    extracted = getattr(resolved, "extracted", None)
    if extracted is None:
        return False
    ref_type = str(getattr(extracted, "ref_type", "") or "")
    if ref_type == "draft":
        return True
    # Fallback: inspect the URI's local-name shape — DraftLegislation
    # URIs usually carry a "Draft" / "draft_" prefix in the corpus. This
    # keeps the route honest when the parser doesn't yet emit a
    # "draft" ref_type but the resolver returns a DraftLegislation URI.
    entity_uri = str(getattr(resolved, "entity_uri", "") or "")
    local = entity_uri.rsplit("#", 1)[-1] if "#" in entity_uri else entity_uri.rsplit("/", 1)[-1]
    return "draft" in local.lower()


def _render_burden_result(
    *,
    auth: Any,
    theme: str,
    resolved: Any,
    sisend: str,
) -> Any:
    """Render the result page for a single resolved entity.

    Branches on the resolved ref shape:
    * URI + provision ref_type → :func:`list_burden_for_provision`
    * URI + draft ref_type → :func:`burden_delta_for_draft`
    * URI + any other ref_type → :func:`list_burden_for_act` (URI;
      the helper accepts both URI and literal title and dispatches
      via ``_looks_like_uri``).
    * No URI but ``partial_match["act_title"]`` set (bare law input
      like ``TLS``) → :func:`list_burden_for_act` against the literal
      title. Wave 2 Step 5 (#801 follow-up).
    """
    entity_uri_raw = getattr(resolved, "entity_uri", None)
    entity_uri = str(entity_uri_raw) if entity_uri_raw else ""
    label = _resolved_label(resolved, sisend)
    type_label = _resolved_type_label(resolved)
    partial_title = _partial_act_title(resolved)

    if entity_uri and _is_provision_resolved(resolved):
        summary = list_burden_for_provision(entity_uri)
        results_block: Any = _burden_results_block(summary)
        evidence_summary = summary
    elif entity_uri and _is_draft_resolved(resolved):
        delta = burden_delta_for_draft(entity_uri)
        results_block = _burden_delta_block(delta)
        evidence_summary = delta.before
    elif partial_title is not None and not entity_uri:
        # Bare law input — pass the literal title to the act helper.
        summary = list_burden_for_act(partial_title)
        results_block = _burden_results_block(summary)
        evidence_summary = summary
    else:
        summary = list_burden_for_act(entity_uri)
        results_block = _burden_results_block(summary)
        evidence_summary = summary

    input_summary = P(  # noqa: F405
        "Analüüsisin: ",
        Strong(label),  # noqa: F405
        (f" — {type_label}" if type_label else ""),
    )
    evidence_rows = _burden_evidence_block(evidence_summary)
    evidence_block: Any = evidence_rows if evidence_rows else _missing_row("Tõendeid ei leitud.")
    actions = _burden_actions(focus_uri=entity_uri)

    return analysis_result_shell(
        workflow_title="Halduskoormus",
        input_summary=input_summary,
        results_block=results_block,
        evidence_block=evidence_block,
        actions=actions,
        user=auth,
        theme=theme,
        scope_block=_burden_scope_block(sisend),
    )


def _render_burden_disambiguation(
    *,
    auth: Any,
    theme: str,
    resolved: list[Any],
    sisend: str,
) -> Any:
    """Render a disambiguation page listing plausible resolutions as links."""
    candidates: list[dict[str, str]] = []
    for r in resolved:
        label = _resolved_label(r, sisend)
        extracted = getattr(r, "extracted", None)
        ref_text = str(getattr(extracted, "ref_text", "") or label)
        candidates.append({"label": label, "ref": ref_text})

    items: list[Any] = []
    for c in candidates:
        ref = (c.get("ref") or c.get("label") or "").strip()
        if not ref:
            continue
        items.append(Li(A(c.get("label") or ref, href=_burden_link(ref))))  # noqa: F405

    results_block: list[Any] = [
        Alert("Sisend võib viidata mitmele üksusele. Vali, millist analüüsida:", variant="info"),
    ]
    if items:
        results_block.append(Ul(*items, cls="analyysikeskus-candidates"))  # noqa: F405

    return analysis_result_shell(
        workflow_title="Halduskoormus",
        input_summary=P(f"Sisestasite: «{sisend}»"),  # noqa: F405
        results_block=results_block,
        evidence_block=_missing_row("—"),
        actions=[
            {"label": "Küsi nõustajalt", "href": "/chat/new"},
            {"label": "Tagasi analüüsikeskusesse", "href": "/analyysikeskus"},
        ],
        user=auth,
        theme=theme,
        scope_block=_burden_scope_block(sisend),
    )


def _render_burden_unresolved(
    *,
    auth: Any,
    theme: str,
    sisend: str,
    org_id: str | None,
) -> Any:
    """Render the "no structured reference recognised" page (+ optional RAG candidates)."""
    warning = Alert(
        "Ei tuvastanud õiguslikku viidet. Proovige nt akti nime («Töölepingu seadus»), "
        "lühinime («KMS», «TLS»), §-viidet («TLS § 12») või CELEX-numbrit (32016R0679).",
        variant="warning",
    )
    candidates = _rag_candidates(sisend, org_id)
    items: list[Any] = []
    for c in candidates:
        ref = (c.get("ref") or c.get("label") or "").strip()
        if not ref:
            continue
        items.append(Li(A(c.get("label") or ref, href=_burden_link(ref))))  # noqa: F405

    results_children: list[Any] = [warning]
    if items:
        results_children.append(
            P("Võimalikud aktid / sätted, mida võisite mõelda:", cls="muted-text")  # noqa: F405
        )
        results_children.append(Ul(*items, cls="analyysikeskus-candidates"))  # noqa: F405

    return analysis_result_shell(
        workflow_title="Halduskoormus",
        input_summary=P(f"Sisestasite: «{sisend}»"),  # noqa: F405
        results_block=results_children,
        evidence_block=_missing_row("—"),
        actions=[
            {"label": "Küsi nõustajalt", "href": "/chat/new"},
            {"label": "Tagasi analüüsikeskusesse", "href": "/analyysikeskus"},
        ],
        user=auth,
        theme=theme,
        scope_block=_burden_scope_block(sisend),
    )


def halduskoormus_page(req: Request):
    """GET /analyysikeskus/halduskoormus?sisend=<text> — Halduskoormus (A2 v1).

    Flow:

    1. Blank ``sisend`` → render the workflow shell with the input form.
    2. Else parse ``sisend`` → resolve via :class:`ReferenceResolver`:
       * exactly one resolved entity → branch on ref_type:
         - provision → :func:`list_burden_for_provision`
         - draft → :func:`burden_delta_for_draft`
         - law / eu_act / other → :func:`list_burden_for_act`
       * multiple plausible resolutions → render disambiguation links.
       * nothing resolved → friendly "no structured ref" warning + RAG
         candidates (same pattern as Normi mõjuahel / Sanktsioonid).

    V1 deviates from full target-group bucketing — the "Kohustatud isik
    (esialgne, vt #214)" column uses the ``estleg:dutyHolder`` literal
    as a fallback. V2 (after ontology issue #214 lands) will switch to
    the multi-valued ``estleg:targetGroup`` enum.
    """
    auth = req.scope.get("auth") or None
    theme = get_theme_from_request(req)
    org_id = auth.get("org_id") if auth else None

    sisend = (req.query_params.get("sisend") or "").strip()

    if not sisend:
        return _render_burden_landing(auth=auth, theme=theme)

    parsed_refs = parse_user_reference(sisend)
    resolved = _resolve_refs(parsed_refs)
    # Wave 2 Step 5 (#801 follow-up): include partial-match refs so a
    # bare law input (``TLS``, ``Töölepingu seadus``) reaches the
    # ``list_burden_for_act(act_title)`` branch in the renderer.
    unique_resolved = _select_dispatchable(resolved)

    if len(unique_resolved) == 1:
        return _render_burden_result(
            auth=auth,
            theme=theme,
            resolved=unique_resolved[0],
            sisend=sisend,
        )

    if len(unique_resolved) > 1:
        return _render_burden_disambiguation(
            auth=auth,
            theme=theme,
            resolved=unique_resolved,
            sisend=sisend,
        )

    return _render_burden_unresolved(
        auth=auth,
        theme=theme,
        sisend=sisend,
        org_id=org_id,
    )


# ---------------------------------------------------------------------------
# A3 — Pädevuste kaardistus (#797)
# ---------------------------------------------------------------------------
#
# Workflow flow:
#
#   1. Blank ``sisend`` → render the workflow shell with the input form
#      (the 5-card shell, no competence rows yet).
#   2. ``sisend`` looks like a free-text institution name → fuzzy-match
#      via :func:`search_institutions_by_label`:
#        * exactly one match → render the institution-level competence
#          view (per-act sub-sections + overlaps table).
#        * several matches → disambiguation card with clickable
#          candidate names.
#        * nothing matches → friendly "Ei tuvastanud asutust" warning.
#
# v1 limitation (per plan section 5.5): the ontology has no populated
# ``estleg:competenceArea`` predicate and no ``estleg:grantedBy`` on
# the reified ``Competence`` node yet (filed as ontology issue #215),
# so the page:
#
#   * groups powers by **act**, not by competence area
#   * lists **overlaps with other institutions**, not gap-area analysis
#   * does **not** answer "which act granted this competence"
#
# A persistent info banner on the result page calls out the v2 gap so
# a ministry lawyer doesn't mistake the institution-level view for the
# full picture.
#
# A dead Jena anywhere ⇒ the route degrades to the "ei tuvastanud
# asutust" branch rather than 500.

_WORKFLOW_PADEVUSED = "padevused"
_WORKFLOW_ACTION[_WORKFLOW_PADEVUSED] = "/analyysikeskus/padevused"

# Bucket key used by :func:`gather_institution_competences` for
# competence provisions whose owning act is missing — the route renders
# them under a "Muud" section heading rather than blank.
_PADEVUSED_ORPHAN_BUCKET_LABEL = "Muud sätted"


def _padevused_link(sisend: str, *, scope: _Scope | None = None) -> str:
    """Build a ``/analyysikeskus/padevused?sisend=…`` link (scope-carrying)."""
    from urllib.parse import urlencode

    if scope is not None:
        pairs = list(scope.query_pairs(sisend))
        return f"/analyysikeskus/padevused?{urlencode(pairs)}"
    return f"/analyysikeskus/padevused?{urlencode([('sisend', sisend)])}"


def _padevused_scope_block(sisend: str, scope: _Scope) -> Any:
    """The ``Ulatus`` scope form for the Pädevuste kaardistus workflow.

    Legal-language only — the controls explain what the page covers (and
    what it does **not** yet cover in v1). No active toggles in v1 since
    the grouping vocabulary (competence area) is missing from the
    ontology; the form is kept for UX parity and shareability.
    """
    _ = scope  # call-site symmetry
    return Form(  # noqa: F405
        P(  # noqa: F405
            "Vaatan kõiki sätteid, mille puhul valitud asutus on määratud "
            "pädevaks asutuseks. Rühmitan need akti kaupa ja toon välja "
            "kattuvused teiste asutustega.",
            cls="muted-text",
        ),
        Hidden(name="sisend", value=sisend),  # noqa: F405
        Hidden(name="ulatus_submitted", value="1"),  # noqa: F405
        Small(  # noqa: F405
            "Pädevusalade kaupa rühmitamine ja lünkade analüüs on tulekul — "
            "ootame ontoloogia täiendust (CompetenceShape + competenceArea).",
            cls="muted-text",
        ),
        Button(
            "Uuenda ulatust",
            type="submit",
            variant="secondary",
            size="sm",
            disabled=True,
            title="Tulekul",
        ),
        method="get",
        action="/analyysikeskus/padevused",
        cls="analyysikeskus-scope-form",
    )


def _padevused_v2_banner() -> Any:
    """The persistent info banner explaining the v1 scope.

    A ministry lawyer must not mistake the institution-level view for
    the full competence map. The banner sits at the top of every result
    page (above the per-act sections) and points at the ontology issue
    tracking v2 (competence-area grouping + "granted by which act").
    """
    return Alert(
        (
            "Näitan pädevusi asutuse tasandil — sätted on rühmitatud akti kaupa. "
            "Rühmitamine pädevusalade kaupa ja lünkade analüüs (millises "
            "valdkonnas pole pädevat asutust määratud) on tulekul ja ootavad "
            "ontoloogia täiendust."
        ),
        variant="info",
    )


def _padevused_summary_line(view: InstitutionCompetences) -> Any:
    """The one-line ``Tulemused`` lead — "N pädevust X aktis"."""
    total = view.total_count
    if total == 0:
        return P("Pädevusi ei leitud.", cls="muted-text")  # noqa: F405
    n_acts = sum(1 for k in view.by_act if k)  # non-orphan act count
    if not n_acts and view.by_act:
        # Every provision was an orphan — still tell the user we have rows.
        n_acts = 1
    word = "pädevus" if total == 1 else "pädevust"
    suffix = " (tulemus on kärbitud)" if view.truncated else ""
    return P(  # noqa: F405
        Strong(f"{total} {word}"),  # noqa: F405
        f" — {n_acts} aktis.{suffix}",
    )


def _padevused_act_heading(act_uri: str, act_label: str) -> Any:
    """Render the per-act sub-section heading.

    Clickable to the Õiguskaart when the act has a URI; orphan rows
    (empty title literal) get the static "Muud sätted" heading. In the
    prod corpus the provision → act join is the literal
    ``estleg:sourceAct`` title (no act URIs exist) so the URI argument
    is always empty and the heading renders as a label only — that's
    intentional, the explorer can't focus on a string title.
    """
    if not act_label:
        return H4(  # noqa: F405
            _PADEVUSED_ORPHAN_BUCKET_LABEL,
            cls="analyysikeskus-subsection-title",
        )
    if not act_uri:
        # Prod path — title-only heading, no link.
        return H4(  # noqa: F405
            act_label,
            cls="analyysikeskus-subsection-title",
        )
    label = act_label or act_uri.rsplit("#", 1)[-1].rsplit("/", 1)[-1] or "Akt"
    return H4(  # noqa: F405
        A(label, href=explorer_focus_url(act_uri), cls="data-table-link"),  # noqa: F405
        cls="analyysikeskus-subsection-title",
    )


def _padevused_act_table(rows: list[Any]) -> Any:
    """Render one per-act block: a small table of competence provisions."""
    columns = [
        Column(
            key="provision",
            label="Säte",
            sortable=False,
            render=lambda r: (
                A(  # noqa: F405
                    r.get("label") or "—",
                    href=explorer_focus_url(r["uri"]),
                    cls="data-table-link",
                )
                if r.get("uri")
                else Span(r.get("label") or "—")  # noqa: F405
            ),
        ),
    ]
    data_rows: list[dict[str, Any]] = [
        {"uri": r.provision_uri, "label": r.provision_label or "—"} for r in rows
    ]
    return DataTable(
        columns=columns,
        rows=data_rows,
        empty_message="Pädevusi ei leitud.",
    )


def _padevused_overlaps_section(view: InstitutionCompetences) -> Any:
    """Render the ``Kattuvad pädevused`` section.

    Empty ⇒ a one-line muted "Kattuvusi teiste asutustega ei leitud." line
    so the section heading stays present (the section is a key part of the
    workflow's value prop).
    """
    overlaps = view.overlaps[:_MAX_RESULT_ROWS]
    if not overlaps:
        return _sub_section(
            "Kattuvad pädevused",
            _missing_row("Kattuvusi teiste asutustega ei leitud."),
        )

    columns = [
        Column(
            key="provision",
            label="Säte",
            sortable=False,
            render=lambda r: (
                A(  # noqa: F405
                    r.get("provisionLabel") or "—",
                    href=explorer_focus_url(r["provisionUri"]),
                    cls="data-table-link",
                )
                if r.get("provisionUri")
                else Span(r.get("provisionLabel") or "—")  # noqa: F405
            ),
        ),
        Column(
            key="act",
            label="Akt",
            sortable=False,
            render=lambda r: (
                A(  # noqa: F405
                    r.get("actLabel") or "—",
                    href=explorer_focus_url(r["actUri"]),
                    cls="data-table-link",
                )
                if r.get("actUri")
                else Span(r.get("actLabel") or "—")  # noqa: F405
            ),
        ),
        Column(
            key="other",
            label="Teine pädev asutus",
            sortable=False,
            render=lambda r: (
                A(  # noqa: F405
                    r.get("otherLabel") or "—",
                    href=explorer_focus_url(r["otherUri"]),
                    cls="data-table-link",
                )
                if r.get("otherUri")
                else Span(r.get("otherLabel") or "—")  # noqa: F405
            ),
        ),
    ]
    data_rows = [
        {
            "provisionUri": o.provision_uri,
            "provisionLabel": o.provision_label or "—",
            "actUri": o.act_uri,
            "actLabel": o.act_label or "—",
            "otherUri": o.other_institution_uri,
            "otherLabel": o.other_institution_label or "—",
        }
        for o in overlaps
    ]
    return _sub_section(
        "Kattuvad pädevused",
        DataTable(
            columns=columns,
            rows=data_rows,
            empty_message="Kattuvusi ei leitud.",
        ),
    )


def _padevused_results_block(view: InstitutionCompetences) -> list[Any]:
    """Assemble the ``Tulemused`` content: banner + summary + per-act sections + overlaps."""
    blocks: list[Any] = [_padevused_v2_banner(), _padevused_summary_line(view)]
    if view.total_count == 0:
        blocks.append(_padevused_overlaps_section(view))
        return blocks
    for bucket_key, rows in view.by_act.items():
        # In prod the bucket key is the literal ``estleg:sourceAct``
        # title (no provision → act URI edge exists in the corpus).
        # We still surface ``rows[0].act_uri`` to ``_padevused_act_heading``
        # so a future ontology that re-introduces act URIs gets a
        # clickable link for free; in prod today the URI is always
        # empty and the heading falls back to a label-only render.
        act_uri = rows[0].act_uri if rows else ""
        act_label = (rows[0].act_label if rows else "") or bucket_key
        blocks.append(
            Div(  # noqa: F405
                _padevused_act_heading(act_uri, act_label),
                _padevused_act_table(rows),
            )
        )
    blocks.append(_padevused_overlaps_section(view))
    if view.truncated:
        blocks.append(
            P(  # noqa: F405
                f"Kuvan esimesed {view.total_count} pädevust — "
                "asutusel on rohkem volitusi, kui ühel lehel mahub.",
                cls="muted-text",
            )
        )
    return blocks


def _padevused_evidence_block(view: InstitutionCompetences) -> list[Any]:
    """Assemble the ``Tõendid`` rows from the competence view.

    One row per competence provision (capped at ``_MAX_RESULT_ROWS`` so
    the Tõendid card stays scannable on heavyweight institutions) using
    the canonical "pädev asutus" phrase from :mod:`app.ontology.relations`.
    """
    out: list[Any] = []
    count = 0
    institution_label = view.institution_label or "Asutus"
    for _act_uri, rows in view.by_act.items():
        for r in rows:
            if count >= _MAX_RESULT_ROWS:
                break
            count += 1
            target_label = r.provision_label or "Säte"
            if r.act_label:
                target_label = f"{target_label} ({r.act_label})"
            out.append(
                _evidence_row(
                    source_label=institution_label,
                    relation="on pädev asutus sättes",
                    target_label=target_label,
                    uri=r.provision_uri,
                    why=(
                        "See säte määrab valitud asutuse pädevaks — "
                        "muudatus selles sättes mõjutab otse asutuse volitusi."
                    ),
                    draft_id=None,
                )
            )
        if count >= _MAX_RESULT_ROWS:
            break
    return out


def _padevused_actions(
    *,
    institution_uri: str | None,
    institution_label: str,
) -> list[dict[str, str]]:
    """The ``Soovitatud tegevused`` action set for the Pädevuste kaardistus page."""
    from urllib.parse import urlencode

    actions: list[dict[str, str]] = []
    if institution_uri:
        actions.append({"label": "Ava õiguskaardil", "href": explorer_focus_url(institution_uri)})
    if institution_label:
        chat_seed = f"Millised on asutuse «{institution_label}» peamised volitused?"
        actions.append(
            {
                "label": "Küsi nõustajalt",
                "href": f"/chat/new?{urlencode([('q', chat_seed)])}",
            }
        )
    else:
        actions.append({"label": "Küsi nõustajalt", "href": "/chat/new"})
    actions.append({"label": "Tagasi analüüsikeskusesse", "href": "/analyysikeskus"})
    return actions


def _render_padevused_landing(
    *,
    auth: Any,
    theme: str,
) -> Any:
    """Render the workflow shell with no input yet (the empty-form landing)."""
    landing_input = Div(  # noqa: F405
        P(  # noqa: F405
            "Sisestage asutuse nimi (näiteks «Andmekaitse Inspektsioon») — "
            "kuvatakse kõik volitused, mille puhul asutus on määratud "
            "pädevaks, rühmitatuna akti kaupa, koos kattuvustega teiste "
            "asutustega."
        ),
        Form(  # noqa: F405
            Input(
                "sisend",
                type="text",
                placeholder="Nt: Andmekaitse Inspektsioon · Maksu- ja Tolliamet",
                aria_label="Asutuse nimi",
                cls="analyysikeskus-input",
            ),
            Button("Otsi pädevusi", type="submit", variant="primary"),
            method="get",
            action="/analyysikeskus/padevused",
            cls="analyysikeskus-workflow-form",
        ),
        Small(  # noqa: F405
            "Näited: «Andmekaitse Inspektsioon» · «Tarbijakaitse ja Tehnilise Järelevalve Amet»",
            cls="muted-text",
        ),
    )
    return analysis_result_shell(
        workflow_title="Pädevuste kaardistus",
        input_summary=landing_input,
        results_block=P(  # noqa: F405
            "Sisestage asutuse nimi, et näha selle pädevusi.",
            cls="muted-text",
        ),
        evidence_block=_missing_row("—"),
        actions=[
            {"label": "Küsi nõustajalt", "href": "/chat/new"},
            {"label": "Tagasi analüüsikeskusesse", "href": "/analyysikeskus"},
        ],
        user=auth,
        theme=theme,
        scope_block=Span(  # noqa: F405
            "Sisestage päring, et muuta ulatust.",
            cls="muted-text",
        ),
    )


def _render_padevused_result(
    *,
    auth: Any,
    theme: str,
    institution_uri: str,
    institution_label: str,
    sisend: str,
    scope: _Scope,
) -> Any:
    """Render the result page for one resolved institution."""
    view = gather_institution_competences(
        institution_uri,
        institution_label=institution_label,
    )
    label = view.institution_label or institution_label or sisend

    input_summary = P(  # noqa: F405
        "Analüüsisin asutust: ",
        Strong(label),  # noqa: F405
    )
    results_block = _padevused_results_block(view)
    evidence_rows = _padevused_evidence_block(view)
    evidence_block: Any = evidence_rows if evidence_rows else _missing_row("Tõendeid ei leitud.")
    actions = _padevused_actions(
        institution_uri=view.institution_uri or institution_uri,
        institution_label=label,
    )

    return analysis_result_shell(
        workflow_title="Pädevuste kaardistus",
        input_summary=input_summary,
        results_block=results_block,
        evidence_block=evidence_block,
        actions=actions,
        user=auth,
        theme=theme,
        scope_block=_padevused_scope_block(sisend, scope),
    )


def _render_padevused_disambiguation(
    *,
    auth: Any,
    theme: str,
    candidates: list[Any],
    sisend: str,
    scope: _Scope,
) -> Any:
    """Render a disambiguation page listing institution candidates as links."""
    items: list[Any] = []
    for c in candidates:
        label = (getattr(c, "label", "") or "").strip()
        if not label:
            continue
        items.append(
            Li(  # noqa: F405
                A(  # noqa: F405
                    label,
                    href=_padevused_link(label, scope=scope),
                )
            )
        )

    results_block: list[Any] = [
        Alert(
            "Sisend võib viidata mitmele asutusele. Vali, millist analüüsida:",
            variant="info",
        ),
    ]
    if items:
        results_block.append(Ul(*items, cls="analyysikeskus-candidates"))  # noqa: F405

    return analysis_result_shell(
        workflow_title="Pädevuste kaardistus",
        input_summary=P(f"Sisestasite: «{sisend}»"),  # noqa: F405
        results_block=results_block,
        evidence_block=_missing_row("—"),
        actions=[
            {"label": "Küsi nõustajalt", "href": "/chat/new"},
            {"label": "Tagasi analüüsikeskusesse", "href": "/analyysikeskus"},
        ],
        user=auth,
        theme=theme,
        scope_block=_padevused_scope_block(sisend, scope),
    )


def _render_padevused_unresolved(
    *,
    auth: Any,
    theme: str,
    sisend: str,
    scope: _Scope,
) -> Any:
    """Render the "no institution recognised" page (no RAG fallback — names only)."""
    warning = Alert(
        "Ei tuvastanud asutust. Proovige nime täiskujul, nt «Andmekaitse "
        "Inspektsioon» või «Maksu- ja Tolliamet».",
        variant="warning",
    )
    return analysis_result_shell(
        workflow_title="Pädevuste kaardistus",
        input_summary=P(f"Sisestasite: «{sisend}»"),  # noqa: F405
        results_block=warning,
        evidence_block=_missing_row("—"),
        actions=[
            {"label": "Küsi nõustajalt", "href": "/chat/new"},
            {"label": "Tagasi analüüsikeskusesse", "href": "/analyysikeskus"},
        ],
        user=auth,
        theme=theme,
        scope_block=_padevused_scope_block(sisend, scope),
    )


def padevused_page(req: Request):
    """GET /analyysikeskus/padevused?sisend=<text> — Pädevuste kaardistus (A3 v1).

    Flow:

    1. Blank ``sisend`` → render the workflow shell with the input form
       (the 5-card shell with a search-form Sisend card).
    2. Else search institutions by free-text label
       (:func:`search_institutions_by_label`):
       * exactly one candidate → render the institution-level competence
         view (per-act sections + overlaps + v2 banner).
       * several candidates → disambiguation card with clickable
         candidate names.
       * nothing matches → friendly "Ei tuvastanud asutust" warning.
    3. When the input is itself a full estleg Institution URI (e.g.
       deep-linked from the explorer evidence card), skip the label
       search and resolve the label directly via
       :func:`get_institution_label`.
    """
    auth = req.scope.get("auth") or None
    theme = get_theme_from_request(req)

    sisend = (req.query_params.get("sisend") or "").strip()
    scope = _Scope(req.query_params, workflow=_WORKFLOW_NORMI)
    # Use the padevused workflow tag so query_pairs links back to this route.
    scope.workflow = _WORKFLOW_PADEVUSED

    if not sisend:
        return _render_padevused_landing(auth=auth, theme=theme)

    # Direct URI deep-link branch (explorer evidence card → workflow).
    if sisend.startswith("http://") or sisend.startswith("https://"):
        label = get_institution_label(sisend)
        if label:
            return _render_padevused_result(
                auth=auth,
                theme=theme,
                institution_uri=sisend,
                institution_label=label,
                sisend=sisend,
                scope=scope,
            )
        return _render_padevused_unresolved(auth=auth, theme=theme, sisend=sisend, scope=scope)

    candidates = search_institutions_by_label(sisend)
    # Exact-match short-circuit: if a candidate's label matches the input
    # ignoring case + whitespace, prefer it over disambiguation. Mirrors
    # the "click a disambiguation row → run again" round-trip.
    norm = sisend.strip().lower()
    exact = [c for c in candidates if (getattr(c, "label", "") or "").strip().lower() == norm]
    if len(exact) == 1:
        return _render_padevused_result(
            auth=auth,
            theme=theme,
            institution_uri=exact[0].uri,
            institution_label=exact[0].label,
            sisend=sisend,
            scope=scope,
        )

    if len(candidates) == 1:
        return _render_padevused_result(
            auth=auth,
            theme=theme,
            institution_uri=candidates[0].uri,
            institution_label=candidates[0].label,
            sisend=sisend,
            scope=scope,
        )

    if len(candidates) > 1:
        return _render_padevused_disambiguation(
            auth=auth,
            theme=theme,
            candidates=candidates,
            sisend=sisend,
            scope=scope,
        )

    return _render_padevused_unresolved(auth=auth, theme=theme, sisend=sisend, scope=scope)


# ---------------------------------------------------------------------------
# Ajalooline kehtivus (A4 v1 — plan section 5)
# ---------------------------------------------------------------------------
#
# Workflow flow:
#
#   1. Blank ``sisend`` → render the workflow shell with the input form
#      (the 5-card shell, no timeline yet).
#   2. ``parse_user_reference(sisend)`` → if a §-reference / CELEX /
#      case number resolves to exactly one entity, run
#      :func:`app.analyysikeskus.history.get_history_bundle` against
#      that URI and render the result page with five sub-sections:
#      Akti ajatelg / Muudatused / Kohtupraktika / Mõjuanalüüsid /
#      Pooleliolevad eelnõud. **When the resolved input is a Provision**,
#      a persistent warning banner sits at the top of the result page
#      announcing the v1 act-level-only limitation (plan line 318:
#      "lawyers must not miss this"). Act-level inputs see no banner
#      because act-level data is complete for acts.
#   3. Multiple resolutions → render a disambiguation card.
#      Nothing recognised → friendly warning + RAG candidates (same
#      pattern as Normi / Sanktsioonid).
#
# The page uses a vertical timeline (per the plan: "vertical timeline,
# not D3 graph") rendered as a CSS-friendly ordered list — no SVG. A
# dead Jena anywhere ⇒ per-section muted-row fallbacks, not 500.

_WORKFLOW_AJALUGU = "ajalugu"
_WORKFLOW_ACTION[_WORKFLOW_AJALUGU] = "/analyysikeskus/ajalugu"

# The v1 limitation banner — shown verbatim at the top of the result
# page when the input is a Provision. Plan line 318: "lawyers must not
# miss this". Kept as module-level constants so tests can pin the
# exact copy and the route stays declarative.
_AJALUGU_V1_BANNER_HEADING_ET = "⚠️ Ainult akti tasandi ajalugu"
_AJALUGU_V1_BANNER_BODY_ET = (
    "Sätte tasandi versioonid (§-de tekstierinevused redaktsioonide vahel) "
    "on saadaval näidisaktidele (käibemaksuseadus, tulumaksuseadus), kuid "
    "kogu korpus on alles sissevõtmisel. Täielik kate on jälgitav "
    "ontoloogia probleemi #208 all."
)
_AJALUGU_V1_BANNER_ISSUE_URL = "https://github.com/henrikaavik/estonian-legal-ontology/issues/208"


def _ajalugu_link(sisend: str, *, scope: _Scope | None = None) -> str:
    """Build a ``/analyysikeskus/ajalugu?sisend=…`` link (scope-carrying)."""
    from urllib.parse import urlencode

    if scope is not None:
        pairs = list(scope.query_pairs(sisend))
        return f"/analyysikeskus/ajalugu?{urlencode(pairs)}"
    return f"/analyysikeskus/ajalugu?{urlencode([('sisend', sisend)])}"


def _ajalugu_scope_block(sisend: str, scope: _Scope) -> Any:
    """The ``Ulatus`` scope form for the Ajalooline kehtivus workflow.

    Legal-language only. V1 has no toggles that actually filter — the
    underlying data is what it is — but we keep a form for UX parity
    so URLs stay shareable and the user sees a familiar pattern.
    """
    _ = scope  # call-site symmetry
    return Form(  # noqa: F405
        P(  # noqa: F405
            "Kuvan akti tasandi ajateljel jõustumise, muudatused ja kehtetuks "
            "tunnistamise. Sätte tasandi versioonid lisanduvad pärast ontoloogia "
            "rikastamist.",
            cls="muted-text",
        ),
        Hidden(name="sisend", value=sisend),  # noqa: F405
        Hidden(name="ulatus_submitted", value="1"),  # noqa: F405
        Small(  # noqa: F405
            "Filtrid muudatuste liigi (jõustunud / pooleli / tühistatud) ja "
            "kuupäevavahemiku kaupa on tulekul.",
            cls="muted-text",
        ),
        Button(
            "Uuenda ulatust",
            type="submit",
            variant="secondary",
            size="sm",
            disabled=True,
            title="Tulekul",
        ),
        method="get",
        action="/analyysikeskus/ajalugu",
        cls="analyysikeskus-scope-form",
    )


def _is_ajalugu_provision_input(resolved: Any) -> bool:
    """Return True when *resolved* came from a ``provision`` ExtractedRef."""
    extracted = getattr(resolved, "extracted", None)
    if extracted is None:
        return False
    return getattr(extracted, "ref_type", "") == "provision"


def _ajalugu_input_type(resolved: Any) -> str:
    """Return the canonical ``input_type`` for :func:`get_history_bundle`.

    Maps the :class:`ExtractedRef`'s ``ref_type`` into the three
    buckets the history bundle understands: ``"provision"`` (also
    drives the v1 limitation banner) → ``"court_decision"`` for case
    numbers → ``"act"`` for everything else (law / EU act / concept).
    """
    extracted = getattr(resolved, "extracted", None)
    if extracted is None:
        return "act"
    ref_type = getattr(extracted, "ref_type", "") or ""
    if ref_type == "provision":
        return "provision"
    if ref_type == "court_decision":
        return "court_decision"
    return "act"


def _fmt_date_et(value: date | None) -> str:
    """Render *value* in Estonian short date form (``"15.03.2023"``); ``"—"`` for None."""
    if value is None:
        return "—"
    return value.strftime("%d.%m.%Y")


def _ajalugu_v1_banner() -> Any:
    """Render the persistent v1-limitation Alert (provision-input only).

    The banner is **not** a tooltip — it sits at the top of the Tulemused
    card body so the lawyer cannot dismiss or miss it. The issue link is
    rendered as a plain anchor (no JS).
    """
    return Alert(
        Div(  # noqa: F405
            Strong(_AJALUGU_V1_BANNER_HEADING_ET),  # noqa: F405
            P(  # noqa: F405
                _AJALUGU_V1_BANNER_BODY_ET,
                " ",
                A(  # noqa: F405
                    "Loe lähemalt",
                    href=_AJALUGU_V1_BANNER_ISSUE_URL,
                    rel="noopener",
                    target="_blank",
                ),
                ".",
            ),
            cls="analyysikeskus-ajalugu-banner",
        ),
        variant="warning",
    )


def _ajalugu_timeline_section(heading: str, items: list[Any]) -> Any:
    """Wrap a heading + a list of timeline items into a sub-section block."""
    if not items:
        body: Any = _missing_row("Andmeid ei leitud.")
    else:
        body = Ul(*items, cls="analyysikeskus-ajalugu-timeline")  # noqa: F405
    return _sub_section(heading, body)


def _ajalugu_act_timeline_items(timeline: Any) -> list[Any]:
    """Render the act-level timeline (entry / repeal / last amendment / status)."""
    items: list[Any] = []
    if timeline.entry_into_force is not None:
        items.append(
            Li(  # noqa: F405
                Strong(_fmt_date_et(timeline.entry_into_force)),  # noqa: F405
                " — Akt jõustus.",
            )
        )
    if timeline.last_amendment_date is not None:
        items.append(
            Li(  # noqa: F405
                Strong(_fmt_date_et(timeline.last_amendment_date)),  # noqa: F405
                " — Viimane muudatus.",
            )
        )
    if timeline.repeal_date is not None:
        items.append(
            Li(  # noqa: F405
                Strong(_fmt_date_et(timeline.repeal_date)),  # noqa: F405
                " — Akt tunnistati kehtetuks.",
            )
        )
    if timeline.temporal_status:
        items.append(
            Li(  # noqa: F405
                "Hetkestaatus: ",
                Badge(
                    temporal_status_label(timeline.temporal_status),
                    variant=(
                        "danger"
                        if timeline.temporal_status in ("repealed", "expired")
                        else "success"
                    ),
                ),
            )
        )
    return items


def _ajalugu_amendment_items(amendments: list[Any]) -> list[Any]:
    """Render each AmendmentEvent as one timeline row."""
    items: list[Any] = []
    for ev in amendments:
        primary_date = ev.event_date or ev.entry_into_force_date
        date_str = _fmt_date_et(primary_date)
        bits: list[Any] = [
            Strong(date_str),  # noqa: F405
            " — ",
            ev.event_label or "Muudatus",
        ]
        if ev.rt_reference:
            bits.append(f" · {ev.rt_reference}")
        if ev.entry_into_force_date and ev.entry_into_force_date != ev.event_date:
            bits.append(f" · jõustub {_fmt_date_et(ev.entry_into_force_date)}")
        if ev.affected_provisions:
            preview = ", ".join(label for _, label in ev.affected_provisions[:3])
            extra = (
                f" (+{len(ev.affected_provisions) - 3})" if len(ev.affected_provisions) > 3 else ""
            )
            bits.append(f" · puudutab: {preview}{extra}")
        items.append(Li(*bits))  # noqa: F405
    return items


def _ajalugu_court_items(decisions: list[Any]) -> list[Any]:
    """Render each interpreting court decision as one timeline row."""
    items: list[Any] = []
    for d in decisions:
        date_str = _fmt_date_et(d.decision_date)
        bits: list[Any] = [
            Strong(date_str),  # noqa: F405
            " — ",
            d.decision_label or "Kohtulahend",
        ]
        if d.interprets_label:
            bits.append(f" · tõlgendab: {d.interprets_label}")
        items.append(Li(*bits))  # noqa: F405
    return items


def _ajalugu_impact_items(reports: list[Any]) -> list[Any]:
    """Render each historical impact-report touch as one timeline row."""
    items: list[Any] = []
    for r in reports:
        when = r.generated_at.date() if r.generated_at is not None else None
        date_str = _fmt_date_et(when)
        version = f" (v{r.version_number})" if r.version_number is not None else ""
        title = r.draft_title or "Eelnõu"
        bits: list[Any] = [
            Strong(date_str),  # noqa: F405
            " — Mõjuanalüüs: ",
            title,
            version,
        ]
        if r.draft_id:
            bits.append(
                A(  # noqa: F405
                    " Ava eelnõu →",
                    href=f"/docs/{r.draft_id}",
                    cls="muted-link",
                )
            )
        items.append(Li(*bits))  # noqa: F405
    return items


def _ajalugu_pending_items(drafts: list[Any]) -> list[Any]:
    """Render each pending draft / drafting intent as one timeline row."""
    items: list[Any] = []
    for d in drafts:
        date_str = _fmt_date_et(d.submitted_date)
        if d.draft_type == "DraftLegislation":
            type_label_et = "Eelnõu"
        elif d.draft_type == "DraftingIntent":
            type_label_et = "Väljatöötamiskavatsus"
        else:
            type_label_et = "Eelnõu"
        items.append(
            Li(  # noqa: F405
                Strong(date_str),  # noqa: F405
                f" — {type_label_et}: ",
                d.draft_label or "—",
            )
        )
    return items


def _ajalugu_results_block(bundle: Any, *, show_banner: bool) -> list[Any]:
    """Assemble the Tulemused block — banner (if applicable) + five sub-sections."""
    blocks: list[Any] = []
    if show_banner:
        blocks.append(_ajalugu_v1_banner())

    timeline = bundle.act_timeline
    status_text = (
        temporal_status_label(timeline.temporal_status) if timeline.temporal_status else "—"
    )
    summary_bits: list[Any] = [
        f"{len(bundle.amendments)} muudatust",
        f" · {len(bundle.court_decisions)} kohtulahendit",
        f" · {len(bundle.impact_reports)} mõjuanalüüsi",
        f" · {len(bundle.pending_drafts)} pooleliolevat eelnõu",
        f" · hetkestaatus: {status_text}",
    ]
    blocks.append(P(*summary_bits, cls="analyysikeskus-summary"))  # noqa: F405

    blocks.append(_ajalugu_timeline_section("Akti ajatelg", _ajalugu_act_timeline_items(timeline)))
    blocks.append(
        _ajalugu_timeline_section("Muudatused", _ajalugu_amendment_items(bundle.amendments))
    )
    blocks.append(
        _ajalugu_timeline_section(
            "Tõlgendav kohtupraktika", _ajalugu_court_items(bundle.court_decisions)
        )
    )
    blocks.append(
        _ajalugu_timeline_section(
            "Varasemad mõjuanalüüsid", _ajalugu_impact_items(bundle.impact_reports)
        )
    )
    blocks.append(
        _ajalugu_timeline_section(
            "Pooleliolevad eelnõud", _ajalugu_pending_items(bundle.pending_drafts)
        )
    )
    return blocks


def _ajalugu_evidence_block(bundle: Any) -> list[Any]:
    """Assemble the ``Tõendid`` block — one row per AmendmentEvent (+ RT cite)."""
    out: list[Any] = []
    for ev in bundle.amendments:
        link_uri = ev.event_uri
        date_str = _fmt_date_et(ev.event_date or ev.entry_into_force_date)
        target_label = (
            f"{ev.event_label} ({date_str})" if ev.event_label else f"Muudatus {date_str}"
        )
        why = (
            "See muudatus on osa sätte ajaloolisest kehtivusest — sätte "
            "tähendus võib enne ja pärast seda kuupäeva erineda."
        )
        if ev.rt_reference:
            why = f"{why} RT viide: {ev.rt_reference}."
        out.append(
            _evidence_row(
                source_label=ev.event_label or "Muudatus",
                relation="muudab",
                target_label=target_label,
                uri=link_uri,
                why=why,
                when=date_str,
                draft_id=None,
            )
        )
    return out


def _ajalugu_actions(*, focus_uri: str | None, sisend: str) -> list[dict[str, str]]:
    """The ``Soovitatud tegevused`` action set for the result page. Static."""
    _ = sisend  # reserved for future cross-workflow links
    actions: list[dict[str, str]] = []
    if focus_uri:
        actions.append({"label": "Ava õiguskaardil", "href": explorer_focus_url(focus_uri)})
    actions.append({"label": "Küsi nõustajalt", "href": "/chat/new"})
    actions.append({"label": "Tagasi analüüsikeskusesse", "href": "/analyysikeskus"})
    return actions


def _render_ajalugu_landing(*, auth: Any, theme: str) -> Any:
    """Render the workflow shell with no input yet (the empty-form landing)."""
    landing_input = Div(  # noqa: F405
        P(  # noqa: F405
            "Sisestage säte, akt, CELEX-number või kohtuasja number — kuvatakse "
            "akti tasandi ajatelg: jõustumine, muudatused, kohtupraktika ja "
            "pooleliolevad eelnõud."
        ),
        Form(  # noqa: F405
            Input(
                "sisend",
                type="text",
                placeholder="Nt: AvTS § 35 · KMS · CELEX-number · 3-2-1-100-15",
                aria_label="Õiguslik viide või kirjeldus",
                cls="analyysikeskus-input",
            ),
            Button("Vaata ajalugu", type="submit", variant="primary"),
            method="get",
            action="/analyysikeskus/ajalugu",
            cls="analyysikeskus-workflow-form",
        ),
        Small(  # noqa: F405
            "Näited: «AvTS § 35» · «KMS» · «3-2-1-100-15»",
            cls="muted-text",
        ),
    )
    return analysis_result_shell(
        workflow_title="Ajalooline kehtivus",
        input_summary=landing_input,
        results_block=P(  # noqa: F405
            "Sisestage päring, et näha ajatelge.",
            cls="muted-text",
        ),
        evidence_block=_missing_row("—"),
        actions=[
            {"label": "Küsi nõustajalt", "href": "/chat/new"},
            {"label": "Tagasi analüüsikeskusesse", "href": "/analyysikeskus"},
        ],
        user=auth,
        theme=theme,
        scope_block=Span(  # noqa: F405
            "Sisestage päring, et muuta ulatust.",
            cls="muted-text",
        ),
    )


def _render_ajalugu_result(
    *,
    auth: Any,
    theme: str,
    resolved: Any,
    sisend: str,
    scope: _Scope,
) -> Any:
    """Render the result page for a single resolved entity.

    A URI-resolved ref runs :func:`get_history_bundle` against the URI.
    A partial-match-only ref (bare law input like ``KarS``) routes the
    literal act title through the same helper with ``input_type="act"``
    — ``get_history_bundle`` accepts either a URI or a literal title
    (Wave 2 Step 5 (#801 follow-up)).
    """
    entity_uri_raw = getattr(resolved, "entity_uri", None)
    entity_uri = str(entity_uri_raw) if entity_uri_raw else ""
    label = _resolved_label(resolved, sisend)
    type_label = _resolved_type_label(resolved)
    partial_title = _partial_act_title(resolved)
    # Force act-type when the only signal is a partial-match act title.
    # Otherwise honour the resolver's ref_type → input_type mapping.
    if partial_title is not None and not entity_uri:
        input_type = "act"
        bundle_input = partial_title
    else:
        input_type = _ajalugu_input_type(resolved)
        bundle_input = entity_uri
    is_provision = input_type == "provision"

    bundle = get_history_bundle(bundle_input, input_type=input_type)

    input_summary = P(  # noqa: F405
        "Analüüsisin: ",
        Strong(label),  # noqa: F405
        (f" — {type_label}" if type_label else ""),
    )
    results_block = _ajalugu_results_block(bundle, show_banner=is_provision)
    evidence_rows = _ajalugu_evidence_block(bundle)
    evidence_block: Any = evidence_rows if evidence_rows else _missing_row("Tõendeid ei leitud.")
    actions = _ajalugu_actions(focus_uri=entity_uri, sisend=sisend)

    return analysis_result_shell(
        workflow_title="Ajalooline kehtivus",
        input_summary=input_summary,
        results_block=results_block,
        evidence_block=evidence_block,
        actions=actions,
        user=auth,
        theme=theme,
        scope_block=_ajalugu_scope_block(sisend, scope),
    )


def _render_ajalugu_disambiguation(
    *,
    auth: Any,
    theme: str,
    resolved: list[Any],
    sisend: str,
    scope: _Scope,
) -> Any:
    """Render a disambiguation page listing plausible resolutions as links."""
    candidates: list[dict[str, str]] = []
    for r in resolved:
        label = _resolved_label(r, sisend)
        extracted = getattr(r, "extracted", None)
        ref_text = str(getattr(extracted, "ref_text", "") or label)
        candidates.append({"label": label, "ref": ref_text})

    items: list[Any] = []
    for c in candidates:
        ref = (c.get("ref") or c.get("label") or "").strip()
        if not ref:
            continue
        items.append(
            Li(  # noqa: F405
                A(  # noqa: F405
                    c.get("label") or ref,
                    href=_ajalugu_link(ref, scope=scope),
                )
            )
        )

    results_block: list[Any] = [
        Alert(
            "Sisend võib viidata mitmele üksusele. Vali, millist analüüsida:",
            variant="info",
        ),
    ]
    if items:
        results_block.append(Ul(*items, cls="analyysikeskus-candidates"))  # noqa: F405

    return analysis_result_shell(
        workflow_title="Ajalooline kehtivus",
        input_summary=P(f"Sisestasite: «{sisend}»"),  # noqa: F405
        results_block=results_block,
        evidence_block=_missing_row("—"),
        actions=[
            {"label": "Küsi nõustajalt", "href": "/chat/new"},
            {"label": "Tagasi analüüsikeskusesse", "href": "/analyysikeskus"},
        ],
        user=auth,
        theme=theme,
        scope_block=_ajalugu_scope_block(sisend, scope),
    )


def _render_ajalugu_unresolved(
    *,
    auth: Any,
    theme: str,
    sisend: str,
    org_id: str | None,
    scope: _Scope,
) -> Any:
    """Render the "no structured reference recognised" page (+ optional RAG candidates)."""
    warning = Alert(
        "Ei tuvastanud õiguslikku viidet. Proovige nt «AvTS § 35», CELEX-numbrit "
        "(32016R0679) või kohtuasja numbrit (3-2-1-100-15).",
        variant="warning",
    )
    candidates = _rag_candidates(sisend, org_id)
    items: list[Any] = []
    for c in candidates:
        ref = (c.get("ref") or c.get("label") or "").strip()
        if not ref:
            continue
        items.append(
            Li(  # noqa: F405
                A(  # noqa: F405
                    c.get("label") or ref,
                    href=_ajalugu_link(ref),
                )
            )
        )

    results_children: list[Any] = [warning]
    if items:
        results_children.append(
            P("Võimalikud sätted, mida võisite mõelda:", cls="muted-text")  # noqa: F405
        )
        results_children.append(Ul(*items, cls="analyysikeskus-candidates"))  # noqa: F405

    return analysis_result_shell(
        workflow_title="Ajalooline kehtivus",
        input_summary=P(f"Sisestasite: «{sisend}»"),  # noqa: F405
        results_block=results_children,
        evidence_block=_missing_row("—"),
        actions=[
            {"label": "Küsi nõustajalt", "href": "/chat/new"},
            {"label": "Tagasi analüüsikeskusesse", "href": "/analyysikeskus"},
        ],
        user=auth,
        theme=theme,
        scope_block=_ajalugu_scope_block(sisend, scope),
    )


def ajalugu_page(req: Request):
    """GET /analyysikeskus/ajalugu?sisend=<text> — Ajalooline kehtivus (A4 v1).

    Flow mirrors the other workflows:

    1. Blank ``sisend`` → render the landing form.
    2. Resolve via :class:`ReferenceResolver`:
       * single resolution → :func:`_render_ajalugu_result` (with v1
         banner when the input is a Provision).
       * multiple → disambiguation card.
       * none → friendly warning + RAG candidates.

    A dead Jena / DB ⇒ per-section degradation, not a 500.
    """
    auth = req.scope.get("auth") or None
    theme = get_theme_from_request(req)
    org_id = auth.get("org_id") if auth else None

    sisend = (req.query_params.get("sisend") or "").strip()
    scope = _Scope(req.query_params, workflow=_WORKFLOW_NORMI)

    if not sisend:
        return _render_ajalugu_landing(auth=auth, theme=theme)

    parsed_refs = parse_user_reference(sisend)
    resolved = _resolve_refs(parsed_refs)
    # Wave 2 Step 5 (#801 follow-up): include partial-match refs so a
    # bare law input (``KarS``, ``Karistusseadustik``) reaches the
    # act-level history bundle in the renderer.
    unique_resolved = _select_dispatchable(resolved)

    if len(unique_resolved) == 1:
        return _render_ajalugu_result(
            auth=auth,
            theme=theme,
            resolved=unique_resolved[0],
            sisend=sisend,
            scope=scope,
        )

    if len(unique_resolved) > 1:
        return _render_ajalugu_disambiguation(
            auth=auth,
            theme=theme,
            resolved=unique_resolved,
            sisend=sisend,
            scope=scope,
        )

    return _render_ajalugu_unresolved(
        auth=auth,
        theme=theme,
        sisend=sisend,
        org_id=org_id,
        scope=scope,
    )


# ---------------------------------------------------------------------------
# A5 — Otsi sarnaseid sätteid (similarity workflow)
# ---------------------------------------------------------------------------
#
# Hybrid from v1 (per the user decision 2026-05-15 — plan section 5, A5):
# the ontology-declared similarity track + the same-topic-cluster track +
# the embedding cosine track, merged + de-duplicated with "why this
# matched" badges. The route resolves the user's input the same way as
# Normi mõjuahel (rule-based parser → ReferenceResolver), then:
#
#   * resolved entity → ontology tracks seeded on the URI, embedding track
#     seeded on the resolved label (NOT the user's raw query — keeps the
#     embedding side deterministic for short §-refs);
#   * nothing resolved → ontology tracks empty, embedding track seeded on
#     the raw query text (free-text path). Privacy: query text is never
#     persisted (see ``similarity.py`` module docstring).
#
# All five result-shell cards use the same Estonian legal vocabulary as
# Normi mõjuahel / Sanktsioonide indeks; no SPARQL / RDF / embedding
# language anywhere on the page.

_WORKFLOW_SIMILARITY = "sarnasus"
_WORKFLOW_ACTION[_WORKFLOW_SIMILARITY] = "/analyysikeskus/sarnasus"


def _similarity_link(sisend: str) -> str:
    """Build a ``/analyysikeskus/sarnasus?sisend=…`` link."""
    from urllib.parse import urlencode

    return f"/analyysikeskus/sarnasus?{urlencode([('sisend', sisend)])}"


# Map the merge layer's reason codes to Badge variants. Ontology-declared
# wins "success" because it's the most authoritative signal; cluster is
# "primary" (membership, not a numeric match — gets the brand colour);
# embedding is "warning" because it's the soft, statistical signal that
# may surprise the reader. Variants are visual cues only — the Estonian
# labels live in :mod:`similarity`.
_SIMILARITY_REASON_VARIANTS: dict[str, BadgeVariant] = {
    "ontology_declared": "success",
    "same_cluster": "primary",
    "embedding_cosine": "warning",
}


def _similarity_reason_badges(reasons: tuple[str, ...] | list[str]) -> Any:
    """Render the row's reason codes as inline Badges (Estonian labels)."""
    from app.analyysikeskus.similarity import REASON_LABELS_ET

    bits: list[Any] = []
    for code in reasons or []:
        label = REASON_LABELS_ET.get(code)
        if not label:
            continue
        variant: BadgeVariant = _SIMILARITY_REASON_VARIANTS.get(code, "default")
        bits.append(Badge(label, variant=variant))
        bits.append(" ")
    return Span(*bits) if bits else Span("—")  # noqa: F405


def _similarity_row_link(row: SimilarityRow) -> Any:
    """Render a similarity row's label as an Õiguskaart deep link."""
    label = row.label or row.entity_uri.rsplit("#", 1)[-1] or "—"
    if not row.entity_uri:
        return Span(label)  # noqa: F405
    return A(label, href=explorer_focus_url(row.entity_uri), cls="data-table-link")  # noqa: F405


def _similarity_results_block(rows: list[SimilarityRow]) -> list[Any]:
    """Assemble the ``Tulemused`` content for the sarnasus workflow."""
    if not rows:
        return [
            P(  # noqa: F405
                "Sarnaseid sätteid ei leitud.",
                cls="muted-text",
            )
        ]

    lead = P(  # noqa: F405
        Strong(f"{len(rows)} sarnast üksust"),  # noqa: F405
        " — järjestatud sarnasuse skoori järgi (kõrgemad eespool).",
    )

    columns = [
        Column(
            key="entity",
            label="Üksus",
            sortable=False,
            render=lambda r: _similarity_row_link(r["_row"]),
        ),
        Column(
            key="reasons",
            label="Miks see sobib",
            sortable=False,
            render=lambda r: _similarity_reason_badges(r["_row"].reasons),
        ),
        Column(
            key="score",
            label="Skoor",
            sortable=False,
            render=lambda r: Span(f"{r['_row'].score:.2f}", cls="muted-text"),  # noqa: F405
        ),
    ]
    table_rows = [{"_row": r} for r in rows[:_MAX_RESULT_ROWS]]
    return [
        lead,
        DataTable(
            columns=columns,
            rows=table_rows,
            empty_message="Sarnaseid sätteid ei leitud.",
        ),
    ]


def _similarity_evidence_block(rows: list[SimilarityRow]) -> list[Any]:
    """Assemble the ``Tõendid`` rows for the sarnasus workflow.

    Each row carries the matched entity's label, the legal-language
    "miks see sobib" badges as text, an "Ava õiguskaardil" deep link,
    and a "Küsi nõustajalt" seed form. The snippet (when present from
    the embedding track) appears as a muted sub-line.
    """
    if not rows:
        return []
    out: list[Any] = []
    for r in rows:
        reasons_et = ", ".join(reason_labels_et(r.reasons)) or "sarnane"
        why = (
            "See säte sarnaneb teie sisendiga — kasutage võrdluseks, "
            "et oma sõnastust vajadusel kohandada."
        )
        out.append(
            _evidence_row(
                source_label=r.label or r.entity_uri.rsplit("#", 1)[-1] or "Üksus",
                relation=f"sarnane ({reasons_et})",
                target_label="",
                uri=r.entity_uri,
                why=why,
                snippet=r.snippet,
                draft_id=None,
            )
        )
    return out


def _similarity_actions(focus_uri: str | None) -> list[dict[str, str]]:
    """The ``Soovitatud tegevused`` action set for a sarnasus result page."""
    actions: list[dict[str, str]] = []
    if focus_uri:
        actions.append({"label": "Ava õiguskaardil", "href": explorer_focus_url(focus_uri)})
    actions.append({"label": "Küsi nõustajalt", "href": "/chat/new"})
    actions.append({"label": "Tagasi analüüsikeskusesse", "href": "/analyysikeskus"})
    return actions


def _render_similarity_landing(
    *,
    auth: Any,
    theme: str,
) -> Any:
    """Render the workflow shell with no input yet — the empty-form landing."""
    landing_input = Div(  # noqa: F405
        P(  # noqa: F405
            "Sisestage säte, akt, CELEX-number või vabas vormis tekst — "
            "kuvame sõnastuselt või sisult sarnased sätted teistes aktides."
        ),
        Form(  # noqa: F405
            Input(
                "sisend",
                type="text",
                placeholder=("Nt: AvTS § 35 · CELEX-number · või kirjeldage sätte sisu"),
                aria_label="Õiguslik viide või vaba tekst",
                cls="analyysikeskus-input",
            ),
            Button("Otsi sarnaseid sätteid", type="submit", variant="primary"),
            method="get",
            action="/analyysikeskus/sarnasus",
            cls="analyysikeskus-workflow-form",
        ),
        Small(  # noqa: F405
            "Näited: «AvTS § 35» · «menetlustähtaegade pikendamine»",
            cls="muted-text",
        ),
    )
    return analysis_result_shell(
        workflow_title="Otsi sarnaseid sätteid",
        input_summary=landing_input,
        results_block=P(  # noqa: F405
            "Sisestage päring, et näha sarnaseid sätteid.",
            cls="muted-text",
        ),
        evidence_block=_missing_row("—"),
        actions=[
            {"label": "Küsi nõustajalt", "href": "/chat/new"},
            {"label": "Tagasi analüüsikeskusesse", "href": "/analyysikeskus"},
        ],
        user=auth,
        theme=theme,
        scope_block=Span(  # noqa: F405
            "Sisestage päring, et muuta ulatust.", cls="muted-text"
        ),
    )


def _render_similarity_result(
    *,
    auth: Any,
    theme: str,
    sisend: str,
    seed_uri: str | None,
    seed_label: str | None,
    rows: list[SimilarityRow],
) -> Any:
    """Render the result page for a similarity query.

    Args:
        sisend: The raw user input — for the ``Sisend`` card echo.
        seed_uri: The resolved entity URI when a structured ref was
            recognised; ``None`` for the free-text path.
        seed_label: The resolved label when *seed_uri* is set.
        rows: Merged similarity candidates from
            :func:`app.analyysikeskus.similarity.find_similar`.
    """
    if seed_uri and seed_label:
        input_summary: Any = P(  # noqa: F405
            "Analüüsisin: ",
            Strong(seed_label),  # noqa: F405
            " — otsin sarnaseid sätteid kolmest allikast (ontoloogia, temaatika, sõnastus).",
        )
    else:
        input_summary = P(  # noqa: F405
            "Analüüsisin vaba teksti: «",
            Strong(sisend),  # noqa: F405
            "» — otsin sarnaseid sätteid sõnastuse alusel.",
        )

    results_block = _similarity_results_block(rows)
    evidence_rows = _similarity_evidence_block(rows)
    evidence_block: Any = evidence_rows if evidence_rows else _missing_row("Tõendeid ei leitud.")
    actions = _similarity_actions(focus_uri=seed_uri)

    return analysis_result_shell(
        workflow_title="Otsi sarnaseid sätteid",
        input_summary=input_summary,
        results_block=results_block,
        evidence_block=evidence_block,
        actions=actions,
        user=auth,
        theme=theme,
        scope_block=Span(  # noqa: F405
            "Tulemused ühendavad ontoloogia, temaatika ja sõnastuse "
            "sarnasuse — ulatuse seaded on tulekul.",
            cls="muted-text",
        ),
    )


def _render_similarity_disambiguation(
    *,
    auth: Any,
    theme: str,
    sisend: str,
    resolved: list[Any],
) -> Any:
    """Render disambiguation links when multiple entities resolved."""
    items: list[Any] = []
    for r in resolved:
        label = _resolved_label(r, sisend)
        extracted = getattr(r, "extracted", None)
        ref_text = str(getattr(extracted, "ref_text", "") or label)
        items.append(Li(A(label, href=_similarity_link(ref_text))))  # noqa: F405

    results_block: list[Any] = [
        Alert("Sisend võib viidata mitmele üksusele. Vali, millist analüüsida:", variant="info"),
    ]
    if items:
        results_block.append(Ul(*items, cls="analyysikeskus-candidates"))  # noqa: F405

    return analysis_result_shell(
        workflow_title="Otsi sarnaseid sätteid",
        input_summary=P(f"Sisestasite: «{sisend}»"),  # noqa: F405
        results_block=results_block,
        evidence_block=_missing_row("—"),
        actions=[
            {"label": "Küsi nõustajalt", "href": "/chat/new"},
            {"label": "Tagasi analüüsikeskusesse", "href": "/analyysikeskus"},
        ],
        user=auth,
        theme=theme,
        scope_block=Span(  # noqa: F405
            "Tulemused ühendavad ontoloogia, temaatika ja sõnastuse "
            "sarnasuse — ulatuse seaded on tulekul.",
            cls="muted-text",
        ),
    )


def sarnasus_page(req: Request):
    """GET /analyysikeskus/sarnasus?sisend=<text> — Otsi sarnaseid sätteid (A5a).

    Flow:

    1. Blank ``sisend`` → render the workflow shell with the input form.
    2. Parse ``sisend`` → resolve via :class:`ReferenceResolver`:
       * exactly one resolved entity → run the hybrid similarity engine
         (ontology + cluster + embedding) seeded on the URI + the
         resolved label. Render the merged + de-duplicated result with
         "why this matched" badges.
       * multiple plausible resolutions → render disambiguation links.
       * nothing resolved (free-text path) → run the embedding track on
         the raw input; if it returns anything, render those rows; else
         render the friendly "no structured ref" fallback.

    **Privacy:** the raw input is never persisted by this route. The
    embedding call delegates to :class:`VoyageProvider` (subject to the
    project's approved data-processing controls) and the cosine search
    is scoped to the public corpus (``org_id IS NULL``). See
    :mod:`app.analyysikeskus.similarity` for the full privacy posture.
    """
    auth = req.scope.get("auth") or None
    theme = get_theme_from_request(req)

    sisend = (req.query_params.get("sisend") or "").strip()
    if not sisend:
        return _render_similarity_landing(auth=auth, theme=theme)

    parsed_refs = parse_user_reference(sisend)
    resolved = _resolve_refs(parsed_refs)
    resolved_with_uri = [
        r for r in resolved if getattr(r, "entity_uri", None) and str(r.entity_uri).strip()
    ]
    # Deduplicate by URI — the resolver may return both a precise
    # provision ref and a fallback law ref pointing to the same URI.
    seen: set[str] = set()
    unique_resolved: list[Any] = []
    for r in resolved_with_uri:
        uri = str(r.entity_uri)
        if uri in seen:
            continue
        seen.add(uri)
        unique_resolved.append(r)

    if len(unique_resolved) > 1:
        return _render_similarity_disambiguation(
            auth=auth, theme=theme, sisend=sisend, resolved=unique_resolved
        )

    if len(unique_resolved) == 1:
        resolved_one = unique_resolved[0]
        seed_uri = str(resolved_one.entity_uri)
        seed_label = _resolved_label(resolved_one, sisend)
        # Embedding query: use the resolved label, not the raw sisend.
        # Short §-refs like "AvTS § 35" don't carry semantic content; the
        # resolved label ("Avaliku teabe seadus § 35 — …") does. The
        # SPARQL tracks already use the URI, so the embedding side is the
        # only one that benefits from richer text.
        rows = find_similar(seed_uri=seed_uri, query_text=seed_label)
        return _render_similarity_result(
            auth=auth,
            theme=theme,
            sisend=sisend,
            seed_uri=seed_uri,
            seed_label=seed_label,
            rows=rows,
        )

    # Free-text path — only the embedding track has signal. Privacy
    # check: the raw text is sent to VoyageProvider (vendor-processed)
    # but never persisted by this route. See similarity.py for the
    # canonical posture.
    rows = find_similar(query_text=sisend)
    if rows:
        return _render_similarity_result(
            auth=auth,
            theme=theme,
            sisend=sisend,
            seed_uri=None,
            seed_label=None,
            rows=rows,
        )

    # Nothing resolved, nothing embedded — friendly fallback.
    warning = Alert(
        "Ei tuvastanud õiguslikku viidet ega leidnud sarnast sisu. "
        "Proovige nt «AvTS § 35», CELEX-numbrit (32016R0679) või akti "
        "lühinime.",
        variant="warning",
    )
    return analysis_result_shell(
        workflow_title="Otsi sarnaseid sätteid",
        input_summary=P(f"Sisestasite: «{sisend}»"),  # noqa: F405
        results_block=[warning],
        evidence_block=_missing_row("—"),
        actions=[
            {"label": "Küsi nõustajalt", "href": "/chat/new"},
            {"label": "Tagasi analüüsikeskusesse", "href": "/analyysikeskus"},
        ],
        user=auth,
        theme=theme,
        scope_block=Span(  # noqa: F405
            "Tulemused ühendavad ontoloogia, temaatika ja sõnastuse "
            "sarnasuse — ulatuse seaded on tulekul.",
            cls="muted-text",
        ),
    )


def register_analyysikeskus_routes(rt) -> None:  # type: ignore[no-untyped-def]
    """Register Analüüsikeskus routes on the FastHTML route decorator *rt*."""
    rt("/analyysikeskus", methods=["GET"])(analyysikeskus_page)
    rt("/analyysikeskus/normi-mojuahel", methods=["GET"])(normi_mojuahel_page)
    rt("/analyysikeskus/el-ulevott", methods=["GET"])(el_ulevott_page)
    rt("/analyysikeskus/sanktsioonid", methods=["GET"])(sanktsioonid_page)
    rt("/analyysikeskus/kohtupraktika", methods=["GET"])(kohtupraktika_page)
    rt("/analyysikeskus/halduskoormus", methods=["GET"])(halduskoormus_page)
    rt("/analyysikeskus/padevused", methods=["GET"])(padevused_page)
    rt("/analyysikeskus/ajalugu", methods=["GET"])(ajalugu_page)
    rt("/analyysikeskus/sarnasus", methods=["GET"])(sarnasus_page)
