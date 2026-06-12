"""Sarnasus — ``GET /analyysikeskus/sarnasus`` (#860).

Resolves the input (or takes free text) and finds similar provisions via the
similarity engine, badging the match reasons. Renders through the shared
result shell.

Patch where used (post-#860), e.g.::

  patch("app.analyysikeskus.routes._sarnasus.find_similar")
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
    _missing_row,
    _resolve_refs,
    _resolved_label,
)
from app.analyysikeskus.similarity import (
    SimilarityRow,
    find_similar,
    reason_labels_et,
)
from app.docs.report_routes import explorer_focus_url
from app.ui.data.data_table import Column, DataTable
from app.ui.primitives.badge import Badge, BadgeVariant
from app.ui.primitives.button import Button
from app.ui.surfaces.alert import Alert
from app.ui.theme import get_theme_from_request

logger = logging.getLogger(__name__)


_WORKFLOW_SIMILARITY = "sarnasus"
_WORKFLOW_ACTION[_WORKFLOW_SIMILARITY] = "/analyysikeskus/sarnasus"


def _similarity_link(sisend: str) -> str:
    """Build a ``/analyysikeskus/sarnasus?sisend=…`` link."""
    from urllib.parse import urlencode

    return f"/analyysikeskus/sarnasus?{urlencode([('sisend', sisend)])}"


_SIMILARITY_REASON_VARIANTS: dict[str, BadgeVariant] = {
    "ontology_declared": "success",
    "same_cluster": "primary",
    "embedding_cosine": "warning",
}


def _similarity_reason_badges(reasons: tuple[str, ...] | list[str]) -> Any:
    """Render the row's reason codes as inline Badges (Estonian labels)."""
    from app.analyysikeskus.similarity import REASON_LABELS_ET

    bits: list[Any] = []
    for code in reasons or []:
        label = REASON_LABELS_ET.get(code)
        if not label:
            continue
        variant: BadgeVariant = _SIMILARITY_REASON_VARIANTS.get(code, "default")
        bits.append(Badge(label, variant=variant))
        bits.append(" ")
    return Span(*bits) if bits else Span("—")  # noqa: F405


def _similarity_row_link(row: SimilarityRow) -> Any:
    """Render a similarity row's label as an Õiguskaart deep link."""
    label = row.label or row.entity_uri.rsplit("#", 1)[-1] or "—"
    if not row.entity_uri:
        return Span(label)  # noqa: F405
    return A(label, href=explorer_focus_url(row.entity_uri), cls="data-table-link")  # noqa: F405


def _similarity_results_block(rows: list[SimilarityRow]) -> list[Any]:
    """Assemble the ``Tulemused`` content for the sarnasus workflow."""
    if not rows:
        return [
            P(  # noqa: F405
                "Sarnaseid sätteid ei leitud.",
                cls="muted-text",
            )
        ]

    lead = P(  # noqa: F405
        Strong(f"{len(rows)} sarnast üksust"),  # noqa: F405
        " — järjestatud sarnasuse skoori järgi (kõrgemad eespool).",
    )

    columns = [
        Column(
            key="entity",
            label="Üksus",
            sortable=False,
            render=lambda r: _similarity_row_link(r["_row"]),
        ),
        Column(
            key="reasons",
            label="Miks see sobib",
            sortable=False,
            render=lambda r: _similarity_reason_badges(r["_row"].reasons),
        ),
        Column(
            key="score",
            label="Skoor",
            sortable=False,
            render=lambda r: Span(f"{r['_row'].score:.2f}", cls="muted-text"),  # noqa: F405
        ),
    ]
    table_rows = [{"_row": r} for r in rows[:_MAX_RESULT_ROWS]]
    return [
        lead,
        DataTable(
            columns=columns,
            rows=table_rows,
            empty_message="Sarnaseid sätteid ei leitud.",
        ),
    ]


def _similarity_evidence_block(rows: list[SimilarityRow]) -> list[Any]:
    """Assemble the ``Tõendid`` rows for the sarnasus workflow.

    Each row carries the matched entity's label, the legal-language
    "miks see sobib" badges as text, an "Ava õiguskaardil" deep link,
    and a "Küsi nõustajalt" seed form. The snippet (when present from
    the embedding track) appears as a muted sub-line.
    """
    if not rows:
        return []
    out: list[Any] = []
    for r in rows:
        reasons_et = ", ".join(reason_labels_et(r.reasons)) or "sarnane"
        why = (
            "See säte sarnaneb teie sisendiga — kasutage võrdluseks, "
            "et oma sõnastust vajadusel kohandada."
        )
        out.append(
            _evidence_row(
                source_label=r.label or r.entity_uri.rsplit("#", 1)[-1] or "Üksus",
                relation=f"sarnane ({reasons_et})",
                target_label="",
                uri=r.entity_uri,
                why=why,
                snippet=r.snippet,
                draft_id=None,
            )
        )
    return out


