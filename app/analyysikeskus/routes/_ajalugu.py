"""Ajalugu — ``GET /analyysikeskus/ajalugu`` (#860).

Resolves the input and renders the act/provision\'s legislative history
timeline (amendments, court interpretations, impact reports, pending
drafts), with an act-level-only V1 banner. Renders through the shared
result shell.

Patch where used (post-#860), e.g.::

  patch("app.analyysikeskus.routes._ajalugu.get_history_bundle")
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

from fasthtml.common import *  # noqa: F403
from starlette.requests import Request

from app.analyysikeskus.history import get_history_bundle, temporal_status_label
from app.analyysikeskus.input_parser import parse_user_reference
from app.analyysikeskus.result_shell import analysis_result_shell
from app.analyysikeskus.routes._common import (
    _WORKFLOW_ACTION,
    _WORKFLOW_NORMI,
    _evidence_row,
    _missing_row,
    _partial_act_title,
    _rag_candidates,
    _resolve_refs,
    _resolved_label,
    _resolved_type_label,
    _Scope,
    _select_dispatchable,
    _sub_section,
)
from app.docs.report_routes import explorer_focus_url
from app.ui.primitives.badge import Badge
from app.ui.primitives.button import Button
from app.ui.surfaces.alert import Alert
from app.ui.theme import get_theme_from_request

logger = logging.getLogger(__name__)


_WORKFLOW_AJALUGU = "ajalugu"
_WORKFLOW_ACTION[_WORKFLOW_AJALUGU] = "/analyysikeskus/ajalugu"


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
