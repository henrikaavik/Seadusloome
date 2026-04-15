"""FastHTML routes for the Phase 3B AI Advisory Chat.

Route map:

    GET  /chat                -- conversation list
    GET  /chat/new            -- create new conversation, redirect to view
    GET  /chat/{conv_id}      -- conversation view (message history + input)
    POST /chat/{conv_id}/delete -- delete conversation

All routes require authentication (they are NOT in ``SKIP_PATHS``).
Cross-org access returns 404 to avoid leaking conversation existence.
"""

from __future__ import annotations

import html as _html
import logging
import uuid
from datetime import datetime
from typing import Any

from fasthtml.common import *  # noqa: F403
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

from app.auth.helpers import require_auth as _require_auth
from app.auth.policy import can_access_conversation
from app.chat.audit import (
    log_chat_conversation_create,
    log_chat_conversation_delete,
)
from app.chat.models import (
    Conversation,
    create_conversation,
    delete_conversation,
    get_conversation,
    list_conversations_for_user,
    list_messages,
)
from app.chat.sanitize import render_markdown_safe, render_plaintext_safe
from app.db import get_connection as _connect
from app.ui.data.data_table import Column, DataTable
from app.ui.data.pagination import Pagination
from app.ui.layout import PageShell
from app.ui.primitives.annotation_button import AnnotationButton
from app.ui.primitives.badge import Badge
from app.ui.primitives.button import Button
from app.ui.surfaces.alert import Alert
from app.ui.surfaces.card import Card, CardBody, CardHeader
from app.ui.surfaces.info_box import InfoBox
from app.ui.theme import get_theme_from_request
from app.ui.time import format_tallinn

logger = logging.getLogger(__name__)

_PAGE_SIZE = 25


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_uuid(raw: str) -> uuid.UUID | None:
    """Return a UUID parsed from *raw*, or None if invalid."""
    try:
        return uuid.UUID(raw)
    except (ValueError, TypeError):
        return None


def _format_timestamp(value: Any) -> str:
    """Render a ``datetime`` in Europe/Tallinn (see app.ui.time)."""
    return format_tallinn(value)


def _not_found_page(req: Request):
    """Render the 404 page for missing or cross-org conversations."""
    auth = req.scope.get("auth")
    theme = get_theme_from_request(req)
    return PageShell(
        H1("Vestlust ei leitud", cls="page-title"),  # noqa: F405
        Alert(
            "Otsitud vestlust ei ole olemas voi Teil puudub selle vaatamise oigus.",
            variant="warning",
        ),
        P(A("< Tagasi vestluste nimekirja", href="/chat"), cls="back-link"),  # noqa: F405
        title="Vestlust ei leitud",
        user=auth,
        theme=theme,
        active_nav="/chat",
    )


def _get_message_count(conv_id: uuid.UUID) -> int:
    """Return the number of messages in a conversation."""
    try:
        with _connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM messages WHERE conversation_id = %s",
                (str(conv_id),),
            ).fetchone()
        return row[0] if row else 0
    except Exception:
        logger.exception("Failed to count messages for conversation %s", conv_id)
        return 0


def _get_last_message_at(conv_id: uuid.UUID) -> datetime | None:
    """Return the timestamp of the most recent message."""
    try:
        with _connect() as conn:
            row = conn.execute(
                "SELECT MAX(created_at) FROM messages WHERE conversation_id = %s",
                (str(conv_id),),
            ).fetchone()
        return row[0] if row and row[0] else None
    except Exception:
        logger.exception("Failed to get last message time for conversation %s", conv_id)
        return None


# ---------------------------------------------------------------------------
# GET /chat -- conversation list
# ---------------------------------------------------------------------------


def _conversation_rows(conversations: list[Conversation]) -> list[dict[str, Any]]:
    """Shape Conversation objects into dict rows for DataTable."""
    rows: list[dict[str, Any]] = []
    for c in conversations:
        msg_count = _get_message_count(c.id)
        last_msg = _get_last_message_at(c.id)
        rows.append(
            {
                "id": str(c.id),
                "title": c.title,
                "message_count": msg_count,
                "last_message_at": _format_timestamp(last_msg),
                "context_draft_id": str(c.context_draft_id) if c.context_draft_id else None,
                "created_at": _format_timestamp(c.created_at),
                "is_pinned": bool(getattr(c, "is_pinned", False)),
                "is_archived": bool(getattr(c, "is_archived", False)),
            }
        )
    return rows


