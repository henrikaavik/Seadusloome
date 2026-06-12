"""Kohtupraktika — ``GET /analyysikeskus/kohtupraktika`` (#860).

Resolves the input to a provision/act and groups the related Supreme Court /
EU court decisions by court, with year-trend summaries. Renders through the
shared result shell.

Patch where used (post-#860), e.g.::

  patch("app.analyysikeskus.routes._kohtupraktika.list_decisions_for_provision")
"""

from __future__ import annotations

import logging
from typing import Any

from fasthtml.common import *  # noqa: F403
from starlette.requests import Request

from app.analyysikeskus.court_practice import (
    CourtDecisionRow,
    CourtPracticeGroup,
    group_by_court,
    list_decisions_for_act,
    list_decisions_for_provision,
)
from app.analyysikeskus.input_parser import parse_user_reference
from app.analyysikeskus.result_shell import analysis_result_shell
from app.analyysikeskus.routes._common import (
    _MAX_RESULT_ROWS,
    _WORKFLOW_ACTION,
    _WORKFLOW_NORMI,
    _evidence_row,
    _missing_row,
    _partial_act_title,
    _rag_candidates,
    _resolve_refs,
    _resolved_label,
    _resolved_type_label,
    _sanctions_link,
    _Scope,
    _select_dispatchable,
    _sub_section,
    _temporal_scope_select,
)
from app.docs.report_routes import explorer_focus_url
from app.ui.primitives.button import Button
from app.ui.surfaces.alert import Alert
from app.ui.theme import get_theme_from_request

logger = logging.getLogger(__name__)


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
    return Form(  # noqa: F405
        P(  # noqa: F405
            "Vaatan kõiki kohtuid, mis on seda sätet või akti tõlgendanud. "
            "Tulemused on rühmitatud kohtu järgi.",
            cls="muted-text",
        ),
        Hidden(name="sisend", value=sisend),  # noqa: F405
        Hidden(name="ulatus_submitted", value="1"),  # noqa: F405
        # #850: temporal scope — kehtiv õigus (default) excludes court
        # practice that interprets provisions of a repealed act.
        _temporal_scope_select(scope.oigus),
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
    ts = scope.temporal_scope  # #850 — kehtiv õigus / kogu ajalugu

    if entity_uri and _is_court_practice_provision(resolved):
        rows = list_decisions_for_provision(entity_uri, scope=ts)
    elif partial_title is not None and not entity_uri:
        # Bare law input — the act-level helper accepts a literal title.
        rows = list_decisions_for_act(partial_title, scope=ts)
    else:
        rows = list_decisions_for_act(entity_uri, scope=ts)

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
