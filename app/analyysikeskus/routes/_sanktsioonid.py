"""Sanktsioonide indeks — ``GET /analyysikeskus/sanktsioonid`` (#860).

Resolves the input to a provision/act and lists the sanctions attached,
with an optional "similar sanctions" comparison band. Renders through the
shared result shell.

Patch where used (post-#860), e.g.::

  patch("app.analyysikeskus.routes._sanktsioonid.list_sanctions_for_provision")
  patch("app.analyysikeskus.routes._sanktsioonid.find_similar_sanctions")
"""

from __future__ import annotations

import logging
from typing import Any

from fasthtml.common import *  # noqa: F403
from starlette.requests import Request

from app.analyysikeskus.input_parser import parse_user_reference
from app.analyysikeskus.result_shell import analysis_result_shell
from app.analyysikeskus.routes._common import (
    _MAX_RESULT_ROWS,
    _WORKFLOW_ACTION,
    _evidence_row,
    _is_provision_resolved,
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
from app.analyysikeskus.sanctions import (
    SanctionRow,
    find_similar_sanctions,
    list_sanctions_for_act,
    list_sanctions_for_provision,
    sanction_type_label,
    sanction_unit_label,
)
from app.docs.report_routes import explorer_focus_url
from app.ui.data.data_table import Column, DataTable
from app.ui.primitives.badge import Badge
from app.ui.primitives.button import Button
from app.ui.primitives.input import Checkbox
from app.ui.surfaces.alert import Alert
from app.ui.theme import get_theme_from_request

logger = logging.getLogger(__name__)


_WORKFLOW_SANCTIONS = "sanktsioonid"
_WORKFLOW_ACTION[_WORKFLOW_SANCTIONS] = "/analyysikeskus/sanktsioonid"


def _sanctions_scope_block(sisend: str, scope: _Scope, *, include_comparison: bool) -> Any:
    """The enabled ``Ulatus`` scope form for the Sanktsioonide indeks workflow.

    Legal-language only — the toggles read as "what to include in the
    sanctions index", never as query configuration. The
    ``vordle_sarnaste_aktidega`` checkbox is the only workflow-specific
    control (everything else mirrors Normi). When checked, the route
    also runs :func:`find_similar_sanctions` and renders the comparison
    section.
    """
    return Form(  # noqa: F405
        P(  # noqa: F405
            "Vaikimisi näitan ainult valitud sätte/akti sanktsioone. Märkige, "
            "et võrrelda ka sarnaste aktide sanktsioonidega.",
            cls="muted-text",
        ),
        Hidden(name="sisend", value=sisend),  # noqa: F405
        Hidden(name="ulatus_submitted", value="1"),  # noqa: F405
        # #850: temporal scope — kehtiv õigus (default) vs kogu ajalugu.
        _temporal_scope_select(scope.oigus),
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
    ts = scope.temporal_scope  # #850 — kehtiv õigus / kogu ajalugu

    if entity_uri and _is_provision_resolved(resolved):
        rows = list_sanctions_for_provision(entity_uri, scope=ts)
    elif partial_title is not None and not entity_uri:
        # Bare law input — Wave 2 Step 2 resolver returns
        # entity_uri=None, partial_match.act_title=<literal title>.
        # Route directly to the act-level helper with that title.
        rows = list_sanctions_for_act(partial_title, scope=ts)
    else:
        # The act join is on the ``estleg:sourceAct`` literal title in
        # prod (no act URIs exist on provisions — see the Wave 2 spike
        # in ``docs/2026-05-18-bugfix-plan.md``). The best title we have
        # for a resolved law ref is the human label the resolver
        # surfaced; pass that to the SPARQL helper.
        rows = list_sanctions_for_act(label, scope=ts)

    similar_rows: list[Any] | None = None
    if include_comparison and rows:
        # Seed on the first row — keeps the comparison deterministic.
        # Future iterations may surface a "pick which sanction to
        # compare" affordance for multi-sanction provisions.
        similar_rows = find_similar_sanctions(rows[0], limit=_MAX_RESULT_ROWS, scope=ts)

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
