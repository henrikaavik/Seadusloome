"""Mõju poliitikamõttest — policy-intent → impact flow (#814 Phase 2b, #860).

A three-step HTMX flow: an intake form (free-text policy intent + chip
selectors), an LLM-extracted candidate-confirmation panel
(``POST .../extract``), and an aggregated per-URI impact run
(``POST .../analyze``). Reuses the Normi result-section machinery from
``_common`` so the visual language is identical across workflows.

Routes: ``GET/POST /analyysikeskus/moju-poliitikamottest{,/extract,/analyze}``.

Patch where used (post-#860), e.g.::

  patch("app.analyysikeskus.routes._intent.extract_candidates")
  patch("app.analyysikeskus.routes._intent.run_aggregated_analysis")
"""

from __future__ import annotations

import logging
from typing import Any

from fasthtml.common import *  # noqa: F403
from starlette.requests import Request

from app.analyysikeskus.intent_analysis import (
    AggregatedResult,
    ResolvedCandidate,
    extract_candidates,
    prepare_intent_form_context,
    resolve_candidates,
    run_aggregated_analysis,
)
from app.analyysikeskus.intent_extractor import IntentCandidate
from app.analyysikeskus.result_shell import analysis_result_shell
from app.analyysikeskus.routes._common import (
    _build_results_block,
    _missing_row,
    _Scope,
)
from app.docs.report_routes import explorer_focus_url
from app.ui.primitives.badge import Badge
from app.ui.primitives.button import Button
from app.ui.primitives.input import Checkbox, Input, Textarea
from app.ui.surfaces.alert import Alert
from app.ui.surfaces.card import Card, CardBody, CardHeader
from app.ui.theme import get_theme_from_request

logger = logging.getLogger(__name__)


_INTENT_RESULT_REGION_ID = "moju-poliitikamottest-result"


_MAX_INTENT_CANDIDATES = 12


_INTENT_CONFIDENCE_PRE_CHECK = 0.7


_MAX_INTENT_LEN = 5000


_MAX_INTENT_KNOWN_REFS = 10


_MAX_INTENT_CONFIRMED_URIS = 10


def _intent_back_link() -> Any:
    """The shared "← Analüüsikeskus" back link rendered above the intake form."""
    return A(  # noqa: F405
        "← Analüüsikeskus",
        href="/analyysikeskus",
        cls="back-link",
    )


def _intent_chip_group(
    *,
    name: str,
    label: str,
    chips: tuple[str, ...] | list[str],
    selected: list[str] | None = None,
) -> Any:
    """Render a multi-select chip group as a fieldset of checkboxes.

    Each chip is a labelled checkbox; visually styled by CSS to read as a
    chip via the ``.chip-checkbox`` / ``.chip-group`` classes (already in
    the design system for tag-style multi-selects). Checkboxes are the
    accessible primitive — they announce "checked/not checked" to screen
    readers, ride through GET/POST form bodies without JS, and degrade
    cleanly when CSS fails to load.
    """
    selected_set = set(selected or [])
    items = [
        Checkbox(
            name=name,
            value=chip,
            checked=chip in selected_set,
            label=chip,
            cls="chip-checkbox",
        )
        for chip in chips
    ]
    return Fieldset(  # noqa: F405
        Legend(label),  # noqa: F405
        Div(*items, cls="chip-group"),  # noqa: F405
        cls="form-field",
    )


