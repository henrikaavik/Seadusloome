"""Drafter wizard step-page renderers.

Surgical extraction of the FastHTML page renderers used by the Phase 3A
AI Law Drafter wizard. Originally co-located in
:mod:`app.drafter.routes` but split out so ``routes.py`` stays focused
on route handlers and HTTP wiring.

The 13 renderers in this module are imported back into
:mod:`app.drafter.routes` so existing patch paths
(``patch("app.drafter.routes.<helper>")``) keep working — every name
is re-bound in the ``routes`` namespace at import time.

Call-time indirection
---------------------
To keep ``patch("app.drafter.routes.<name>")`` working from inside
these renderers, references to helpers that are *also* re-exported on
``app.drafter.routes`` are looked up on that module at *call* time via
:func:`_routes_attr`, rather than bound at module import time. That
avoids a circular import while ensuring a test patch on the routes
module takes effect on the very next call from here. The pattern
covers two kinds of dependency:

* Helpers that live in ``routes.py`` (``_step_tracker``) or are
  imported there from sibling modules (``decrypt_text``,
  ``_find_latest_job``) — the original motivation for the indirection.
* Sibling renderers in this very module (``_step_1_content``,
  ``_step_waiting_page``, ``_step_error_page``,
  ``_research_category_card``, ``_render_clause_card``) — these *are*
  defined here but are also re-imported into ``routes.py``, so a
  ``patch("app.drafter.routes.<helper>")`` must take effect on the
  intra-module calls that follow, exactly as it did pre-extraction
  when caller and callee were both in ``routes.py``.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any
from urllib.parse import quote as url_quote

from fasthtml.common import *  # noqa: F403

from app.auth.provider import UserDict
from app.db import get_connection as _connect
from app.docs.report_routes import explorer_focus_url
from app.drafter.session_model import DraftingSession
from app.drafter.state_machine import STEP_LABELS_ET, Step
from app.ui.forms.app_form import AppForm
from app.ui.layout import PageShell
from app.ui.primitives.annotation_button import AnnotationButton
from app.ui.primitives.button import Button
from app.ui.surfaces.alert import Alert
from app.ui.surfaces.card import Card, CardBody, CardHeader
from app.ui.surfaces.info_box import InfoBox

logger = logging.getLogger(__name__)

# Mirrored from routes.py (``_INTENT_MAX_LENGTH``) — both modules need this
# constant: routes.py uses it in ``submit_intent`` validation, this module
# uses it in the Step 1 form's ``maxlength`` attribute and help text.
_INTENT_MAX_LENGTH = 2000


# ---------------------------------------------------------------------------
# Helper: lookup a routes-module attribute at call time so test patches
# (``patch("app.drafter.routes.X")``) take effect inside these renderers.
# ---------------------------------------------------------------------------


def _routes_attr(name: str):
    """Return ``app.drafter.routes.<name>`` resolved at call time.

    Tests patch helpers at ``app.drafter.routes.<name>``; resolving them
    here at call time (rather than via a top-level ``from`` import) makes
    those patches affect the renderers in this module as well.
    """
    from app.drafter import routes as _routes  # local import avoids cycle

    return getattr(_routes, name)


# ---------------------------------------------------------------------------
# Step 1: Intent Capture
# ---------------------------------------------------------------------------


def _step_1_page(session: DraftingSession, auth: UserDict):
    """Render Step 1: Intent Capture form."""
    return _routes_attr("_step_1_content")(session, auth)


def _step_1_content(
    session: DraftingSession,
    auth: UserDict,
    *,
    error: str | None = None,
    intent_value: str | None = None,
):
    """Render Step 1 with optional error and preserved intent value."""
    value = intent_value if intent_value is not None else (session.intent or "")

    error_alert = Alert(error, variant="danger") if error else None

    form = AppForm(
        Div(  # noqa: F405
            Label(  # noqa: F405
                "Kirjeldage seaduse kavatsust",
                Span(" *", cls="form-field-required", aria_hidden="true"),  # noqa: F405
                fr="field-intent",
                cls="form-field-label",
            ),
            Textarea(  # noqa: F405
                value,
                name="intent",
                id="field-intent",
                rows="6",
                maxlength=str(_INTENT_MAX_LENGTH),
                required=True,
                placeholder=(
                    "Nt: Soovin koostada seaduse, mis reguleerib tehisintellekti "
                    "kasutamist avalikus sektoris, sealhulgas andmekaitse "
                    "nõudeid ja läbi­paistvuse kohustust."
                ),
                cls="input textarea",
            ),
            Small(  # noqa: F405
                f"Kuni {_INTENT_MAX_LENGTH} tähemärki.",
                cls="form-field-help",
            ),
            cls="form-field",
        ),
        Div(  # noqa: F405
            Button("Jätka täpsustamisega", type="submit", variant="primary"),
            A("Tagasi", href="/drafter", cls="btn btn-ghost btn-md"),  # noqa: F405
            cls="form-actions",
        ),
        method="post",
        action=f"/drafter/{session.id}/step/1",
    )

    children: list = []
    if error_alert:
        children.append(error_alert)
    children.append(form)

    return PageShell(
        H1("Kavatsuse kirjeldamine", cls="page-title"),  # noqa: F405
        _routes_attr("_step_tracker")(session.current_step),
        InfoBox(
            P(
                "Kirjeldage oma seadusandlikku kavatsust vabas vormis. "
                "Mida täpsem kirjeldus, seda paremad tulemused."
            ),
            variant="tip",
            dismissible=True,
        ),
        Card(
            CardHeader(H3("1. samm: Kavatsus", cls="card-title")),  # noqa: F405
            CardBody(*children),
        ),
        P(A("← Tagasi koostaja nimekirja", href="/drafter"), cls="back-link"),  # noqa: F405
        title="Kavatsus",
        user=auth,
        active_nav="/drafter",
    )


def _find_latest_job(
    session_id: uuid.UUID,
    job_type: str,
    extra_filter: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Find the most recent background job of *job_type* for this session.

    Queries ``background_jobs`` for rows whose JSONB payload contains
    ``session_id`` (and optionally ``extra_filter`` fields — e.g.
    ``{"clause_index": 3}`` for regenerate-clause lookups so concurrent
    regenerations of different clauses don't cross-pollinate).

    Returns a dict with keys ``status``, ``result``, ``error_message``,
    or ``None`` if no job exists.
    """
    filter_payload: dict[str, Any] = {"session_id": str(session_id)}
    if extra_filter:
        filter_payload.update(extra_filter)
    try:
        with _connect() as conn:
            row = conn.execute(
                """
                SELECT status, result, error_message
                FROM background_jobs
                WHERE job_type = %s
                  AND payload @> %s::jsonb
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (job_type, json.dumps(filter_payload)),
            ).fetchone()
    except Exception:
        logger.exception("Failed to look up job %s for session %s", job_type, session_id)
        return None

    if row is None:
        return None

    status, result, error_message = row
    # Parse result JSONB
    parsed_result = result
    if isinstance(parsed_result, str):
        try:
            parsed_result = json.loads(parsed_result)
        except (json.JSONDecodeError, TypeError):
            parsed_result = None

    return {
        "status": status,
        "result": parsed_result if isinstance(parsed_result, dict) else None,
        "error_message": error_message,
    }


# ---------------------------------------------------------------------------
# Step 2: Clarification Q&A
# ---------------------------------------------------------------------------


def _step_2_page(session: DraftingSession, auth: UserDict):
    """Render Step 2: Clarification Q&A."""
    clarifications = session.clarifications or []

    # If no clarifications yet, check job status
    if not clarifications:
        job = _routes_attr("_find_latest_job")(session.id, "drafter_clarify")
        _waiting = _routes_attr("_step_waiting_page")
        if job is None:
            # No job enqueued yet — shouldn't happen, but show a waiting state
            return _waiting(session, 2, auth, "Küsimuste genereerimine...")

        if job["status"] in ("pending", "claimed", "running"):
            return _waiting(session, 2, auth, "Küsimuste genereerimine...")

        if job["status"] == "failed":
            return _routes_attr("_step_error_page")(
                session,
                2,
                auth,
                job.get("error_message") or "Küsimuste genereerimine ebaõnnestus.",
            )

    # Find the first unanswered question
    unanswered_idx = None
    for i, c in enumerate(clarifications):
        if c.get("answer") is None:
            unanswered_idx = i
            break

    all_answered = unanswered_idx is None and len(clarifications) >= 3

    children: list[Any] = []

    # Show previously answered questions
    for i, c in enumerate(clarifications):
        q = c.get("question", "")
        a = c.get("answer")
        if a is not None:
            children.append(
                Div(  # noqa: F405
                    P(Strong(f"K{i + 1}: "), q, cls="clarification-question"),  # noqa: F405
                    P(Em(f"V: {a}"), cls="clarification-answer"),  # noqa: F405
                    cls="clarification-item answered",
                )
            )

    # Show the current unanswered question as a form
    if unanswered_idx is not None:
        c = clarifications[unanswered_idx]
        q = c.get("question", "")
        remaining = sum(1 for x in clarifications if x.get("answer") is None)
        children.append(
            AppForm(
                Div(  # noqa: F405
                    P(  # noqa: F405
                        Strong(f"Küsimus {unanswered_idx + 1}/{len(clarifications)}: "),  # noqa: F405
                        q,
                        cls="clarification-question current",
                    ),
                    Small(  # noqa: F405
                        f"{remaining} küsimust vastamata",
                        cls="form-field-help",
                    ),
                    Textarea(  # noqa: F405
                        "",
                        name="answer",
                        rows="3",
                        required=True,
                        placeholder="Kirjutage oma vastus siia...",
                        cls="input textarea",
                    ),
                    Input(type="hidden", name="question_index", value=str(unanswered_idx)),  # noqa: F405
                    cls="form-field",
                ),
                Div(  # noqa: F405
                    Button("Vasta", type="submit", variant="primary"),
                    cls="form-actions",
                ),
                method="post",
                action=f"/drafter/{session.id}/step/2",
            )
        )

    # Show "proceed" button if all answered
    if all_answered:
        children.append(
            Div(  # noqa: F405
                Alert(
                    "Kõik küsimused on vastatud. Võite jätkata uurimise sammuga.",
                    variant="success",
                ),
                AppForm(
                    Button("Jätka uurimisega", type="submit", variant="primary"),
                    Input(type="hidden", name="action", value="advance"),  # noqa: F405
                    method="post",
                    action=f"/drafter/{session.id}/step/2",
                ),
                cls="step-advance",
            )
        )

    return PageShell(
        H1("Täpsustamine", cls="page-title"),  # noqa: F405
        _routes_attr("_step_tracker")(session.current_step),
        InfoBox(
            P(
                "AI esitab täpsustavaid küsimusi teie kavatsuse kohta. "
                "Vastake vähemalt 3 küsimusele enne jätkamist."
            ),
            variant="tip",
            dismissible=True,
        ),
        Card(
            CardHeader(H3("2. samm: Täpsustamine", cls="card-title")),  # noqa: F405
            CardBody(*children)
            if children
            else CardBody(
                P("Küsimusi ei leitud.", cls="muted-text")  # noqa: F405
            ),
        ),
        P(A("← Tagasi koostaja nimekirja", href="/drafter"), cls="back-link"),  # noqa: F405
        title="Täpsustamine",
        user=auth,
        active_nav="/drafter",
    )


# ---------------------------------------------------------------------------
# Step 3: Ontology Research
# ---------------------------------------------------------------------------


def _step_3_page(session: DraftingSession, auth: UserDict):
    """Render Step 3: Ontology Research results."""
    if session.research_data_encrypted is None:
        # Check job status
        job = _routes_attr("_find_latest_job")(session.id, "drafter_research")
        if job is None or job["status"] in ("pending", "claimed", "running"):
            return _routes_attr("_step_waiting_page")(session, 3, auth, "Ontoloogia uurimine...")
        if job["status"] == "failed":
            return _routes_attr("_step_error_page")(
                session,
                3,
                auth,
                job.get("error_message") or "Uurimine ebaõnnestus.",
            )

    # Decrypt and show research results
    research: dict[str, Any] = {}
    if session.research_data_encrypted:
        try:
            research = json.loads(_routes_attr("decrypt_text")(session.research_data_encrypted))
        except Exception:
            logger.warning("Could not decrypt research data for session %s", session.id)

    provisions = research.get("provisions", [])
    eu_directives = research.get("eu_directives", [])
    court_decisions = research.get("court_decisions", [])
    topic_clusters = research.get("topic_clusters", [])

    cards: list[Any] = []

    # Summary cards for each category
    _category_card = _routes_attr("_research_category_card")
    cards.append(_category_card("Õigusaktide sätted", provisions, "provision"))
    cards.append(_category_card("EL-i õigusaktid", eu_directives, "eu"))
    cards.append(_category_card("Kohtulahendid", court_decisions, "court"))
    cards.append(_category_card("Teemaklastrid", topic_clusters, "cluster"))

    # Advance button
    advance_form = AppForm(
        Button("Jätka struktuuriga", type="submit", variant="primary"),
        Input(type="hidden", name="action", value="advance"),  # noqa: F405
        method="post",
        action=f"/drafter/{session.id}/step/3",
    )

    return PageShell(
        H1("Ontoloogia uurimine", cls="page-title"),  # noqa: F405
        _routes_attr("_step_tracker")(session.current_step),
        InfoBox(
            P(
                "Süsteem uurib ontoloogiat ja leiab seotud sätted, "
                "EL-i direktiivid ja kohtuotsused."
            ),
            variant="info",
            dismissible=True,
        ),
        Card(
            CardHeader(H3("3. samm: Uurimine", cls="card-title")),  # noqa: F405
            CardBody(
                *cards,
                Div(advance_form, cls="step-advance"),  # noqa: F405
            ),
        ),
        P(A("← Tagasi koostaja nimekirja", href="/drafter"), cls="back-link"),  # noqa: F405
        title="Uurimine",
        user=auth,
        active_nav="/drafter",
    )


def _research_category_card(title: str, items: list[dict[str, str]], category: str):
    """Render a summary card for a research category.

    #759: every researched entity that carries an ontology URI gets an
    "Ava õiguskaardil →" deep link (URL-encoded by
    :func:`app.docs.report_routes.explorer_focus_url`) so the drafter can
    jump from a found provision / EL act / court decision straight to the
    legal map centred on it. Items without a URI (e.g. topic clusters)
    render as plain text, matching how Analüüsikeskus handles the same case.
    """
    count = len(items)
    if count == 0:
        return Div(  # noqa: F405
            H4(f"{title}: 0", cls="research-category-title"),  # noqa: F405
            P("Tulemusi ei leitud.", cls="muted-text"),  # noqa: F405
            cls="research-category",
        )

    item_list: list[Any] = []
    for item in items[:10]:
        uri = str(item.get("uri") or "").strip()
        label = item.get("label") or item.get("act_label") or uri or "—"
        if uri:
            item_list.append(
                Li(  # noqa: F405
                    Span(label, cls="research-item-label"),  # noqa: F405
                    " ",
                    A(  # noqa: F405
                        "Ava õiguskaardil →",
                        href=explorer_focus_url(uri),
                        cls="data-table-link research-item-link",
                    ),
                    cls="research-item",
                )
            )
        else:
            item_list.append(Li(label, cls="research-item"))  # noqa: F405

    return Div(  # noqa: F405
        H4(f"{title}: {count}", cls="research-category-title"),  # noqa: F405
        Ul(*item_list, cls="research-item-list"),  # noqa: F405
        cls="research-category",
    )


# ---------------------------------------------------------------------------
# Step 4: Structure Generation (editable tree)
# ---------------------------------------------------------------------------


def _step_4_page(session: DraftingSession, auth: UserDict):
    """Render Step 4: Structure Generation / Editing."""
    if session.proposed_structure is None:
        # Check job status
        job = _routes_attr("_find_latest_job")(session.id, "drafter_structure")
        if job is None or job["status"] in ("pending", "claimed", "running"):
            return _routes_attr("_step_waiting_page")(
                session, 4, auth, "Struktuuri genereerimine..."
            )
        if job["status"] == "failed":
            return _routes_attr("_step_error_page")(
                session,
                4,
                auth,
                job.get("error_message") or "Struktuuri genereerimine ebaõnnestus.",
            )

    structure = session.proposed_structure or {}
    title = structure.get("title", "")
    chapters = structure.get("chapters", [])

    # Build the editable tree
    tree_items: list[Any] = []
    for ci, chapter in enumerate(chapters):
        section_items: list[Any] = []
        for si, section in enumerate(chapter.get("sections", [])):
            section_items.append(
                Li(  # noqa: F405
                    Div(  # noqa: F405
                        Input(  # noqa: F405
                            type="text",
                            name=f"chapter_{ci}_section_{si}_paragraph",
                            value=section.get("paragraph", ""),
                            cls="input input-sm structure-paragraph",
                        ),
                        Input(  # noqa: F405
                            type="text",
                            name=f"chapter_{ci}_section_{si}_title",
                            value=section.get("title", ""),
                            cls="input input-sm structure-section-title",
                        ),
                        cls="structure-section-row",
                    ),
                    cls="structure-section",
                )
            )

        tree_items.append(
            Div(  # noqa: F405
                Div(  # noqa: F405
                    Input(  # noqa: F405
                        type="text",
                        name=f"chapter_{ci}_number",
                        value=chapter.get("number", ""),
                        cls="input input-sm structure-chapter-number",
                    ),
                    Input(  # noqa: F405
                        type="text",
                        name=f"chapter_{ci}_title",
                        value=chapter.get("title", ""),
                        cls="input input-sm structure-chapter-title",
                    ),
                    cls="structure-chapter-row",
                ),
                Ul(*section_items, cls="structure-section-list"),  # noqa: F405
                Input(  # noqa: F405
                    type="hidden",
                    name=f"chapter_{ci}_section_count",
                    value=str(len(chapter.get("sections", []))),
                ),
                cls="structure-chapter",
            )
        )

    form = AppForm(
        Div(  # noqa: F405
            Label("Seaduse pealkiri", cls="form-field-label"),  # noqa: F405
            Input(  # noqa: F405
                type="text",
                name="law_title",
                value=title,
                cls="input",
            ),
            cls="form-field",
        ),
        Input(type="hidden", name="chapter_count", value=str(len(chapters))),  # noqa: F405
        Div(*tree_items, cls="structure-tree"),  # noqa: F405
        Div(  # noqa: F405
            Button("Salvesta ja jätka koostamisega", type="submit", variant="primary"),
            cls="form-actions",
        ),
        method="post",
        action=f"/drafter/{session.id}/step/4",
    )

    wf_label = "VTK" if session.workflow_type == "vtk" else "Seadus"

    return PageShell(
        H1("Struktuuri muutmine", cls="page-title"),  # noqa: F405
        _routes_attr("_step_tracker")(session.current_step),
        InfoBox(
            P(
                "AI pakub välja seaduse struktuuri. Saate peatükke "
                "ja paragrahve muuta, lisada või eemaldada."
            ),
            variant="tip",
            dismissible=True,
        ),
        Card(
            CardHeader(H3(f"4. samm: Struktuur ({wf_label})", cls="card-title")),  # noqa: F405
            CardBody(form),
        ),
        P(A("← Tagasi koostaja nimekirja", href="/drafter"), cls="back-link"),  # noqa: F405
        title="Struktuur",
        user=auth,
        active_nav="/drafter",
    )


# ---------------------------------------------------------------------------
# Step 5: Clause-by-Clause Drafting
# ---------------------------------------------------------------------------


def _render_clause_card(
    clause: dict[str, Any],
    session_id: uuid.UUID,
    idx: int,
    *,
    success_alert: bool = False,
):
    """Render a single drafter step-5 clause card.

    Shared renderer (#774) for the initial step-5 page and the
    ``regenerate_clause_status`` HTMX success fragment. Both paths MUST
    emit identical markup so HTMX outerHTML swaps keep the
    ``.clause-actions`` row (Muuda + Genereeri uuesti + AnnotationButton)
    after a regeneration — otherwise the user loses the ability to edit,
    regenerate again, or annotate the clause until they navigate away.

    Parameters
    ----------
    clause:
        Decoded clause dict from ``session.draft_content_encrypted``.
    session_id:
        Drafting session UUID — used to build the edit/regenerate URLs.
    idx:
        Position of the clause in the list — used to build the DOM id
        (``clause-{idx}``) and HTMX targets.
    success_alert:
        When ``True``, append a ``"Uuesti genereeritud."`` success Alert
        below the citations. Used by ``regenerate_clause_status`` after
        a successful regeneration so the user gets feedback.
    """
    chapter = clause.get("chapter", "")
    chapter_title = clause.get("chapter_title", "")
    para = clause.get("paragraph", "")
    title = clause.get("title", "")
    text = clause.get("text", "")
    citations = clause.get("citations", [])
    notes = clause.get("notes", "")

    citation_links: list[Any] = []
    for cit in citations:
        citation_links.append(
            A(
                cit,
                href=f"/explorer?search={url_quote(cit)}",
                cls="citation-link",
                target="_blank",
            )  # noqa: F405
        )

    return Div(  # noqa: F405
        H4(f"{para} {title}", cls="clause-heading"),  # noqa: F405
        Small(  # noqa: F405
            f"Peatükk: {chapter} {chapter_title}",
            cls="clause-chapter-ref muted-text",
        ),
        Div(  # noqa: F405
            P(text, cls="clause-text") if text else P("(Sisu puudub)", cls="muted-text"),  # noqa: F405
            cls="clause-body",
        ),
        Div(*citation_links, cls="clause-citations") if citation_links else None,  # noqa: F405
        P(Em(f"Märkus: {notes}"), cls="clause-notes muted-text") if notes else None,  # noqa: F405
        Alert("Uuesti genereeritud.", variant="success") if success_alert else None,
        Div(  # noqa: F405
            Button(  # noqa: F405
                "Muuda",
                hx_get=f"/drafter/{session_id}/step/5/edit/{idx}",
                hx_target=f"#clause-{idx}",
                hx_swap="outerHTML",
                variant="ghost",
                size="sm",
            ),
            Button(  # noqa: F405
                "Genereeri uuesti",
                hx_post=f"/drafter/{session_id}/step/5/regenerate/{idx}",
                hx_target=f"#clause-{idx}",
                hx_swap="outerHTML",
                variant="ghost",
                size="sm",
            ),
            AnnotationButton("provision", f"{session_id}-clause-{idx}"),
            cls="clause-actions",
        ),
        id=f"clause-{idx}",
        cls="clause-item",
    )


def _step_5_page(session: DraftingSession, auth: UserDict):
    """Render Step 5: Drafted clauses with inline editing."""
    if session.draft_content_encrypted is None:
        job = _routes_attr("_find_latest_job")(session.id, "drafter_draft")
        if job is None or job["status"] in ("pending", "claimed", "running"):
            return _routes_attr("_step_waiting_page")(
                session, 5, auth, "Seaduseteksti koostamine..."
            )
        if job["status"] == "failed":
            return _routes_attr("_step_error_page")(
                session,
                5,
                auth,
                job.get("error_message") or "Koostamine ebaõnnestus.",
            )

    # Decrypt clauses
    clauses: list[dict[str, Any]] = []
    if session.draft_content_encrypted:
        try:
            data = json.loads(_routes_attr("decrypt_text")(session.draft_content_encrypted))
            clauses = data.get("clauses", [])
        except Exception:
            logger.warning("Could not decrypt draft content for session %s", session.id)

    _clause_card = _routes_attr("_render_clause_card")
    clause_items: list[Any] = [
        _clause_card(clause, session.id, i) for i, clause in enumerate(clauses)
    ]

    # Advance form
    advance_form = AppForm(
        Button("Jätka ülevaatega", type="submit", variant="primary"),
        Input(type="hidden", name="action", value="advance"),  # noqa: F405
        method="post",
        action=f"/drafter/{session.id}/step/5",
    )

    return PageShell(
        H1("Seaduseteksti koostamine", cls="page-title"),  # noqa: F405
        _routes_attr("_step_tracker")(session.current_step),
        InfoBox(
            P(
                "AI koostab iga paragrahvi sisu viidete ja märkustega. "
                "Saate igat sätet muuta või uuesti genereerida."
            ),
            variant="tip",
            dismissible=True,
        ),
        Card(
            CardHeader(  # noqa: F405
                H3(  # noqa: F405
                    f"5. samm: Koostamine ({len(clauses)} paragrahvi)",
                    cls="card-title",
                )
            ),
            CardBody(
                *clause_items,
                Div(advance_form, cls="step-advance"),  # noqa: F405
            ),
        ),
        P(A("← Tagasi koostaja nimekirja", href="/drafter"), cls="back-link"),  # noqa: F405
        title="Koostamine",
        user=auth,
        active_nav="/drafter",
    )


# ---------------------------------------------------------------------------
# Step 6: Integrated Review
# ---------------------------------------------------------------------------


def _step_6_page(session: DraftingSession, auth: UserDict):
    """Render Step 6: Integrated Review with impact analysis."""
    if session.integrated_draft_id is None:
        # Not yet triggered — show the trigger button
        return PageShell(
            H1("Integreeritud ülevaade", cls="page-title"),  # noqa: F405
            _routes_attr("_step_tracker")(session.current_step),
            InfoBox(
                P(
                    "Koostatud eelnõu analüüsitakse "
                    "mõjuanalüüsi süsteemis. "
                    "Vaadake konflikte ja mõjutatud sätteid."
                ),
                variant="info",
                dismissible=True,
            ),
            Card(
                CardHeader(H3("6. samm: Ülevaade", cls="card-title")),  # noqa: F405
                CardBody(
                    P(  # noqa: F405
                        "Selles sammus läbitakse koostatud "
                        "eelnõu Phase 2 mõjuanalüüsi torustiku "
                        "kaudu. See loob eelnõu (.docx), parsib "
                        "selle, tuvastab viited ja kuvab "
                        "mõjuanalüüsi.",
                        cls="page-lead",
                    ),
                    AppForm(
                        Button(
                            "Käivita mõjuanalüüs",
                            type="submit",
                            variant="primary",
                        ),
                        method="post",
                        action=f"/drafter/{session.id}/step/6",
                    ),
                ),
            ),
            P(A("← Tagasi koostaja nimekirja", href="/drafter"), cls="back-link"),  # noqa: F405
            title="Ülevaade",
            user=auth,
            active_nav="/drafter",
        )

    # Impact analysis is linked — show report inline
    draft_id = str(session.integrated_draft_id)

    return PageShell(
        H1("Integreeritud ülevaade", cls="page-title"),  # noqa: F405
        _routes_attr("_step_tracker")(session.current_step),
        InfoBox(
            P(
                "Koostatud eelnõu analüüsitakse "
                "mõjuanalüüsi süsteemis. "
                "Vaadake konflikte ja mõjutatud sätteid."
            ),
            variant="info",
            dismissible=True,
        ),
        Card(
            CardHeader(H3("6. samm: Ülevaade", cls="card-title")),  # noqa: F405
            CardBody(
                Alert("Mõjuanalüüs on seotud eelnõuga.", variant="success"),
                P(  # noqa: F405
                    A(  # noqa: F405
                        "Vaata mõjuanalüüsi aruannet",
                        href=f"/drafts/{draft_id}/report",
                        cls="btn btn-secondary btn-md",
                    ),
                ),
                Div(  # noqa: F405
                    id="impact-report-inline",
                    hx_get=f"/drafts/{draft_id}/report/summary",
                    hx_trigger="load",
                    hx_swap="innerHTML",
                ),
                Div(  # noqa: F405
                    AppForm(
                        Button("Jätka ekspordiga", type="submit", variant="primary"),
                        Input(type="hidden", name="action", value="advance"),  # noqa: F405
                        method="post",
                        action=f"/drafter/{session.id}/step/6",
                    ),
                    cls="step-advance",
                ),
            ),
        ),
        P(A("< Tagasi koostaja nimekirja", href="/drafter"), cls="back-link"),  # noqa: F405
        title="Ülevaade",
        user=auth,
        active_nav="/drafter",
    )


# ---------------------------------------------------------------------------
# Step 7: Export
# ---------------------------------------------------------------------------


def _step_7_page(session: DraftingSession, auth: UserDict):
    """Render Step 7: Export .docx."""
    return PageShell(
        H1("Eksport", cls="page-title"),  # noqa: F405
        _routes_attr("_step_tracker")(session.current_step),
        InfoBox(
            P(
                "Laadige alla valmis .docx fail eelnõuga. "
                "Fail sisaldab AI-genereeritud märget ja "
                "viidete registrit."
            ),
            variant="tip",
            dismissible=True,
        ),
        Card(
            CardHeader(H3("7. samm: Eksport", cls="card-title")),  # noqa: F405
            CardBody(
                P(  # noqa: F405
                    "Teie eelnõu on valmis. Laadige alla .docx fail.",
                    cls="page-lead",
                ),
                A(  # noqa: F405
                    "Laadi alla .docx",
                    href=f"/drafter/{session.id}/export",
                    cls="btn btn-primary btn-md",
                ),
                P(  # noqa: F405
                    Small(  # noqa: F405
                        "See dokument on genereeritud tehisintellekti abil. "
                        "Palun kontrollige sisu enne ametlikku kasutamist.",
                        cls="muted-text",
                    ),
                ),
            ),
        ),
        P(A("< Tagasi koostaja nimekirja", href="/drafter"), cls="back-link"),  # noqa: F405
        title="Eksport",
        user=auth,
        active_nav="/drafter",
    )


# ---------------------------------------------------------------------------
# Waiting / Error shared pages
# ---------------------------------------------------------------------------


def _step_waiting_page(
    session: DraftingSession,
    step_num: int,
    auth: UserDict,
    message: str,
):
    """Render a polling spinner page while a background job runs."""
    step = Step(step_num)
    label = STEP_LABELS_ET.get(step, str(step_num))
    return PageShell(
        H1(f"{step_num}. samm: {label}", cls="page-title"),  # noqa: F405
        _routes_attr("_step_tracker")(session.current_step),
        Card(
            CardHeader(H3(f"{step_num}. samm: {label}", cls="card-title")),  # noqa: F405
            CardBody(
                Div(  # noqa: F405
                    P(message, cls="muted-text"),  # noqa: F405
                    Div(cls="spinner"),  # noqa: F405
                    id="step-status",
                    hx_get=f"/drafter/{session.id}/step/{step_num}/status",
                    hx_trigger="every 3s",
                    hx_swap="outerHTML",
                ),
            ),
        ),
        P(A("< Tagasi koostaja nimekirja", href="/drafter"), cls="back-link"),  # noqa: F405
        title=label,
        user=auth,
        active_nav="/drafter",
    )


def _step_error_page(
    session: DraftingSession,
    step_num: int,
    auth: UserDict,
    error_message: str,
):
    """Render an error page for a failed background job."""
    step = Step(step_num)
    label = STEP_LABELS_ET.get(step, str(step_num))
    return PageShell(
        H1(f"{step_num}. samm: {label}", cls="page-title"),  # noqa: F405
        _routes_attr("_step_tracker")(session.current_step),
        Card(
            CardHeader(H3(f"{step_num}. samm: {label}", cls="card-title")),  # noqa: F405
            CardBody(
                Alert(error_message, variant="danger", title="Viga"),
                P(  # noqa: F405
                    A("< Tagasi koostaja nimekirja", href="/drafter"),  # noqa: F405
                    cls="back-link",
                ),
            ),
        ),
        title=label,
        user=auth,
        active_nav="/drafter",
    )