def _conversation_list_columns() -> list[Column]:
    """Return the column definitions for the conversations DataTable."""

    def _title_cell(row: dict[str, Any]):
        # Star glyph (filled) when pinned.
        star = (
            Span("\u2605 ", cls="chat-pin-indicator", aria_label="Kinnitatud")  # noqa: F405
            if row["is_pinned"]
            else ""
        )
        return Span(  # noqa: F405
            star,
            A(  # noqa: F405
                row["title"],
                href=f"/chat/{row['id']}",
                cls="data-table-link",
            ),
        )

    def _context_cell(row: dict[str, Any]):
        if row["context_draft_id"]:
            return Badge("Eelnou", variant="primary")
        return Span("\u2014", cls="muted-text")  # noqa: F405

    def _actions_cell(row: dict[str, Any]):
        conv_id = row["id"]
        pin_label = "Eemalda kinnitus" if row["is_pinned"] else "Kinnita"
        archive_label = "Taasta" if row["is_archived"] else "Arhiveeri"
        pin_form = Form(  # noqa: F405
            Button(pin_label, variant="secondary", size="sm", type="submit"),
            method="post",
            action=f"/chat/{conv_id}/pin",
            hx_post=f"/chat/{conv_id}/pin",
            hx_target="body",
            hx_swap="outerHTML",
            cls="chat-list-action-form",
        )
        archive_form = Form(  # noqa: F405
            Button(archive_label, variant="secondary", size="sm", type="submit"),
            method="post",
            action=f"/chat/{conv_id}/archive",
            hx_post=f"/chat/{conv_id}/archive",
            hx_target="body",
            hx_swap="outerHTML",
            cls="chat-list-action-form",
        )
        rename_form = Form(  # noqa: F405
            Input(  # noqa: F405
                type="text",
                name="title",
                placeholder="Uus pealkiri",
                cls="chat-list-rename-input",
                required=True,
            ),
            Button("Nimeta", variant="secondary", size="sm", type="submit"),
            method="post",
            action=f"/chat/{conv_id}/rename",
            hx_post=f"/chat/{conv_id}/rename",
            hx_target="body",
            hx_swap="outerHTML",
            cls="chat-list-action-form chat-list-rename-form",
        )
        delete_form = Form(  # noqa: F405
            Button("Kustuta", variant="danger", size="sm", type="submit"),
            method="post",
            action=f"/chat/{conv_id}/delete",
            hx_post=f"/chat/{conv_id}/delete",
            hx_confirm="Kas olete kindel, et soovite vestluse kustutada?",
            hx_target="body",
            hx_swap="outerHTML",
            cls="chat-list-action-form",
        )
        open_link = A(  # noqa: F405
            "Ava",
            href=f"/chat/{conv_id}",
            cls="btn btn-secondary btn-sm",
        )
        return Div(  # noqa: F405
            open_link,
            pin_form,
            archive_form,
            rename_form,
            delete_form,
            cls="chat-list-actions",
        )

    return [
        Column(key="title", label="Pealkiri", sortable=False, render=_title_cell),
        Column(key="message_count", label="Sonumeid", sortable=False),
        Column(key="last_message_at", label="Viimane sonum", sortable=False),
        Column(key="context", label="Kontekst", sortable=False, render=_context_cell),
        Column(key="created_at", label="Loodud", sortable=False),
        Column(key="actions", label="Tegevused", sortable=False, render=_actions_cell),
    ]


def _count_conversations_for_user(
    user_id: str,
    *,
    search: str | None = None,
    include_archived: bool = False,
) -> int:
    """Count total conversations for a user.

    Mirrors the filters used by :func:`list_conversations_for_user` so the
    pagination total reflects the same result set that is rendered.
    """
    where = ["user_id = %s"]
    params: list[Any] = [user_id]
    if not include_archived:
        where.append("is_archived = FALSE")
    if search:
        where.append("title ILIKE %s")
        params.append(f"%{search}%")
    try:
        with _connect() as conn:
            row = conn.execute(
                f"SELECT COUNT(*) FROM conversations WHERE {' AND '.join(where)}",  # type: ignore[arg-type]
                tuple(params),
            ).fetchone()
        return row[0] if row else 0
    except Exception:
        logger.exception("Failed to count conversations for user %s", user_id)
        return 0