def _intent_intake_form(prefill: str = "") -> Any:
    """Render the policy-intent intake form (the Step-1 surface).

    ``prefill`` is the value to seed the intent textarea with — typically
    the capability example carried in via ``?sisend=`` (so the dashboard
    "Näide:" affordance + global-search row land the user on a populated
    form, not a blank one).
    """
    ctx = prepare_intent_form_context()
    return Form(  # noqa: F405
        # Intent textarea — required, multiline, captures the policy idea.
        Div(  # noqa: F405
            Label(  # noqa: F405
                "Mida soovid muuta või lisada?",
                fr="moju-poliitikamottest-intent",
            ),
            Textarea(
                "intent",
                value=prefill or None,
                placeholder=(
                    "Kirjelda poliitilist kavatsust vabas vormis. Nt: «Soovin "
                    "lihtsustada puudega inimese toetuse taotlemist nii, et "
                    "osa andmeid liiguks automaatselt Tervisekassast ja "
                    "Töötukassast.»"
                ),
                rows=6,
                required=True,
                id="moju-poliitikamottest-intent",
                aria_label="Poliitiline kavatsus vabas vormis",
            ),
            cls="form-field",
        ),
        # Scope-metadata chips below. These currently DO NOT influence
        # the LLM extractor — they're captured as user-visible context
        # and echoed back in the result page's Ulatus block so the user
        # has a record of their stated scope. Chip-driven extraction is
        # a future enhancement (would require passing them into
        # ``extract_candidates`` and tuning the prompt). The label copy
        # makes this expectation explicit so users don't think a chip
        # selection silently changed the suggestions.
        _intent_chip_group(
            name="target_groups",
            label="Sihtrühm (kuvatakse tulemustes — ei mõjuta kandidaatide otsingut)",
            chips=ctx.target_groups,
        ),
        _intent_chip_group(
            name="affected_areas",
            label=(
                "Mõjutatud valdkonnad (kuvatakse tulemustes — ei mõjuta kandidaatide otsingut)"
            ),
            chips=ctx.affected_areas,
        ),
        # Optional known-refs free-text input.
        Div(  # noqa: F405
            Label(  # noqa: F405
                "Teadaolevad õiguslikud viited (valikuline)",
                fr="moju-poliitikamottest-known-refs",
            ),
            Input(
                "known_refs",
                type="text",
                placeholder=(
                    "Kui tead juba mõnda sätet või seadust, lisa siia (nt AvTS § 35, KarS § 121)"
                ),
                id="moju-poliitikamottest-known-refs",
                aria_label="Teadaolevad õiguslikud viited",
            ),
            cls="form-field",
        ),
        Button(
            "Otsi mõjutatud sätteid",
            type="submit",
            variant="primary",
        ),
        method="post",
        action="/analyysikeskus/moju-poliitikamottest/extract",
        hx_post="/analyysikeskus/moju-poliitikamottest/extract",
        hx_target=f"#{_INTENT_RESULT_REGION_ID}",
        hx_swap="innerHTML",
        cls="moju-poliitikamottest-intake-form",
    )


def _intent_result_region(*children: Any) -> Any:
    """Wrap content in the HTMX target region the POST handlers swap into."""
    return Div(*children, id=_INTENT_RESULT_REGION_ID)  # noqa: F405


def _intent_intake_page(req: Request) -> Any:
    """Render the GET intake page (Step 1).

    Reuses :func:`analysis_result_shell` for the standard chrome but
    keeps the heavy lifting in two custom cards: the intake form sits in
    the ``Sisend`` block, and an empty ``#moju-poliitikamottest-result``
    div sits in the ``Tulemused`` block — both POST handlers swap their
    fragments into the latter so the user never leaves the page.

    Reads ``?sisend=`` from the query string and pre-fills the intent
    textarea — this is the same param the capability-card / global-
    search helpers already weave into deep-links for any
    ``/analyysikeskus/*`` workflow, so landing from the dashboard's
    "Näide:" affordance puts the example policy intent into the form
    automatically. Length-capped to ``_MAX_INTENT_LEN`` for parity with
    the POST handler.
    """
    auth = req.scope.get("auth") or None
    theme = get_theme_from_request(req)

    prefill = str(req.query_params.get("sisend") or "").strip()[:_MAX_INTENT_LEN]

    return analysis_result_shell(
        workflow_title="Analüüsi poliitikamõttest",
        input_summary=_intent_intake_form(prefill=prefill),
        results_block=_intent_result_region(
            P(  # noqa: F405
                "Esita ülal poliitiline kavatsus. Süsteem pakub välja "
                "kandidaadid mõjutatud õigusaktidest, mida saad kinnitada "
                "või eemaldada enne mõjuanalüüsi käivitamist.",
                cls="muted-text",
            )
        ),
        evidence_block=_missing_row("Tõendid ilmuvad pärast mõjuanalüüsi käivitamist."),
        actions=[
            {"label": "Tagasi analüüsikeskusesse", "href": "/analyysikeskus"},
        ],
        user=auth,
        theme=theme,
        scope_block=Span(  # noqa: F405
            "Ulatuse seaded ilmuvad pärast mõjuanalüüsi käivitamist.",
            cls="muted-text",
        ),
    )


