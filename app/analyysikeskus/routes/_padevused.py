"""Pädevused — ``GET /analyysikeskus/padevused`` (#860).

Resolves an institution name and renders its competences grouped by act,
plus an overlaps section flagging competences shared with other
institutions. Renders through the shared result shell.

Patch where used (post-#860), e.g.::

  patch("app.analyysikeskus.routes._padevused.gather_institution_competences")
  patch("app.analyysikeskus.routes._padevused.search_institutions_by_label")
"""

from __future__ import annotations

import logging
from typing import Any

from fasthtml.common import *  # noqa: F403
from starlette.requests import Request

from app.analyysikeskus.competency import (
    InstitutionCompetences,
    gather_institution_competences,
    get_institution_label,
    is_estleg_institution_uri,
    search_institutions_by_label,
)
from app.analyysikeskus.result_shell import analysis_result_shell
from app.analyysikeskus.routes._common import (
    _MAX_RESULT_ROWS,
    _WORKFLOW_ACTION,
    _WORKFLOW_NORMI,
    _evidence_row,
    _missing_row,
    _Scope,
    _sub_section,
    _temporal_scope_select,
)
from app.docs.report_routes import explorer_focus_url
from app.ui.data.data_table import Column, DataTable
from app.ui.primitives.button import Button
from app.ui.surfaces.alert import Alert
from app.ui.theme import get_theme_from_request

logger = logging.getLogger(__name__)


_WORKFLOW_PADEVUSED = "padevused"
_WORKFLOW_ACTION[_WORKFLOW_PADEVUSED] = "/analyysikeskus/padevused"


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
    what it does **not** yet cover in v1). Since #850 the temporal-scope
    select (kehtiv õigus / kogu ajalugu) is active and the submit is
    enabled; pädevusala-grouping + gap analysis remain deferred (they
    need ontology CompetenceShape + competenceArea).
    """
    return Form(  # noqa: F405
        P(  # noqa: F405
            "Vaatan kõiki sätteid, mille puhul valitud asutus on määratud "
            "pädevaks asutuseks. Rühmitan need akti kaupa ja toon välja "
            "kattuvused teiste asutustega.",
            cls="muted-text",
        ),
        Hidden(name="sisend", value=sisend),  # noqa: F405
        Hidden(name="ulatus_submitted", value="1"),  # noqa: F405
        # #850: temporal scope — kehtiv õigus (default) excludes powers
        # vested by provisions of a repealed act.
        _temporal_scope_select(scope.oigus),
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
        scope=scope.temporal_scope,  # #850 — kehtiv õigus / kogu ajalugu
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
        # A2 (#844): only resolve URIs in the canonical estleg namespace.
        # A foreign URI (another org's draft graph, an arbitrary URL) must
        # never reach the institution-label lookup — short-circuit to the
        # "ei tuvastanud asutust" page without touching Jena.
        if not is_estleg_institution_uri(sisend):
            return _render_padevused_unresolved(auth=auth, theme=theme, sisend=sisend, scope=scope)
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