def chat_list_page(req: Request):
    """GET /chat -- paginated list of the caller's conversations."""
    auth_or_redirect = _require_auth(req)
    if isinstance(auth_or_redirect, Response):
        return auth_or_redirect
    auth = auth_or_redirect
    theme = get_theme_from_request(req)
    user_id = auth.get("id")

    page_str = req.query_params.get("page", "1")
    try:
        page = max(1, int(page_str))
    except ValueError:
        page = 1
    offset = (page - 1) * _PAGE_SIZE

    # New: search + include-archived filters (migration 017 UX polish).
    search_q = (req.query_params.get("q") or "").strip()
    include_archived = req.query_params.get("archived") in ("1", "true", "on")

    if not user_id:
        body: Any = Alert(
            "Kasutaja andmed puuduvad.",
            variant="warning",
        )
        pagination = None
    else:
        try:
            with _connect() as conn:
                conversations = list_conversations_for_user(
                    conn,
                    user_id,
                    limit=_PAGE_SIZE,
                    offset=offset,
                    include_archived=include_archived,
                    pinned_first=True,
                    search=search_q or None,
                )
        except Exception:
            logger.exception("Failed to list conversations")
            conversations = []

        total = _count_conversations_for_user(
            user_id,
            search=search_q or None,
            include_archived=include_archived,
        )
        total_pages = max(1, (total + _PAGE_SIZE - 1) // _PAGE_SIZE)

        if total == 0 and not search_q:
            body = Div(  # noqa: F405
                P(  # noqa: F405
                    "Vestlusi pole. Alustage uut vestlust!",
                    cls="muted-text",
                ),
                A(  # noqa: F405
                    "Alusta uut vestlust",
                    href="/chat/new",
                    cls="btn btn-primary btn-md",
                ),
                cls="empty-state",
            )
            pagination = None
        else:
            body = Div(  # noqa: F405
                DataTable(
                    columns=_conversation_list_columns(),
                    rows=_conversation_rows(conversations),
                    empty_message="Vestlusi ei leitud.",
                ),
                id="chat-list-body",
            )
            pagination = (
                Pagination(
                    current_page=page,
                    total_pages=total_pages,
                    base_url="/chat",
                    page_size=_PAGE_SIZE,
                    total=total,
                )
                if total > 0
                else None
            )

    # Search + archived toolbar
    toolbar = Form(  # noqa: F405
        Input(  # noqa: F405
            type="search",
            name="q",
            placeholder="Otsi vestlusi...",
            value=search_q,
            cls="chat-search-input",
            aria_label="Otsi vestlusi",
        ),
        Label(  # noqa: F405
            Input(  # noqa: F405
                type="checkbox",
                name="archived",
                value="1",
                checked=include_archived,
            ),
            " N\u00e4ita arhiveeritud",
            cls="chat-archived-toggle",
        ),
        Button("Otsi", variant="secondary", size="sm", type="submit"),
        method="get",
        action="/chat",
        hx_get="/chat/search",
        hx_target="#chat-list-body",
        hx_trigger=(
            "input changed delay:300ms from:input[name='q'], "
            "change from:input[name='archived'], submit"
        ),
        cls="chat-list-toolbar",
    )

    header_children: list = [H1("Vestlused", cls="page-title")]  # noqa: F405
    header_children.append(
        InfoBox(
            P(
                "AI n\u00f5ustaja vastab k\u00fcsimustele Eesti \u00f5iguse kohta. "
                "Vestlused on privaatsed ja seotud teie organisatsiooniga."
            ),
            variant="info",
            dismissible=True,
        )
    )
    header_children.append(
        Div(  # noqa: F405
            A(  # noqa: F405
                "Uus vestlus",
                href="/chat/new",
                cls="btn btn-primary btn-md",
            ),
            cls="page-actions",
        )
    )

    card_body_children: list = [toolbar, body]
    if pagination is not None:
        card_body_children.append(pagination)

    return PageShell(
        *header_children,
        Card(
            CardHeader(H3("Minu vestlused", cls="card-title")),  # noqa: F405
            CardBody(*card_body_children),
        ),
        title="Vestlused",
        user=auth,
        theme=theme,
        active_nav="/chat",
    )


# ---------------------------------------------------------------------------
# GET /chat/new -- create new conversation
# ---------------------------------------------------------------------------


def new_conversation(req: Request):
    """GET /chat/new -- create a new conversation and redirect to it.

    Optionally accepts ``?draft=<draft_id>`` to bind the conversation
    to a specific draft. The title is auto-generated.
    """
    auth_or_redirect = _require_auth(req)
    if isinstance(auth_or_redirect, Response):
        return auth_or_redirect
    auth = auth_or_redirect

    user_id = auth.get("id")
    org_id = auth.get("org_id")

    if not user_id or not org_id:
        return RedirectResponse(url="/chat", status_code=303)

    # Optional draft context
    draft_param = req.query_params.get("draft")
    context_draft_id: uuid.UUID | None = None
    title = "Vestlus"

    if draft_param:
        parsed_draft = _parse_uuid(draft_param)
        if parsed_draft:
            # Validate the draft belongs to the user's org
            draft_title = _get_draft_title(str(parsed_draft), org_id)
            if draft_title is not None:
                context_draft_id = parsed_draft
                title = f"Vestlus \u2014 {draft_title}"
            else:
                # Draft not found or belongs to another org — ignore it
                logger.warning(
                    "Draft %s not found or not owned by org %s",
                    parsed_draft,
                    org_id,
                )

    if not context_draft_id:
        from app.ui.time import now_tallinn

        title = f"Vestlus \u2014 {now_tallinn().strftime('%d.%m.%Y %H:%M')}"

    try:
        with _connect() as conn:
            conversation = create_conversation(
                conn,
                user_id,
                org_id,
                title=title,
                context_draft_id=context_draft_id,
            )
            conn.commit()
    except Exception:
        logger.exception("Failed to create conversation")
        return RedirectResponse(url="/chat", status_code=303)

    log_chat_conversation_create(
        user_id,
        conversation.id,
        context_draft_id,
    )

    return RedirectResponse(url=f"/chat/{conversation.id}", status_code=303)


def _get_draft_title(draft_id: str, org_id: str) -> str | None:
    """Attempt to read the draft title from the DB.

    Returns ``None`` when the draft does not exist **or** belongs to a
    different organisation — preventing cross-org context leakage.
    """
    try:
        with _connect() as conn:
            row = conn.execute(
                "SELECT filename FROM drafts WHERE id = %s AND org_id = %s",
                (draft_id, org_id),
            ).fetchone()
        return row[0] if row else None
    except Exception:
        logger.exception("Failed to read draft title for %s", draft_id)
        return None


# ---------------------------------------------------------------------------
# GET /chat/{conv_id} -- conversation view
# ---------------------------------------------------------------------------


def _message_action_row(
    *,
    conv_id: str,
    msg_id: str,
    is_user: bool,
):
    """Render the per-message action toolbar (regenerate/copy/feedback).

    User messages get Edit + Copy; assistant messages get the full set.
    All buttons carry ``data-message-id`` + ``data-conv-id`` so chat.js
    can dispatch the right fetch without needing inline handlers.
    """
    common_attrs: dict[str, Any] = {
        "data_message_id": msg_id,
        "data_conv_id": conv_id,
    }

    def _btn(label: str, action: str, title: str):
        return Button(  # noqa: F405
            label,
            type="button",
            variant="secondary",
            size="sm",
            cls="chat-message-action-btn",
            title=title,
            data_action=action,
            **common_attrs,
        )

    if is_user:
        return Div(  # noqa: F405
            _btn("Muuda", "edit", "Muuda ja saada uuesti"),
            _btn("Kopeeri", "copy", "Kopeeri"),
            cls="chat-message-actions",
        )

    return Div(  # noqa: F405
        _btn("Genereeri uuesti", "regenerate", "Genereeri uuesti"),
        _btn("Kopeeri", "copy", "Kopeeri"),
        _btn("\u2191", "feedback-up", "Kasulik"),
        _btn("\u2193", "feedback-down", "Ei olnud kasulik"),
        cls="chat-message-actions",
    )


def _rag_sources_block(rag_context: list[dict] | None):
    """Render a ``<details>`` disclosure of RAG source chunks."""
    if not rag_context:
        return ""

    items = []
    for chunk in rag_context:
        if not isinstance(chunk, dict):
            continue
        source_uri = str(chunk.get("source_uri") or "").strip()
        content = str(chunk.get("content") or "")
        # Derive a title from the last URI segment; fall back to the URI.
        title = source_uri.rstrip("/").rsplit("/", 1)[-1] if source_uri else "(allikas)"
        snippet = content[:200].strip()
        if len(content) > 200:
            snippet = snippet + "\u2026"
        if source_uri:
            link: Any = A(  # noqa: F405
                title,
                href=f"/explorer?q={source_uri}",
                target="_blank",
                rel="noopener noreferrer",
            )
        else:
            link = Span(title)  # noqa: F405
        items.append(
            Li(  # noqa: F405
                link,
                P(snippet, cls="chat-source-snippet") if snippet else "",  # noqa: F405
            )
        )

    if not items:
        return ""

    return Details(  # noqa: F405
        Summary(f"Allikad ({len(items)})"),  # noqa: F405
        Ul(*items, cls="chat-sources-list"),  # noqa: F405
        cls="chat-sources",
    )


def _render_message(msg: Any):
    """Render a single message as a chat bubble."""
    role = msg.role
    content = msg.content or ""
    msg_id = str(msg.id) if getattr(msg, "id", None) else ""
    is_pinned = bool(getattr(msg, "is_pinned", False))
    is_truncated = bool(getattr(msg, "is_truncated", False))

    if role == "user":
        wrapper_cls = "chat-message chat-message-user"
        if is_pinned:
            wrapper_cls += " chat-message--pinned"
        pin_indicator = (
            Span("\u2605", cls="chat-pin-indicator", title="Kinnitatud")  # noqa: F405
            if is_pinned
            else ""
        )
        # Escape user-authored text but preserve newlines as <br>.
        return Div(  # noqa: F405
            Div(  # noqa: F405
                pin_indicator,
                P(Safe(render_plaintext_safe(content)), cls="chat-message-text"),  # noqa: F405
                cls="chat-bubble chat-bubble-user",
            ),
            _message_action_row(
                conv_id=str(getattr(msg, "conversation_id", "") or ""),
                msg_id=msg_id,
                is_user=True,
            ),
            cls=wrapper_cls,
            data_message_id=msg_id,
        )
    elif role == "assistant":
        rendered = render_markdown_safe(content)
        if is_truncated:
            # Append an italic note that the stream was interrupted.
            rendered = (
                rendered + ' \u2014 <em class="chat-message-truncated">vastus katkestati</em>'
            )

        wrapper_cls = "chat-message chat-message-assistant"
        if is_pinned:
            wrapper_cls += " chat-message--pinned"
        pin_indicator = (
            Span("\u2605", cls="chat-pin-indicator", title="Kinnitatud")  # noqa: F405
            if is_pinned
            else ""
        )

        extras: list = []
        sources = _rag_sources_block(getattr(msg, "rag_context", None))
        if sources:
            extras.append(sources)

        conv_id_str = str(getattr(msg, "conversation_id", "") or "")
        return Div(  # noqa: F405
            Div(  # noqa: F405
                pin_indicator,
                Div(Safe(rendered), cls="chat-message-text"),  # noqa: F405
                cls="chat-bubble chat-bubble-assistant",
            ),
            *extras,
            _message_action_row(
                conv_id=conv_id_str,
                msg_id=msg_id,
                is_user=False,
            ),
            AnnotationButton("conversation", msg_id) if msg_id else "",
            cls=wrapper_cls,
            data_message_id=msg_id,
        )
    elif role == "tool":
        tool_name = msg.tool_name or "tool"
        return Div(  # noqa: F405
            Div(  # noqa: F405
                P(f"[{tool_name}]", cls="chat-tool-label"),  # noqa: F405
                cls="chat-bubble chat-bubble-tool",
            ),
            cls="chat-message chat-message-tool",
            data_message_id=msg_id,
        )
    # system messages are hidden
    return ""


# ---------------------------------------------------------------------------
# Empty-state example prompts
# ---------------------------------------------------------------------------

# Default prompts — shown when the conversation has no messages yet.
_DEFAULT_EXAMPLE_PROMPTS: tuple[tuple[str, str], ...] = (
    (
        "Isikuandmete t\u00f6\u00f6tlemine",
        "Millised seadused m\u00f5jutavad isikuandmete t\u00f6\u00f6tlemist?",
    ),
    (
        "Vastuolud EL \u00f5igusega",
        "Leia vastuolud EL \u00f5igusega minu eeln\u00f5us",
    ),
    (
        "Kohtupraktika",
        "N\u00e4ita hiljutisi Riigikohtu lahendeid teemal X",
    ),
    (
        "Lihtsas keeles",
        "Selgita s\u00e4tet lihtsas keeles: ...",
    ),
    (
        "M\u00f5jude kaardistus",
        "Millised s\u00e4tted seotud karistusseadustikuga peavad muutuma?",
    ),
)

# Draft-specific prompts — used when the conversation is anchored to a draft.
_DRAFT_EXAMPLE_PROMPTS: tuple[tuple[str, str], ...] = (
    (
        "V\u00f5rdle kehtiva \u00f5igusega",
        "V\u00f5rdle eeln\u00f5u kehtiva regulatsiooniga",
    ),
    (
        "Leia vastuolud",
        "Leia vastuolud EL \u00f5igusega selles eeln\u00f5us",
    ),
    (
        "Sarnased eeln\u00f5ud",
        "Leia varasemad sarnased eeln\u00f5ud",
    ),
    (
        "Kohtulahendid",
        "Millised kohtulahendid m\u00f5jutavad seda eeln\u00f5u?",
    ),
)


def _empty_state(prompts: tuple[tuple[str, str], ...]):
    """Render the initial empty-state block with example prompt cards."""
    cards = []
    for label, prompt_text in prompts:
        cards.append(
            Button(  # noqa: F405
                Span(label, cls="chat-example-prompt-label"),  # noqa: F405
                Span(prompt_text, cls="chat-example-prompt-hint"),  # noqa: F405
                type="button",
                variant="secondary",
                cls="chat-example-prompt",
                data_prompt=prompt_text,
            )
        )
    return Div(  # noqa: F405
        Div(  # noqa: F405
            "Tere tulemast AI \u00f5igusn\u00f5ustaja juurde",
            cls="chat-empty-state-title",
        ),
        Div(  # noqa: F405
            "K\u00fcsi ja ma otsin vastused Eesti \u00f5iguse ontoloogiast ning kohtupraktikast.",
            cls="chat-empty-state-body",
        ),
        Div(*cards, cls="chat-example-prompts"),  # noqa: F405
        id="chat-empty-state",
        cls="chat-empty-state",
    )


def conversation_view_page(req: Request, conv_id: str):
    """GET /chat/{conv_id} -- render the conversation view."""
    auth_or_redirect = _require_auth(req)
    if isinstance(auth_or_redirect, Response):
        return auth_or_redirect
    auth = auth_or_redirect
    theme = get_theme_from_request(req)

    parsed = _parse_uuid(conv_id)
    if parsed is None:
        return _not_found_page(req)

    # Load conversation
    try:
        with _connect() as conn:
            conversation = get_conversation(conn, parsed)
    except Exception:
        logger.exception("Failed to load conversation %s", conv_id)
        return _not_found_page(req)

    if conversation is None:
        return _not_found_page(req)

    # Cross-org access check
    if not can_access_conversation(auth, conversation):
        return _not_found_page(req)

    # Load messages
    try:
        with _connect() as conn:
            messages = list_messages(conn, parsed)
    except Exception:
        logger.exception("Failed to load messages for conversation %s", conv_id)
        messages = []

    # Render message history (excluding system turns which are UI-invisible)
    visible_messages = [m for m in messages if m.role != "system"]
    message_bubbles = [_render_message(m) for m in visible_messages]

    # Empty-state — only shown on the initial render with no user/assistant turns.
    prompts = _DRAFT_EXAMPLE_PROMPTS if conversation.context_draft_id else _DEFAULT_EXAMPLE_PROMPTS
    empty_state = _empty_state(prompts) if not visible_messages else ""

    # Draft context header
    draft_header = None
    if conversation.context_draft_id:
        draft_title = _get_draft_title(str(conversation.context_draft_id), str(auth.get("org_id")))
        draft_label = draft_title or str(conversation.context_draft_id)
        draft_header = Div(  # noqa: F405
            Badge("Eelnou", variant="primary"),
            A(  # noqa: F405
                f" Seotud eelnouga: {draft_label}",
                href=f"/drafts/{conversation.context_draft_id}",
                cls="draft-context-link",
            ),
            A(  # noqa: F405
                "Vaata m\u00f5ju",
                href=f"/drafts/{conversation.context_draft_id}",
                target="_blank",
                rel="noopener noreferrer",
                cls="draft-context-impact-link",
            ),
            cls="chat-draft-context",
        )

    # Connection/quota header row. JS populates quota live via /api/me/usage.
    header_meta = Div(  # noqa: F405
        Span(  # noqa: F405
            "\u00dchendatud",
            id="chat-status",
            cls="chat-status chat-status--connected",
            role="status",
            aria_live="polite",
        ),
        Div(  # noqa: F405
            Span(cls="chat-quota-label"),  # noqa: F405
            Span(  # noqa: F405
                Span(cls="chat-quota-bar"),  # noqa: F405
                cls="chat-quota-bar-wrap",
            ),
            id="chat-quota",
            cls="chat-quota",
            role="progressbar",
            aria_valuemin="0",
            aria_valuemax="100",
            aria_valuenow="0",
            # Marked busy until the first /api/me/usage response paints real
            # values; screen readers otherwise announce a misleading "0".
            # chat.js clears both attributes after the first refresh.
            aria_busy="true",
            data_initial="true",
        ),
        cls="chat-header-meta",
    )

    # Build chat page
    chat_container = Div(  # noqa: F405
        header_meta,
        # Message history + empty-state wrapper. aria-live=polite lets
        # screen readers pick up streamed assistant turns.
        Div(  # noqa: F405
            empty_state,
            *message_bubbles,
            id="chat-messages",
            cls="chat-messages",
            aria_live="polite",
        ),
        # Input area help text
        Small(  # noqa: F405
            "K\u00fcsige k\u00fcsimusi Eesti seaduste, kohtuotsuste v\u00f5i "
            "EL-i \u00f5igusaktide kohta. AI kasutab ontoloogiat ja RAG-i "
            "vastuste p\u00f5hjendamiseks. "
            "Vajuta / nupuks, et n\u00e4ha k\u00e4sklusi.",
            cls="form-field-help",
        ),
        # Slash-command palette (populated by chat.js)
        Div(id="chat-slash-palette", cls="chat-slash-palette", hidden=True),  # noqa: F405
        # Input area
        Div(  # noqa: F405
            Textarea(  # noqa: F405
                id="chat-input",
                name="content",
                placeholder=(
                    "K\u00fcsi Eesti \u00f5iguse kohta... "
                    "(Enter = uus rida, Cmd/Ctrl+Enter = saada, / = k\u00e4sud)"
                ),
                cls="chat-input",
                rows="2",
            ),
            Button("Saada", id="chat-send-btn", variant="primary", type="button"),
            Button(
                "Peata",
                id="chat-stop-btn",
                variant="secondary",
                type="button",
                hidden=True,
            ),
            cls="chat-input-area",
        ),
        id="chat-container",
        data_conversation_id=str(parsed),
        cls="chat-container",
    )

    # Document-root toast container (JS appends toasts here).
    toast_container = Div(  # noqa: F405
        id="chat-toast-container",
        cls="chat-toast-container",
        aria_live="polite",
        aria_atomic="true",
    )

    # Delete button
    delete_form = Form(  # noqa: F405
        Button("Kustuta vestlus", variant="danger", type="submit", size="sm"),
        method="post",
        action=f"/chat/{parsed}/delete",
        hx_post=f"/chat/{parsed}/delete",
        hx_confirm="Kas olete kindel, et soovite vestluse kustutada?",
        hx_target="body",
        hx_swap="outerHTML",
        cls="chat-delete-form",
    )

    header_children: list = [
        H1(conversation.title, cls="page-title"),  # noqa: F405
        Div(delete_form, cls="page-actions"),  # noqa: F405
    ]

    content_parts: list = []
    if draft_header:
        content_parts.append(draft_header)
    content_parts.append(chat_container)

    # Vendor libs for markdown rendering on the client side. The URLs pin
    # exact patch versions (marked@12.0.2, dompurify@3.1.6) so SRI hashes
    # provide a meaningful defence against a compromised CDN serving altered
    # bytes. Bumping either version requires recomputing the sha384 digest:
    #
    #     curl -sL <url> | openssl dgst -sha384 -binary | openssl base64 -A
    #
    # The server-side ``render_markdown_safe`` path remains the primary XSS
    # boundary; DOMPurify on the client is belt-and-suspenders for messages
    # rendered entirely from streaming deltas.
    vendor_scripts = (
        Script(  # noqa: F405
            src="https://cdn.jsdelivr.net/npm/marked@12.0.2/marked.min.js",
            integrity="sha384-/TQbtLCAerC3jgaim+N78RZSDYV7ryeoBCVqTuzRrFec2akfBkHS7ACQ3PQhvMVi",
            crossorigin="anonymous",
            defer=True,
        ),
        Script(  # noqa: F405
            src="https://cdn.jsdelivr.net/npm/dompurify@3.1.6/dist/purify.min.js",
            integrity="sha384-+VfUPEb0PdtChMwmBcBmykRMDd+v6D/oFmB3rZM/puCMDYcIvF968OimRh4KQY9a",
            crossorigin="anonymous",
            defer=True,
        ),
        Script(src="/static/js/chat.js", defer=True),  # noqa: F405
    )

    return PageShell(
        *header_children,
        Card(
            CardBody(*content_parts),
        ),
        toast_container,
        *vendor_scripts,
        title=conversation.title,
        user=auth,
        theme=theme,
        active_nav="/chat",
    )


# ---------------------------------------------------------------------------
# POST /chat/{conv_id}/delete -- delete conversation
# ---------------------------------------------------------------------------


def delete_conversation_handler(req: Request, conv_id: str):
    """POST /chat/{conv_id}/delete -- delete a conversation."""
    auth_or_redirect = _require_auth(req)
    if isinstance(auth_or_redirect, Response):
        return auth_or_redirect
    auth = auth_or_redirect

    parsed = _parse_uuid(conv_id)
    if parsed is None:
        return _not_found_page(req)

    # Load and verify ownership
    try:
        with _connect() as conn:
            conversation = get_conversation(conn, parsed)
    except Exception:
        logger.exception("Failed to load conversation %s for delete", conv_id)
        return _not_found_page(req)

    if conversation is None:
        return _not_found_page(req)

    # Cross-org access check
    if not can_access_conversation(auth, conversation):
        return _not_found_page(req)

    # Delete
    try:
        with _connect() as conn:
            delete_conversation(conn, parsed)
            conn.commit()
    except Exception:
        logger.exception("Failed to delete conversation %s", conv_id)
        return _not_found_page(req)

    log_chat_conversation_delete(auth.get("id"), parsed)

    # HX-Redirect pattern (same as Phase 2 draft delete)
    if req.headers.get("HX-Request") == "true":
        return Response(
            status_code=204,
            headers={"HX-Redirect": "/chat"},
        )
    return RedirectResponse(url="/chat", status_code=303)


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------


def register_chat_routes(rt) -> None:  # type: ignore[no-untyped-def]
    """Mount the chat routes on the FastHTML route decorator *rt*.

    The chat pages are behind the global auth Beforeware, so
    **do not** add ``/chat`` to ``SKIP_PATHS``.

    Registration order matters: Starlette matches routes by the order
    they are added. Static/parameter-less chat handler paths
    (``/chat/search``, ``/chat/{conv_id}/pin``, ``/api/me/usage`` etc.)
    MUST be registered **before** the ``/chat/{conv_id}`` catch-all so
    the dispatcher resolves them correctly. Delegating to
    ``register_chat_handler_routes`` first achieves that without needing
    the post-hoc reordering hack that lives in ``app.chat.handlers``
    (which remains in place as an idempotent safety net).
    """
    from app.chat.handlers import register_chat_handler_routes

    # Register mutation / export / search / usage handlers FIRST so their
    # static paths win over the dynamic /chat/{conv_id} route below.
    register_chat_handler_routes(rt)

    rt("/chat", methods=["GET"])(chat_list_page)
    rt("/chat/new", methods=["GET"])(new_conversation)
    rt("/chat/{conv_id}", methods=["GET"])(conversation_view_page)
    rt("/chat/{conv_id}/delete", methods=["POST"])(delete_conversation_handler)


# ---------------------------------------------------------------------------
# Backwards-compat: _html module kept imported so tests that monkeypatch
# ``app.chat.routes._html`` (the HTML escape helper) continue to work.
# ---------------------------------------------------------------------------
_ = _html  # noqa: F841  (silence unused-import warnings on strict linters)