async def moju_poliitikamottest_page(req: Request):
    """GET /analyysikeskus/moju-poliitikamottest — the intake form.

    Step 1 of the three-step intent → impact flow. Renders the Koostaja-
    style intake form (intent textarea + target-group/affected-area chip
    groups + optional known-refs input). The form posts to
    ``/extract``, which HTMX-swaps the candidate-confirmation panel into
    the ``#moju-poliitikamottest-result`` region without a page reload.
    """
    return _intent_intake_page(req)


def _intent_row_label(cand: IntentCandidate) -> str:
    """Estonian label for the candidate's ref_type chip."""
    return {
        "law": "seadus",
        "provision": "säte",
        "eu_act": "EL õigusakt",
        "court_decision": "kohtulahend",
        "concept": "õigusmõiste",
    }.get(cand.ref_type, cand.ref_type)


def _intent_candidate_row(
    rc: ResolvedCandidate,
    *,
    index: int,
) -> Any:
    """Render one row of the candidate-confirmation panel.

    The user toggles inclusion via a checkbox; multiple resolver matches
    surface as a radio-group (so the user picks which URI to analyse).
    A single resolver match becomes a hidden input. Fully-unresolved
    candidates show a muted "ei tuvastatud ontoloogias" badge — the
    user can still toggle them to keep them visible but the per-URI
    analyser cannot run on a None URI, so they're filtered out at
    submit time.
    """
    cand = rc.candidate
    resolved = rc.resolved
    pre_checked = cand.confidence >= _INTENT_CONFIDENCE_PRE_CHECK

    label = (resolved.matched_label or cand.ref_text).strip()
    type_label = _intent_row_label(cand)
    confidence_pct = int(round(cand.confidence * 100))

    # The URI(s) that this row could resolve to. The resolver returns one
    # URI per ExtractedRef today, so we treat it as a single hidden value
    # rather than a radio. Defensive: if entity_uri is missing, no hidden
    # input is rendered and the row is treated as unresolvable.
    uri = (resolved.entity_uri or "").strip()
    label_id = f"intent-candidate-{index}"

    uri_marker: Any
    if uri:
        # Carry the chosen URI through as a hidden input keyed on the
        # candidate's row index. The /analyze handler reads every
        # ``confirmed_uri`` value off the form.
        uri_marker = Hidden(name=f"uri_{index}", value=uri)  # noqa: F405
    else:
        uri_marker = Span(  # noqa: F405
            Badge(
                "Ei tuvastatud ontoloogias",
                variant="warning",
            ),
            cls="muted-text",
        )

    # Label snapshot — needed by /analyze so the result page can render
    # "Mõjuahel sätte X analüüsist" headings without re-resolving.
    label_marker = Hidden(  # noqa: F405
        name=f"label_{index}",
        value=label,
    )

    # The checkbox controls inclusion. Defensive ``value`` carries the
    # row index back so the handler knows which ``uri_N`` / ``label_N``
    # pair to read.
    checkbox = Checkbox(
        name="confirmed",
        value=str(index),
        checked=pre_checked and bool(uri),
        disabled=not uri,
        label=None,
    )

    return Div(  # noqa: F405
        Div(  # noqa: F405
            checkbox,
            cls="moju-poliitikamottest-candidate-checkbox",
        ),
        Div(  # noqa: F405
            P(  # noqa: F405
                Strong(label),  # noqa: F405
                " ",
                Badge(type_label, variant="default"),
                " ",
                Span(f"({confidence_pct}%)", cls="muted-text"),  # noqa: F405
            ),
            P(cand.reasoning or "—", cls="muted-text small-text"),  # noqa: F405
            uri_marker,
            label_marker,
            cls="moju-poliitikamottest-candidate-body",
        ),
        cls="moju-poliitikamottest-candidate-row",
        id=label_id,
    )


