"""Cross-workflow helpers for the Analüüsikeskus routes package (#860).

Pure-ish helpers shared by **two or more** of the per-workflow submodules
extracted from the former ``app/analyysikeskus/routes.py`` monolith:

* :class:`_Scope` — the ``Ulatus`` scope parsed off the GET query string,
  with workflow-specific checkbox defaults (Normi vs EL ülevõtt).
* Type / label helpers — ``_type_localname`` / ``_type_label`` /
  ``_entity_display_label`` / ``_entity_link`` / ``_split_link_query`` /
  ``_workflow_link``.
* The shared ``Ulatus`` scope-form builder ``_scope_form`` +
  ``_temporal_scope_select`` (#850) and its option tables.
* The **impact-result rendering machinery** — ``_build_results_block`` +
  its ``Tulemused`` sub-sections + the ``Tõendid`` builders
  (``_build_evidence_block`` / ``_evidence_row`` / …). Used verbatim by
  both Normi mõjuahel (``_normi``) and the policy-intent flow
  (``_intent``), so it lives here rather than in either workflow.
* Reference-resolution helpers — ``_resolve_refs`` / ``_select_dispatchable``
  / ``_partial_act_title`` / ``_resolved_label`` / ``_rag_candidates`` / …
  — shared by every reference-driven workflow (Normi, Sanktsioonid,
  Kohtupraktika, Halduskoormus, Ajalugu, Sarnasus).

The package ``__init__`` re-exports each public name so existing direct
imports (``from app.analyysikeskus.routes import _Scope``) keep working.

**Patch-path caveat (post-#860, same contract as the docs/routes split):**
``patch("app.analyysikeskus.routes.X")`` only rebinds the package-level
alias — NOT the bindings inside submodules that imported the symbol at
module load time. To intercept a dependency used here, patch where it is
USED, e.g.::

  patch("app.analyysikeskus.routes._common._rag_candidates")
  patch("app.analyysikeskus.routes._common._build_results_block")

Pinned by ``tests/test_analyysikeskus_routes_patch_paths.py``.
"""

from __future__ import annotations

import logging
import re
import uuid
from typing import Any

from fasthtml.common import *  # noqa: F403

from app.docs.entity_extractor import ExtractedRef
from app.docs.labels import TYPE_LABELS_ET as _TYPE_LABELS_ET
from app.docs.reference_resolver import ReferenceResolver
from app.docs.report_routes import explorer_focus_url
from app.impact.analyzer import ImpactFindings
from app.impact.scoring import ImpactBand, impact_band
from app.ontology.temporal_scope import TemporalScope, scope_from_param
from app.ui.data.data_table import Column, DataTable
from app.ui.primitives.badge import Badge, BadgeVariant  # noqa: E402  (re-import after wildcard)
from app.ui.primitives.button import Button  # noqa: E402  (re-import after wildcard)
from app.ui.primitives.input import Checkbox, Input, Select

logger = logging.getLogger(__name__)


_MAX_RECENT_ANALYSES = 10


_MAX_RAG_CANDIDATES = 5


_MAX_RESULT_ROWS = 30


_DRAFT_TYPE_LOCALNAMES = frozenset({"DraftLegislation", "DraftingIntent"})
_COURT_TYPE_LOCALNAMES = frozenset({"CourtDecision", "EUCourtDecision"})


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


_WORKFLOW_NORMI = "normi"
_WORKFLOW_EL = "el_ulevott"

_WORKFLOW_ACTION: dict[str, str] = {
    _WORKFLOW_NORMI: "/analyysikeskus/normi-mojuahel",
    _WORKFLOW_EL: "/analyysikeskus/el-ulevott",
}


_CELEX_TOKEN_RE = re.compile(r"^\d{5}[A-Z]\d{1,4}$", re.IGNORECASE)


