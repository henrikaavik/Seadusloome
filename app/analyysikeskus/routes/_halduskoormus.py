"""Halduskoormus — ``GET /analyysikeskus/halduskoormus`` (#860).

Resolves the input to an act/provision/draft and summarises the
administrative-burden obligations it carries (counts by duty holder + per-
bucket tables, and a draft-vs-current delta when the input is a draft).
Renders through the shared result shell.

Patch where used (post-#860), e.g.::

  patch("app.analyysikeskus.routes._halduskoormus.list_burden_for_provision")
"""

from __future__ import annotations

import logging
from typing import Any

from fasthtml.common import *  # noqa: F403
from starlette.requests import Request

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
from app.analyysikeskus.input_parser import parse_user_reference
from app.analyysikeskus.result_shell import analysis_result_shell
from app.analyysikeskus.routes._common import (
    _WORKFLOW_ACTION,
    _WORKFLOW_NORMI,
    _evidence_row,
    _is_provision_resolved,
    _missing_row,
    _partial_act_title,
    _rag_candidates,
    _resolve_refs,
    _resolved_label,
    _resolved_type_label,
    _Scope,
    _select_dispatchable,
    _sub_section,
    _temporal_scope_select,
)
from app.docs.report_routes import explorer_focus_url
from app.ui.data.data_table import Column, DataTable
from app.ui.primitives.button import Button
from app.ui.surfaces.alert import Alert
from app.ui.surfaces.card import Card, CardBody, CardHeader
from app.ui.theme import get_theme_from_request

logger = logging.getLogger(__name__)


_WORKFLOW_BURDEN = "halduskoormus"
_WORKFLOW_ACTION[_WORKFLOW_BURDEN] = "/analyysikeskus/halduskoormus"


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


def _burden_scope_block(sisend: str, scope: _Scope) -> Any:
    """The ``Ulatus`` form for Halduskoormus — temporal scope + intro (#850)."""
    return Form(  # noqa: F405
        P(  # noqa: F405
            "Vaikimisi loendatakse kõik sätte või akti deontilised liigitused. "
            "Sihtgrupi (kodanik / ettevõtja / avalik asutus) grupeering tuleb "
            "pärast ontoloogia muudatust #214.",
            cls="muted-text",
        ),
        Hidden(name="sisend", value=sisend),  # noqa: F405
        Hidden(name="ulatus_submitted", value="1"),  # noqa: F405
        # #850: temporal scope — kehtiv õigus (default) excludes the
        # deontic rows of provisions belonging to a repealed act.
        _temporal_scope_select(scope.oigus),
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
    scope: _Scope,
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

    The act/provision branches honour the ``?oigus=`` temporal scope
    (#850); the draft-delta branch is owned by the draft engine and is
    left on its existing behaviour.
    """
    entity_uri_raw = getattr(resolved, "entity_uri", None)
    entity_uri = str(entity_uri_raw) if entity_uri_raw else ""
    label = _resolved_label(resolved, sisend)
    type_label = _resolved_type_label(resolved)
    partial_title = _partial_act_title(resolved)
    ts = scope.temporal_scope

    if entity_uri and _is_provision_resolved(resolved):
        summary = list_burden_for_provision(entity_uri, scope=ts)
        results_block: Any = _burden_results_block(summary)
        evidence_summary = summary
    elif entity_uri and _is_draft_resolved(resolved):
        delta = burden_delta_for_draft(entity_uri)
        results_block = _burden_delta_block(delta)
        evidence_summary = delta.before
    elif partial_title is not None and not entity_uri:
        # Bare law input — pass the literal title to the act helper.
        summary = list_burden_for_act(partial_title, scope=ts)
        results_block = _burden_results_block(summary)
        evidence_summary = summary
    else:
        summary = list_burden_for_act(entity_uri, scope=ts)
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
        scope_block=_burden_scope_block(sisend, scope),
    )


def _render_burden_disambiguation(
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
        scope_block=_burden_scope_block(sisend, scope),
    )


def _render_burden_unresolved(
    *,
    auth: Any,
    theme: str,
    sisend: str,
    org_id: str | None,
    scope: _Scope,
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
        scope_block=_burden_scope_block(sisend, scope),
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
    # #850: parse the ``?oigus=`` temporal scope so the engine calls + the
    # scope form honour kehtiv õigus (default) vs kogu ajalugu.
    scope = _Scope(req.query_params, workflow=_WORKFLOW_NORMI)

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
            scope=scope,
        )

    if len(unique_resolved) > 1:
        return _render_burden_disambiguation(
            auth=auth,
            theme=theme,
            resolved=unique_resolved,
            sisend=sisend,
            scope=scope,
        )

    return _render_burden_unresolved(
        auth=auth,
        theme=theme,
        sisend=sisend,
        org_id=org_id,
        scope=scope,
    )