def _similarity_actions(focus_uri: str | None) -> list[dict[str, str]]:
    """The ``Soovitatud tegevused`` action set for a sarnasus result page."""
    actions: list[dict[str, str]] = []
    if focus_uri:
        actions.append({"label": "Ava õiguskaardil", "href": explorer_focus_url(focus_uri)})
    actions.append({"label": "Küsi nõustajalt", "href": "/chat/new"})
    actions.append({"label": "Tagasi analüüsikeskusesse", "href": "/analyysikeskus"})
    return actions


def _render_similarity_landing(
    *,
    auth: Any,
    theme: str,
) -> Any:
    """Render the workflow shell with no input yet — the empty-form landing."""
    landing_input = Div(  # noqa: F405
        P(  # noqa: F405
            "Sisestage säte, akt, CELEX-number või vabas vormis tekst — "
            "kuvame sõnastuselt või sisult sarnased sätted teistes aktides."
        ),
        Form(  # noqa: F405
            Input(
                "sisend",
                type="text",
                placeholder=("Nt: AvTS § 35 · CELEX-number · või kirjeldage sätte sisu"),
                aria_label="Õiguslik viide või vaba tekst",
                cls="analyysikeskus-input",
            ),
            Button("Otsi sarnaseid sätteid", type="submit", variant="primary"),
            method="get",
            action="/analyysikeskus/sarnasus",
            cls="analyysikeskus-workflow-form",
        ),
        Small(  # noqa: F405
            "Näited: «AvTS § 35» · «menetlustähtaegade pikendamine»",
            cls="muted-text",
        ),
    )
    return analysis_result_shell(
        workflow_title="Otsi sarnaseid sätteid",
        input_summary=landing_input,
        results_block=P(  # noqa: F405
            "Sisestage päring, et näha sarnaseid sätteid.",
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
            "Sisestage päring, et muuta ulatust.", cls="muted-text"
        ),
    )


def _render_similarity_result(
    *,
    auth: Any,
    theme: str,
    sisend: str,
    seed_uri: str | None,
    seed_label: str | None,
    rows: list[SimilarityRow],
) -> Any:
    """Render the result page for a similarity query.

    Args:
        sisend: The raw user input — for the ``Sisend`` card echo.
        seed_uri: The resolved entity URI when a structured ref was
            recognised; ``None`` for the free-text path.
        seed_label: The resolved label when *seed_uri* is set.
        rows: Merged similarity candidates from
            :func:`app.analyysikeskus.similarity.find_similar`.
    """
    if seed_uri and seed_label:
        input_summary: Any = P(  # noqa: F405
            "Analüüsisin: ",
            Strong(seed_label),  # noqa: F405
            " — otsin sarnaseid sätteid kolmest allikast (ontoloogia, temaatika, sõnastus).",
        )
    else:
        input_summary = P(  # noqa: F405
            "Analüüsisin vaba teksti: «",
            Strong(sisend),  # noqa: F405
            "» — otsin sarnaseid sätteid sõnastuse alusel.",
        )

    results_block = _similarity_results_block(rows)
    evidence_rows = _similarity_evidence_block(rows)
    evidence_block: Any = evidence_rows if evidence_rows else _missing_row("Tõendeid ei leitud.")
    actions = _similarity_actions(focus_uri=seed_uri)

    return analysis_result_shell(
        workflow_title="Otsi sarnaseid sätteid",
        input_summary=input_summary,
        results_block=results_block,
        evidence_block=evidence_block,
        actions=actions,
        user=auth,
        theme=theme,
        scope_block=Span(  # noqa: F405
            "Tulemused ühendavad ontoloogia, temaatika ja sõnastuse "
            "sarnasuse — ulatuse seaded on tulekul.",
            cls="muted-text",
        ),
    )


def _render_similarity_disambiguation(
    *,
    auth: Any,
    theme: str,
    sisend: str,
    resolved: list[Any],
) -> Any:
    """Render disambiguation links when multiple entities resolved."""
    items: list[Any] = []
    for r in resolved:
        label = _resolved_label(r, sisend)
        extracted = getattr(r, "extracted", None)
        ref_text = str(getattr(extracted, "ref_text", "") or label)
        items.append(Li(A(label, href=_similarity_link(ref_text))))  # noqa: F405

    results_block: list[Any] = [
        Alert("Sisend võib viidata mitmele üksusele. Vali, millist analüüsida:", variant="info"),
    ]
    if items:
        results_block.append(Ul(*items, cls="analyysikeskus-candidates"))  # noqa: F405

    return analysis_result_shell(
        workflow_title="Otsi sarnaseid sätteid",
        input_summary=P(f"Sisestasite: «{sisend}»"),  # noqa: F405
        results_block=results_block,
        evidence_block=_missing_row("—"),
        actions=[
            {"label": "Küsi nõustajalt", "href": "/chat/new"},
            {"label": "Tagasi analüüsikeskusesse", "href": "/analyysikeskus"},
        ],
        user=auth,
        theme=theme,
        scope_block=Span(  # noqa: F405
            "Tulemused ühendavad ontoloogia, temaatika ja sõnastuse "
            "sarnasuse — ulatuse seaded on tulekul.",
            cls="muted-text",
        ),
    )