def _intent_chip_summary(*, target_groups: list[str], affected_areas: list[str]) -> Any:
    """Render a compact "applied chips" summary above the candidate list."""
    parts: list[Any] = []
    if target_groups:
        parts.append(
            P(  # noqa: F405
                Strong("Sihtrühm: "),  # noqa: F405
                ", ".join(target_groups),
            )
        )
    if affected_areas:
        parts.append(
            P(  # noqa: F405
                Strong("Valdkonnad: "),  # noqa: F405
                ", ".join(affected_areas),
            )
        )
    if not parts:
        return ""
    return Div(*parts, cls="moju-poliitikamottest-chip-summary")  # noqa: F405


def _intent_confirmation_panel(
    *,
    intent_text: str,
    target_groups: list[str],
    affected_areas: list[str],
    resolved: list[ResolvedCandidate],
) -> Any:
    """Render the candidate-confirmation panel (Step-2 surface)."""
    if not resolved:
        return Div(  # noqa: F405
            Alert(
                "Süsteem ei suutnud sellest kavatsusest kandidaate välja "
                "pakkuda. Proovige sõnastust täpsustada või lisada teadaolevaid "
                "viiteid (nt «AvTS § 35»).",
                variant="warning",
            ),
            A(  # noqa: F405
                "← Tagasi sisestuse juurde",
                href="/analyysikeskus/moju-poliitikamottest",
                cls="back-link",
            ),
        )

    # Cap rendered rows so a runaway LLM can't bloat the page.
    trimmed = resolved[:_MAX_INTENT_CANDIDATES]

    rows = [_intent_candidate_row(rc, index=i) for i, rc in enumerate(trimmed)]

    # Count how many rows are pre-checked + resolvable — drives the
    # initial submit-button label ("Käivita mõjuanalüüs ({N} kinnitatud
    # sätet)"). Live counts are JS work; this is the deterministic
    # server-side estimate.
    initial_checked = sum(
        1
        for rc in trimmed
        if rc.candidate.confidence >= _INTENT_CONFIDENCE_PRE_CHECK
        and (rc.resolved.entity_uri or "").strip()
    )

    return Div(  # noqa: F405
        H3(  # noqa: F405
            "Süsteem leidis järgmised kandidaadid",
            cls="card-title",
        ),
        P(  # noqa: F405
            "Kinnitage, eemaldage või lisage kandidaate. Iga kinnitatud säte saab oma mõjuahela.",
            cls="muted-text",
        ),
        _intent_chip_summary(
            target_groups=target_groups,
            affected_areas=affected_areas,
        ),
        Form(  # noqa: F405
            # Carry the intent text + chip selections through so the
            # /analyze handler can render the Sisend block honestly.
            Hidden(name="intent", value=intent_text),  # noqa: F405
            *[
                Hidden(name="target_groups", value=tg)  # noqa: F405
                for tg in target_groups
            ],
            *[
                Hidden(name="affected_areas", value=aa)  # noqa: F405
                for aa in affected_areas
            ],
            Div(*rows, cls="moju-poliitikamottest-candidate-list"),  # noqa: F405
            Button(
                f"Käivita mõjuanalüüs ({initial_checked} kinnitatud sätet)",
                type="submit",
                variant="primary",
            ),
            A(  # noqa: F405
                "Tagasi sisestuse juurde",
                href="/analyysikeskus/moju-poliitikamottest",
                cls="btn btn-secondary",
            ),
            method="post",
            action="/analyysikeskus/moju-poliitikamottest/analyze",
            hx_post="/analyysikeskus/moju-poliitikamottest/analyze",
            hx_target=f"#{_INTENT_RESULT_REGION_ID}",
            hx_swap="innerHTML",
            cls="moju-poliitikamottest-confirm-form",
        ),
    )


