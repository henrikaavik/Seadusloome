"""The analysis-result shell — the 5-block layout every Analüüsikeskus workflow uses (#721).

Epic #714, design doc ``docs/2026-05-11-ministry-lawyer-ui-structure.md``
("Core UI Pattern for Every Workflow"). Every legal-analysis workflow
result page is the same five cards, in this order:

    1. ``Sisend``               — echoes what the user gave us
    2. ``Ulatus``               — legal-language scope controls + explanation
    3. ``Tulemused``            — key findings / risk level / recommended action
    4. ``Tõendid``              — sources, relations, dates, links
    5. ``Soovitatud tegevused`` — a *static* action set (no LLM advice — Phase D)

For the stub workflows (#722 Normi mõjuahel, #723 EL ülevõtt) ``results_block``
and ``evidence_block`` carry a "koostamisel — tulekul" placeholder; those
issues replace the placeholders with real computed findings + evidence.

Critically — per the design doc — the ``Ulatus`` controls read as
legal/policy scope, never as query configuration. No SPARQL, RDF,
named-graph, embedding, or "graph URI" language anywhere on this page.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from fasthtml.common import *  # noqa: F403

from app.auth.provider import UserDict
from app.ui.layout import PageShell
from app.ui.primitives.button import Button  # noqa: E402  (re-import after wildcard)
from app.ui.primitives.input import Checkbox, Input, Select
from app.ui.surfaces.card import Card, CardBody, CardHeader

# Explanatory sentence shown above the scope controls — straight from the
# design doc's "Ulatus" section. Says what the default scope is in legal
# terms and that the user may change it.
_SCOPE_EXPLANATION = (
    "Analüüsin vaikimisi kehtivat õigust, seotud eelnõusid, Riigikohtu "
    "praktikat ja EL õigusakte. Võite ulatust muuta."
)

# "Õigus" select — current law (default) vs. current + earlier redactions.
# Legal language only; this is "which redactions of the law count", not a
# version-graph picker.
_LAW_SCOPE_OPTIONS: list[tuple[str, str]] = [
    ("current", "Kehtiv õigus"),
    ("current_plus_history", "Kehtiv + varasemad redaktsioonid"),
]


def _card(heading: str, content: Any) -> Any:
    """A compact section card — ``Card(CardHeader(H3(...)), CardBody(...))``.

    Mirrors ``app/templates/dashboard.py::_section_card`` so the
    Analüüsikeskus pages share the dense-but-calm card rhythm with the
    rest of ``app/``.
    """
    return Card(
        CardHeader(H3(heading, cls="card-title")),  # noqa: F405
        CardBody(content),
    )


def _muted_missing_row(text: str) -> Any:
    """A one-line muted "…ei leitud" row used in place of an empty card body."""
    return P(text, cls="muted-text")  # noqa: F405


def _block_body(content: Any, *, empty_text: str) -> Any:
    """Render *content*, or a one-line muted fallback when it's empty.

    Keeps an empty block from rendering as an empty card body (per the
    #721 spec). ``None``, a blank string, and an empty list/tuple all
    count as empty; a non-empty list/tuple is wrapped in a ``Div`` so it
    nests cleanly inside ``CardBody`` rather than rendering as a bare
    Python list. Matters once #722/#723 pass computed finding lists here.
    """
    if content is None:
        return _muted_missing_row(empty_text)
    if isinstance(content, str):
        return content if content.strip() else _muted_missing_row(empty_text)
    if isinstance(content, (list, tuple)):
        return Div(*content) if content else _muted_missing_row(empty_text)  # noqa: F405
    return content


def _scope_block(scope_block: Any) -> Any:
    """Render the ``Ulatus`` card body.

    When the caller passes its own ``scope_block`` we use that verbatim
    (a richer workflow-specific control set). Otherwise we render the
    default legal-language scope form described in the #721 spec — the
    controls exist but don't re-run the analysis yet (that wiring lands
    in #722). All copy is legal/policy language; nothing here mentions
    SPARQL, named graphs, embeddings, or graph URIs.
    """
    if scope_block is not None:
        return scope_block

    return Form(  # noqa: F405
        P(_SCOPE_EXPLANATION, cls="muted-text"),  # noqa: F405
        # "Õigus" — which redactions of the law count.
        Div(  # noqa: F405
            Label("Õigus", fr="analyysikeskus-scope-law"),  # noqa: F405
            Select(
                "oigus",
                _LAW_SCOPE_OPTIONS,
                value="current",
                id="analyysikeskus-scope-law",
            ),
            cls="form-field",
        ),
        # Toggles — EU law + court practice on by default (the default
        # scope sentence above promises both).
        Checkbox(
            "kaasa_el",
            checked=True,
            label="Kaasa EL õigus",
        ),
        Checkbox(
            "kaasa_kohtupraktika",
            checked=True,
            label="Kaasa kohtupraktika",
        ),
        # Org-wide drafts off by default → only the user's own drafts are
        # in scope unless they opt in. Stated in the helper text below.
        Checkbox(
            "kogu_organisatsioon",
            checked=False,
            label="Kogu organisatsiooni eelnõud",
        ),
        Small(  # noqa: F405
            "Vaikimisi vaatan ainult teie enda eelnõusid; märkige see, et "
            "kaasata kogu organisatsiooni eelnõud.",
            cls="muted-text",
        ),
        # KOV regulations — not wired yet; disabled with a "Tulekul" tooltip.
        Checkbox(
            "kaasa_kov",
            checked=False,
            label="Kaasa KOV regulatsioonid",
            disabled=True,
            title="Tulekul",
        ),
        # Optional time range.
        Div(  # noqa: F405
            Label("Ajavahemik (valikuline)"),  # noqa: F405
            Span(  # noqa: F405
                Input("ajavahemik_algus", type="date", aria_label="Alguskuupäev"),
                Span(" – ", cls="muted-text"),  # noqa: F405
                Input("ajavahemik_lopp", type="date", aria_label="Lõppkuupäev"),
                cls="analyysikeskus-date-range",
            ),
            cls="form-field",
        ),
        # No submit yet — the controls are inert in this round (#722 wires
        # the re-run). A disabled button keeps the form visually complete
        # and signals the affordance is coming.
        Button(
            "Uuenda ulatust",
            type="submit",
            variant="secondary",
            size="sm",
            disabled=True,
            title="Tulekul",
        ),
        method="get",
        cls="analyysikeskus-scope-form",
    )


def _actions_block(actions: Sequence[Mapping[str, str]] | None) -> Any:
    """Render the ``Soovitatud tegevused`` card body from ``{label, href}`` dicts.

    Always a *static* action set in this round — no LLM-generated
    recommendations (that's Phase D). Each entry becomes a secondary
    link-styled button.
    """
    items = list(actions or [])
    if not items:
        return _muted_missing_row("Soovitatud tegevusi pole.")
    links = [
        A(  # noqa: F405
            str(a.get("label") or "Tegevus"),
            href=str(a.get("href") or "#"),
            cls="btn btn-secondary btn-md",
        )
        for a in items
    ]
    return Div(*links, cls="analyysikeskus-actions")  # noqa: F405


def analysis_result_shell(
    *,
    workflow_title: str,
    input_summary: Any,
    results_block: Any,
    evidence_block: Any,
    actions: Sequence[Mapping[str, str]],
    user: UserDict | None,
    theme: str = "dark",
    scope_block: Any | None = None,
) -> Any:
    """Build a workflow result page: a :func:`PageShell` with the 5-block layout.

    Args:
        workflow_title: The workflow's display name (e.g. ``"Normi mõjuahel"``).
            Used in both the page ``<title>`` and the ``H1``.
        input_summary: FT content for the ``Sisend`` card — for the stub
            workflows just ``P(f"Sisestasite: «{sisend}»")``; #722/#723 pass
            a richer resolved summary.
        results_block: FT content for the ``Tulemused`` card. Stubs pass an
            ``Alert``/``InfoBox`` saying the computation is "koostamisel".
        evidence_block: FT content for the ``Tõendid`` card.
        actions: A list of ``{"label": ..., "href": ...}`` dicts rendered as
            buttons in the ``Soovitatud tegevused`` card. Static — never
            LLM-generated.
        user: The authenticated user dict (forwarded to ``PageShell``).
        theme: Theme name (forwarded to ``PageShell``; the UI is dark-only).
        scope_block: Optional FT content for the ``Ulatus`` card. When omitted
            the default legal-language scope form is rendered.

    Returns:
        The ``PageShell(...)`` tree — a full page with a back-link near the
        top followed by the five cards in order.
    """
    back_link = A(  # noqa: F405
        "← Analüüsikeskus",
        href="/analyysikeskus",
        cls="back-link",
    )

    header = Div(  # noqa: F405
        back_link,
        H1(workflow_title, cls="page-title"),  # noqa: F405
        cls="analyysikeskus-result-header",
    )

    return PageShell(
        header,
        # 1. Sisend — what the user gave us.
        _card("Sisend", _block_body(input_summary, empty_text="Sisendit ei leitud.")),
        # 2. Ulatus — legal-language scope controls + explanation.
        _card("Ulatus", _scope_block(scope_block)),
        # 3. Tulemused — key findings / risk / recommended action.
        _card(
            "Tulemused",
            _block_body(results_block, empty_text="Tulemusi ei leitud."),
        ),
        # 4. Tõendid — sources, relations, dates, links.
        _card(
            "Tõendid",
            _block_body(evidence_block, empty_text="Tõendeid ei leitud."),
        ),
        # 5. Soovitatud tegevused — static action set.
        _card("Soovitatud tegevused", _actions_block(actions)),
        title=f"{workflow_title} — Analüüsikeskus",
        user=user,
        theme=theme,
        active_nav="/analyysikeskus",
    )