def sarnasus_page(req: Request):
    """GET /analyysikeskus/sarnasus?sisend=<text> — Otsi sarnaseid sätteid (A5a).

    Flow:

    1. Blank ``sisend`` → render the workflow shell with the input form.
    2. Parse ``sisend`` → resolve via :class:`ReferenceResolver`:
       * exactly one resolved entity → run the hybrid similarity engine
         (ontology + cluster + embedding) seeded on the URI + the
         resolved label. Render the merged + de-duplicated result with
         "why this matched" badges.
       * multiple plausible resolutions → render disambiguation links.
       * nothing resolved (free-text path) → run the embedding track on
         the raw input; if it returns anything, render those rows; else
         render the friendly "no structured ref" fallback.

    **Privacy:** the raw input is never persisted by this route. The
    embedding call delegates to :class:`VoyageProvider` (subject to the
    project's approved data-processing controls) and the cosine search
    is scoped to the public corpus (``org_id IS NULL``). See
    :mod:`app.analyysikeskus.similarity` for the full privacy posture.
    """
    auth = req.scope.get("auth") or None
    theme = get_theme_from_request(req)

    sisend = (req.query_params.get("sisend") or "").strip()
    if not sisend:
        return _render_similarity_landing(auth=auth, theme=theme)

    parsed_refs = parse_user_reference(sisend)
    resolved = _resolve_refs(parsed_refs)
    resolved_with_uri = [
        r for r in resolved if getattr(r, "entity_uri", None) and str(r.entity_uri).strip()
    ]
    # Deduplicate by URI — the resolver may return both a precise
    # provision ref and a fallback law ref pointing to the same URI.
    seen: set[str] = set()
    unique_resolved: list[Any] = []
    for r in resolved_with_uri:
        uri = str(r.entity_uri)
        if uri in seen:
            continue
        seen.add(uri)
        unique_resolved.append(r)

    if len(unique_resolved) > 1:
        return _render_similarity_disambiguation(
            auth=auth, theme=theme, sisend=sisend, resolved=unique_resolved
        )

    if len(unique_resolved) == 1:
        resolved_one = unique_resolved[0]
        seed_uri = str(resolved_one.entity_uri)
        seed_label = _resolved_label(resolved_one, sisend)
        # Embedding query: use the resolved label, not the raw sisend.
        # Short §-refs like "AvTS § 35" don't carry semantic content; the
        # resolved label ("Avaliku teabe seadus § 35 — …") does. The
        # SPARQL tracks already use the URI, so the embedding side is the
        # only one that benefits from richer text.
        rows = find_similar(seed_uri=seed_uri, query_text=seed_label)
        return _render_similarity_result(
            auth=auth,
            theme=theme,
            sisend=sisend,
            seed_uri=seed_uri,
            seed_label=seed_label,
            rows=rows,
        )

    # Free-text path — only the embedding track has signal. Privacy
    # check: the raw text is sent to VoyageProvider (vendor-processed)
    # but never persisted by this route. See similarity.py for the
    # canonical posture.
    rows = find_similar(query_text=sisend)
    if rows:
        return _render_similarity_result(
            auth=auth,
            theme=theme,
            sisend=sisend,
            seed_uri=None,
            seed_label=None,
            rows=rows,
        )

    # Nothing resolved, nothing embedded — friendly fallback.
    warning = Alert(
        "Ei tuvastanud õiguslikku viidet ega leidnud sarnast sisu. "
        "Proovige nt «AvTS § 35», CELEX-numbrit (32016R0679) või akti "
        "lühinime.",
        variant="warning",
    )
    return analysis_result_shell(
        workflow_title="Otsi sarnaseid sätteid",
        input_summary=P(f"Sisestasite: «{sisend}»"),  # noqa: F405
        results_block=[warning],
        evidence_block=_missing_row("—"),
        actions=[
            {"label": "Küsi nõustajalt", "href": "/chat/new"},
            {"label": "Tagasi analüüsikeskusesse", "href": "/analyysikeskus"},
        ],
        user=auth,
        theme=theme,
        scope_block=Span(  # noqa: F405
            "Tulemused ühendavad ontoloogia, temaatika ja sõnastuse "
            "sarnasuse — ulatuse seaded on tulekul.",
            cls="muted-text",
        ),
    )