class _Scope:
    """Parsed ``Ulatus`` scope from the GET query params.

    ``oigus`` is the temporal-scope token: ``"current"`` (*kehtiv õigus*,
    the default) vs ``"all"`` (*kogu ajalugu*). Since #850 it is **wired
    end-to-end** — :attr:`temporal_scope` maps it to a
    :class:`~app.ontology.temporal_scope.TemporalScope`, the Sanktsioonid
    / Halduskoormus / Pädevused / Kohtupraktika handlers pass that scope
    to their engine calls, and :func:`_scope_form` reflects the URL state
    in the select. The boolean flags are workflow-specific:

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
        # Canonicalise the temporal-scope token through the helper so the
        # form select + carried links always reflect a known value
        # (``"current"`` / ``"all"``) even when the URL carried a legacy
        # alias (``"kogu_ajalugu"`` / ``"current_plus_history"``). #850.
        self.oigus = scope_from_param(params.get("oigus")).value
        self.ajavahemik_algus = params.get("ajavahemik_algus") or ""
        self.ajavahemik_lopp = params.get("ajavahemik_lopp") or ""

    @property
    def temporal_scope(self) -> TemporalScope:
        """Map the ``?oigus=`` token to a :class:`TemporalScope` (#850).

        ``"current"`` → :attr:`TemporalScope.CURRENT` (kehtiv õigus,
        default); ``"all"`` / legacy history aliases →
        :attr:`TemporalScope.ALL` (kogu ajalugu). The engines splice this
        into their SPARQL so a positively-repealed provision is excluded
        under the current-law default and kept under the all-history
        scope.
        """
        return scope_from_param(self.oigus)

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


_LAW_SCOPE_OPTIONS: list[tuple[str, str]] = [
    ("current", "Kehtiv õigus"),
    ("current_plus_history", "Kehtiv + varasemad redaktsioonid (tulekul)"),
]

# #850: the *enabled* temporal-scope options for the four engines that
# honour the scope end-to-end (Sanktsioonid / Halduskoormus / Pädevused /
# Kohtupraktika). "Kehtiv õigus" excludes positively-repealed provisions;
# "Kogu ajalugu" includes them. The option values are the canonical
# ``TemporalScope`` tokens so the form round-trips through ``?oigus=``.
_TEMPORAL_SCOPE_OPTIONS: list[tuple[str, str]] = [
    (TemporalScope.CURRENT.value, "Kehtiv õigus"),
    (TemporalScope.ALL.value, "Kogu ajalugu"),
]


def _temporal_scope_select(oigus: str) -> Any:
    """Render the enabled ``Õigus`` (temporal-scope) field reflecting the URL.

    Shared by the four scope-aware workflow forms (#850). The select's
    value mirrors the request's ``?oigus=`` token (canonicalised by
    :class:`_Scope` to ``"current"`` / ``"all"``) so a reload preserves
    the user's choice, and the field name ``oigus`` round-trips the
    selection straight back into the workflow URL. The default option,
    *Kehtiv õigus*, excludes provisions whose owning act is positively
    marked repealed; *Kogu ajalugu* includes the full history.
    """
    value = oigus if oigus in {opt[0] for opt in _TEMPORAL_SCOPE_OPTIONS} else "current"
    return Div(  # noqa: F405
        Label("Õigus", fr="analyysikeskus-scope-law"),  # noqa: F405
        Select(
            "oigus",
            _TEMPORAL_SCOPE_OPTIONS,
            value=value,
            id="analyysikeskus-scope-law",
        ),
        Small(  # noqa: F405
            "“Kehtiv õigus” jätab välja kehtetuks tunnistatud sätted; "
            "“Kogu ajalugu” kaasab ka varasema õiguse.",
            cls="muted-text",
        ),
        cls="form-field",
    )


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
                feature="analyysikeskus_normi_mojuahel",
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


def _is_provision_resolved(resolved: Any) -> bool:
    """Return True when *resolved* came from a ``provision`` ExtractedRef.

    The :class:`ReferenceResolver` keeps the original
    :class:`ExtractedRef` on the resolved row; the ref_type field tells
    us whether the user input named a specific paragraph (``provision``)
    or only a law / act (``law`` / ``eu_act``). Shared by Sanktsioonid
    and Halduskoormus to branch between the ``…_for_provision`` and
    ``…_for_act`` engine helpers.
    """
    extracted = getattr(resolved, "extracted", None)
    if extracted is None:
        return False
    return getattr(extracted, "ref_type", "") == "provision"


def _sanctions_link(sisend: str, *, scope: _Scope | None = None) -> str:
    """Build a ``/analyysikeskus/sanktsioonid?sisend=…`` link (scope-carrying).

    Shared by Sanktsioonid (its own candidate / disambiguation links) and
    Kohtupraktika (the "Vaata sätte sanktsioone" cross-link in
    ``_court_practice_actions``). When *scope* is provided we reuse its
    ``query_pairs`` so the user's scope selection rides through. The
    sanctions-specific ``vordle_sarnaste_aktidega`` flag is **not** carried
    through ``_Scope`` (which only knows the Normi/EL scope vocabulary);
    callers that want to preserve it build their links inline.
    """
    from urllib.parse import urlencode

    if scope is not None:
        pairs = list(scope.query_pairs(sisend))
        return f"/analyysikeskus/sanktsioonid?{urlencode(pairs)}"
    return f"/analyysikeskus/sanktsioonid?{urlencode([('sisend', sisend)])}"
