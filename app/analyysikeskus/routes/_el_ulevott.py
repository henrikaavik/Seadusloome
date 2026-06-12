"""EL ülevõtt ja harmoneerimine — ``GET /analyysikeskus/el-ulevott`` (#723, #860).

The route is a thin adapter: it delegates all orchestration to
:func:`app.analyysikeskus.services.el_ulevott.analyse_el_ulevott` (the
framework-free service) and only renders the typed result. The service
resolves the user\'s input to one ``estleg:EULegislation`` URI (a CELEX via
:class:`app.docs.reference_resolver.ReferenceResolver`, or a label search via
:func:`app.analyysikeskus.eu_lookup.search_eu_acts_by_label`), then runs an
act/provision-level transposition query
(:func:`app.impact.eu_transposition.run_eu_transposition`). The route branches
on the returned dataclass (``ElTranspositionResult`` / ``ElUlevottDisambiguation``
/ ``ElUlevottUnresolved``) and renders the transposition table + risk band
through the shared result shell.

Patch where used (post-#860), e.g.::

  # service boundary, as seen from the route
  patch("app.analyysikeskus.routes._el_ulevott.analyse_el_ulevott")
  # orchestration deps — now used inside the service, so patch them there
  patch("app.analyysikeskus.services.el_ulevott.run_eu_transposition")
  patch("app.analyysikeskus.services.el_ulevott.search_eu_acts_by_label")
"""

from __future__ import annotations

import logging
from typing import Any

from fasthtml.common import *  # noqa: F403
from starlette.requests import Request
from starlette.responses import RedirectResponse

from app.analyysikeskus.result_shell import analysis_result_shell
from app.analyysikeskus.routes._common import (
    _MAX_RESULT_ROWS,
    _WORKFLOW_EL,
    _evidence_row,
    _missing_row,
    _Scope,
    _scope_form,
    _sub_section,
    _workflow_link,
)
from app.analyysikeskus.services.el_ulevott import (
    ElTranspositionResult,
    ElUlevottDisambiguation,
    analyse_el_ulevott,
)
from app.docs.report_routes import explorer_focus_url
from app.ui.data.data_table import Column, DataTable
from app.ui.primitives.badge import Badge, BadgeVariant
from app.ui.primitives.input import Checkbox
from app.ui.surfaces.alert import Alert
from app.ui.theme import get_theme_from_request

logger = logging.getLogger(__name__)


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


_TRANSPOSITION_STATUS_BADGE: dict[str, BadgeVariant] = {
    "kaetud": "success",
    "osaline": "warning",
    "puudub": "danger",
    "ebaselge": "default",
}


_TRANSPOSITION_STATUS_LABEL_ET: dict[str, str] = {
    "kaetud": "Kaetud",
    "osaline": "Osaline",
    "puudub": "Puudub",
    "ebaselge": "Ebaselge",
}


_TRANSPOSITION_STATUS_ACTION_ET: dict[str, str] = {
    "kaetud": "—",
    "osaline": "Täpsusta ülevõttu",
    "puudub": "Lisa puuduv säte",
    "ebaselge": "Kontrolli ülevõtu staatust",
}


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
    :mod:`app.impact.eu_transposition`), and shipping a column of
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
    rows: list[dict[str, Any]],
    sisend: str,
    scope: _Scope,
) -> Any:
    """Render the EL ülevõtt result page for a resolved EU act + its rows.

    The transposition *rows* were already fetched (entity-centred, no
    synthetic graph) by the service's
    :func:`app.impact.eu_transposition.run_eu_transposition` call. A dead Jena
    ⇒ ``rows`` is empty ⇒ the ``Tulemused`` block shows a graceful "ei
    õnnestunud" line rather than a 500.
    """
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
    canonical_celex_shape: bool,
) -> Any:
    """Render the "Ei tuvastanud EL õigusakti" warning page.

    Two message variants (#805), selected by *canonical_celex_shape* (computed
    by the service):

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
    if canonical_celex_shape:
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


def el_ulevott_page(req: Request):
    """GET /analyysikeskus/el-ulevott?sisend=<text> — EL ülevõtt ja harmoneerimine (#723).

    Thin route over :func:`app.analyysikeskus.services.el_ulevott.analyse_el_ulevott`
    (the framework-free Phase-5 service): parse the request, call the service,
    render the typed result.

    1. Blank ``sisend`` → 303 back to ``/analyysikeskus``.
    2. The service returns one of three typed outcomes — a transposition
       result (resolved act + rows), a disambiguation (several label hits), or
       unresolved — and this route maps each to its ``analysis_result_shell``
       render branch.

    A dead Jena anywhere ⇒ the service returns empty ``rows`` / unresolved, and
    this route shows a graceful message inside the result shell, never a 500.
    """
    auth = req.scope.get("auth") or None
    theme = get_theme_from_request(req)

    sisend = (req.query_params.get("sisend") or "").strip()
    if not sisend:
        return RedirectResponse(url="/analyysikeskus", status_code=303)

    scope = _Scope(req.query_params, workflow=_WORKFLOW_EL)

    result = analyse_el_ulevott(sisend)

    if isinstance(result, ElTranspositionResult):
        return _render_eu_transposition_result(
            auth=auth,
            theme=theme,
            eu_label=result.eu_label,
            celex=result.celex,
            rows=result.rows,
            eu_act_uri=result.eu_act_uri,
            sisend=sisend,
            scope=scope,
        )

    if isinstance(result, ElUlevottDisambiguation):
        return _render_eu_disambiguation(
            auth=auth,
            theme=theme,
            candidates=[
                {"uri": c.uri, "label": c.label, "celex": c.celex or ""} for c in result.candidates
            ],
            sisend=sisend,
            scope=scope,
        )

    # ElUlevottUnresolved — the message variant is chosen from the flag.
    return _render_eu_unresolved(
        auth=auth,
        theme=theme,
        sisend=sisend,
        scope=scope,
        canonical_celex_shape=result.canonical_celex_shape,
    )
