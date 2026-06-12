"""Normi mõjuahel — ``GET /analyysikeskus/normi-mojuahel`` (#722, #860).

Resolves the user\'s free-text input to one ontology entity URI, runs the
impact analyser against an ephemeral synthetic named graph
(:func:`app.analyysikeskus.adhoc_analysis.run_adhoc_impact_analysis`), and
renders the findings through the shared result shell. A UUID matching an
owned draft with an ``impact_reports`` row short-circuits to that draft\'s
persisted report. The ``Tulemused`` / ``Tõendid`` rendering machinery is
shared with the policy-intent flow and lives in ``_common``.

Patch where used (post-#860), e.g.::

  patch("app.analyysikeskus.routes._normi.run_adhoc_impact_analysis")
  patch("app.analyysikeskus.routes._normi._load_owned_draft_report")
  patch("app.analyysikeskus.routes._normi._build_results_block")
"""

from __future__ import annotations

import logging
from typing import Any

from fasthtml.common import *  # noqa: F403
from starlette.requests import Request
from starlette.responses import RedirectResponse

from app.analyysikeskus.result_shell import analysis_result_shell
from app.analyysikeskus.routes._common import (
    _WORKFLOW_NORMI,
    _build_evidence_block,
    _build_results_block,
    _missing_row,
    _rag_candidates,
    _Scope,
    _scope_form,
    _workflow_link,
)
from app.analyysikeskus.services.normi_mojuahel import (
    NormiAdhocResult,
    NormiDisambiguation,
    NormiDraftBackedResult,
    analyse_normi_mojuahel,
)
from app.docs.report_routes import explorer_focus_url
from app.impact.analyzer import ImpactFindings
from app.ui.primitives.input import Checkbox
from app.ui.surfaces.alert import Alert
from app.ui.theme import get_theme_from_request

logger = logging.getLogger(__name__)


def _normi_link(sisend: str, *, scope: _Scope | None = None) -> str:
    """Build a ``/analyysikeskus/normi-mojuahel?sisend=…`` link (scope-carrying)."""
    return _workflow_link(sisend, workflow=_WORKFLOW_NORMI, scope=scope)


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


def normi_mojuahel_page(req: Request):
    """GET /analyysikeskus/normi-mojuahel?sisend=<text> — the Normi mõjuahel workflow.

    Thin route over :func:`app.analyysikeskus.services.normi_mojuahel.analyse_normi_mojuahel`
    (the framework-free Phase-5 service): parse the request, call the service,
    render the typed result. Flow (per the epic #714 design note):

    1. Blank ``sisend`` → 303 back to ``/analyysikeskus``.
    2. The service returns one of four typed outcomes — draft-backed report,
       single-entity ad-hoc analysis, disambiguation, or unresolved — and this
       route maps each to the matching ``analysis_result_shell`` render branch.

    The ephemeral synthetic graph is minted + analysed + **always deleted**
    inside the service's ``run_adhoc_impact_analysis`` call, so a render error
    here can never leave a graph behind.
    """
    auth = req.scope.get("auth") or None
    theme = get_theme_from_request(req)
    org_id = auth.get("org_id") if auth else None

    sisend = (req.query_params.get("sisend") or "").strip()
    if not sisend:
        return RedirectResponse(url="/analyysikeskus", status_code=303)

    scope = _Scope(req.query_params)

    result = analyse_normi_mojuahel(sisend, org_id=org_id)

    if isinstance(result, NormiDraftBackedResult):
        return _render_draft_backed_result(
            auth=auth,
            theme=theme,
            draft_id=result.draft_id,
            draft_title=result.draft_title,
            findings=result.findings,
            impact_score=result.score,
            sisend=sisend,
            scope=scope,
        )

    if isinstance(result, NormiAdhocResult):
        return _render_adhoc_result(
            auth=auth,
            theme=theme,
            entity_uri=result.entity_uri,
            label=result.label,
            type_label=result.type_label,
            findings=result.findings,
            score=result.score,
            sisend=sisend,
            scope=scope,
        )

    if isinstance(result, NormiDisambiguation):
        return _render_disambiguation(
            auth=auth,
            theme=theme,
            candidates=[{"label": c.label, "ref": c.ref} for c in result.candidates],
            sisend=sisend,
            scope=scope,
        )

    # NormiUnresolved — RAG candidates are a render-time nicety fetched here.
    return _render_unresolved(
        auth=auth,
        theme=theme,
        sisend=sisend,
        scope=scope,
        org_id=org_id,
    )


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
    *,
    auth: Any,
    theme: str,
    entity_uri: str,
    label: str,
    type_label: str,
    findings: ImpactFindings,
    score: int,
    sisend: str,
    scope: _Scope,
) -> Any:
    """Render the result page for a single resolved entity (ephemeral-graph path).

    The ephemeral synthetic graph was already minted + analysed + **deleted**
    by the service's :func:`run_adhoc_impact_analysis` call, so a render error
    here cannot leave a graph behind. This helper only lays the (already
    computed) findings out through :func:`analysis_result_shell`.
    """
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
    *,
    auth: Any,
    theme: str,
    draft_id: str,
    draft_title: str,
    findings: ImpactFindings,
    impact_score: int,
    sisend: str,
    scope: _Scope,
) -> Any:
    """Render the result page from a draft's persisted ``impact_reports`` row.

    No synthetic graph here — the *findings* come from the service, already
    rebuilt from the ``impact_reports`` row and masked against the viewer's
    org (#844). ``Lisa märkus`` is enabled (links to ``/drafts/{id}/report``
    where the row-annotation flow lives).
    """
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
    *,
    auth: Any,
    theme: str,
    candidates: list[dict[str, str]],
    sisend: str,
    scope: _Scope,
) -> Any:
    """Render a disambiguation page listing the plausible resolutions as links."""
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
