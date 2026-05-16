"""CapabilityCard — a single discoverable workflow rendered as an interactive card.

Used by the Töölaud "Mida soovid teha?" capability map (B2) and reused by any
future surface that wants to advertise a workflow from
:mod:`app.ui.capabilities`. The card is a self-contained ``<a>`` element so the
entire surface is clickable and keyboard-focusable in a single tab stop — the
touch-target is the whole card (well over the 44×44 px WCAG minimum).

Rendered structure (one entry on the dashboard grid)::

    <a class="capability-card" href="...">
      <div class="capability-card__icon"><Icon/></div>
      <div class="capability-card__body">
        <h3 class="capability-card__title">
          Käivita Normi mõjuahel <Badge variant="warning">Tulekul</Badge>
        </h3>
        <p class="capability-card__desc">…one-line description…</p>
        <p class="capability-card__example">Näide: «AvTS § 35»</p>
      </div>
    </a>

Planned-status capabilities ("Tulekul") still render as a real anchor — the
target page itself shows the "this workflow opens soon" copy (see
``_planned_workflow_card`` in :mod:`app.analyysikeskus.routes`). Marking the
card with ``aria-disabled`` would over-promise; clicking still navigates to a
useful descriptor page rather than dead-ending.
"""

from __future__ import annotations

from urllib.parse import urlencode

from fasthtml.common import *  # noqa: F403

from app.ui.capabilities import Capability
from app.ui.primitives.badge import Badge
from app.ui.primitives.icon import Icon


def capability_href(cap: Capability) -> str:
    """Build the deep-link URL for a capability card.

    For Analüüsikeskus workflows whose form accepts a ``sisend`` text input
    (Normi mõjuahel, EL ülevõtt, Sanktsioonid, …) we prefill the example so
    one click lands the user on a populated form. For anything else we just
    return the bare ``target_url`` — chat seeds need a POST, the Õiguskaart
    needs no query, and the drafts index opens at its own landing page.
    """
    target = cap.target_url
    if cap.example_input and target.startswith("/analyysikeskus/"):
        return f"{target}?{urlencode({'sisend': cap.example_input})}"
    return target


def CapabilityCard(cap: Capability):  # noqa: ANN201 — returns an FT element
    """Render a :class:`Capability` as a clickable discovery card.

    The whole card is a single ``<a>`` so screen readers announce it as one
    link and keyboard users land on it with one Tab stop. ``aria-label``
    bundles the title + description so non-sighted users get the full
    context before deciding to follow the link.
    """
    title_parts: list = [cap.canonical_name_et]
    if cap.status == "planned":
        # Match the Analüüsikeskus directory's "Tulekul" affordance so the
        # same workflow reads the same way across surfaces.
        title_parts.extend([" ", Badge("Tulekul", variant="warning")])  # noqa: F405

    body_children: list = [
        H3(*title_parts, cls="capability-card__title"),  # noqa: F405
        P(cap.one_line_description_et, cls="capability-card__desc"),  # noqa: F405
    ]
    if cap.example_input:
        body_children.append(
            P(  # noqa: F405
                Span("Näide: ", cls="capability-card__example-label"),  # noqa: F405
                Span(f"«{cap.example_input}»", cls="capability-card__example-value"),  # noqa: F405
                cls="capability-card__example",
            )
        )

    aria_label = f"{cap.canonical_name_et} — {cap.one_line_description_et}"
    classes = "capability-card"
    if cap.status == "planned":
        classes += " capability-card--planned"

    return A(  # noqa: F405
        Div(  # noqa: F405
            Icon(cap.icon, size="lg", cls="capability-card__icon-svg"),
            cls="capability-card__icon",
        ),
        Div(*body_children, cls="capability-card__body"),  # noqa: F405
        href=capability_href(cap),
        cls=classes,
        aria_label=aria_label,
        data_capability_slug=cap.slug,
    )
