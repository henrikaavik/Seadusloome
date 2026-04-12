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
            }
        )
    return rows


def _conversation_list_columns() -> list[Column]:
    """Return the column definitions for the conversations DataTable."""

    def _title_cell(row: dict[str, Any]):
        return A(  # noqa: F405
            row["title"],
            href=f"/chat/{row['id']}",
            cls="data-table-link",
        )

    def _context_cell(row: dict[str, Any]):
        if row["context_draft_id"]:
            return Badge("Eelnou", variant="primary")
        return Span("\u2014", cls="muted-text")  # noqa: F405

    def _actions_cell(row: dict[str, Any]):
        return A(  # noqa: F405
            "Ava",
            href=f"/chat/{row['id']}",
            cls="btn btn-secondary btn-sm",
        )

    return [
        Column(key="title", label="Pealkiri", sortable=False, render=_title_cell),
        Column(key="message_count", label="Sonumeid", sortable=False),
        Column(key="last_message_at", label="Viimane sonum", sortable=False),
        Column(key="context", label="Kontekst", sortable=False, render=_context_cell),
        Column(key="created_at", label="Loodud", sortable=False),
        Column(key="actions", label="Tegevused", sortable=False, render=_actions_cell),
    ]


def _count_conversations_for_user(user_id: str) -> int:
    """Count total conversations for a user."""
    try:
        with _connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM conversations WHERE user_id = %s",
                (user_id,),
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
                )
        except Exception:
            logger.exception("Failed to list conversations")
            conversations = []

        total = _count_conversations_for_user(user_id)
        total_pages = max(1, (total + _PAGE_SIZE - 1) // _PAGE_SIZE)

        if total == 0:
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
            body = DataTable(
                columns=_conversation_list_columns(),
                rows=_conversation_rows(conversations),
                empty_message="Vestlusi ei leitud.",
            )
            pagination = Pagination(
                current_page=page,
                total_pages=total_pages,
                base_url="/chat",
                page_size=_PAGE_SIZE,
                total=total,
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

    card_body_children: list = [body]
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


def _render_message(msg: Any):
    """Render a single message as a chat bubble."""
    role = msg.role
    content = msg.content or ""

    if role == "user":
        return Div(  # noqa: F405
            Div(  # noqa: F405
                P(content, cls="chat-message-text"),  # noqa: F405
                cls="chat-bubble chat-bubble-user",
            ),
            cls="chat-message chat-message-user",
        )
    elif role == "assistant":
        msg_id = str(msg.id) if hasattr(msg, "id") and msg.id else ""
        return Div(  # noqa: F405
            Div(  # noqa: F405
                Div(Safe(_format_assistant_content(content)), cls="chat-message-text"),  # noqa: F405
                cls="chat-bubble chat-bubble-assistant",
            ),
            AnnotationButton("conversation", msg_id) if msg_id else "",
            cls="chat-message chat-message-assistant",
        )
    elif role == "tool":
        tool_name = msg.tool_name or "tool"
        return Div(  # noqa: F405
            Div(  # noqa: F405
                P(f"[{tool_name}]", cls="chat-tool-label"),  # noqa: F405
                cls="chat-bubble chat-bubble-tool",
            ),
            cls="chat-message chat-message-tool",
        )
    # system messages are hidden
    return ""


def _format_assistant_content(content: str) -> str:
    """Apply minimal formatting to assistant content.

    Handles bold markers for citations and preserves code blocks.
    This is deliberately simple -- full markdown rendering is deferred.
    """
    import html

    content = html.escape(content)
    # Bold: **text** -> <strong>text</strong>
    import re

    content = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", content)
    # Code blocks: ```text``` -> <pre><code>text</code></pre>
    content = re.sub(
        r"```(\w*)\n?(.*?)```",
        r"<pre><code>\2</code></pre>",
        content,
        flags=re.DOTALL,
    )
    # Inline code: `text` -> <code>text</code>
    content = re.sub(r"`([^`]+)`", r"<code>\1</code>", content)
    # Newlines -> <br>
    content = content.replace("\n", "<br>")
    return content


_CHAT_JS = """
(function() {
    var ws = null;
    var convId = document.getElementById('chat-container').dataset.conversationId;
    var messagesDiv = document.getElementById('chat-messages');
    var input = document.getElementById('chat-input');
    var sendBtn = document.getElementById('chat-send-btn');
    var statusDiv = document.getElementById('chat-status');
    var currentAssistantBubble = null;

    function connect() {
        var proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
        ws = new WebSocket(proto + '//' + location.host + '/ws/chat');
        ws.onopen = function() {
            if (statusDiv) statusDiv.textContent = 'Uhendatud';
        };
        ws.onclose = function() {
            if (statusDiv) statusDiv.textContent = 'Uhendus katkes';
            setTimeout(connect, 3000);
        };
        ws.onmessage = function(e) {
            try {
                var event = JSON.parse(e.data);
                handleEvent(event);
            } catch(err) { /* ignore parse errors */ }
        };
    }

    function handleEvent(event) {
        if (event.type === 'content_delta') {
            if (!currentAssistantBubble) {
                currentAssistantBubble = document.createElement('div');
                currentAssistantBubble.className = 'chat-message chat-message-assistant';
                currentAssistantBubble.innerHTML =
                    '<div class="chat-bubble chat-bubble-assistant">' +
                    '<div class="chat-message-text"></div></div>';
                messagesDiv.appendChild(currentAssistantBubble);
            }
            var textDiv = currentAssistantBubble.querySelector('.chat-message-text');
            textDiv.textContent += event.delta;
            messagesDiv.scrollTop = messagesDiv.scrollHeight;
        } else if (event.type === 'tool_use') {
            var toolDiv = document.createElement('div');
            toolDiv.className = 'chat-message chat-message-tool';
            toolDiv.innerHTML = '<div class="chat-bubble chat-bubble-tool">' +
                '<p class="chat-tool-label">[' + (event.tool || 'tool') + ']</p></div>';
            messagesDiv.appendChild(toolDiv);
        } else if (event.type === 'done') {
            currentAssistantBubble = null;
            if (sendBtn) sendBtn.disabled = false;
            if (input) input.disabled = false;
        } else if (event.type === 'error') {
            var errDiv = document.createElement('div');
            errDiv.className = 'chat-message chat-message-error';
            errDiv.innerHTML = '<div class="chat-bubble chat-bubble-error">' +
                '<p>' + (event.message || 'Viga') + '</p></div>';
            messagesDiv.appendChild(errDiv);
            currentAssistantBubble = null;
            if (sendBtn) sendBtn.disabled = false;
            if (input) input.disabled = false;
        }
    }

    function sendMessage() {
        if (!ws || ws.readyState !== WebSocket.OPEN) return;
        var text = input.value.trim();
        if (!text) return;
        // Add user message to UI
        var userDiv = document.createElement('div');
        userDiv.className = 'chat-message chat-message-user';
        userDiv.innerHTML = '<div class="chat-bubble chat-bubble-user">' +
            '<p class="chat-message-text">' + text.replace(/</g, '&lt;') + '</p></div>';
        messagesDiv.appendChild(userDiv);
        messagesDiv.scrollTop = messagesDiv.scrollHeight;
        // Send via WS
        ws.send(JSON.stringify({
            type: 'send_message',
            conversation_id: convId,
            content: text
        }));
        input.value = '';
        if (sendBtn) sendBtn.disabled = true;
        if (input) input.disabled = true;
    }

    if (sendBtn) {
        sendBtn.addEventListener('click', sendMessage);
    }
    if (input) {
        input.addEventListener('keydown', function(e) {
            if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                sendMessage();
            }
        });
    }

    connect();
})();
"""


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

    # Render message history
    message_bubbles = [_render_message(m) for m in messages if m.role != "system"]

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
            cls="chat-draft-context",
        )

    # Build chat page
    chat_container = Div(  # noqa: F405
        # Message history
        Div(  # noqa: F405
            *message_bubbles,
            id="chat-messages",
            cls="chat-messages",
        ),
        # Input area help text
        Small(  # noqa: F405
            "K\u00fcsige k\u00fcsimusi Eesti seaduste, kohtuotsuste v\u00f5i "
            "EL-i \u00f5igusaktide kohta. AI kasutab ontoloogiat ja RAG-i "
            "vastuste p\u00f5hjendamiseks.",
            cls="form-field-help",
        ),
        # Input area
        Div(  # noqa: F405
            Textarea(  # noqa: F405
                id="chat-input",
                name="content",
                placeholder="Kirjutage oma kuuimus...",
                cls="chat-input",
                rows="2",
            ),
            Button("Saada", id="chat-send-btn", variant="primary", type="button"),
            cls="chat-input-area",
        ),
        id="chat-container",
        data_conversation_id=str(parsed),
        cls="chat-container",
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

    return PageShell(
        *header_children,
        Card(
            CardBody(*content_parts),
        ),
        Script(Safe(_CHAT_JS)),  # noqa: F405
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
    """
    rt("/chat", methods=["GET"])(chat_list_page)
    rt("/chat/new", methods=["GET"])(new_conversation)
    rt("/chat/{conv_id}", methods=["GET"])(conversation_view_page)
    rt("/chat/{conv_id}/delete", methods=["POST"])(delete_conversation_handler)