async def moju_poliitikamottest_extract(req: Request):
    """POST /analyysikeskus/moju-poliitikamottest/extract — Step 2.

    Reads the intake form, runs the semantic-inference extractor over
    the policy intent, resolves each candidate to a URI, and returns
    the candidate-confirmation panel as an HTMX fragment.
    """
    auth = req.scope.get("auth") or None
    user_id = auth.get("id") if auth else None
    org_id = auth.get("org_id") if auth else None

    try:
        form = await req.form()
    except Exception:
        logger.warning("moju_poliitikamottest_extract: failed to read form", exc_info=True)
        return Alert(
            "Vormi lugemine ebaõnnestus. Proovige uuesti.",
            variant="danger",
        )

    intent_text = str(form.get("intent") or "").strip()[:_MAX_INTENT_LEN]
    target_groups = [str(v) for v in form.getlist("target_groups")]
    affected_areas = [str(v) for v in form.getlist("affected_areas")]
    known_refs_raw = str(form.get("known_refs") or "").strip()

    # Empty intent → friendly validation, NO LLM call.
    if not intent_text:
        return Alert(
            "Palun sisesta poliitiline kavatsus enne kandidaatide otsimist.",
            variant="warning",
        )

    # Run the LLM-driven extractor (cost-tracked with
    # ``feature="intent_analysis"``).
    llm_candidates = extract_candidates(
        intent_text,
        user_id=user_id,
        org_id=org_id,
    )

    # Collect the user's manually entered known refs — split on commas,
    # treat each as a literal ``ref_text`` for the resolver. ``ref_type``
    # defaults to ``provision`` since most known-ref entries from the
    # usability tests are §-shaped; the resolver tolerates the guess (a
    # law name still resolves cleanly through ``_resolve_provision``
    # because the act-only branch fires on partial matches).
    #
    # Cap to ``_MAX_INTENT_KNOWN_REFS`` so a comma-bomb POST can't
    # trigger an unbounded number of resolver SPARQL lookups.
    manual_candidates: list[IntentCandidate] = []
    if known_refs_raw:
        for raw_ref in known_refs_raw.split(","):
            if len(manual_candidates) >= _MAX_INTENT_KNOWN_REFS:
                break
            ref_text = raw_ref.strip()
            if not ref_text:
                continue
            manual_candidates.append(
                IntentCandidate(
                    ref_text=ref_text,
                    ref_type="provision",
                    confidence=1.0,
                    reasoning="Kasutaja sisestatud käsitsi teadaolev viide.",
                )
            )

    # Merge with manual refs winning over LLM. A previous revision of
    # this handler appended manual refs after the LLM list and then
    # truncated to ``_MAX_INTENT_CANDIDATES`` from the front — which
    # silently dropped every manual ref whenever the LLM filled the cap
    # (#822 PR review P2). Explicit user input outranks inferred
    # candidates, so manual refs go in first; LLM rows then fill the
    # remaining slots in confidence order, highest first.
    candidates: list[IntentCandidate] = list(manual_candidates)
    llm_slots_remaining = _MAX_INTENT_CANDIDATES - len(candidates)
    if llm_slots_remaining > 0:
        sorted_llm = sorted(
            llm_candidates,
            key=lambda c: c.confidence,
            reverse=True,
        )
        candidates.extend(sorted_llm[:llm_slots_remaining])

    # Resolve every candidate to a URI (or ``None`` for unresolvable).
    resolved = resolve_candidates(candidates)

    return _intent_confirmation_panel(
        intent_text=intent_text,
        target_groups=target_groups,
        affected_areas=affected_areas,
        resolved=resolved,
    )


def _intent_result_input_summary(
    *,
    intent_text: str,
    confirmed_labels: list[str],
) -> Any:
    """Render the ``Sisend`` block content for the result page."""
    return Div(  # noqa: F405
        P(  # noqa: F405
            Strong("Poliitiline kavatsus: "),  # noqa: F405
            intent_text,
        ),
        P(  # noqa: F405
            Strong("Kinnitatud viited: "),  # noqa: F405
            ", ".join(confirmed_labels) if confirmed_labels else "—",
        ),
    )


