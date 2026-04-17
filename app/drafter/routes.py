"""FastHTML routes for the Phase 3A AI Law Drafter wizard.

Route map:

    GET  /drafter                          -- session list
    GET  /drafter/new                      -- workflow selection form
    POST /drafter/new                      -- create session handler
    GET  /drafter/{session_id}             -- redirect to current step
    GET  /drafter/{session_id}/step/{n}    -- step page (wizard)
    POST /drafter/{session_id}/step/1      -- submit intent
    POST /drafter/{session_id}/step/2      -- submit clarification answer
    POST /drafter/{session_id}/step/4      -- save edited structure
    POST /drafter/{session_id}/step/5      -- edit clause / regenerate
    POST /drafter/{session_id}/step/6      -- trigger integrated review
    GET  /drafter/{session_id}/step/{n}/status -- HTMX polling fragment
    GET  /drafter/{session_id}/export      -- download .docx

All routes require authentication (they are NOT in ``SKIP_PATHS``).
Cross-org access returns 404 to avoid leaking session existence.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any
from urllib.parse import quote as url_quote

from fasthtml.common import *  # noqa: F403
from starlette.requests import Request
from starlette.responses import FileResponse, RedirectResponse, Response

from app.auth.audit import log_action
from app.auth.helpers import require_auth as _require_auth
from app.auth.policy import can_access_drafter_session
from app.auth.provider import UserDict
from app.db import get_connection as _connect
from app.drafter.audit import (
    log_drafter_clause_edit,
    log_drafter_export,
    log_drafter_regenerate,
    log_drafter_step_advance,
)
from app.drafter.errors import DrafterNotAvailableError
from app.drafter.guards import require_real_llm
from app.drafter.session_model import (
    DraftingSession,
    count_sessions_for_user_conn,
    create_session,
    fetch_session,
    fetch_sessions_for_user,
    get_session,
    update_session,
)
from app.drafter.state_machine import (
    STEP_LABELS_ET,
    Step,
    StepTransitionError,
    advance_step,
)
from app.jobs import JobQueue
from app.storage import decrypt_text, encrypt_text
from app.ui.data.data_table import Column, DataTable
from app.ui.data.pagination import Pagination
from app.ui.forms.app_form import AppForm
from app.ui.layout import PageShell
from app.ui.primitives.annotation_button import AnnotationButton
from app.ui.primitives.badge import Badge, BadgeVariant
from app.ui.primitives.button import Button
from app.ui.surfaces.alert import Alert
from app.ui.surfaces.card import Card, CardBody, CardHeader
from app.ui.surfaces.info_box import InfoBox
from app.ui.time import format_tallinn

logger = logging.getLogger(__name__)

_PAGE_SIZE = 25
_INTENT_MAX_LENGTH = 2000


# ---------------------------------------------------------------------------
# Status / step display helpers
# ---------------------------------------------------------------------------

_STATUS_VARIANT_MAP: dict[str, BadgeVariant] = {
    "active": "primary",
    "completed": "success",
    "abandoned": "default",
}

_STATUS_LABELS_ET: dict[str, str] = {
    "active": "Aktiivne",
    "completed": "Valmis",
    "abandoned": "Katkestatud",
}

_WORKFLOW_LABELS_ET: dict[str, str] = {
    "full_law": "Seadus",
    "vtk": "VTK",
}


def _status_badge(status: str):
    """Return a Badge for a session status."""
    variant: BadgeVariant = _STATUS_VARIANT_MAP.get(status, "default")
    label = _STATUS_LABELS_ET.get(status, status)
    return Badge(label, variant=variant)


def _workflow_badge(workflow_type: str):
    """Return a Badge for a workflow type."""
    label = _WORKFLOW_LABELS_ET.get(workflow_type, workflow_type)
    return Badge(label, variant="default")


def _step_tracker(current_step: int):
    """Render a horizontal 7-step status tracker for the drafter wizard.

    Reuses the same ``draft-stage-*`` CSS classes from Phase 2's document
    upload status tracker.
    """
    items: list = []
    for step in Step:
        label = STEP_LABELS_ET.get(step, str(step))
        classes = ["draft-stage"]
        if int(step) < current_step:
            classes.append("draft-stage-done")
        elif int(step) == current_step:
            classes.append("draft-stage-active")
        else:
            classes.append("draft-stage-idle")
        items.append(
            Li(  # noqa: F405
                Span(str(int(step)), cls="draft-stage-number", aria_hidden="true"),  # noqa: F405
                Span(label, cls="draft-stage-label"),  # noqa: F405
                cls=" ".join(classes),
            )
        )
    return Ol(*items, cls="draft-status-tracker", aria_label="Koostaja sammud")  # noqa: F405


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_uuid(raw: str) -> uuid.UUID | None:
    """Return a ``UUID`` parsed from *raw*, or ``None`` if invalid."""
    try:
        return uuid.UUID(raw)
    except (ValueError, TypeError):
        return None


def _not_found_page(req: Request):
    """Render the 404 page for missing or cross-org sessions."""
    auth = req.scope.get("auth")
    return PageShell(
        H1("Koostamissessioon ei leitud", cls="page-title"),  # noqa: F405
        Alert(
            "Otsitud koostamissessioon ei ole olemas voi Te ei oma selle vaatamise oigust.",
            variant="warning",
        ),
        P(A("< Tagasi koostaja nimekirja", href="/drafter"), cls="back-link"),  # noqa: F405
        title="Sessioon ei leitud",
        user=auth,
        active_nav="/drafter",
    )


def _format_timestamp(value: Any) -> str:
    """Render a ``datetime`` in Europe/Tallinn (see app.ui.time)."""
    return format_tallinn(value)


# ---------------------------------------------------------------------------
# GET /drafter -- session list
# ---------------------------------------------------------------------------


def _session_rows(sessions: list[DraftingSession]) -> list[dict[str, Any]]:
    """Shape ``DraftingSession`` objects into the dict rows for DataTable."""
    rows: list[dict[str, Any]] = []
    for s in sessions:
        step_label = STEP_LABELS_ET.get(Step(s.current_step), str(s.current_step))
        rows.append(
            {
                "id": str(s.id),
                "workflow_type": s.workflow_type,
                "current_step_label": f"{s.current_step}. {step_label}",
                "status_raw": s.status,
                "created_at": _format_timestamp(s.created_at),
            }
        )
    return rows


def _session_list_columns() -> list[Column]:
    """Return the column definitions for the sessions DataTable."""

    def _workflow_cell(row: dict[str, Any]):
        return _workflow_badge(row["workflow_type"])

    def _step_cell(row: dict[str, Any]):
        return A(  # noqa: F405
            row["current_step_label"],
            href=f"/drafter/{row['id']}",
            cls="data-table-link",
        )

    def _status_cell(row: dict[str, Any]):
        return _status_badge(row["status_raw"])

    def _actions_cell(row: dict[str, Any]):
        return A(  # noqa: F405
            "Jatka",
            href=f"/drafter/{row['id']}",
            cls="btn btn-secondary btn-sm",
        )

    return [
        Column(key="workflow_type", label="Toovoog", sortable=False, render=_workflow_cell),
        Column(key="current_step_label", label="Samm", sortable=False, render=_step_cell),
        Column(key="status", label="Staatus", sortable=False, render=_status_cell),
        Column(key="created_at", label="Loodud", sortable=False),
        Column(key="actions", label="Tegevused", sortable=False, render=_actions_cell),
    ]


def drafter_list_page(req: Request):
    """GET /drafter -- paginated list of the caller's drafting sessions."""
    auth_or_redirect = _require_auth(req)
    if isinstance(auth_or_redirect, Response):
        return auth_or_redirect
    auth = auth_or_redirect
    org_id = auth.get("org_id")
    user_id = auth.get("id")

    page_str = req.query_params.get("page", "1")
    try:
        page = max(1, int(page_str))
    except ValueError:
        page = 1
    offset = (page - 1) * _PAGE_SIZE

    if not org_id or not user_id:
        body: Any = Alert(
            "Te ei kuulu uhtegi organisatsiooni.",
            variant="warning",
        )
        pagination = None
    else:
        sessions = fetch_sessions_for_user(user_id, org_id, limit=_PAGE_SIZE, offset=offset)
        total = count_sessions_for_user_conn(user_id, org_id)
        total_pages = max(1, (total + _PAGE_SIZE - 1) // _PAGE_SIZE)

        if total == 0:
            body = Div(  # noqa: F405
                P(  # noqa: F405
                    "Teil ei ole veel uhtegi koostamissessiooni.",
                    cls="muted-text",
                ),
                A(  # noqa: F405
                    "Alusta uut koostamist",
                    href="/drafter/new",
                    cls="btn btn-primary btn-md",
                ),
                cls="empty-state",
            )
            pagination = None
        else:
            body = DataTable(
                columns=_session_list_columns(),
                rows=_session_rows(sessions),
                empty_message="Sessioone ei leitud.",
            )
            pagination = Pagination(
                current_page=page,
                total_pages=total_pages,
                base_url="/drafter",
                page_size=_PAGE_SIZE,
                total=total,
            )

    header_children: list = [H1("AI koostaja", cls="page-title")]  # noqa: F405
    header_children.append(
        InfoBox(
            P(
                "AI koostaja aitab teil kirjutada uue seaduse eeln\u00f5u "
                "samm-sammult. Kirjeldage oma kavatsust ja s\u00fcsteem "
                "genereerib k\u00fcsimused, uurib ontoloogiat, pakub "
                "v\u00e4lja struktuuri ja koostab s\u00e4tted."
            ),
            variant="info",
            dismissible=True,
        )
    )
    if org_id:
        header_children.append(
            Div(  # noqa: F405
                A(  # noqa: F405
                    "Alusta uut koostamist",
                    href="/drafter/new",
                    cls="btn btn-primary btn-md",
                ),
                cls="page-actions",
            )
        )

    card_body_children: list = [body]
    if pagination is not None:
        card_body_children.append(pagination)

    return PageShell(
        *header_children,
        Card(
            CardHeader(H3("Minu koostamissessioonid", cls="card-title")),  # noqa: F405
            CardBody(*card_body_children),
        ),
        title="AI koostaja",
        user=auth,
        active_nav="/drafter",
    )


# ---------------------------------------------------------------------------
# GET /drafter/new -- workflow selection
# ---------------------------------------------------------------------------


def _workflow_form(*, error: str | None = None):
    """Render the workflow selection form with radio buttons."""
    error_alert = Alert(error, variant="danger") if error else None

    return AppForm(
        Div(  # noqa: F405
            Label("Toovoo tyyp", cls="form-field-label"),  # noqa: F405
            Div(  # noqa: F405
                Label(  # noqa: F405
                    Input(  # noqa: F405
                        type="radio",
                        name="workflow_type",
                        value="full_law",
                        checked=True,
                        cls="radio-input",
                    ),
                    Span("Taielik seadus", cls="radio-label"),  # noqa: F405
                    P(  # noqa: F405
                        "Koostab terve seaduse kavatsusest kuni valmis eelnouks.",
                        cls="radio-description muted-text",
                    ),
                    cls="radio-option",
                ),
                Label(  # noqa: F405
                    Input(  # noqa: F405
                        type="radio",
                        name="workflow_type",
                        value="vtk",
                        cls="radio-input",
                    ),
                    Span("VTK eelanaluus", cls="radio-label"),  # noqa: F405
                    P(  # noqa: F405
                        "Koostab vabariigi valitsuse korralduse eelanaluusi dokumendi.",
                        cls="radio-description muted-text",
                    ),
                    cls="radio-option",
                ),
                cls="radio-group",
            ),
            cls="form-field",
        ),
        Div(  # noqa: F405
            Button("Alusta koostamist", type="submit", variant="primary"),
            A("Tuhista", href="/drafter", cls="btn btn-ghost btn-md"),  # noqa: F405
            cls="form-actions",
        ),
        method="post",
        action="/drafter/new",
    ), error_alert


def new_session_page(req: Request):
    """GET /drafter/new -- render the workflow selection form."""
    auth_or_redirect = _require_auth(req)
    if isinstance(auth_or_redirect, Response):
        return auth_or_redirect
    auth = auth_or_redirect

    if not auth.get("org_id"):
        return PageShell(
            H1("Uus koostamine", cls="page-title"),  # noqa: F405
            Alert(
                "Te ei kuulu uhtegi organisatsiooni.",
                variant="warning",
            ),
            title="Uus koostamine",
            user=auth,
            active_nav="/drafter",
        )

    form, error_alert = _workflow_form()
    card_children: list = []
    if error_alert is not None:
        card_children.append(error_alert)
    card_children.append(form)

    return PageShell(
        H1("Uus koostamine", cls="page-title"),  # noqa: F405
        InfoBox(
            P(
                "Valige t\u00f6\u00f6voog: 'T\u00e4isseadus' loob terve seaduse "
                "eeln\u00f5u, 'VTK' loob v\u00e4ljat\u00f6\u00f6tamiskavatsuse "
                "standardvormis."
            ),
            variant="info",
            dismissible=True,
        ),
        Card(CardBody(*card_children)),
        P(A("\u2190 Tagasi koostaja nimekirja", href="/drafter"), cls="back-link"),  # noqa: F405
        title="Uus koostamine",
        user=auth,
        active_nav="/drafter",
    )


# ---------------------------------------------------------------------------
# POST /drafter/new -- create session
# ---------------------------------------------------------------------------


async def create_session_handler(req: Request):
    """POST /drafter/new -- create a new drafting session."""
    auth_or_redirect = _require_auth(req)
    if isinstance(auth_or_redirect, Response):
        return auth_or_redirect
    auth = auth_or_redirect
    org_id = auth.get("org_id")
    user_id = auth.get("id")

    if not org_id or not user_id:
        return PageShell(
            H1("Uus koostamine", cls="page-title"),  # noqa: F405
            Alert("Te ei kuulu uhtegi organisatsiooni.", variant="warning"),
            title="Uus koostamine",
            user=auth,
            active_nav="/drafter",
        )

    # Check that the LLM is available before creating a session.
    try:
        require_real_llm()
    except DrafterNotAvailableError as exc:
        form, _ = _workflow_form(error=str(exc))
        return PageShell(
            H1("Uus koostamine", cls="page-title"),  # noqa: F405
            Alert(str(exc), variant="danger"),
            Card(CardBody(form)),
            P(A("< Tagasi koostaja nimekirja", href="/drafter"), cls="back-link"),  # noqa: F405
            title="Uus koostamine",
            user=auth,
            active_nav="/drafter",
        )

    form_data = await req.form()
    workflow_type = str(form_data.get("workflow_type", "full_law"))
    if workflow_type not in ("full_law", "vtk"):
        workflow_type = "full_law"

    try:
        with _connect() as conn:
            session = create_session(conn, user_id, org_id, workflow_type)
            conn.commit()
    except Exception:
        logger.exception("Failed to create drafting session")
        form, _ = _workflow_form(error="Sessiooni loomine ebaonnestus. Palun proovige uuesti.")
        return PageShell(
            H1("Uus koostamine", cls="page-title"),  # noqa: F405
            Alert("Sessiooni loomine ebaonnestus.", variant="danger"),
            Card(CardBody(form)),
            title="Uus koostamine",
            user=auth,
            active_nav="/drafter",
        )

    log_action(
        user_id,
        "drafter.session.create",
        {
            "session_id": str(session.id),
            "workflow_type": workflow_type,
        },
    )

    return RedirectResponse(
        url=f"/drafter/{session.id}/step/1",
        status_code=303,
    )


# ---------------------------------------------------------------------------
# GET /drafter/{session_id} -- redirect to current step
# ---------------------------------------------------------------------------


def session_redirect(req: Request, session_id: str):
    """GET /drafter/{session_id} -- redirect to the current step."""
    auth_or_redirect = _require_auth(req)
    if isinstance(auth_or_redirect, Response):
        return auth_or_redirect
    auth = auth_or_redirect

    parsed = _parse_uuid(session_id)
    if parsed is None:
        return _not_found_page(req)

    session = fetch_session(parsed)
    if session is None:
        return _not_found_page(req)
    if not can_access_drafter_session(auth, session):
        return _not_found_page(req)

    return RedirectResponse(
        url=f"/drafter/{session_id}/step/{session.current_step}",
        status_code=303,
    )


# ---------------------------------------------------------------------------
# GET /drafter/{session_id}/step/{n} -- step pages
# ---------------------------------------------------------------------------


def _step_1_page(session: DraftingSession, auth: UserDict):
    """Render Step 1: Intent Capture form."""
    return _step_1_content(session, auth)


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
                    "noudeid ja labi\u00adpaistvuse kohustust."
                ),
                cls="input textarea",
            ),
            Small(  # noqa: F405
                f"Kuni {_INTENT_MAX_LENGTH} tahemarki.",
                cls="form-field-help",
            ),
            cls="form-field",
        ),
        Div(  # noqa: F405
            Button("Jatka tapsustamisega", type="submit", variant="primary"),
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
        _step_tracker(session.current_step),
        InfoBox(
            P(
                "Kirjeldage oma seadusandlikku kavatsust vabas vormis. "
                "Mida t\u00e4psem kirjeldus, seda paremad tulemused."
            ),
            variant="tip",
            dismissible=True,
        ),
        Card(
            CardHeader(H3("1. samm: Kavatsus", cls="card-title")),  # noqa: F405
            CardBody(*children),
        ),
        P(A("\u2190 Tagasi koostaja nimekirja", href="/drafter"), cls="back-link"),  # noqa: F405
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
        job = _find_latest_job(session.id, "drafter_clarify")
        if job is None:
            # No job enqueued yet — shouldn't happen, but show a waiting state
            return _step_waiting_page(session, 2, auth, "Kusimuste genereerimine...")

        if job["status"] in ("pending", "claimed", "running"):
            return _step_waiting_page(session, 2, auth, "Kusimuste genereerimine...")

        if job["status"] == "failed":
            return _step_error_page(
                session,
                2,
                auth,
                job.get("error_message") or "Kusimuste genereerimine ebaonnestus.",
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
                        Strong(f"Kusimus {unanswered_idx + 1}/{len(clarifications)}: "),  # noqa: F405
                        q,
                        cls="clarification-question current",
                    ),
                    Small(  # noqa: F405
                        f"{remaining} kusimust vastamata",
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
                    "Koik kusimused on vastatud. Voite jatkata uurimise sammuga.",
                    variant="success",
                ),
                AppForm(
                    Button("Jatka uurimisega", type="submit", variant="primary"),
                    Input(type="hidden", name="action", value="advance"),  # noqa: F405
                    method="post",
                    action=f"/drafter/{session.id}/step/2",
                ),
                cls="step-advance",
            )
        )

    return PageShell(
        H1("Tapsustamine", cls="page-title"),  # noqa: F405
        _step_tracker(session.current_step),
        InfoBox(
            P(
                "AI esitab t\u00e4psustavaid k\u00fcsimusi teie kavatsuse kohta. "
                "Vastake v\u00e4hemalt 3 k\u00fcsimusele enne j\u00e4tkamist."
            ),
            variant="tip",
            dismissible=True,
        ),
        Card(
            CardHeader(H3("2. samm: Tapsustamine", cls="card-title")),  # noqa: F405
            CardBody(*children)
            if children
            else CardBody(
                P("Kusimusi ei leitud.", cls="muted-text")  # noqa: F405
            ),
        ),
        P(A("\u2190 Tagasi koostaja nimekirja", href="/drafter"), cls="back-link"),  # noqa: F405
        title="Tapsustamine",
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
        job = _find_latest_job(session.id, "drafter_research")
        if job is None or job["status"] in ("pending", "claimed", "running"):
            return _step_waiting_page(session, 3, auth, "Ontoloogia uurimine...")
        if job["status"] == "failed":
            return _step_error_page(
                session,
                3,
                auth,
                job.get("error_message") or "Uurimine ebaonnestus.",
            )

    # Decrypt and show research results
    research: dict[str, Any] = {}
    if session.research_data_encrypted:
        try:
            research = json.loads(decrypt_text(session.research_data_encrypted))
        except Exception:
            logger.warning("Could not decrypt research data for session %s", session.id)

    provisions = research.get("provisions", [])
    eu_directives = research.get("eu_directives", [])
    court_decisions = research.get("court_decisions", [])
    topic_clusters = research.get("topic_clusters", [])

    cards: list[Any] = []

    # Summary cards for each category
    cards.append(_research_category_card("Oigusaktide satted", provisions, "provision"))
    cards.append(_research_category_card("EL-i oigusaktid", eu_directives, "eu"))
    cards.append(_research_category_card("Kohtulahendid", court_decisions, "court"))
    cards.append(_research_category_card("Teemaklastrid", topic_clusters, "cluster"))

    # Advance button
    advance_form = AppForm(
        Button("Jatka struktuuriga", type="submit", variant="primary"),
        Input(type="hidden", name="action", value="advance"),  # noqa: F405
        method="post",
        action=f"/drafter/{session.id}/step/3",
    )

    return PageShell(
        H1("Ontoloogia uurimine", cls="page-title"),  # noqa: F405
        _step_tracker(session.current_step),
        InfoBox(
            P(
                "S\u00fcsteem uurib ontoloogiat ja leiab seotud s\u00e4tted, "
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
        P(A("\u2190 Tagasi koostaja nimekirja", href="/drafter"), cls="back-link"),  # noqa: F405
        title="Uurimine",
        user=auth,
        active_nav="/drafter",
    )


def _research_category_card(title: str, items: list[dict[str, str]], category: str):
    """Render a summary card for a research category."""
    count = len(items)
    if count == 0:
        return Div(  # noqa: F405
            H4(f"{title}: 0", cls="research-category-title"),  # noqa: F405
            P("Tulemusi ei leitud.", cls="muted-text"),  # noqa: F405
            cls="research-category",
        )

    item_list: list[Any] = []
    for item in items[:10]:
        label = item.get("label") or item.get("act_label") or item.get("uri", "")
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
        job = _find_latest_job(session.id, "drafter_structure")
        if job is None or job["status"] in ("pending", "claimed", "running"):
            return _step_waiting_page(session, 4, auth, "Struktuuri genereerimine...")
        if job["status"] == "failed":
            return _step_error_page(
                session,
                4,
                auth,
                job.get("error_message") or "Struktuuri genereerimine ebaonnestus.",
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
            Button("Salvesta ja jatka koostamisega", type="submit", variant="primary"),
            cls="form-actions",
        ),
        method="post",
        action=f"/drafter/{session.id}/step/4",
    )

    wf_label = "VTK" if session.workflow_type == "vtk" else "Seadus"

    return PageShell(
        H1("Struktuuri muutmine", cls="page-title"),  # noqa: F405
        _step_tracker(session.current_step),
        InfoBox(
            P(
                "AI pakub v\u00e4lja seaduse struktuuri. Saate peat\u00fckke "
                "ja paragrahve muuta, lisada v\u00f5i eemaldada."
            ),
            variant="tip",
            dismissible=True,
        ),
        Card(
            CardHeader(H3(f"4. samm: Struktuur ({wf_label})", cls="card-title")),  # noqa: F405
            CardBody(form),
        ),
        P(A("\u2190 Tagasi koostaja nimekirja", href="/drafter"), cls="back-link"),  # noqa: F405
        title="Struktuur",
        user=auth,
        active_nav="/drafter",
    )


# ---------------------------------------------------------------------------
# Step 5: Clause-by-Clause Drafting
# ---------------------------------------------------------------------------


def _step_5_page(session: DraftingSession, auth: UserDict):
    """Render Step 5: Drafted clauses with inline editing."""
    if session.draft_content_encrypted is None:
        job = _find_latest_job(session.id, "drafter_draft")
        if job is None or job["status"] in ("pending", "claimed", "running"):
            return _step_waiting_page(session, 5, auth, "Seaduseteksti koostamine...")
        if job["status"] == "failed":
            return _step_error_page(
                session,
                5,
                auth,
                job.get("error_message") or "Koostamine ebaonnestus.",
            )

    # Decrypt clauses
    clauses: list[dict[str, Any]] = []
    if session.draft_content_encrypted:
        try:
            data = json.loads(decrypt_text(session.draft_content_encrypted))
            clauses = data.get("clauses", [])
        except Exception:
            logger.warning("Could not decrypt draft content for session %s", session.id)

    clause_items: list[Any] = []
    for i, clause in enumerate(clauses):
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

        clause_items.append(
            Div(  # noqa: F405
                H4(f"{para} {title}", cls="clause-heading"),  # noqa: F405
                Small(f"Peatukk: {chapter} {chapter_title}", cls="clause-chapter-ref muted-text"),  # noqa: F405
                Div(  # noqa: F405
                    P(text, cls="clause-text") if text else P("(Sisu puudub)", cls="muted-text"),  # noqa: F405
                    cls="clause-body",
                ),
                Div(*citation_links, cls="clause-citations") if citation_links else None,  # noqa: F405
                P(Em(f"Markus: {notes}"), cls="clause-notes muted-text") if notes else None,  # noqa: F405
                Div(  # noqa: F405
                    Button(  # noqa: F405
                        "Muuda",
                        hx_get=f"/drafter/{session.id}/step/5/edit/{i}",
                        hx_target=f"#clause-{i}",
                        hx_swap="outerHTML",
                        variant="ghost",
                        size="sm",
                    ),
                    Button(  # noqa: F405
                        "Genereeri uuesti",
                        hx_post=f"/drafter/{session.id}/step/5/regenerate/{i}",
                        hx_target=f"#clause-{i}",
                        hx_swap="outerHTML",
                        variant="ghost",
                        size="sm",
                    ),
                    AnnotationButton("provision", f"{session.id}-clause-{i}"),
                    cls="clause-actions",
                ),
                id=f"clause-{i}",
                cls="clause-item",
            )
        )

    # Advance form
    advance_form = AppForm(
        Button("Jatka ulevaatega", type="submit", variant="primary"),
        Input(type="hidden", name="action", value="advance"),  # noqa: F405
        method="post",
        action=f"/drafter/{session.id}/step/5",
    )

    return PageShell(
        H1("Seaduseteksti koostamine", cls="page-title"),  # noqa: F405
        _step_tracker(session.current_step),
        InfoBox(
            P(
                "AI koostab iga paragrahvi sisu viidete ja m\u00e4rkustega. "
                "Saate igat s\u00e4tet muuta v\u00f5i uuesti genereerida."
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
        P(A("\u2190 Tagasi koostaja nimekirja", href="/drafter"), cls="back-link"),  # noqa: F405
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
            H1("Integreeritud ulevaade", cls="page-title"),  # noqa: F405
            _step_tracker(session.current_step),
            InfoBox(
                P(
                    "Koostatud eeln\u00f5u anal\u00fc\u00fcsitakse "
                    "m\u00f5juanal\u00fc\u00fcsi s\u00fcsteemis. "
                    "Vaadake konflikte ja m\u00f5jutatud s\u00e4tteid."
                ),
                variant="info",
                dismissible=True,
            ),
            Card(
                CardHeader(H3("6. samm: Ulevaade", cls="card-title")),  # noqa: F405
                CardBody(
                    P(  # noqa: F405
                        "Selles sammus labitakse koostatud eelnou Phase 2 mojuanaluusi "
                        "torustiku kaudu. See loob eelnou (.docx), parsib selle, "
                        "tuvastab viited ja kuvab mojuanaluusi.",
                        cls="page-lead",
                    ),
                    AppForm(
                        Button("Kaivita mojuanaluus", type="submit", variant="primary"),
                        method="post",
                        action=f"/drafter/{session.id}/step/6",
                    ),
                ),
            ),
            P(A("\u2190 Tagasi koostaja nimekirja", href="/drafter"), cls="back-link"),  # noqa: F405
            title="Ulevaade",
            user=auth,
            active_nav="/drafter",
        )

    # Impact analysis is linked — show report inline
    draft_id = str(session.integrated_draft_id)

    return PageShell(
        H1("Integreeritud ulevaade", cls="page-title"),  # noqa: F405
        _step_tracker(session.current_step),
        InfoBox(
            P(
                "Koostatud eeln\u00f5u anal\u00fc\u00fcsitakse "
                "m\u00f5juanal\u00fc\u00fcsi s\u00fcsteemis. "
                "Vaadake konflikte ja m\u00f5jutatud s\u00e4tteid."
            ),
            variant="info",
            dismissible=True,
        ),
        Card(
            CardHeader(H3("6. samm: Ulevaade", cls="card-title")),  # noqa: F405
            CardBody(
                Alert("Mojuanaluus on seotud eelnouga.", variant="success"),
                P(  # noqa: F405
                    A(  # noqa: F405
                        "Vaata mojuanaluusi aruannet",
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
                        Button("Jatka ekspordiga", type="submit", variant="primary"),
                        Input(type="hidden", name="action", value="advance"),  # noqa: F405
                        method="post",
                        action=f"/drafter/{session.id}/step/6",
                    ),
                    cls="step-advance",
                ),
            ),
        ),
        P(A("< Tagasi koostaja nimekirja", href="/drafter"), cls="back-link"),  # noqa: F405
        title="Ulevaade",
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
        _step_tracker(session.current_step),
        InfoBox(
            P(
                "Laadige alla valmis .docx fail eeln\u00f5uga. "
                "Fail sisaldab AI-genereeritud m\u00e4rget ja "
                "viidete registrit."
            ),
            variant="tip",
            dismissible=True,
        ),
        Card(
            CardHeader(H3("7. samm: Eksport", cls="card-title")),  # noqa: F405
            CardBody(
                P(  # noqa: F405
                    "Teie eelnou on valmis. Laadige alla .docx fail.",
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
        _step_tracker(session.current_step),
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
        _step_tracker(session.current_step),
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


def step_page(req: Request, session_id: str, n: str):
    """GET /drafter/{session_id}/step/{n} -- render the step-specific page."""
    auth_or_redirect = _require_auth(req)
    if isinstance(auth_or_redirect, Response):
        return auth_or_redirect
    auth = auth_or_redirect

    parsed = _parse_uuid(session_id)
    if parsed is None:
        return _not_found_page(req)

    try:
        step_num = int(n)
        if step_num < 1 or step_num > 7:
            raise ValueError
    except (ValueError, TypeError):
        return _not_found_page(req)

    session = fetch_session(parsed)
    if session is None:
        return _not_found_page(req)
    if not can_access_drafter_session(auth, session):
        return _not_found_page(req)

    # Prevent viewing future steps — allow current and previous only.
    if step_num > session.current_step:
        return RedirectResponse(f"/drafter/{session_id}/step/{session.current_step}", 303)

    if step_num == 1:
        return _step_1_content(session, auth)
    elif step_num == 2:
        return _step_2_page(session, auth)
    elif step_num == 3:
        return _step_3_page(session, auth)
    elif step_num == 4:
        return _step_4_page(session, auth)
    elif step_num == 5:
        return _step_5_page(session, auth)
    elif step_num == 6:
        return _step_6_page(session, auth)
    elif step_num == 7:
        return _step_7_page(session, auth)
    else:
        return _not_found_page(req)


# ---------------------------------------------------------------------------
# POST /drafter/{session_id}/step/1 -- submit intent
# ---------------------------------------------------------------------------


async def submit_intent(req: Request, session_id: str):
    """POST /drafter/{session_id}/step/1 -- save intent and advance to step 2."""
    auth_or_redirect = _require_auth(req)
    if isinstance(auth_or_redirect, Response):
        return auth_or_redirect
    auth = auth_or_redirect

    parsed = _parse_uuid(session_id)
    if parsed is None:
        return _not_found_page(req)

    session = fetch_session(parsed)
    if session is None:
        return _not_found_page(req)
    if not can_access_drafter_session(auth, session):
        return _not_found_page(req)

    form_data = await req.form()
    intent = str(form_data.get("intent", "")).strip()

    # Validation
    if not intent:
        return _step_1_content(
            session,
            auth,
            error="Kavatsuse kirjeldus on kohustuslik.",
            intent_value="",
        )

    if len(intent) > _INTENT_MAX_LENGTH:
        return _step_1_content(
            session,
            auth,
            error=(
                f"Kavatsuse kirjeldus on liiga pikk "
                f"(maksimaalselt {_INTENT_MAX_LENGTH} tahemarki)."
            ),
            intent_value=intent,
        )

    # Save intent and advance step
    try:
        with _connect() as conn:
            update_session(conn, session.id, intent=intent)
            # Re-fetch the session to get the updated intent for advance_step
            updated = get_session(conn, session.id)
            if updated is None:
                raise RuntimeError("Session disappeared after update")
            advance_step(updated, conn)
            conn.commit()
    except StepTransitionError as exc:
        return _step_1_content(
            session,
            auth,
            error=str(exc),
            intent_value=intent,
        )
    except Exception:
        logger.exception("Failed to save intent for session %s", session_id)
        return _step_1_content(
            session,
            auth,
            error="Kavatsuse salvestamine ebaonnestus. Palun proovige uuesti.",
            intent_value=intent,
        )

    log_drafter_step_advance(auth.get("id"), session.id, 1, 2)

    # Enqueue the clarification question generation job
    try:
        queue = JobQueue()
        queue.enqueue(
            "drafter_clarify",
            {"session_id": str(session.id)},
            priority=0,
        )
    except Exception:
        logger.exception("Failed to enqueue drafter_clarify job for session %s", session.id)

    return RedirectResponse(
        url=f"/drafter/{session.id}/step/2",
        status_code=303,
    )


# ---------------------------------------------------------------------------
# GET /drafter/{session_id}/step/{n}/status -- HTMX polling fragment
# ---------------------------------------------------------------------------


def step_status_fragment(req: Request, session_id: str, n: str):
    """GET /drafter/{session_id}/step/{n}/status -- HTMX polling fragment.

    While a background job runs for step 2/3/4/5, this endpoint is
    polled every 3 seconds. When the job completes, it returns a redirect
    header telling HTMX to reload the full step page.
    """
    auth_or_redirect = _require_auth(req)
    if isinstance(auth_or_redirect, Response):
        return auth_or_redirect
    auth = auth_or_redirect

    parsed = _parse_uuid(session_id)
    if parsed is None:
        return Div("Sessiooni ei leitud.", id="step-status")  # noqa: F405

    session = fetch_session(parsed)
    if session is None or not can_access_drafter_session(auth, session):
        return Div("Sessiooni ei leitud.", id="step-status")  # noqa: F405

    try:
        step_num = int(n)
    except (ValueError, TypeError):
        step_num = session.current_step

    # Map step numbers to their job types
    step_job_map = {
        2: "drafter_clarify",
        3: "drafter_research",
        4: "drafter_structure",
        5: "drafter_draft",
    }

    job_type = step_job_map.get(step_num)
    if job_type:
        job = _find_latest_job(parsed, job_type)
        if job and job["status"] == "success":
            # Job is done — tell HTMX to redirect to the step page
            return Response(
                content="",
                status_code=200,
                headers={"HX-Redirect": f"/drafter/{session_id}/step/{step_num}"},
            )
        if job and job["status"] == "failed":
            error_msg = job.get("error_message") or "Tootlus ebaonnestus."
            return Div(  # noqa: F405
                Alert(error_msg, variant="danger"),
                id="step-status",
            )

    # Still running — keep polling
    _step_messages = {
        2: "Kusimuste genereerimine...",
        3: "Ontoloogia uurimine...",
        4: "Struktuuri genereerimine...",
        5: "Seaduseteksti koostamine...",
    }
    message = _step_messages.get(step_num, "Ootamine...")

    return Div(  # noqa: F405
        P(message, cls="muted-text"),  # noqa: F405
        Div(cls="spinner"),  # noqa: F405
        id="step-status",
        hx_get=f"/drafter/{session_id}/step/{n}/status",
        hx_trigger="every 3s",
        hx_swap="outerHTML",
    )


# ---------------------------------------------------------------------------
# POST /drafter/{session_id}/step/2 -- submit clarification answer
# ---------------------------------------------------------------------------


async def submit_clarification(req: Request, session_id: str):
    """POST /drafter/{session_id}/step/2 -- save answer or advance to step 3."""
    auth_or_redirect = _require_auth(req)
    if isinstance(auth_or_redirect, Response):
        return auth_or_redirect
    auth = auth_or_redirect

    parsed = _parse_uuid(session_id)
    if parsed is None:
        return _not_found_page(req)

    session = fetch_session(parsed)
    if session is None:
        return _not_found_page(req)
    if not can_access_drafter_session(auth, session):
        return _not_found_page(req)

    form_data = await req.form()
    action = str(form_data.get("action", ""))

    # Advance to step 3
    if action == "advance":
        try:
            with _connect() as conn:
                updated = get_session(conn, session.id)
                if updated is None:
                    raise RuntimeError("Session disappeared")
                advance_step(updated, conn)
                conn.commit()
        except StepTransitionError:
            return _step_2_page(session, auth)
        except Exception:
            logger.exception("Failed to advance from step 2 for session %s", session_id)
            return _step_2_page(session, auth)

        log_drafter_step_advance(auth.get("id"), session.id, 2, 3)

        # Enqueue research job
        try:
            queue = JobQueue()
            queue.enqueue(
                "drafter_research",
                {"session_id": str(session.id)},
                priority=0,
            )
        except Exception:
            logger.exception("Failed to enqueue drafter_research job for session %s", session.id)

        return RedirectResponse(
            url=f"/drafter/{session.id}/step/3",
            status_code=303,
        )

    # Save an answer
    answer = str(form_data.get("answer", "")).strip()
    question_index_raw = str(form_data.get("question_index", ""))

    if not answer:
        return _step_2_page(session, auth)

    try:
        question_index = int(question_index_raw)
    except (ValueError, TypeError):
        return _step_2_page(session, auth)

    clarifications = list(session.clarifications or [])
    if question_index < 0 or question_index >= len(clarifications):
        return _step_2_page(session, auth)

    clarifications[question_index]["answer"] = answer

    try:
        with _connect() as conn:
            update_session(conn, session.id, clarifications=clarifications)
            conn.commit()
    except Exception:
        logger.exception("Failed to save clarification answer for session %s", session_id)

    # Re-fetch and re-render
    session = fetch_session(parsed) or session
    return _step_2_page(session, auth)


# ---------------------------------------------------------------------------
# POST /drafter/{session_id}/step/3 -- advance from research to structure
# ---------------------------------------------------------------------------


async def advance_from_research(req: Request, session_id: str):
    """POST /drafter/{session_id}/step/3 -- advance to step 4."""
    auth_or_redirect = _require_auth(req)
    if isinstance(auth_or_redirect, Response):
        return auth_or_redirect
    auth = auth_or_redirect

    parsed = _parse_uuid(session_id)
    if parsed is None:
        return _not_found_page(req)

    session = fetch_session(parsed)
    if session is None:
        return _not_found_page(req)
    if not can_access_drafter_session(auth, session):
        return _not_found_page(req)

    try:
        with _connect() as conn:
            updated = get_session(conn, session.id)
            if updated is None:
                raise RuntimeError("Session disappeared")
            advance_step(updated, conn)
            conn.commit()
    except StepTransitionError:
        return _step_3_page(session, auth)
    except Exception:
        logger.exception("Failed to advance from step 3 for session %s", session_id)
        return _step_3_page(session, auth)

    log_drafter_step_advance(auth.get("id"), session.id, 3, 4)

    # Enqueue structure generation job
    try:
        queue = JobQueue()
        queue.enqueue(
            "drafter_structure",
            {"session_id": str(session.id)},
            priority=0,
        )
    except Exception:
        logger.exception("Failed to enqueue drafter_structure job for session %s", session.id)

    return RedirectResponse(
        url=f"/drafter/{session.id}/step/4",
        status_code=303,
    )


# ---------------------------------------------------------------------------
# POST /drafter/{session_id}/step/4 -- save edited structure + advance
# ---------------------------------------------------------------------------


async def submit_structure(req: Request, session_id: str):
    """POST /drafter/{session_id}/step/4 -- save structure and advance to step 5."""
    auth_or_redirect = _require_auth(req)
    if isinstance(auth_or_redirect, Response):
        return auth_or_redirect
    auth = auth_or_redirect

    parsed = _parse_uuid(session_id)
    if parsed is None:
        return _not_found_page(req)

    session = fetch_session(parsed)
    if session is None:
        return _not_found_page(req)
    if not can_access_drafter_session(auth, session):
        return _not_found_page(req)

    form_data = await req.form()

    # Reconstruct the structure from form fields
    law_title = str(form_data.get("law_title", ""))
    chapter_count_raw = str(form_data.get("chapter_count", "0"))
    try:
        chapter_count = int(chapter_count_raw)
    except ValueError:
        chapter_count = 0

    chapters: list[dict[str, Any]] = []
    for ci in range(chapter_count):
        chapter_number = str(form_data.get(f"chapter_{ci}_number", ""))
        chapter_title = str(form_data.get(f"chapter_{ci}_title", ""))
        section_count_raw = str(form_data.get(f"chapter_{ci}_section_count", "0"))
        try:
            section_count = int(section_count_raw)
        except ValueError:
            section_count = 0

        sections: list[dict[str, str]] = []
        for si in range(section_count):
            para = str(form_data.get(f"chapter_{ci}_section_{si}_paragraph", ""))
            sec_title = str(form_data.get(f"chapter_{ci}_section_{si}_title", ""))
            if para or sec_title:
                sections.append({"paragraph": para, "title": sec_title})

        if chapter_number or chapter_title:
            chapters.append(
                {
                    "number": chapter_number,
                    "title": chapter_title,
                    "sections": sections,
                }
            )

    structure = {
        "title": law_title,
        "chapters": chapters,
    }

    # Save the edited structure
    try:
        with _connect() as conn:
            update_session(conn, session.id, proposed_structure=structure)
            # Now advance
            updated = get_session(conn, session.id)
            if updated is None:
                raise RuntimeError("Session disappeared")
            advance_step(updated, conn)
            conn.commit()
    except StepTransitionError as exc:
        logger.warning("Cannot advance from step 4: %s", exc)
        session = fetch_session(parsed) or session
        return _step_4_page(session, auth)
    except Exception:
        logger.exception("Failed to save structure for session %s", session_id)
        session = fetch_session(parsed) or session
        return _step_4_page(session, auth)

    log_drafter_step_advance(auth.get("id"), session.id, 4, 5)

    # Enqueue clause drafting job
    try:
        queue = JobQueue()
        queue.enqueue(
            "drafter_draft",
            {"session_id": str(session.id)},
            priority=0,
        )
    except Exception:
        logger.exception("Failed to enqueue drafter_draft job for session %s", session.id)

    return RedirectResponse(
        url=f"/drafter/{session.id}/step/5",
        status_code=303,
    )


# ---------------------------------------------------------------------------
# POST /drafter/{session_id}/step/5 -- advance from drafting to review
# ---------------------------------------------------------------------------


async def submit_draft_advance(req: Request, session_id: str):
    """POST /drafter/{session_id}/step/5 -- advance to step 6."""
    auth_or_redirect = _require_auth(req)
    if isinstance(auth_or_redirect, Response):
        return auth_or_redirect
    auth = auth_or_redirect

    parsed = _parse_uuid(session_id)
    if parsed is None:
        return _not_found_page(req)

    session = fetch_session(parsed)
    if session is None:
        return _not_found_page(req)
    if not can_access_drafter_session(auth, session):
        return _not_found_page(req)

    form_data = await req.form()
    action = str(form_data.get("action", ""))

    if action == "advance":
        try:
            with _connect() as conn:
                updated = get_session(conn, session.id)
                if updated is None:
                    raise RuntimeError("Session disappeared")
                advance_step(updated, conn)
                conn.commit()
        except StepTransitionError as exc:
            logger.warning("Cannot advance from step 5: %s", exc)
            return _step_5_page(session, auth)
        except Exception:
            logger.exception("Failed to advance from step 5 for session %s", session_id)
            return _step_5_page(session, auth)

        log_drafter_step_advance(auth.get("id"), session.id, 5, 6)

        return RedirectResponse(
            url=f"/drafter/{session.id}/step/6",
            status_code=303,
        )

    return _step_5_page(session, auth)


# ---------------------------------------------------------------------------
# POST /drafter/{session_id}/step/6 -- trigger integrated review or advance
# ---------------------------------------------------------------------------


async def submit_review(req: Request, session_id: str):
    """POST /drafter/{session_id}/step/6 -- trigger Phase 2 pipeline or advance."""
    auth_or_redirect = _require_auth(req)
    if isinstance(auth_or_redirect, Response):
        return auth_or_redirect
    auth = auth_or_redirect

    parsed = _parse_uuid(session_id)
    if parsed is None:
        return _not_found_page(req)

    session = fetch_session(parsed)
    if session is None:
        return _not_found_page(req)
    if not can_access_drafter_session(auth, session):
        return _not_found_page(req)

    form_data = await req.form()
    action = str(form_data.get("action", ""))

    if action == "advance":
        # Advance from step 6 to step 7 (export)
        try:
            with _connect() as conn:
                updated = get_session(conn, session.id)
                if updated is None:
                    raise RuntimeError("Session disappeared")
                advance_step(updated, conn)
                conn.commit()
        except StepTransitionError as exc:
            logger.warning("Cannot advance from step 6: %s", exc)
            return _step_6_page(session, auth)
        except Exception:
            logger.exception("Failed to advance from step 6 for session %s", session_id)
            return _step_6_page(session, auth)

        log_drafter_step_advance(auth.get("id"), session.id, 6, 7)

        return RedirectResponse(
            url=f"/drafter/{session.id}/step/7",
            status_code=303,
        )

    # Trigger integrated review: assemble draft, call Phase 2 upload
    if session.integrated_draft_id is not None:
        # Already done — show the page
        return _step_6_page(session, auth)

    try:
        draft_id = _trigger_integrated_review(session, auth)
        with _connect() as conn:
            update_session(conn, session.id, integrated_draft_id=str(draft_id))
            conn.commit()
    except Exception:
        logger.exception("Failed to trigger integrated review for session %s", session_id)
        return PageShell(
            H1("Integreeritud ulevaade", cls="page-title"),  # noqa: F405
            _step_tracker(session.current_step),
            Card(
                CardBody(
                    Alert(
                        "Mojuanaluusi kaivitamine ebaonnestus. Palun proovige uuesti.",
                        variant="danger",
                    ),
                ),
            ),
            title="Ulevaade",
            user=auth,
            active_nav="/drafter",
        )

    # Re-fetch session and show
    session = fetch_session(parsed) or session
    return _step_6_page(session, auth)


def _trigger_integrated_review(session: DraftingSession, auth: UserDict) -> uuid.UUID:
    """Assemble draft into a .docx, call handle_upload, return draft_id.

    This is a synchronous helper that creates a temporary .docx from the
    session's proposed structure and drafted clauses, then passes it
    through Phase 2's upload pipeline.
    """
    from app.drafter.docx_builder import build_drafter_docx

    # Decrypt clauses
    clauses: list[dict[str, Any]] = []
    if session.draft_content_encrypted:
        data = json.loads(decrypt_text(session.draft_content_encrypted))
        clauses = data.get("clauses", [])

    structure = session.proposed_structure or {}
    title = structure.get("title", session.intent or "Eelnou")

    # Build .docx
    docx_path = build_drafter_docx(
        session_id=str(session.id),
        title=title,
        workflow_type=session.workflow_type,
        structure=structure,
        clauses=clauses,
    )

    # Create a draft row and enqueue parse_draft via Phase 2 infrastructure
    from app.docs.draft_model import create_draft
    from app.storage import store_file

    # Read the docx and store it encrypted
    docx_bytes = docx_path.read_bytes()
    stored = store_file(
        docx_bytes,
        filename=f"drafter-{session.id}.docx",
        owner_id=str(session.user_id),
    )

    graph_uri_prefix = "https://data.riik.ee/ontology/estleg/drafts/"

    with _connect() as conn:
        draft = create_draft(
            conn,
            user_id=session.user_id,
            org_id=session.org_id,
            title=title[:200],
            filename=f"drafter-{session.id}.docx",
            content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            file_size=len(docx_bytes),
            storage_path=stored.storage_path,
            graph_uri=f"{graph_uri_prefix}pending-{stored.storage_path}",
        )
        final_graph_uri = f"{graph_uri_prefix}{draft.id}"
        conn.execute(
            "UPDATE drafts SET graph_uri = %s WHERE id = %s",
            (final_graph_uri, str(draft.id)),
        )
        conn.commit()

    # Enqueue parse_draft job
    try:
        queue = JobQueue()
        queue.enqueue(
            "parse_draft",
            {"draft_id": str(draft.id)},
            priority=0,
        )
    except Exception:
        logger.exception("Failed to enqueue parse_draft for integrated review draft=%s", draft.id)

    return draft.id


# ---------------------------------------------------------------------------
# GET /drafter/{session_id}/export -- download .docx
# ---------------------------------------------------------------------------


def export_docx(req: Request, session_id: str):
    """GET /drafter/{session_id}/export -- generate and download .docx."""
    auth_or_redirect = _require_auth(req)
    if isinstance(auth_or_redirect, Response):
        return auth_or_redirect
    auth = auth_or_redirect

    parsed = _parse_uuid(session_id)
    if parsed is None:
        return _not_found_page(req)

    session = fetch_session(parsed)
    if session is None:
        return _not_found_page(req)
    if not can_access_drafter_session(auth, session):
        return _not_found_page(req)

    from app.drafter.docx_builder import build_drafter_docx

    # Decrypt clauses
    clauses: list[dict[str, Any]] = []
    if session.draft_content_encrypted:
        try:
            data = json.loads(decrypt_text(session.draft_content_encrypted))
            clauses = data.get("clauses", [])
        except Exception:
            logger.warning("Could not decrypt draft content for export, session %s", session.id)

    structure = session.proposed_structure or {}
    title = structure.get("title", session.intent or "Eelnou")

    # Try to get impact summary if available
    impact_summary: dict[str, Any] | None = None
    if session.integrated_draft_id:
        try:
            with _connect() as conn:
                row = conn.execute(
                    """
                    SELECT affected_count, conflict_count, gap_count, impact_score
                    FROM impact_reports
                    WHERE draft_id = %s
                    ORDER BY generated_at DESC
                    LIMIT 1
                    """,
                    (str(session.integrated_draft_id),),
                ).fetchone()
                if row:
                    impact_summary = {
                        "affected_count": row[0],
                        "conflict_count": row[1],
                        "gap_count": row[2],
                        "impact_score": row[3],
                    }
        except Exception:
            logger.warning("Could not load impact summary for session %s", session.id)

    docx_path = build_drafter_docx(
        session_id=str(session.id),
        title=title,
        workflow_type=session.workflow_type,
        structure=structure,
        clauses=clauses,
        impact_summary=impact_summary,
    )

    log_drafter_export(auth.get("id"), session.id)

    # Mark session as completed
    try:
        with _connect() as conn:
            update_session(conn, session.id, status="completed")
            conn.commit()
    except Exception:
        logger.exception("Failed to mark session %s as completed", session.id)

    # Notify the session owner that the drafter is complete.
    try:
        from app.notifications.wire import notify_drafter_complete

        notify_drafter_complete(session)
    except Exception:
        logger.debug("notify_drafter_complete failed (non-critical)", exc_info=True)

    filename = f"eelnou-{session.id}.docx"
    return FileResponse(
        str(docx_path),
        filename=filename,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )


# ---------------------------------------------------------------------------
# GET /drafter/{session_id}/step/5/edit/{clause_idx} -- inline edit form
# ---------------------------------------------------------------------------


def clause_edit_form(req: Request, session_id: str, clause_idx: str):
    """GET /drafter/{session_id}/step/5/edit/{clause_idx} -- HTMX inline edit form."""
    auth_or_redirect = _require_auth(req)
    if isinstance(auth_or_redirect, Response):
        return auth_or_redirect
    auth = auth_or_redirect

    parsed = _parse_uuid(session_id)
    if parsed is None:
        return Div("Sessiooni ei leitud.", id=f"clause-{clause_idx}")  # noqa: F405

    session = fetch_session(parsed)
    if session is None or not can_access_drafter_session(auth, session):
        return Div("Sessiooni ei leitud.", id=f"clause-{clause_idx}")  # noqa: F405

    try:
        idx = int(clause_idx)
    except (ValueError, TypeError):
        return Div("Vigane indeks.", id=f"clause-{clause_idx}")  # noqa: F405

    clauses: list[dict[str, Any]] = []
    if session.draft_content_encrypted:
        try:
            data = json.loads(decrypt_text(session.draft_content_encrypted))
            clauses = data.get("clauses", [])
        except Exception:
            pass

    if idx < 0 or idx >= len(clauses):
        return Div("Klausel ei leitud.", id=f"clause-{clause_idx}")  # noqa: F405

    clause = clauses[idx]

    return Div(  # noqa: F405
        H4(f"{clause.get('paragraph', '')} {clause.get('title', '')}", cls="clause-heading"),  # noqa: F405
        AppForm(
            Textarea(  # noqa: F405
                clause.get("text", ""),
                name="text",
                rows="8",
                cls="input textarea",
            ),
            Input(type="hidden", name="clause_index", value=str(idx)),  # noqa: F405
            Div(  # noqa: F405
                Button("Salvesta", type="submit", variant="primary", size="sm"),
                Button(  # noqa: F405
                    "Tuhista",
                    hx_get=f"/drafter/{session_id}/step/5",
                    hx_target="body",
                    hx_swap="outerHTML",
                    variant="ghost",
                    size="sm",
                ),
                cls="clause-actions",
            ),
            method="post",
            action=f"/drafter/{session_id}/step/5/save-clause",
        ),
        id=f"clause-{clause_idx}",
        cls="clause-item editing",
    )


# ---------------------------------------------------------------------------
# POST /drafter/{session_id}/step/5/save-clause -- save edited clause
# ---------------------------------------------------------------------------


async def save_clause(req: Request, session_id: str):
    """POST /drafter/{session_id}/step/5/save-clause -- save an edited clause."""
    auth_or_redirect = _require_auth(req)
    if isinstance(auth_or_redirect, Response):
        return auth_or_redirect
    auth = auth_or_redirect

    parsed = _parse_uuid(session_id)
    if parsed is None:
        return RedirectResponse(url="/drafter", status_code=303)

    session = fetch_session(parsed)
    if session is None or not can_access_drafter_session(auth, session):
        return RedirectResponse(url="/drafter", status_code=303)

    form_data = await req.form()
    text = str(form_data.get("text", ""))
    clause_index_raw = str(form_data.get("clause_index", ""))

    try:
        idx = int(clause_index_raw)
    except (ValueError, TypeError):
        return RedirectResponse(url=f"/drafter/{session_id}/step/5", status_code=303)

    clauses: list[dict[str, Any]] = []
    if session.draft_content_encrypted:
        try:
            data = json.loads(decrypt_text(session.draft_content_encrypted))
            clauses = data.get("clauses", [])
        except Exception:
            pass

    if 0 <= idx < len(clauses):
        clauses[idx]["text"] = text
        encrypted = encrypt_text(json.dumps({"clauses": clauses}, ensure_ascii=False))

        try:
            with _connect() as conn:
                update_session(conn, session.id, draft_content_encrypted=encrypted)
                conn.commit()
        except Exception:
            logger.exception("Failed to save clause edit for session %s", session_id)

        section_ref = f"{clauses[idx].get('chapter', '')}/{clauses[idx].get('paragraph', '')}"
        log_drafter_clause_edit(auth.get("id"), session.id, section_ref)

    return RedirectResponse(url=f"/drafter/{session_id}/step/5", status_code=303)


# ---------------------------------------------------------------------------
# POST /drafter/{session_id}/step/5/regenerate/{clause_idx} -- regenerate clause
# ---------------------------------------------------------------------------


def regenerate_clause(req: Request, session_id: str, clause_idx: str):
    """POST /drafter/{session_id}/step/5/regenerate/{clause_idx}.

    Enqueues a ``drafter_regenerate_clause`` background job and returns
    an HTMX polling fragment so the UI can check for completion.
    """
    auth_or_redirect = _require_auth(req)
    if isinstance(auth_or_redirect, Response):
        return auth_or_redirect
    auth = auth_or_redirect

    parsed = _parse_uuid(session_id)
    if parsed is None:
        return Div("Sessiooni ei leitud.")  # noqa: F405

    session = fetch_session(parsed)
    if session is None or not can_access_drafter_session(auth, session):
        return Div("Sessiooni ei leitud.")  # noqa: F405

    try:
        idx = int(clause_idx)
    except (ValueError, TypeError):
        return Div("Vigane indeks.")  # noqa: F405

    # Validate clause index
    clauses: list[dict[str, Any]] = []
    if session.draft_content_encrypted:
        try:
            data = json.loads(decrypt_text(session.draft_content_encrypted))
            clauses = data.get("clauses", [])
        except Exception:
            pass

    if idx < 0 or idx >= len(clauses):
        return Div("Klausel ei leitud.")  # noqa: F405

    section_ref = f"{clauses[idx].get('chapter', '')}/{clauses[idx].get('paragraph', '')}"
    log_drafter_regenerate(auth.get("id"), session.id, section_ref)

    # Enqueue the regeneration as a background job
    try:
        queue = JobQueue()
        queue.enqueue(
            "drafter_regenerate_clause",
            {
                "session_id": str(session.id),
                "clause_index": idx,
            },
            priority=0,
        )
    except Exception:
        logger.exception(
            "Failed to enqueue drafter_regenerate_clause for session %s clause %d",
            session_id,
            idx,
        )
        return Div(  # noqa: F405
            Alert("Uuesti genereerimine ebaonnestus.", variant="danger"),
            id=f"clause-{clause_idx}",
        )

    # Return polling fragment
    return Div(  # noqa: F405
        P("Klausli uuesti genereerimine...", cls="muted-text"),  # noqa: F405
        Div(cls="spinner"),  # noqa: F405
        id=f"clause-{clause_idx}",
        cls="clause-item regenerating",
        hx_get=f"/drafter/{session_id}/step/5/regenerate/{clause_idx}/status",
        hx_trigger="every 3s",
        hx_swap="outerHTML",
    )


def regenerate_clause_status(req: Request, session_id: str, clause_idx: str):
    """GET /drafter/{session_id}/step/5/regenerate/{clause_idx}/status.

    HTMX polling endpoint that checks whether the regeneration background
    job has completed. Returns the updated clause div on success, keeps
    polling on pending, and shows an error on failure.
    """
    auth_or_redirect = _require_auth(req)
    if isinstance(auth_or_redirect, Response):
        return auth_or_redirect
    auth = auth_or_redirect

    parsed = _parse_uuid(session_id)
    if parsed is None:
        return Div("Sessiooni ei leitud.", id=f"clause-{clause_idx}")  # noqa: F405

    session = fetch_session(parsed)
    if session is None or not can_access_drafter_session(auth, session):
        return Div("Sessiooni ei leitud.", id=f"clause-{clause_idx}")  # noqa: F405

    try:
        idx = int(clause_idx)
    except (ValueError, TypeError):
        return Div("Vigane indeks.", id=f"clause-{clause_idx}")  # noqa: F405

    # Check background job status — filter by BOTH session_id AND clause_index
    # so concurrent regenerations of different clauses don't cross-pollinate.
    job = _find_latest_job(parsed, "drafter_regenerate_clause", extra_filter={"clause_index": idx})
    if job and job["status"] == "success":
        # Re-read session to get updated clauses
        session = fetch_session(parsed) or session
        clauses: list[dict[str, Any]] = []
        if session.draft_content_encrypted:
            try:
                data = json.loads(decrypt_text(session.draft_content_encrypted))
                clauses = data.get("clauses", [])
            except Exception:
                pass

        if 0 <= idx < len(clauses):
            clause = clauses[idx]
            citations = clause.get("citations", [])
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
                H4(
                    f"{clause.get('paragraph', '')} {clause.get('title', '')}",
                    cls="clause-heading",
                ),  # noqa: F405
                Small(  # noqa: F405
                    f"Peatukk: {clause.get('chapter', '')} {clause.get('chapter_title', '')}",
                    cls="clause-chapter-ref muted-text",
                ),
                Div(  # noqa: F405
                    P(clause.get("text", ""), cls="clause-text"),  # noqa: F405
                    cls="clause-body",
                ),
                Div(*citation_links, cls="clause-citations") if citation_links else None,  # noqa: F405
                Alert("Uuesti genereeritud.", variant="success"),
                id=f"clause-{clause_idx}",
                cls="clause-item",
            )

    if job and job["status"] == "failed":
        return Div(  # noqa: F405
            Alert("Uuesti genereerimine ebaonnestus.", variant="danger"),
            id=f"clause-{clause_idx}",
        )

    # Still running — keep polling
    return Div(  # noqa: F405
        P("Klausli uuesti genereerimine...", cls="muted-text"),  # noqa: F405
        Div(cls="spinner"),  # noqa: F405
        id=f"clause-{clause_idx}",
        cls="clause-item regenerating",
        hx_get=f"/drafter/{session_id}/step/5/regenerate/{clause_idx}/status",
        hx_trigger="every 3s",
        hx_swap="outerHTML",
    )


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------


def register_drafter_routes(rt) -> None:  # type: ignore[no-untyped-def]
    """Mount the drafter wizard routes on the FastHTML route decorator *rt*.

    The drafter pages are behind the global auth ``Beforeware``, so
    **do not** add ``/drafter`` to ``SKIP_PATHS``.
    """
    rt("/drafter", methods=["GET"])(drafter_list_page)
    rt("/drafter/new", methods=["GET"])(new_session_page)
    rt("/drafter/new", methods=["POST"])(create_session_handler)
    rt("/drafter/{session_id}", methods=["GET"])(session_redirect)
    rt("/drafter/{session_id}/step/{n}", methods=["GET"])(step_page)
    rt("/drafter/{session_id}/step/1", methods=["POST"])(submit_intent)
    rt("/drafter/{session_id}/step/2", methods=["POST"])(submit_clarification)
    rt("/drafter/{session_id}/step/3", methods=["POST"])(advance_from_research)
    rt("/drafter/{session_id}/step/4", methods=["POST"])(submit_structure)
    rt("/drafter/{session_id}/step/5", methods=["POST"])(submit_draft_advance)
    rt("/drafter/{session_id}/step/5/edit/{clause_idx}", methods=["GET"])(clause_edit_form)
    rt("/drafter/{session_id}/step/5/save-clause", methods=["POST"])(save_clause)
    rt("/drafter/{session_id}/step/5/regenerate/{clause_idx}", methods=["POST"])(regenerate_clause)
    rt("/drafter/{session_id}/step/5/regenerate/{clause_idx}/status", methods=["GET"])(
        regenerate_clause_status
    )
    rt("/drafter/{session_id}/step/6", methods=["POST"])(submit_review)
    rt("/drafter/{session_id}/step/{n}/status", methods=["GET"])(step_status_fragment)
    rt("/drafter/{session_id}/export", methods=["GET"])(export_docx)
