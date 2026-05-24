"""FastHTML routes for the Phase 3B AI Advisory Chat.

Route map:

    GET  /chat                -- conversation list
    POST /chat/seed           -- stash a single-use chat-seed, redirect to /chat/new?seed=<token>
    GET  /chat/new            -- create new conversation (?draft= and/or ?seed=), redirect to view
    GET  /chat/{conv_id}      -- conversation view (?seed=<token> pre-fills the input textarea)
    POST /chat/{conv_id}/delete -- delete conversation

``?seed=<token>`` (#714 PR-J / #724): an opaque single-use token minted by
``POST /chat/seed`` for the "Küsi nõustajalt selle leiu kohta" affordance.
The seed text (which may quote draft/finding content) never travels through
the URL — only the token does. ``GET /chat/new`` peeks the token to bind the
new conversation's draft context; ``GET /chat/{id}`` consumes it and renders
the input textarea pre-filled. An invalid/expired token degrades silently.

All routes require authentication (they are NOT in ``SKIP_PATHS``).
Cross-org access returns 404 to avoid leaking conversation existence.
"""

from __future__ import annotations

import html as _html
import json
import logging
import uuid
from datetime import datetime
from typing import Any

from fasthtml.common import *  # noqa: F403
from starlette.requests import ClientDisconnect, Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from app.auth.helpers import require_auth as _require_auth
from app.auth.policy import can_access_conversation
from app.chat.actions import chat_actions_block
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
from app.chat.ontology_version import get_current_ontology_version
from app.chat.pending_seed import (
    consume_pending_seed,
    create_pending_seed,
    peek_pending_seed,
)
from app.chat.sanitize import render_markdown_safe, render_plaintext_safe
from app.db import get_connection as _connect
from app.docs.report_routes import explorer_focus_url
from app.ui.capabilities import live_capabilities
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

# #724: cap the chat-seed length so a pathological finding/quote can't bloat
# the encrypted blob (or the textarea). Generous enough for a multi-sentence
# finding phrased as a question.
_MAX_SEED_LEN = 2000


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


def _not_found_page(req: Request) -> HTMLResponse:
    """Render the themed 404 page for missing or cross-org conversations.

    Returns a ``starlette`` :class:`HTMLResponse` with an explicit
    ``404`` status (issue #739): the module's security model treats
    missing / cross-org conversations as not-found, and a bare FT element
    rendered through FastHTML's default path answers ``200 OK`` — which
    lets these pages be cached, crawled, or mistaken for success by HTMX /
    API callers. Wrapping the ``PageShell`` in an explicit response keeps
    the themed body *and* the correct status code.
    """
    from fasthtml.common import to_xml

    auth = req.scope.get("auth")
    theme = get_theme_from_request(req)
    page = PageShell(
        H1("Vestlust ei leitud", cls="page-title"),  # noqa: F405
        Alert(
            "Otsitud vestlust ei ole olemas või Teil puudub selle vaatamise õigus.",
            variant="warning",
        ),
        P(A("< Tagasi vestluste nimekirja", href="/chat"), cls="back-link"),  # noqa: F405
        title="Vestlust ei leitud",
        user=auth,
        theme=theme,
        active_nav="/chat",
        request=req,
    )
    return HTMLResponse(to_xml(page), status_code=404)


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


def _conversation_row_dict(conv: Conversation) -> dict[str, Any]:
    """Shape a single :class:`Conversation` into the DataTable row dict.

    Extracted so both the list renderer and the per-row mutation handlers
    (pin / archive / rename) can produce an identical ``<tr>`` payload.
    """
    msg_count = _get_message_count(conv.id)
    last_msg = _get_last_message_at(conv.id)
    return {
        "id": str(conv.id),
        "title": conv.title,
        "message_count": msg_count,
        "last_message_at": _format_timestamp(last_msg),
        "context_draft_id": str(conv.context_draft_id) if conv.context_draft_id else None,
        "created_at": _format_timestamp(conv.created_at),
        "is_pinned": bool(getattr(conv, "is_pinned", False)),
        "is_archived": bool(getattr(conv, "is_archived", False)),
    }


def _conversation_rows(conversations: list[Conversation]) -> list[dict[str, Any]]:
    """Shape Conversation objects into dict rows for DataTable."""
    return [_conversation_row_dict(c) for c in conversations]


def _row_dom_id(conv_id: str) -> str:
    """Return the stable DOM id for a single conversation row."""
    return f"chat-row-{conv_id}"


def _render_conversation_row(conv: Conversation, auth: Any | None = None):
    """Render the single ``<tr>`` fragment for a conversation.

    Used by the list page (indirectly, via :func:`DataTable`) and returned
    directly from the pin / archive / rename handlers so the row swaps in
    place without triggering a full-body reload.

    The ``auth`` argument is accepted for future role-based rendering
    (e.g. hiding action buttons for reviewers) but is currently unused —
    keeping it in the signature avoids touching every call site later.
    """
    del auth  # reserved for future RBAC hooks
    row = _conversation_row_dict(conv)
    columns = _conversation_list_columns()
    return Tr(  # noqa: F405
        *[_row_cell(col, row) for col in columns],
        id=_row_dom_id(row["id"]),
    )