def _intent_per_uri_section(per_uri: Any, *, scope: _Scope) -> Any:
    """Render one per-URI result group.

    The sub-heading carries the per-URI attribution ("Mõjuahel sätte X
    analüüsist") so the user can always trace a finding back to exactly
    one confirmed input. The underlying findings are laid out via the
    same :func:`_build_results_block` the Normi mõjuahel page uses, so
    the visual language is identical across workflows.
    """
    heading = f"Mõjuahel sätte {per_uri.source_label} analüüsist"
    inner_block = _build_results_block(per_uri.adhoc.findings, per_uri.adhoc.score, scope)
    return Div(  # noqa: F405
        H3(heading, cls="card-title"),  # noqa: F405
        *inner_block,
        cls="moju-poliitikamottest-per-uri-section",
    )


def _intent_results_block(agg: AggregatedResult, *, scope: _Scope) -> list[Any]:
    """Assemble the ``Tulemused`` block from the aggregated result."""
    summary = P(  # noqa: F405
        Strong(  # noqa: F405
            f"Kokku: {agg.total_affected} mõjutatud üksust, "
            f"{agg.total_conflicts} konflikti, {agg.total_gaps} lünka "
            f"(üle {len(agg.per_uri)} kinnitatud sätte)."
        )
    )
    sections: list[Any] = [summary]
    sections.extend(_intent_per_uri_section(per, scope=scope) for per in agg.per_uri)
    return sections


def _intent_evidence_block(agg: AggregatedResult) -> Any:
    """Render the ``Tõendid`` block — a flat list of (source → finding) links."""
    items: list[Any] = []
    for per in agg.per_uri:
        # Per-URI evidence: an entry-point link to the source on
        # Õiguskaart so the user can drill into context.
        items.append(
            Li(  # noqa: F405
                Strong(per.source_label),  # noqa: F405
                " — ",
                A(  # noqa: F405
                    "Ava õiguskaardil →",
                    href=explorer_focus_url(per.entity_uri),
                    cls="data-table-link",
                ),
                " · ",
                Span(  # noqa: F405
                    f"{per.adhoc.findings.affected_count} mõjutatud, "
                    f"{per.adhoc.findings.conflict_count} konflikti, "
                    f"{per.adhoc.findings.gap_count} lünka",
                    cls="muted-text",
                ),
            )
        )
    if not items:
        return _missing_row("Tõendeid ei leitud.")
    return Ul(*items, cls="moju-poliitikamottest-evidence-list")  # noqa: F405


def _intent_result_actions() -> list[dict[str, str]]:
    """Static recommended actions for the intent result page."""
    return [
        {"label": "Küsi nõustajalt", "href": "/chat/new"},
        {"label": "Ava õiguskaart", "href": "/explorer"},
        {"label": "Ava Koostaja", "href": "/drafter/new"},
        {"label": "Laadi üles eelnõu", "href": "/drafts/new"},
        {"label": "Tagasi analüüsikeskusesse", "href": "/analyysikeskus"},
    ]


def _intent_empty_state() -> Any:
    """Render the empty-confirmation friendly state (Step 3 with 0 URIs)."""
    return Div(  # noqa: F405
        Alert(
            "Mõjuanalüüsi käivitamiseks kinnita vähemalt üks säte. "
            "Mine tagasi ja vali vähemalt üks kandidaat.",
            variant="info",
        ),
        A(  # noqa: F405
            "← Tagasi sisestuse juurde",
            href="/analyysikeskus/moju-poliitikamottest",
            cls="back-link",
        ),
    )


