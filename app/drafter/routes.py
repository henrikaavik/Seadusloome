"""FastHTML routes for the Phase 3A AI Law Drafter wizard.

Route map:

    GET  /drafter                          -- session list
    GET  /drafter/new                      -- workflow selection form
    POST /drafter/new                      -- create session handler
    GET  /drafter/{session_id}             -- redirect to current step
    GET  /drafter/{session_id}/step/{n}    -- step page (wizard)
    POST /drafter/{session_id}/step/1      -- submit intent
    GET  /drafter/{session_id}/step/{n}/status -- HTMX polling fragment

All routes require authentication (they are NOT in ``SKIP_PATHS``).
Cross-org access returns 404 to avoid leaking session existence.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, cast

from fasthtml.common import *  # noqa: F403
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

from app.auth.audit import log_action
from app.auth.provider import UserDict
from app.db import get_connection as _connect
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
from app.ui.data.data_table import Column, DataTable
from app.ui.data.pagination import Pagination
from app.ui.forms.app_form import AppForm
from app.ui.layout import PageShell
from app.ui.primitives.badge import Badge, BadgeVariant
from app.ui.primitives.button import Button
from app.ui.surfaces.alert import Alert
from app.ui.surfaces.card import Card, CardBody, CardHeader
from app.ui.theme import get_theme_from_request

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


def _require_auth(req: Request) -> Response | UserDict:
    """Return the auth dict or a 303 redirect to the login page."""
    auth = req.scope.get("auth")
    if not auth or not auth.get("id"):
        return RedirectResponse(url="/auth/login", status_code=303)
    return cast(UserDict, auth)


def _parse_uuid(raw: str) -> uuid.UUID | None:
    """Return a ``UUID`` parsed from *raw*, or ``None`` if invalid."""
    try:
        return uuid.UUID(raw)
    except (ValueError, TypeError):
        return None


def _not_found_page(req: Request):
    """Render the 404 page for missing or cross-org sessions."""
    auth = req.scope.get("auth")
    theme = get_theme_from_request(req)
    return PageShell(
        H1("Koostamissessioon ei leitud", cls="page-title"),  # noqa: F405
        Alert(
            "Otsitud koostamissessioon ei ole olemas voi Te ei oma selle vaatamise oigust.",
            variant="warning",
        ),
        P(A("< Tagasi koostaja nimekirja", href="/drafter"), cls="back-link"),  # noqa: F405
        title="Sessioon ei leitud",
        user=auth,
        theme=theme,
        active_nav="/drafter",
    )


def _format_timestamp(value: Any) -> str:
    """Render a ``datetime`` as dd.mm.YYYY HH:MM."""
    if value is None:
        return "\u2014"
    try:
        return value.strftime("%d.%m.%Y %H:%M")
    except AttributeError:
        return str(value)


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
    theme = get_theme_from_request(req)
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
        theme=theme,
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
    theme = get_theme_from_request(req)

    if not auth.get("org_id"):
        return PageShell(
            H1("Uus koostamine", cls="page-title"),  # noqa: F405
            Alert(
                "Te ei kuulu uhtegi organisatsiooni.",
                variant="warning",
            ),
            title="Uus koostamine",
            user=auth,
            theme=theme,
            active_nav="/drafter",
        )

    form, error_alert = _workflow_form()
    card_children: list = []
    if error_alert is not None:
        card_children.append(error_alert)
    card_children.append(form)

    return PageShell(
        H1("Uus koostamine", cls="page-title"),  # noqa: F405
        P(  # noqa: F405
            "Valige toovoo tyyp. AI koostaja juhib Teid labi 7-sammulise "
            "protsessi, mille lopuks saate valmis eelnou.",
            cls="page-lead",
        ),
        Card(CardBody(*card_children)),
        P(A("< Tagasi koostaja nimekirja", href="/drafter"), cls="back-link"),  # noqa: F405
        title="Uus koostamine",
        user=auth,
        theme=theme,
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
    theme = get_theme_from_request(req)
    org_id = auth.get("org_id")
    user_id = auth.get("id")

    if not org_id or not user_id:
        return PageShell(
            H1("Uus koostamine", cls="page-title"),  # noqa: F405
            Alert("Te ei kuulu uhtegi organisatsiooni.", variant="warning"),
            title="Uus koostamine",
            user=auth,
            theme=theme,
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
            theme=theme,
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
            theme=theme,
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
    if str(session.org_id) != str(auth.get("org_id")):
        return _not_found_page(req)

    return RedirectResponse(
        url=f"/drafter/{session_id}/step/{session.current_step}",
        status_code=303,
    )


# ---------------------------------------------------------------------------
# GET /drafter/{session_id}/step/{n} -- step pages
# ---------------------------------------------------------------------------


def _step_1_page(session: DraftingSession, auth: UserDict, theme: str):
    """Render Step 1: Intent Capture form."""
    return _step_1_content(session, auth, theme)


def _step_1_content(
    session: DraftingSession,
    auth: UserDict,
    theme: str,
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
        Card(
            CardHeader(H3("1. samm: Kavatsus", cls="card-title")),  # noqa: F405
            CardBody(*children),
        ),
        P(A("< Tagasi koostaja nimekirja", href="/drafter"), cls="back-link"),  # noqa: F405
        title="Kavatsus",
        user=auth,
        theme=theme,
        active_nav="/drafter",
    )


def _placeholder_step_page(
    session: DraftingSession,
    step_num: int,
    auth: UserDict,
    theme: str,
):
    """Render a placeholder page for steps 2-7."""
    step = Step(step_num)
    label = STEP_LABELS_ET.get(step, str(step_num))

    return PageShell(
        H1(f"{step_num}. samm: {label}", cls="page-title"),  # noqa: F405
        _step_tracker(session.current_step),
        Card(
            CardHeader(H3(f"{step_num}. samm: {label}", cls="card-title")),  # noqa: F405
            CardBody(
                P(  # noqa: F405
                    f'Sammu "{label}" teostus tuleb jargmises arendusetapis.',
                    cls="muted-text",
                ),
                Alert(
                    "See samm ei ole veel valmis. "
                    "Teostus tuleb jargmises arendusetapis (#495-#500).",
                    variant="info",
                    title="Tulemas",
                ),
            ),
        ),
        P(A("< Tagasi koostaja nimekirja", href="/drafter"), cls="back-link"),  # noqa: F405
        title=label,
        user=auth,
        theme=theme,
        active_nav="/drafter",
    )


def step_page(req: Request, session_id: str, n: str):
    """GET /drafter/{session_id}/step/{n} -- render the step-specific page."""
    auth_or_redirect = _require_auth(req)
    if isinstance(auth_or_redirect, Response):
        return auth_or_redirect
    auth = auth_or_redirect
    theme = get_theme_from_request(req)

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
    if str(session.org_id) != str(auth.get("org_id")):
        return _not_found_page(req)

    if step_num == 1:
        return _step_1_content(session, auth, theme)

    return _placeholder_step_page(session, step_num, auth, theme)


# ---------------------------------------------------------------------------
# POST /drafter/{session_id}/step/1 -- submit intent
# ---------------------------------------------------------------------------


async def submit_intent(req: Request, session_id: str):
    """POST /drafter/{session_id}/step/1 -- save intent and advance to step 2."""
    auth_or_redirect = _require_auth(req)
    if isinstance(auth_or_redirect, Response):
        return auth_or_redirect
    auth = auth_or_redirect
    theme = get_theme_from_request(req)

    parsed = _parse_uuid(session_id)
    if parsed is None:
        return _not_found_page(req)

    session = fetch_session(parsed)
    if session is None:
        return _not_found_page(req)
    if str(session.org_id) != str(auth.get("org_id")):
        return _not_found_page(req)

    form_data = await req.form()
    intent = str(form_data.get("intent", "")).strip()

    # Validation
    if not intent:
        return _step_1_content(
            session,
            auth,
            theme,
            error="Kavatsuse kirjeldus on kohustuslik.",
            intent_value="",
        )

    if len(intent) > _INTENT_MAX_LENGTH:
        return _step_1_content(
            session,
            auth,
            theme,
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
            theme,
            error=str(exc),
            intent_value=intent,
        )
    except Exception:
        logger.exception("Failed to save intent for session %s", session_id)
        return _step_1_content(
            session,
            auth,
            theme,
            error="Kavatsuse salvestamine ebaonnestus. Palun proovige uuesti.",
            intent_value=intent,
        )

    log_action(
        auth.get("id"),
        "drafter.step.advance",
        {
            "session_id": str(session.id),
            "from_step": 1,
            "to_step": 2,
        },
    )

    return RedirectResponse(
        url=f"/drafter/{session.id}/step/2",
        status_code=303,
    )


# ---------------------------------------------------------------------------
# GET /drafter/{session_id}/step/{n}/status -- HTMX polling fragment
# ---------------------------------------------------------------------------


def step_status_fragment(req: Request, session_id: str, n: str):
    """GET /drafter/{session_id}/step/{n}/status -- polling placeholder.

    Steps 2/3/4/5 will use this for async progress updates. For now,
    return a simple "Waiting..." div.
    """
    auth_or_redirect = _require_auth(req)
    if isinstance(auth_or_redirect, Response):
        return auth_or_redirect
    auth = auth_or_redirect

    parsed = _parse_uuid(session_id)
    if parsed is None:
        return Div("Sessiooni ei leitud.", id="step-status")  # noqa: F405

    session = fetch_session(parsed)
    if session is None or str(session.org_id) != str(auth.get("org_id")):
        return Div("Sessiooni ei leitud.", id="step-status")  # noqa: F405

    return Div(  # noqa: F405
        P("Ootamine...", cls="muted-text"),  # noqa: F405
        id="step-status",
        hx_get=f"/drafter/{session_id}/step/{n}/status",
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
    rt("/drafter/{session_id}/step/{n}/status", methods=["GET"])(step_status_fragment)