def _row_cell(col: Column, row: dict[str, Any]):
    """Build a single ``<td>`` — mirrors :func:`app.ui.data.data_table._cell`.

    We can't import the private helper without tripping ruff's ``F401``
    and the cross-module coupling is trivial, so inline it here.
    """
    content = col.render(row) if col.render is not None else str(row.get(col.key, ""))
    align_cls = f"text-{col.align}" if col.align != "left" else ""
    classes = f"data-table-td {align_cls}".strip()
    return Td(content, cls=classes, data_label=col.label)  # noqa: F405


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
        # Row-targeted swaps (bug #654): each mutation returns a replacement
        # <tr> (pin / rename) or an empty fragment (archive / delete) so the
        # list updates in place instead of reloading the whole page.
        pin_form = Form(  # noqa: F405
            Button(pin_label, variant="secondary", size="sm", type="submit"),
            method="post",
            action=f"/chat/{conv_id}/pin",
            hx_post=f"/chat/{conv_id}/pin",
            hx_target="closest tr",
            hx_swap="outerHTML",
            cls="chat-list-action-form",
        )
        archive_form = Form(  # noqa: F405
            Button(archive_label, variant="secondary", size="sm", type="submit"),
            method="post",
            action=f"/chat/{conv_id}/archive",
            hx_post=f"/chat/{conv_id}/archive",
            hx_target="closest tr",
            hx_swap="outerHTML",
            cls="chat-list-action-form",
        )
        rename_form = Form(  # noqa: F405
            Input(  # noqa: F405
                type="text",
                name="title",
                placeholder="Uus pealkiri",
                cls="chat-list-rename-input",
                # #813: HTML4 string form survives the FastHTML HTTP renderer.
                required="required",
            ),
            Button("Nimeta", variant="secondary", size="sm", type="submit"),
            method="post",
            action=f"/chat/{conv_id}/rename",
            hx_post=f"/chat/{conv_id}/rename",
            hx_target="closest tr",
            hx_swap="outerHTML",
            cls="chat-list-action-form chat-list-rename-form",
        )
        delete_form = Form(  # noqa: F405
            # Hidden marker so :func:`delete_conversation_handler` can tell
            # a row-level delete (which should collapse the <tr> in place)
            # apart from the detail-page delete (which still needs
            # HX-Redirect back to /chat).
            Input(type="hidden", name="from_list", value="1"),  # noqa: F405
            Button("Kustuta", variant="danger", size="sm", type="submit"),
            method="post",
            action=f"/chat/{conv_id}/delete",
            hx_post=f"/chat/{conv_id}/delete",
            hx_confirm="Kas olete kindel, et soovite vestluse kustutada?",
            hx_target="closest tr",
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
        Column(key="last_message_at", label="Viimane sõnum", sortable=False),
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


def _chat_list_base_url(*, search_q: str, include_archived: bool) -> str:
    """Build the ``/chat`` base URL with the active filter params preserved.

    Used as :class:`Pagination`'s ``base_url`` so paging through search
    results / archived views keeps the same filter set (#775). The
    :func:`app.ui.data.pagination._build_url` helper strips any
    pre-existing ``page=`` value and appends the requested page, so
    ``q`` / ``archived`` survive unchanged across page links.
    """
    from urllib.parse import urlencode

    params: list[tuple[str, str]] = []
    if search_q:
        params.append(("q", search_q))
    if include_archived:
        params.append(("archived", "1"))
    if not params:
        return "/chat"
    return "/chat?" + urlencode(params)


def _render_chat_list_body(
    user_id: str,
    *,
    page: int,
    search_q: str,
    include_archived: bool,
):
    """Render the ``#chat-list-body`` fragment for the chat list page.

    Bug #663 (post-review fix): archive and delete handlers used to
    return ``HX-Refresh: true`` which scrolls the user back to the top
    of the page on every action. By returning this fragment with
    ``HX-Reswap: outerHTML`` + ``HX-Retarget: #chat-list-body`` the
    handlers can update the rows AND the pagination counts in place,
    preserving scroll.

    The fragment now wraps both the data table AND the pagination
    footer inside a single ``#chat-list-body`` div so one swap updates
    both. Empty-state and "no results" branches are shaped so the
    parent container stays stable.

    #775: the toolbar's HTMX search now delegates to this same renderer
    so the swapped fragment carries the full conversation table
    (pin / archive / rename / delete row actions) and respects the
    ``Näita arhiveeritud`` toggle. Pagination links carry ``q`` and
    ``archived`` so paging through filtered results does not silently
    drop the filters.

    Args:
        user_id: caller's user UUID (already validated by the caller).
        page: 1-indexed page (clamped to >= 1 by the caller).
        search_q: optional title-substring filter.
        include_archived: whether to include archived rows.
    """
    offset = (page - 1) * _PAGE_SIZE
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
        # Genuine empty state (no conversations at all). Wrap inside
        # the same #chat-list-body anchor so the swap target stays
        # stable when an archive/delete empties the list.
        return Div(  # noqa: F405
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
            id="chat-list-body",
        )

    pagination = (
        Pagination(
            current_page=page,
            total_pages=total_pages,
            base_url=_chat_list_base_url(
                search_q=search_q,
                include_archived=include_archived,
            ),
            page_size=_PAGE_SIZE,
            total=total,
        )
        if total > 0
        else None
    )
    children: list = [
        DataTable(
            columns=_conversation_list_columns(),
            rows=_conversation_rows(conversations),
            empty_message="Vestlusi ei leitud.",
        ),
    ]
    if pagination is not None:
        children.append(pagination)
    return Div(*children, id="chat-list-body")  # noqa: F405


def _chat_list_state_from_request(req: Request) -> tuple[int, str, bool]:
    """Extract the chat-list query state from a request.

    For HTMX-driven row mutations the state lives in the
    ``HX-Current-URL`` header (which HTMX always sets to the URL of
    the page that triggered the request) rather than the form's POST
    body. For full-page GETs the state is in the query string. This
    helper reads from both shapes so the handlers don't have to.

    Returns ``(page, search_q, include_archived)`` with the same
    parsing semantics as ``chat_list_page``.
    """
    from urllib.parse import parse_qs, urlparse

    # Prefer the request's own query params (covers GET /chat).
    params = dict(req.query_params)
    if "page" not in params and "q" not in params and "archived" not in params:
        # Fall back to HX-Current-URL (covers HTMX row mutations posted
        # from the chat list view).
        current_url = req.headers.get("HX-Current-URL")
        if current_url:
            qs = parse_qs(urlparse(current_url).query)
            if "page" in qs:
                params["page"] = qs["page"][0]
            if "q" in qs:
                params["q"] = qs["q"][0]
            if "archived" in qs:
                params["archived"] = qs["archived"][0]

    page_str = params.get("page", "1")
    try:
        page = max(1, int(page_str))
    except ValueError:
        page = 1
    search_q = (params.get("q") or "").strip()
    include_archived = params.get("archived") in ("1", "true", "on")
    return page, search_q, include_archived


def chat_list_page(req: Request):
    """GET /chat -- paginated list of the caller's conversations."""
    auth_or_redirect = _require_auth(req)
    if isinstance(auth_or_redirect, Response):
        return auth_or_redirect
    auth = auth_or_redirect
    theme = get_theme_from_request(req)
    user_id = auth.get("id")

    page, search_q, include_archived = _chat_list_state_from_request(req)

    if not user_id:
        body: Any = Alert(
            "Kasutaja andmed puuduvad.",
            variant="warning",
        )
    else:
        body = _render_chat_list_body(
            user_id,
            page=page,
            search_q=search_q,
            include_archived=include_archived,
        )

    # Search + archived toolbar.
    #
    # #775: the form serialises ``q`` and ``archived`` together so HTMX
    # search requests carry both \u2014 the same filter contract the full
    # ``/chat`` GET page uses. The hidden ``page=1`` input resets the
    # offset on every keystroke / toggle change so a search executed
    # while standing on page 5 doesn't try to render "page 5 of the
    # filtered set". ``hx-swap=outerHTML`` matches the swap shape
    # ``_render_chat_list_body`` already uses for the archive / delete
    # HTMX paths so the ``#chat-list-body`` wrapper is replaced in one
    # go. (We deliberately do not push the URL on the HTMX path: the
    # toolbar input is updated as the user types, and the in-fragment
    # pagination links carry the filters server-side \u2014 keeping the
    # address bar pinned to ``/chat`` avoids exposing the internal
    # ``/chat/search`` endpoint via browser history.)
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
        # Hidden marker so the HTMX search request resets the offset.
        Input(type="hidden", name="page", value="1"),  # noqa: F405
        Button("Otsi", variant="secondary", size="sm", type="submit"),
        method="get",
        action="/chat",
        hx_get="/chat/search",
        hx_target="#chat-list-body",
        hx_swap="outerHTML",
        hx_trigger=(
            "input changed delay:300ms from:input[name='q'], "
            "change from:input[name='archived'], submit"
        ),
        cls="chat-list-toolbar",
    )

    header_children: list = [H1("Nõustaja", cls="page-title")]  # noqa: F405
    # InfoBox body: framing prose + a generated list of "what else you can
    # do" pulled from :mod:`app.ui.capabilities` (B3). Use cases 1-5 cover
    # the conversational + analytical surface area; the chat itself (use
    # case 1, slug ``noustaja``) is omitted because the user is already
    # here. Falling out of the list automatically means a new live workflow
    # (e.g. A1 Sanctions when it ships) appears in the InfoBox with zero
    # touch-up here.
    capability_items = [
        Li(  # noqa: F405
            A(  # noqa: F405
                cap.canonical_name_et,
                href=cap.target_url,
                cls="info-box-link",
            ),
            " — ",
            cap.one_line_description_et,
        )
        for cap in live_capabilities()
        if cap.use_case_from_section_2 in {1, 2, 3, 4, 5} and cap.slug != "noustaja"
    ]
    header_children.append(
        InfoBox(
            P(
                "AI õigusnõustaja Claude vastab teie küsimustele "
                "Eesti õiguse kohta. Vastused tuginevad ontoloogiale "
                "(50 000+ kehtivat sätet, 615 seadust, "
                "22 832 eelnõud, 12 137 Riigikohtu lahendit, "
                "33 242 EL õigusakti, 22 290 EL kohtulahendit) "
                "ning RAG-süsteemile, mis leiab semantiliselt sarnaseid "
                "õigusakti lõike."
            ),
            P(
                "Küsige konkreetsete sätete tähenduse kohta, võrrelge "
                "eelnõud kehtiva regulatsiooniga, otsige pretsedente "
                "või arutage eelnõu mõju. AI kasutab vajadusel "
                "tööriistu (ontoloogiapäringud, sätete otsing, "
                "mõjuanalüüs, sätte detailid) ja viitab vastustes "
                "alusallikatele, et saaksite väited kontrollida."
            ),
            P("Lisaks Nõustajale saate Seadusloomes:"),
            Ul(*capability_items, cls="info-box-capabilities"),  # noqa: F405
            P(
                "Vestlused on privaatsed ja seotud teie organisatsiooniga. "
                "Saate vestluse siduda konkreetse eelnõuga "
                "(kontekst paneb AI vastused selle dokumendi suhtes), "
                "kinnitada olulised vestlused tähekesega, otsida "
                "vanadest vestlustest ja vajadusel arhiveerida lõpetatud "
                "vestlused. Vajutage „Uus vestlus“, et alustada."
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

    # Pagination is now nested inside ``body`` (the #chat-list-body
    # fragment) so a single archive/delete swap updates rows AND counts
    # in place — see :func:`_render_chat_list_body`.
    card_body_children: list = [toolbar, body]

    return PageShell(
        *header_children,
        Card(
            CardHeader(H3("Minu vestlused", cls="card-title")),  # noqa: F405
            CardBody(*card_body_children),
        ),
        title="Nõustaja",
        user=auth,
        theme=theme,
        active_nav="/chat",
    )


# ---------------------------------------------------------------------------
# POST /chat/seed -- stash a single-use chat-seed token (#724)
# ---------------------------------------------------------------------------


async def seed_chat_handler(req: Request):
    """POST /chat/seed -- stash a chat-seed and redirect to ``/chat/new?seed=<token>``.

    Wired to the "Küsi nõustajalt selle leiu kohta" affordance on
    Analüüsikeskus result rows (#724). Form fields:

      * ``seed_text`` (required) -- the question/finding text to pre-fill the
        chat input with. May quote draft/finding content, which is exactly why
        it goes through a server-side token instead of the URL. Truncated to
        :data:`_MAX_SEED_LEN`.
      * ``draft_id`` (optional, UUID) -- bind the new conversation to this
        draft. Validated against the caller's org via :func:`_get_draft_title`;
        a draft the caller can't see is silently dropped (the seed is still
        stashed, just without a draft context).

    A blank ``seed_text`` short-circuits to a plain ``/chat/new`` redirect.
    """
    auth_or_redirect = _require_auth(req)
    if isinstance(auth_or_redirect, Response):
        return auth_or_redirect
    auth = auth_or_redirect

    user_id = auth.get("id")
    org_id = auth.get("org_id")
    if not user_id or not org_id:
        return RedirectResponse(url="/chat/new", status_code=303)

    try:
        form = await req.form()
        raw_seed = form.get("seed_text") or ""
        raw_draft = form.get("draft_id") or ""
    except ClientDisconnect:
        return RedirectResponse(url="/chat/new", status_code=303)
    except Exception:
        logger.warning("seed_chat_handler: failed to read form body", exc_info=True)
        return RedirectResponse(url="/chat/new", status_code=303)

    seed_text = str(raw_seed).strip()[:_MAX_SEED_LEN]
    if not seed_text:
        # Nothing to seed — behave like a plain "Uus vestlus" click.
        return RedirectResponse(url="/chat/new", status_code=303)

    # Validate the optional draft context against the caller's org. A draft
    # they can't see is dropped, not an error — the seed alone is still useful.
    draft_id: uuid.UUID | None = None
    raw_draft_str = str(raw_draft).strip() if raw_draft else ""
    if raw_draft_str:
        parsed_draft = _parse_uuid(raw_draft_str)
        if parsed_draft and _get_draft_title(str(parsed_draft), org_id) is not None:
            draft_id = parsed_draft

    try:
        with _connect() as conn:
            token = create_pending_seed(
                conn,
                user_id=user_id,
                org_id=org_id,
                draft_id=draft_id,
                seed_text=seed_text,
            )
            conn.commit()
    except Exception:
        logger.exception("seed_chat_handler: failed to stash pending chat seed")
        token = None

    if not token:
        # Couldn't persist the seed — fall back to a plain new conversation
        # rather than 500ing on the user.
        return RedirectResponse(url="/chat/new", status_code=303)

    return RedirectResponse(url=f"/chat/new?seed={token}", status_code=303)


# ---------------------------------------------------------------------------
# GET /chat/new -- create new conversation
# ---------------------------------------------------------------------------


def new_conversation(req: Request):
    """GET /chat/new -- create a new conversation and redirect to it.

    Optionally accepts ``?draft=<draft_id>`` to bind the conversation
    to a specific draft, and/or ``?seed=<token>`` (a single-use token
    minted by :func:`seed_chat_handler`). When a ``?seed=`` token is
    present and valid, its ``draft_id`` (if any) overrides ``?draft=``
    for the new conversation's context — the same context the chat view
    will then surface from the seed. The token is *not* consumed here;
    it's passed through to ``/chat/{id}?seed=<token>`` where the view
    page consumes it and pre-fills the input. The title is auto-generated.
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
    seed_param = req.query_params.get("seed")
    context_draft_id: uuid.UUID | None = None
    # #714: conversations live under the "Nõustaja" section, so generated
    # titles use that framing rather than the old "Vestlus" prefix.
    title = "Nõustamine"

    if draft_param:
        parsed_draft = _parse_uuid(draft_param)
        if parsed_draft:
            # Validate the draft belongs to the user's org
            draft_title = _get_draft_title(str(parsed_draft), org_id)
            if draft_title is not None:
                context_draft_id = parsed_draft
                title = f"N\u00f5ustamine \u2014 {draft_title}"
            else:
                # Draft not found or belongs to another org — ignore it
                logger.warning(
                    "Draft %s not found or not owned by org %s",
                    parsed_draft,
                    org_id,
                )

    # #724: a ?seed= token may carry its own draft_id. If it does, it wins
    # over ?draft= (the seed is the more specific intent). Peek -- don't
    # consume -- so the /chat/{id}?seed= view can still read & pre-fill it.
    if seed_param:
        try:
            with _connect() as conn:
                peeked = peek_pending_seed(conn, token=seed_param, user_id=user_id)
        except Exception:
            logger.warning("new_conversation: pending-seed peek failed", exc_info=True)
            peeked = None
        if peeked is not None:
            _seed_text, seed_draft_id = peeked
            if seed_draft_id is not None:
                seed_draft_title = _get_draft_title(str(seed_draft_id), org_id)
                if seed_draft_title is not None:
                    context_draft_id = seed_draft_id
                    title = f"N\u00f5ustamine \u2014 {seed_draft_title}"

    if not context_draft_id:
        from app.ui.time import now_tallinn

        title = f"N\u00f5ustamine \u2014 {now_tallinn().strftime('%d.%m.%Y %H:%M')}"

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

    # Carry the seed token through to the view page (which consumes it and
    # pre-fills the input). A blank/absent token just redirects to the bare
    # conversation view.
    if seed_param:
        return RedirectResponse(url=f"/chat/{conversation.id}?seed={seed_param}", status_code=303)
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
    """Render a ``<details>`` disclosure of RAG source chunks.

    #759: each source row that carries an ontology URI also gets a small
    "vaata kaardil \u2192" affordance that deep-links into \u00d5iguskaart centred
    on that entity (``/explorer?focus=<urlencoded-uri>``, via
    :func:`app.docs.report_routes.explorer_focus_url`). RAG context is
    sourced from ontology-derived chunks, so the chunk URIs are exactly
    the provision / act / court-case entities the assistant grounded its
    answer on \u2014 making them the "cited URIs" the design doc asks us to
    surface a map link for.
    """
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
        link: Any
        map_link: Any = ""
        if source_uri:
            link = A(  # noqa: F405
                title,
                href=f"/explorer?q={source_uri}",
                target="_blank",
                rel="noopener noreferrer",
            )
            map_link = A(  # noqa: F405
                "vaata kaardil \u2192",
                href=explorer_focus_url(source_uri),
                cls="chat-source-map-link",
                title="Ava see allikas \u00d5iguskaardil.",
            )
        else:
            link = Span(title)  # noqa: F405
        items.append(
            Li(  # noqa: F405
                link,
                map_link,
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


# ---------------------------------------------------------------------------
# #352 — outdated-ontology drift banner
# ---------------------------------------------------------------------------


# Chat tool names that reference ontology nodes. When a stored
# assistant turn has children of these tool kinds, OR carries
# ``rag_context`` (RAG chunks are sourced from the ontology), we consider
# it "ontology-grounded" and worth checking for drift. Other tool names
# (e.g. future non-ontology tools) would not. Mirrors the executor map
# in :mod:`app.chat.tools` so it stays in sync when tools are added.
_ONTOLOGY_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "query_ontology",
        "search_provisions",
        "get_draft_impact",
        "get_provision_details",
        "get_court_decisions_for_provision",
        "get_eu_transposition_for_provision",
        "get_provision_amendments",
        "get_related_concepts",
    }
)


# RAG ``source_type`` values that are sourced from the public ontology
# (the JSON-LD source graph synced into Jena Fuseki) — see
# ``migrations/009_rag_chunks.sql`` for the CHECK constraint. A chunk
# tagged with one of these source types reflects ontology state at
# retrieval time, so a stale snapshot tag on the same turn means the
# answer may now be outdated. ``draft`` chunks (private uploads) are
# explicitly excluded — they are tenant-private and unaffected by
# ontology-side drift.
_ONTOLOGY_RAG_SOURCE_TYPES: frozenset[str] = frozenset(
    {
        "ontology",
        "law_text",
        "court_decision",
        "eu_act",
    }
)


def _rag_context_grounds_on_ontology(rag_context: Any) -> bool:
    """Return True when at least one chunk in *rag_context* is sourced
    from the public ontology (not a private draft).

    ``rag_context`` is the persisted list-of-dicts produced by
    :mod:`app.chat.orchestrator` (see ``rag_context_json`` build site).
    Post-#833 each chunk carries a ``source_type`` field — when present,
    we accept the chunk only if its ``source_type`` is in
    :data:`_ONTOLOGY_RAG_SOURCE_TYPES`. Pre-#833 chunks (and any chunk
    where ``source_type`` is missing/None) are accepted to preserve the
    older behaviour and keep the banner working for legacy rows.
    """
    if not rag_context or not isinstance(rag_context, list):
        return False
    for chunk in rag_context:
        if not isinstance(chunk, dict):
            # Unexpected shape — be defensive and treat as
            # ontology-grounded to preserve the older behaviour.
            return True
        source_type = chunk.get("source_type")
        if source_type is None:
            # Legacy chunk persisted before #833 — no source_type was
            # captured, so we cannot rule it out. Treat as
            # ontology-grounded so historical drift detection still
            # fires for those rows.
            return True
        if source_type in _ONTOLOGY_RAG_SOURCE_TYPES:
            return True
    return False


def _conversation_has_outdated_ontology_citations(
    messages: list[Any],
    current_version: str,
) -> bool:
    """Return True when at least one assistant message in *messages*
    was generated against an older ontology snapshot than *current_version*
    AND that message actually grounded its answer on ontology nodes
    (either via RAG chunks or a tool-use turn referencing ontology).

    "Older" is a literal string-inequality check on the snapshot tags
    (``<iso-timestamp>@<entity_count>`` — see
    :mod:`app.chat.ontology_version`). NULL / "unknown" on either side
    is treated as "cannot detect drift" and skipped silently, so the
    banner never fires on pre-#352 rows (those still carry NULL).

    We pair each assistant row with its tool children (linked via
    ``parent_message_id`` from migration 036 / #315) so we can tell
    whether the answer was grounded on the ontology at all — a turn
    that asked the model a generic question with no RAG chunks and no
    SPARQL tool use does not deserve a drift banner.

    #833 review: RAG ``source_type`` is now consulted before accepting
    the chunk as ontology-grounded. A chunk tagged ``source_type='draft'``
    is private to the tenant and unaffected by ontology drift, so
    seeing only draft chunks in ``rag_context`` does NOT fire the
    banner.
    """
    if not current_version or current_version == "unknown":
        return False

    # Group tool messages by parent so we can ask "did this assistant
    # turn use an ontology tool?" in O(1).
    tools_by_parent: dict[Any, list[Any]] = {}
    for msg in messages:
        if getattr(msg, "role", None) != "tool":
            continue
        parent = getattr(msg, "parent_message_id", None)
        if parent is None:
            continue
        tools_by_parent.setdefault(parent, []).append(msg)

    for msg in messages:
        if getattr(msg, "role", None) != "assistant":
            continue
        msg_version = getattr(msg, "ontology_version", None)
        if not msg_version or msg_version == "unknown":
            # Pre-#352 row or lookup-failed row — cannot detect drift.
            continue
        if msg_version == current_version:
            continue

        # Drift candidate — confirm the turn was ontology-grounded.
        rag_context = getattr(msg, "rag_context", None)
        if _rag_context_grounds_on_ontology(rag_context):
            return True
        children = tools_by_parent.get(getattr(msg, "id", None), [])
        if any((c.tool_name or "") in _ONTOLOGY_TOOL_NAMES for c in children):
            return True

    return False


def _render_outdated_ontology_banner(conv_id: uuid.UUID):
    """Render the warning banner shown when at least one assistant turn
    in this conversation was generated against an older ontology
    snapshot than the live one.

    The "Küsi uuesti" action navigates to ``/chat/{conv_id}`` with the
    ``?reask=1`` flag — the GET handler resolves the most recent
    persisted user message and pre-fills the textarea with it
    (mirroring the #724 seed mechanism, but without burning a token).
    The user then chooses whether to send. This is intentionally
    simpler than wiring a one-click "regenerate the last turn"
    affordance: the user retains full control over what gets re-asked.
    """
    return Alert(
        Div(  # noqa: F405
            P(  # noqa: F405
                "Mõned viidatud allikad võivad olla aegunud. "
                "Ontoloogia on uuenenud pärast nende vastuste "
                "genereerimist.",
                cls="chat-outdated-ontology-text",
            ),
            A(  # noqa: F405
                "Küsi uuesti",
                href=f"/chat/{conv_id}?reask=1",
                cls="chat-outdated-ontology-reask",
                title="Eeltäida sisestusväli sinu viimase küsimusega.",
            ),
            cls="chat-outdated-ontology-banner",
        ),
        variant="warning",
        title="Ontoloogia on uuenenud",
        cls="chat-outdated-ontology-alert",
        data_outdated_ontology="1",
    )


def _resolve_last_user_message_text(messages: list[Any]) -> str:
    """Return the content of the most recent ``role='user'`` message,
    or the empty string when no user turn exists. Used by the
    ``?reask=1`` re-ask flow to pre-fill the textarea."""
    for msg in reversed(messages):
        if getattr(msg, "role", None) == "user":
            return (getattr(msg, "content", None) or "")[:4000]
    return ""


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
        # C1: outbound action links per cited entity type (provision →
        # Normi mõjuahel, EU act → EL ülevõtt, court decision → chat
        # seed). Renders nothing when no sources are cited or no
        # entity type maps to an action.
        actions = chat_actions_block(getattr(msg, "rag_context", None))
        if actions:
            extras.append(actions)

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
        # #315: surface the tool call so the persisted tool turns are
        # visible in the conversation history (the live stream already
        # shows them via the WS ``tool_use`` / ``tool_result`` events,
        # but a page reload would otherwise hide every previously-run
        # tool — only the final assistant text remained). The tool input
        # / output are JSON-formatted inside a ``<details>`` so the bubble
        # stays compact and the structured payload is on-demand.
        tool_name = msg.tool_name or "tool"
        tool_input = getattr(msg, "tool_input", None)
        tool_output = getattr(msg, "tool_output", None)
        try:
            tool_input_text = (
                json.dumps(tool_input, ensure_ascii=False, indent=2)
                if tool_input is not None
                else ""
            )
        except (TypeError, ValueError):
            tool_input_text = ""
        try:
            tool_output_text = (
                json.dumps(tool_output, ensure_ascii=False, indent=2)
                if tool_output is not None
                else (msg.content or "")
            )
        except (TypeError, ValueError):
            tool_output_text = msg.content or ""

        details_children: list = []
        if tool_input_text:
            details_children.append(
                Div(  # noqa: F405
                    Strong("Sisend:"),  # noqa: F405
                    Pre(tool_input_text, cls="chat-tool-input"),  # noqa: F405
                    cls="chat-tool-section",
                )
            )
        if tool_output_text:
            details_children.append(
                Div(  # noqa: F405
                    Strong("Tulemus:"),  # noqa: F405
                    Pre(tool_output_text, cls="chat-tool-result"),  # noqa: F405
                    cls="chat-tool-section",
                )
            )
        # ``data-parent-message-id`` lets future CSS / JS group tool
        # bubbles visually under their parent assistant turn.
        parent_id = getattr(msg, "parent_message_id", None)
        return Div(  # noqa: F405
            Details(  # noqa: F405
                Summary(  # noqa: F405
                    Span(f"[{tool_name}]", cls="chat-tool-label"),  # noqa: F405
                    " ",
                    Span("(tööriistakutse)", cls="chat-tool-hint"),  # noqa: F405
                ),
                *details_children,
                cls="chat-tool-activity chat-tool-activity-done",
            ),
            cls="chat-message chat-message-tool",
            data_message_id=msg_id,
            data_parent_message_id=str(parent_id) if parent_id else "",
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

    # #352: detect ontology drift across the conversation. We render a
    # single conversation-level banner (rather than per-message badges)
    # to keep the chat surface uncluttered — the user is mostly going to
    # re-ask their question anyway, and the banner makes that the
    # obvious action.
    current_ontology_version = get_current_ontology_version()
    show_outdated_banner = _conversation_has_outdated_ontology_citations(
        messages, current_ontology_version
    )
    outdated_banner = _render_outdated_ontology_banner(parsed) if show_outdated_banner else ""

    # #724: a ?seed=<token> means we arrived here from a "Küsi nõustajalt
    # selle leiu kohta" affordance — consume the single-use token and
    # pre-fill the input textarea with the stashed finding/question. The
    # token is single-use, so a refresh shows an empty textarea (fine). An
    # invalid/expired token degrades silently to the normal empty textarea.
    seed_param = req.query_params.get("seed")
    prefill_text = ""
    if seed_param:
        try:
            with _connect() as conn:
                consumed = consume_pending_seed(
                    conn, token=seed_param, user_id=str(auth.get("id"))
                )
        except Exception:
            logger.warning("conversation_view_page: pending-seed consume failed", exc_info=True)
            consumed = None
        if consumed is not None:
            prefill_text = consumed[0] or ""

    # #352: ``?reask=1`` is the "Küsi uuesti" affordance on the
    # outdated-ontology banner. When present (and no seed token took
    # precedence above), pre-fill the textarea with the most recent
    # persisted user message so the user can re-send it against the
    # fresh ontology. A reload without ``?reask=1`` shows an empty
    # textarea, matching the seed-token UX.
    if not prefill_text and req.query_params.get("reask") == "1":
        prefill_text = _resolve_last_user_message_text(messages)

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
        # Slash-command palette (populated by chat.js).
        # #813: ``hidden="hidden"`` HTML4 string form survives the FastHTML
        # HTTP renderer (bool-true is dropped on the wire).
        Div(id="chat-slash-palette", cls="chat-slash-palette", hidden="hidden"),  # noqa: F405
        # Input area
        Div(  # noqa: F405
            Textarea(  # noqa: F405
                # #724: ``prefill_text`` (from a consumed ?seed= token) becomes
                # the textarea's text content; FastHTML renders a single
                # positional string child as the element's body. Empty string \u2192
                # an empty textarea, exactly as before.
                prefill_text,
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
                # #813: HTML4 string form survives FastHTML's HTTP renderer.
                hidden="hidden",
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
    # #352: drift banner sits between the draft header and the chat
    # container so it is the first thing the user sees inside the
    # conversation surface but does not displace the existing
    # draft-context header. Rendered as empty string when no drift is
    # detected, so the layout is unchanged in the happy path.
    if outdated_banner:
        content_parts.append(outdated_banner)
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


async def delete_conversation_handler(req: Request, conv_id: str):
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

    # Bug #654: the /chat list posts with ``from_list=1`` so we can return
    # an empty fragment that swaps the row out in place. The detail-page
    # delete form omits the flag and keeps the legacy HX-Redirect pattern
    # (see Phase 2 draft delete for the same shape).
    from_list = False
    try:
        form = await req.form()
        from_list = (form.get("from_list") or "") == "1"
    except ClientDisconnect:
        # #665: the client hung up before the body arrived. Delete is
        # idempotent and the row may already be gone for the caller, so
        # stay silent and proceed — no need to page anyone.
        pass
    except Exception:
        logger.warning("Failed to read delete form for %s", conv_id, exc_info=True)

    # Delete
    try:
        with _connect() as conn:
            delete_conversation(conn, parsed)
            conn.commit()
    except Exception:
        logger.exception("Failed to delete conversation %s", conv_id)
        return _not_found_page(req)

    log_chat_conversation_delete(auth.get("id"), parsed)

    is_htmx = req.headers.get("HX-Request") == "true"
    if is_htmx and from_list:
        # #663 (post-review fix): return the refreshed #chat-list-body
        # fragment with HX-Reswap+HX-Retarget so the row vanishes AND
        # the counts/pagination update in place — preserving scroll
        # instead of doing the previous HX-Refresh full reload that
        # bounced the user to top.
        from fasthtml.common import to_xml

        page, search_q, include_archived = _chat_list_state_from_request(req)
        fragment = _render_chat_list_body(
            str(auth.get("id")),
            page=page,
            search_q=search_q,
            include_archived=include_archived,
        )
        return Response(
            content=to_xml(fragment),
            status_code=200,
            media_type="text/html",
            headers={
                "HX-Reswap": "outerHTML",
                "HX-Retarget": "#chat-list-body",
            },
        )
    if is_htmx:
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
    # #724: static paths (/chat/seed, /chat/new) are registered before the
    # dynamic /chat/{conv_id} catch-all so the dispatcher resolves them
    # correctly (same ordering invariant the handlers module relies on).
    rt("/chat/seed", methods=["POST"])(seed_chat_handler)
    rt("/chat/new", methods=["GET"])(new_conversation)
    rt("/chat/{conv_id}", methods=["GET"])(conversation_view_page)
    rt("/chat/{conv_id}/delete", methods=["POST"])(delete_conversation_handler)


# ---------------------------------------------------------------------------
# Backwards-compat: _html module kept imported so tests that monkeypatch
# ``app.chat.routes._html`` (the HTML escape helper) continue to work.
# ---------------------------------------------------------------------------
_ = _html  # noqa: F841  (silence unused-import warnings on strict linters)