async def moju_poliitikamottest_analyze(req: Request):
    """POST /analyysikeskus/moju-poliitikamottest/analyze — Step 3.

    Reads the confirmation form, runs the per-URI aggregated impact
    analysis over the confirmed URIs, and returns the full result
    layout (Sisend / Ulatus / Tulemused / Tõendid / Soovitatud
    tegevused) as an HTMX fragment swapped into the result region.
    """
    try:
        form = await req.form()
    except Exception:
        logger.warning("moju_poliitikamottest_analyze: failed to read form", exc_info=True)
        return Alert(
            "Vormi lugemine ebaõnnestus. Proovige uuesti.",
            variant="danger",
        )

    intent_text = str(form.get("intent") or "").strip()[:_MAX_INTENT_LEN]
    target_groups = [str(v) for v in form.getlist("target_groups")]
    affected_areas = [str(v) for v in form.getlist("affected_areas")]

    # Read the confirmed-row indices, then pull the URI + label per row.
    confirmed_indices: list[int] = []
    for raw in form.getlist("confirmed"):
        try:
            idx = int(str(raw).strip())
        except ValueError:
            continue
        confirmed_indices.append(idx)

    confirmed_uris: list[str] = []
    confirmed_labels: list[str] = []
    source_labels: dict[str, str] = {}
    for idx in confirmed_indices:
        uri = str(form.get(f"uri_{idx}") or "").strip()
        label = str(form.get(f"label_{idx}") or "").strip()
        if not uri:
            continue
        confirmed_uris.append(uri)
        confirmed_labels.append(label or uri)
        if label:
            source_labels[uri] = label

    # Empty-confirmation friendly state.
    if not confirmed_uris:
        return _intent_empty_state()

    # Cap fan-out before running per-URI Jena impact: each URI triggers
    # a ``run_adhoc_impact_analysis`` SPARQL roundtrip, so an unbounded
    # confirmed list is an easy DOS vector from an authenticated POST.
    # The cap aligns with ``_MAX_INTENT_CANDIDATES`` (12) — anything
    # above is suspicious for a single-intent workflow.
    if len(confirmed_uris) > _MAX_INTENT_CONFIRMED_URIS:
        confirmed_uris = confirmed_uris[:_MAX_INTENT_CONFIRMED_URIS]
        confirmed_labels = confirmed_labels[:_MAX_INTENT_CONFIRMED_URIS]
        # Drop matching ``source_labels`` keys so the result page doesn't
        # carry stale attribution for URIs we trimmed.
        source_labels = {uri: source_labels[uri] for uri in confirmed_uris if uri in source_labels}

    # Run the per-URI aggregated analysis (each URI runs in its own
    # ephemeral synthetic graph; the orchestrator sums the counts).
    agg = run_aggregated_analysis(
        confirmed_uris,
        source_labels=source_labels,
    )

    # ``_build_results_block`` needs a Normi-style scope; the intent
    # workflow doesn't expose scope toggles in this MVP, so we use the
    # defaults (EU on, court practice on, org-wide drafts off).
    scope = _Scope({})

    input_summary = _intent_result_input_summary(
        intent_text=intent_text,
        confirmed_labels=confirmed_labels,
    )
    results_block = _intent_results_block(agg, scope=scope)
    evidence_block = _intent_evidence_block(agg)

    # Render the *full* result layout (5-block shell) as an HTMX
    # fragment — the swap target is the result region, so we wrap the
    # content in fresh ``Sisend`` / ``Ulatus`` / ... cards rather than
    # re-rendering the full ``PageShell``.
    chips_summary = _intent_chip_summary(
        target_groups=target_groups, affected_areas=affected_areas
    )
    scope_block = (
        chips_summary
        if chips_summary
        else Span(  # noqa: F405
            "Vaikimisi ulatus: kehtiv õigus + EL õigus + Riigikohtu praktika.",
            cls="muted-text",
        )
    )

    # The result region only carries the inner blocks — the surrounding
    # PageShell + back-link + workflow H1 stay in place from Step 1.
    # We render each block as a Card so the visual rhythm matches.
    return Div(  # noqa: F405
        Card(
            CardHeader(H3("Sisend", cls="card-title")),  # noqa: F405
            CardBody(input_summary),
        ),
        Card(
            CardHeader(H3("Ulatus", cls="card-title")),  # noqa: F405
            CardBody(scope_block),
        ),
        Card(
            CardHeader(H3("Tulemused", cls="card-title")),  # noqa: F405
            CardBody(*results_block),
        ),
        Card(
            CardHeader(H3("Tõendid", cls="card-title")),  # noqa: F405
            CardBody(evidence_block),
        ),
        Card(
            CardHeader(H3("Soovitatud tegevused", cls="card-title")),  # noqa: F405
            CardBody(
                Div(  # noqa: F405
                    *[
                        A(  # noqa: F405
                            str(a["label"]),
                            href=str(a["href"]),
                            cls="btn btn-secondary btn-md",
                        )
                        for a in _intent_result_actions()
                    ],
                    cls="analyysikeskus-actions",
                )
            ),
        ),
        cls="moju-poliitikamottest-result",
    )
