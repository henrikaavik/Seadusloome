"""Chat orchestrator: coordinates LLM streaming, tool use, and persistence.

The :class:`ChatOrchestrator` is the core engine behind the advisory chat.
It loads conversation history, builds the system prompt, calls the LLM
with tool use enabled, executes tools when requested, and persists all
messages. Streaming content is pushed back to the caller via an async
``send`` callback (typically wired to a WebSocket).

Tool-use rounds are capped at :data:`MAX_TOOL_ROUNDS` to prevent
infinite loops.

Phase 3C additions:

- **RAG integration**: Before calling the LLM, the orchestrator retrieves
  relevant chunks via :class:`app.rag.retriever.Retriever` and injects
  them into the system prompt. When the retriever or embedding provider
  is in stub mode (no ``VOYAGE_API_KEY``), RAG is skipped gracefully.

- **Rate limiting**: :func:`check_message_rate` and
  :func:`check_org_cost_budget` are called at the top of
  :meth:`ChatOrchestrator.handle_message` to enforce per-user and
  per-org usage limits.

Phase UX polish additions (issue #594):

- **Safe send**: every ``send(...)`` call is wrapped in
  :func:`_safe_send` which enforces a 5s timeout. On timeout a
  :class:`WebSocketSendTimeout` is raised so streaming aborts cleanly
  when the client has gone away.

- **Richer events**: emits ``retrieval_done``, ``sources``,
  ``follow_ups`` and a ``tool_call_id`` that pairs ``tool_use`` with
  ``tool_result`` so the client can reconcile out-of-order events.

- **Cancellation**: ``asyncio.CancelledError`` in the streaming loop is
  caught and the partial assistant turn is persisted with
  ``is_truncated=True`` (schema provided by migration 017). A
  ``stopped`` event is emitted so the client can re-enable its input.

- **Auto-title**: after the first assistant reply, a fire-and-forget
  :func:`app.chat.title.generate_title` is scheduled to set a short
  sidebar label. Skipped when ``conversation.title_is_custom`` is true.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from collections.abc import Awaitable, Callable
from contextlib import aclosing
from dataclasses import dataclass, field
from typing import Any

from app.auth.policy import can_access_conversation
from app.chat.models import (
    Conversation,
    Message,
    create_message,
    get_conversation,
    list_messages,
    update_conversation_title,
)
from app.chat.ontology_version import get_current_ontology_version
from app.chat.rate_limiter import (
    CostBudgetExceededError,
    RateLimitExceededError,
    check_message_rate,
    check_org_cost_budget,
)
from app.chat.system_prompt import build_system_prompt
from app.chat.tools import CHAT_TOOLS, execute_tool
from app.db import get_connection
from app.llm.provider import LLMProvider, StreamEvent
from app.ontology.sparql_client import SparqlClient

# RAG integration — import defensively so the chat still works if
# the parallel agent hasn't deployed app.rag yet.
try:
    from app.rag.retriever import RetrievedChunk, Retriever
except ImportError:  # pragma: no cover
    Retriever = None  # type: ignore[assignment,misc]
    RetrievedChunk = None  # type: ignore[assignment,misc]

logger = logging.getLogger(__name__)

MAX_TOOL_ROUNDS = 5
_MAX_HISTORY_MESSAGES = 50
_WS_SEND_TIMEOUT_SECONDS = 5.0
_FOLLOW_UPS_MAX_TOKENS = 180

# #652 — per-call deadlines to prevent hung turns.
# RAG retrieval is bounded because an unresponsive Voyage AI embedding call
# would otherwise hang the whole orchestrator. The LLM streaming loop is
# wrapped in a turn-level deadline so a stuck upstream never leaves the
# client staring at an infinite spinner.
_RAG_RETRIEVE_TIMEOUT_SECONDS = 15.0
_TURN_DEADLINE_SECONDS = 120.0

# #658 — pre-stream DB-call deadline. Each sync DB op (rate check,
# conversation load, history load, user-msg persist, budget check,
# impact-summary load) is wrapped in ``asyncio.wait_for`` so a stalled
# connection pool surfaces as a friendly error event instead of an
# indefinite "thinking" spinner.
#
# Two limits compose:
#   - ``_PRE_STREAM_DB_TIMEOUT_SECONDS`` is the per-step ceiling.
#   - ``_PRE_STREAM_TOTAL_BUDGET_SECONDS`` is the cumulative wall-clock
#     budget for the whole pre-stream phase, computed from
#     ``time.monotonic()``. Without it, five sequential 8s steps could
#     stall a turn for ~40s before the user saw any feedback (post-
#     review fix to #684). With both limits, worst-case pre-stream
#     latency is ``_PRE_STREAM_TOTAL_BUDGET_SECONDS``.
_PRE_STREAM_DB_TIMEOUT_SECONDS = 4.0
_PRE_STREAM_TOTAL_BUDGET_SECONDS = 12.0

# Module-level registry of fire-and-forget background tasks (e.g. the
# auto-title job). Holding a strong reference prevents asyncio from
# GC'ing the task mid-flight and logging the noisy "Task was destroyed
# but it is pending!" warning. We also log any unhandled exception via
# the done-callback so silent failures don't go unnoticed.
_background_tasks: set[asyncio.Task[Any]] = set()


def _track_background_task(task: asyncio.Task[Any]) -> None:
    """Register *task* so it is not GC'd and its exceptions are logged."""
    _background_tasks.add(task)

    def _on_done(done: asyncio.Task[Any]) -> None:
        _background_tasks.discard(done)
        if done.cancelled():
            return
        exc = done.exception()
        if exc is not None:
            logger.debug("Background chat task failed: %r", exc, exc_info=exc)

    task.add_done_callback(_on_done)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class WebSocketSendTimeout(Exception):  # noqa: N818 — name fixed by contract with frontend wave
    """Raised when a ``send(...)`` call exceeds the configured timeout.

    Used internally by :func:`_safe_send` to signal that the client is
    no longer draining events and the orchestrator should abort the
    current streaming task cleanly instead of hanging forever.
    """


# ---------------------------------------------------------------------------
# Safe send helper
# ---------------------------------------------------------------------------


async def _safe_send(
    send: Callable[[dict[str, Any]], Awaitable[None]],
    event: dict[str, Any],
    timeout: float = _WS_SEND_TIMEOUT_SECONDS,
) -> None:
    """Invoke *send(event)* with an ``asyncio.wait_for`` timeout.

    If the send coroutine does not complete within *timeout* seconds we
    log a warning and raise :class:`WebSocketSendTimeout`. This prevents
    an abandoned client socket from hanging the orchestrator task
    indefinitely (issue #594.3).
    """
    try:
        await asyncio.wait_for(send(event), timeout=timeout)
    except TimeoutError as exc:
        event_type = event.get("type", "<unknown>")
        logger.warning(
            "WebSocket send timed out after %.1fs (event type=%s)",
            timeout,
            event_type,
        )
        raise WebSocketSendTimeout(event_type) from exc


# ---------------------------------------------------------------------------
# Feature flag helpers
# ---------------------------------------------------------------------------


def _is_follow_ups_enabled() -> bool:
    """Return True when the follow-up suggestions feature should run.

    Controlled by ``CHAT_FOLLOW_UPS_ENABLED``. Defaults to True when
    unset. Any truthy value (``"1"``, ``"true"``, ``"yes"``, ``"on"``)
    enables the feature; anything else disables it.
    """
    raw = os.environ.get("CHAT_FOLLOW_UPS_ENABLED")
    if raw is None:
        return True
    return raw.strip().lower() in {"1", "true", "yes", "on"}


# ---------------------------------------------------------------------------
# Draft context loader
# ---------------------------------------------------------------------------


def _load_impact_summary(draft_id: str, org_id: str) -> str | None:
    """Load the latest impact report for *draft_id* and compose an Estonian summary.

    The ``impact_reports.report_data`` JSON is the
    :class:`app.docs.impact.analyzer.ImpactFindings` dataclass as
    written by :mod:`app.docs.analyze_handler` — it has NO ``summary``
    key. The headline metrics live in dedicated columns
    (``affected_count`` / ``conflict_count`` / ``gap_count`` /
    ``impact_score``); the entity lists live in the JSON. Issue #809:
    before this fix the loader looked for a non-existent
    ``report_data["summary"]`` key, always returned ``None`` even when
    a report existed, and the system prompt then surfaced the
    placeholder "Mõjuanalüüsi aruanne pole saadaval." while the LLM
    answered from unrelated RAG fragments.

    The query joins through the ``drafts`` table and filters by
    *org_id* to prevent cross-organisation data access.

    Returns ``None`` only when no report exists for this draft, the
    draft belongs to a different organisation, or the query fails.
    """
    try:
        with get_connection() as conn:
            row = conn.execute(
                """
                SELECT ir.affected_count, ir.conflict_count, ir.gap_count,
                       ir.impact_score, ir.report_data
                FROM impact_reports ir
                JOIN drafts d ON d.id = ir.draft_id
                WHERE ir.draft_id = %s AND d.org_id = %s
                ORDER BY ir.generated_at DESC
                LIMIT 1
                """,
                (draft_id, org_id),
            ).fetchone()
    except Exception:
        logger.exception("Failed to load impact summary for draft_id=%s", draft_id)
        return None

    if row is None:
        return None

    affected_count = int(row[0] or 0)
    conflict_count = int(row[1] or 0)
    gap_count = int(row[2] or 0)
    impact_score = int(row[3] or 0)
    report_data: Any = row[4]
    if isinstance(report_data, str):
        try:
            report_data = json.loads(report_data)
        except json.JSONDecodeError:
            report_data = {}
    if not isinstance(report_data, dict):
        report_data = {}

    lines: list[str] = [
        f"Mõjuskoor: {impact_score}/100.",
        (
            f"Mõjutatud üksused: {affected_count} · "
            f"Konfliktid: {conflict_count} · Lüngad: {gap_count}."
        ),
    ]

    def _entity_label(entry: Any) -> str | None:
        if not isinstance(entry, dict):
            return None
        label = str(entry.get("label") or "").strip()
        uri = str(entry.get("uri") or "").strip()
        return label or uri or None

    affected_entities = report_data.get("affected_entities") or []
    if isinstance(affected_entities, list):
        labels = [label for label in (_entity_label(e) for e in affected_entities[:5]) if label]
        if labels:
            lines.append("Mõjutatud sätted (näited): " + "; ".join(labels) + ".")

    conflicts = report_data.get("conflicts") or []
    if isinstance(conflicts, list):
        labels = [label for label in (_entity_label(e) for e in conflicts[:3]) if label]
        if labels:
            lines.append("Konfliktid (näited): " + "; ".join(labels) + ".")

    gaps = report_data.get("gaps") or []
    if isinstance(gaps, list):
        labels = [label for label in (_entity_label(e) for e in gaps[:3]) if label]
        if labels:
            lines.append("Lüngad (näited): " + "; ".join(labels) + ".")

    summary = "\n".join(lines)
    return summary[:2000]


# ---------------------------------------------------------------------------
# Source / follow-up helpers
# ---------------------------------------------------------------------------


def _build_sources_payload(rag_chunks: list[Any]) -> list[dict[str, Any]]:
    """Convert a list of retrieved RAG chunks into ``sources`` event rows.

    Each entry has ``source_uri``, ``title`` (last URI segment, falling
    back to the full URI), ``score`` and a 200-character ``snippet``.
    Invalid / unparseable chunks are skipped silently so a malformed
    metadata entry never aborts the response.
    """
    sources: list[dict[str, Any]] = []
    for chunk in rag_chunks:
        try:
            metadata = getattr(chunk, "metadata", None) or {}
            source_uri = metadata.get("source_uri")
            content = getattr(chunk, "content", "") or ""
            snippet = content[:200]

            title: str
            if source_uri:
                # Pull the last path segment; strip fragments/query.
                tail = str(source_uri).rstrip("/").split("/")[-1]
                tail = tail.split("#")[0].split("?")[0]
                title = tail or str(source_uri)
            else:
                title = snippet[:64] or "(tundmatu allikas)"

            sources.append(
                {
                    "source_uri": source_uri,
                    "title": title,
                    "score": getattr(chunk, "score", None),
                    "snippet": snippet,
                }
            )
        except Exception:
            logger.debug("Skipped malformed RAG chunk while building sources", exc_info=True)
    return sources


async def _generate_follow_ups(
    user_message: str,
    assistant_reply: str,
    *,
    user_id: Any,
    org_id: Any,
) -> list[str]:
    """Return up to 3 short Estonian follow-up suggestions.

    Uses the default LLM provider's ``acomplete`` with a tiny budget.
    On any failure (feature flag off, LLM error, malformed output) we
    return an empty list — the caller is expected to no-op in that case.
    """
    if not _is_follow_ups_enabled():
        return []

    prompt = (
        "Eelnev vestlus:\n"
        f"Kasutaja: {user_message}\n\n"
        f"Assistent: {assistant_reply}\n\n"
        "Paku välja 3 lühikest (iga kuni 80 tähemärki) eestikeelset järgmist "
        "küsimust, mida kasutaja võiks küsida. Vasta AINULT JSON-massiiviga, "
        'nt: ["Küsimus 1?", "Küsimus 2?", "Küsimus 3?"]. Ära lisa muud teksti.'
    )

    try:
        # Imported lazily so tests can patch the provider easily.
        from app.llm import get_default_provider

        provider = get_default_provider()
        raw = await provider.acomplete(
            prompt,
            max_tokens=_FOLLOW_UPS_MAX_TOKENS,
            temperature=0.5,
            feature="chat_follow_ups",
            user_id=user_id,
            org_id=org_id,
        )
    except Exception:
        logger.debug("Follow-up generation failed; skipping", exc_info=True)
        return []

    if not raw:
        return []

    text = raw.strip()
    # Be forgiving about markdown code fences.
    if text.startswith("```"):
        text = text.strip("`")
        # Drop a leading language hint like ```json
        if "\n" in text:
            text = text.split("\n", 1)[1]
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        logger.debug("Follow-up LLM returned non-JSON: %r", raw[:200])
        return []

    if not isinstance(parsed, list):
        return []
    suggestions: list[str] = []
    for item in parsed[:3]:
        if isinstance(item, str) and item.strip():
            suggestions.append(item.strip()[:120])
    return suggestions


# ---------------------------------------------------------------------------
# Title / truncation persistence helpers
# ---------------------------------------------------------------------------


def _persist_partial_assistant(
    conversation_id: uuid.UUID,
    full_content: str,
    model: str | None,
    rag_context_json: list[dict[str, Any]] | None,
    *,
    is_truncated: bool,
    error_suffix: str | None = None,
    tokens_input: int | None = None,
    tokens_output: int | None = None,
    placeholder_message_id: uuid.UUID | None = None,
    ontology_version: str | None = None,
) -> uuid.UUID | None:
    """Persist a partial / truncated assistant message.

    Attempts to set ``is_truncated`` on the new row; when the column is
    missing (migration 017 not yet applied) we fall back to a plain
    insert + WARNING. Returns the new message id on success or ``None``
    if persistence itself fails.

    ``tokens_input`` / ``tokens_output`` capture the per-turn token
    counts accumulated up to the point of cancellation/timeout (post-
    review fix to #688). Without them every cancelled or timed-out turn
    persisted the partial assistant row with NULL tokens despite the
    LLM having produced billable output that ``_log_cost`` already
    recorded in the ``llm_usage`` table.

    #315 review fix (double-insert): when ``placeholder_message_id`` is
    set (a tool turn had already inserted the parent assistant row up
    front so tool rows could link via ``parent_message_id``), UPDATE
    that row in place with the partial content + tokens + rag_context
    + ``is_truncated`` flag and bump ``created_at = NOW()`` so it sorts
    after the tool rows. Without this, the timeout / cancel / error
    paths inserted a SECOND assistant row alongside the placeholder,
    leaving two assistant messages for one logical turn (one empty
    parent + one partial sibling). The placeholder row already carries
    the turn's ``ontology_version`` from its initial INSERT, so the
    UPDATE branch does not re-stamp it.

    ``ontology_version`` (#352) is the snapshot tag the orchestrator
    captured at turn start; on the INSERT branch (non-tool turns), it
    is stamped on the new partial row so drift detection works when
    the user re-opens a cancelled / timed-out conversation.
    """
    if not full_content and not error_suffix:
        # Tool turn whose placeholder was inserted up-front but the model
        # produced no text (cancelled / socket-dropped / errored before
        # the first delta). Delete the empty row so the conversation
        # doesn't keep an empty assistant bubble forever. Non-tool turns
        # (no placeholder) just return — nothing was inserted to begin
        # with.
        if placeholder_message_id is not None:
            try:
                with get_connection() as conn:
                    conn.execute(
                        "DELETE FROM messages WHERE id = %s",
                        (str(placeholder_message_id),),
                    )
                    conn.commit()
            except Exception:
                logger.exception(
                    "Failed to delete empty placeholder %s after interrupted tool turn",
                    placeholder_message_id,
                )
        return None

    text = full_content
    if error_suffix:
        text = f"{text}{error_suffix}"

    # Tool-turn path: a placeholder assistant row already exists. UPDATE
    # in place instead of inserting a second row.
    if placeholder_message_id is not None:
        try:
            with get_connection() as conn:
                _update_assistant_payload(
                    conn,
                    placeholder_message_id,
                    content=text,
                    tokens_input=tokens_input if tokens_input else None,
                    tokens_output=tokens_output if tokens_output else None,
                    rag_context=rag_context_json,
                )
                if is_truncated:
                    try:
                        conn.execute(
                            "UPDATE messages SET is_truncated = TRUE WHERE id = %s",
                            (str(placeholder_message_id),),
                        )
                    except Exception:
                        # Column may not exist yet (migration 017 not applied).
                        logger.debug(
                            "Could not set is_truncated on placeholder %s — column missing?",
                            placeholder_message_id,
                            exc_info=True,
                        )
                conn.commit()
                return placeholder_message_id
        except Exception:
            logger.exception(
                "Failed to update placeholder assistant message %s with partial content",
                placeholder_message_id,
            )
            return None

    # Non-tool turn: no placeholder, INSERT as before.
    try:
        with get_connection() as conn:
            msg = create_message(
                conn,
                conversation_id,
                "assistant",
                text,
                model=model,
                rag_context=rag_context_json,
                tokens_input=tokens_input if tokens_input else None,
                tokens_output=tokens_output if tokens_output else None,
                ontology_version=ontology_version,
            )
            if is_truncated:
                try:
                    conn.execute(
                        "UPDATE messages SET is_truncated = TRUE WHERE id = %s",
                        (str(msg.id),),
                    )
                except Exception:
                    # Column may not exist yet (migration 017 not applied).
                    logger.debug(
                        "Could not set is_truncated on message %s — column missing?",
                        msg.id,
                        exc_info=True,
                    )
            conn.commit()
            return msg.id
    except Exception:
        logger.exception("Failed to persist partial assistant message")
        return None


async def _maybe_generate_title(
    conversation_id: uuid.UUID,
    conversation: Any,
    history_length_before: int,
    user_message: str,
    assistant_reply: str,
    auth: dict[str, Any],
) -> None:
    """Fire-and-forget auto title generation after the first exchange.

    Runs only when the conversation had zero prior messages (so this is
    the first user→assistant exchange) and ``title_is_custom`` is not
    set. Failure is swallowed — title generation is strictly best-effort.
    """
    if history_length_before != 0:
        return
    # Guard: respect manual titles when the column exists.
    title_is_custom = bool(getattr(conversation, "title_is_custom", False))
    if title_is_custom:
        return

    try:
        from app.chat.title import generate_title

        title = await generate_title(
            user_message,
            assistant_reply,
            user_id=auth.get("id"),
            org_id=auth.get("org_id"),
        )
    except Exception:
        logger.debug("Auto-title generation failed", exc_info=True)
        return

    if not title:
        return

    try:
        with get_connection() as conn:
            # ``update_conversation_title`` already writes ``title_is_custom``;
            # passing ``is_custom=False`` here is the full expression of the
            # auto-title intent, so no follow-up UPDATE is needed.
            update_conversation_title(conn, conversation_id, title, is_custom=False)
            conn.commit()
    except Exception:
        logger.exception("Failed to persist auto-generated title")


# ---------------------------------------------------------------------------
# Pre-stream DB helpers (#658)
# ---------------------------------------------------------------------------


def _step_deadline(pre_stream_start: float) -> float:
    """Return the deadline for the next pre-stream DB step.

    Capped at ``_PRE_STREAM_DB_TIMEOUT_SECONDS`` (per-step ceiling) AND
    by the remaining cumulative ``_PRE_STREAM_TOTAL_BUDGET_SECONDS``
    budget. Returns at least 0.1s so :func:`asyncio.wait_for` always
    has something to wait on rather than failing pre-emptively (a
    healthy DB call still completes in a few ms; only stalled pools
    hit the ceiling).
    """
    elapsed = time.monotonic() - pre_stream_start
    remaining = _PRE_STREAM_TOTAL_BUDGET_SECONDS - elapsed
    return max(0.1, min(_PRE_STREAM_DB_TIMEOUT_SECONDS, remaining))


def _persist_user_message(conversation_id: uuid.UUID, user_message: str) -> None:
    """Insert the user message in a tiny standalone transaction.

    Decoupled from the budget transaction (#658) so a stuck advisory
    lock cannot lose user input. Runs in a thread via
    ``asyncio.to_thread`` because psycopg is sync.
    """
    with get_connection() as conn:
        create_message(conn, conversation_id, "user", user_message)
        conn.commit()


def _update_assistant_payload(
    conn: Any,
    message_id: uuid.UUID,
    *,
    content: str,
    tokens_input: int | None,
    tokens_output: int | None,
    rag_context: list[dict[str, Any]] | None,
) -> None:
    """Update an existing assistant ``messages`` row with the final payload.

    #315: when a turn invokes tools, an assistant row is inserted
    up-front so the tool rows can link via ``parent_message_id``. Once
    streaming completes, this helper updates the placeholder with the
    final ciphertext + token counts + RAG context instead of inserting
    a second row.

    ``content`` is re-encrypted with the storage Fernet — same primitive
    used by :func:`app.chat.models.create_message` — so the on-disk
    representation matches a fresh INSERT exactly. ``rag_context`` is
    JSON-encoded then encrypted (NULL for None).

    #315 review fix (ordering): also bumps ``created_at = NOW()`` so the
    placeholder sorts AFTER the tool rows that were inserted between the
    initial placeholder INSERT and this UPDATE. Without this, history
    loaded by ``list_messages`` (``ORDER BY created_at ASC``) returns
    ``[assistant_final, tool_call, tool_result, next_user]`` — the wrong
    semantic order for replay to Claude, which requires
    ``tool_call → tool_result → assistant_final``. Using the DB's
    ``NOW()`` rather than Python's ``datetime.now()`` avoids clock-skew
    issues between the app process and PostgreSQL.
    """
    from app.storage import encrypt_text

    content_ciphertext = encrypt_text(content) if content else encrypt_text("")
    rag_context_ciphertext: bytes | None = (
        encrypt_text(json.dumps(rag_context, ensure_ascii=False))
        if rag_context is not None
        else None
    )
    conn.execute(
        """
        UPDATE messages
        SET content_encrypted = %s,
            tokens_input = %s,
            tokens_output = %s,
            rag_context_encrypted = %s,
            created_at = NOW()
        WHERE id = %s
        """,
        (
            content_ciphertext,
            tokens_input,
            tokens_output,
            rag_context_ciphertext,
            str(message_id),
        ),
    )


def _check_budget_in_own_tx(org_id: uuid.UUID | str) -> None:
    """Run the per-org cost-budget check in its own short-lived tx.

    The advisory lock taken inside ``check_org_cost_budget`` is bounded
    by ``SET LOCAL lock_timeout = '3s'`` (set inside the function),
    so a stuck lock raises ``LockNotAvailable`` which the function's
    own except catches and fails open. Raises
    :class:`CostBudgetExceededError` if the org is over budget.
    """
    with get_connection() as conn:
        check_org_cost_budget(org_id, conn=conn)
        conn.commit()


def _load_conversation(conversation_id: uuid.UUID) -> Conversation | None:
    """Load a conversation row in a sync ``with get_connection()`` block.

    Wrapped by :func:`asyncio.to_thread` in ``handle_message`` so the
    sync psycopg call doesn't block the event loop (#658).
    """
    with get_connection() as conn:
        return get_conversation(conn, conversation_id)


def _load_history(conversation_id: uuid.UUID) -> list[Message]:
    """Load message history for a conversation.

    Wrapped by :func:`asyncio.to_thread` in ``handle_message`` so the
    sync psycopg call doesn't block the event loop (#658).
    """
    with get_connection() as conn:
        return list_messages(conn, conversation_id)


# ---------------------------------------------------------------------------
# Per-turn context
# ---------------------------------------------------------------------------


@dataclass
class _TurnContext:
    """Mutable per-turn state shared across orchestrator phase methods.

    Each phase method on :class:`ChatOrchestrator` reads and writes this
    context, returning a ``bool`` that tells
    :meth:`ChatOrchestrator.handle_message` whether to continue
    (``True``) or abort the turn (``False``). The split keeps
    ``handle_message`` short while preserving every event ordering,
    deadline, recovery path, and bug-rationale comment from the prior
    monolithic version.
    """

    # Inputs from the WS handler
    conversation_id: uuid.UUID
    user_message: str
    auth: dict[str, Any]
    send: Callable[[dict[str, Any]], Awaitable[None]]
    regenerate_pivot_message_id: uuid.UUID | None

    # Derived from auth + clock
    user_id: Any
    org_id: Any
    pre_stream_start: float
    regenerating: bool

    # Loaded during pre-stream phases
    conversation: Conversation | None = None
    history: list[Message] = field(default_factory=list)
    history_length_before: int = 0

    # System prompt / draft context
    impact_summary: str | None = None
    draft_context_id: str | None = None
    system_prompt: str = ""

    # RAG retrieval
    rag_chunks: list[Any] = field(default_factory=list)
    rag_context_json: list[dict[str, Any]] | None = None
    retrieval_attempted: bool = False
    rag_timed_out: bool = False

    # Stream state (mirrors the old ``stream_state`` dict so timeout /
    # cancel paths can still see whatever partial content was streamed
    # before the deadline fired).
    full_content: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    completed: bool = False
    cancelled: bool = False
    assistant_msg_id: uuid.UUID | None = None

    # Issue #352 — ontology snapshot tag captured once per turn and
    # stamped on the persisted assistant row. Resolved lazily in
    # :meth:`ChatOrchestrator._phase_build_system_prompt` so the
    # ``sync_log`` lookup runs after auth + history loads (so a stalled
    # ontology DB doesn't block the cheap pre-stream checks). NULL when
    # the sync_log is empty or the lookup fails — the conversation view
    # treats NULL as "snapshot unknown, no drift banner".
    ontology_version: str | None = None


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


class ChatOrchestrator:
    """Coordinate LLM interaction for a single chat turn.

    Parameters
    ----------
    llm:
        The LLM provider to use for completions.
    sparql:
        Optional SPARQL client for tool execution. A default
        ``SparqlClient()`` is constructed when omitted.
    retriever:
        Optional RAG retriever for grounding the LLM in relevant legal
        context. When ``None``, a default :class:`Retriever` is
        constructed if the module is available. If the retriever or its
        embedding provider is in stub mode, RAG is skipped gracefully.
    """

    def __init__(
        self,
        llm: LLMProvider,
        sparql: SparqlClient | None = None,
        retriever: Any | None = None,
    ) -> None:
        self.llm = llm
        self.sparql = sparql or SparqlClient()
        # Lazily initialise the retriever; None means "try once, then skip"
        self._retriever = retriever
        self._retriever_initialised = retriever is not None

    def _get_retriever(self) -> Any | None:
        """Return the RAG retriever, lazily constructing one on first call.

        Returns ``None`` when the ``app.rag`` module is unavailable or
        the retriever cannot be constructed (e.g. missing API key).
        """
        if self._retriever_initialised:
            return self._retriever

        self._retriever_initialised = True
        if Retriever is None:
            logger.info("app.rag not available — RAG disabled")
            return None
        try:
            self._retriever = Retriever()
            return self._retriever
        except Exception:
            logger.warning("Failed to construct Retriever — RAG disabled", exc_info=True)
            return None

    async def handle_message(
        self,
        conversation_id: uuid.UUID,
        user_message: str,
        auth: dict[str, Any],
        send: Callable[[dict[str, Any]], Awaitable[None]],
        *,
        regenerate_pivot_message_id: uuid.UUID | None = None,
    ) -> None:
        """Process a user message and stream the assistant response.

        Steps:
          1. Load conversation + history
          2. Build system prompt (with optional draft context)
          3. Persist the user message
          4. Call LLM with tool-use enabled
          5. Stream content deltas via *send*
          6. On tool_use: execute, send result, continue streaming
          7. Persist assistant message with token counts
          8. Send ``done`` event + ``sources`` + optional ``follow_ups``

        Parameters
        ----------
        conversation_id:
            UUID of the conversation this message belongs to.
        user_message:
            The text the user typed. The sentinel empty string ``""`` is
            the marker for **regenerate mode** (issues #737 / #738) — the
            WS ``regenerate`` action passes it; the prompt is then taken
            from the persisted history rather than from this argument.
        auth:
            Auth dict from request scope (must have ``id``, ``org_id``).
        send:
            Async callback for pushing events to the client. Wrapped in
            :func:`_safe_send` with a 5-second timeout.
        regenerate_pivot_message_id:
            Only meaningful in regenerate mode. The user message to
            regenerate from; the HTTP regenerate/edit endpoints resolve
            the clicked assistant bubble to that boundary and have
            already deleted the stale reply + downstream. When the id
            resolves to an assistant / tool reply we walk back to the
            nearest preceding user turn; when it is ``None`` the
            conversation's last user message is used. Outside regenerate
            mode (``user_message`` non-empty) this argument is ignored.
        """
        ctx = _TurnContext(
            conversation_id=conversation_id,
            user_message=user_message,
            auth=auth,
            send=send,
            regenerate_pivot_message_id=regenerate_pivot_message_id,
            user_id=auth.get("id"),
            org_id=auth.get("org_id"),
            # Post-review fix to #684: track pre-stream wall-clock so each
            # awaited DB step is capped by ``min(per_step_ceiling,
            # remaining_total_budget)``. Without the global cap, five
            # sequential 8s steps could stall the turn for ~40s before
            # the user saw any feedback. With it, worst-case pre-stream
            # latency is ``_PRE_STREAM_TOTAL_BUDGET_SECONDS``.
            pre_stream_start=time.monotonic(),
            # Regenerate mode is signalled by the WS ``regenerate`` action
            # passing an empty ``user_message``; a normal ``send_message``
            # turn always carries non-empty content (the WS handler
            # rejects blanks).
            regenerating=not (user_message or "").strip(),
        )

        # Pre-stream phases — each guards a DB call with a step deadline
        # and emits a friendly error event on failure (#658, #684). A
        # ``False`` return means the phase has already handled the error
        # (sent the event when the socket was alive) and the turn must
        # abort.
        if not await self._phase_rate_limit(ctx):
            return
        if not await self._phase_load_conversation(ctx):
            return
        if not await self._phase_load_history(ctx):
            return
        if not await self._phase_persist_user_message(ctx):
            return
        if not await self._phase_check_budget(ctx):
            return
        await self._phase_build_system_prompt(ctx)

        # RAG retrieval — emits ``retrieval_started`` / ``retrieval_done``;
        # may re-raise ``CancelledError`` so the outer task records the
        # partial assistant turn.
        if not await self._phase_rag_retrieval(ctx):
            return

        # Streaming + tool-use loop, wrapped in turn-deadline + recovery.
        # May re-raise ``CancelledError`` after persisting partial state.
        if not await self._phase_stream_llm(ctx):
            return
        if ctx.cancelled:
            # Belt-and-suspenders — CancelledError should have re-raised.
            return

        # ``done`` / ``sources`` / ``follow_ups`` / auto-title.
        await self._phase_emit_terminal_events(ctx)

    # ------------------------------------------------------------------
    # Pre-stream phase methods
    # ------------------------------------------------------------------

    async def _phase_rate_limit(self, ctx: _TurnContext) -> bool:
        """Step 0 — per-user message rate limit.

        Cheap pre-check, fail-open on DB error. Bounded via
        ``asyncio.wait_for`` so a stuck pool can't hang the turn (#658).
        Effective deadline is ``min(_PRE_STREAM_DB_TIMEOUT_SECONDS,
        remaining_global_budget)``.

        Demoted to debug (post-review fix): info-level on every turn was
        ~1500 lines/min peak — keep the marker but only at debug.
        """
        logger.debug(
            "chat.handle_message step=0-rate-check conv=%s user=%s",
            ctx.conversation_id,
            ctx.user_id,
        )
        try:
            if ctx.user_id:
                await asyncio.wait_for(
                    asyncio.to_thread(check_message_rate, ctx.user_id),
                    timeout=_step_deadline(ctx.pre_stream_start),
                )
        except RateLimitExceededError as exc:
            try:
                await _safe_send(ctx.send, {"type": "error", "message": str(exc)})
            except WebSocketSendTimeout:
                pass
            return False
        except TimeoutError:
            logger.warning(
                "Rate-limit check timed out after %.1fs of pre-stream budget "
                "for user=%s; failing open",
                time.monotonic() - ctx.pre_stream_start,
                ctx.user_id,
            )
            # Fail open — let the turn through; a stuck rate-limit DB
            # call shouldn't block a paying user from chatting.
        return True

    async def _phase_load_conversation(self, ctx: _TurnContext) -> bool:
        """Step 1 — load the conversation row and enforce owner-only access.

        Bounded for the same reason as step 0. Access control is owner-
        only per NFR §5 matrix (fix #569). The previous org-level check
        allowed any same-org colleague to send turns into another user's
        private conversation.
        """
        logger.debug(
            "chat.handle_message step=1-load-conv conv=%s",
            ctx.conversation_id,
        )
        try:
            conversation = await asyncio.wait_for(
                asyncio.to_thread(_load_conversation, ctx.conversation_id),
                timeout=_step_deadline(ctx.pre_stream_start),
            )
        except TimeoutError:
            logger.warning(
                "Conversation load timed out after %.1fs of pre-stream budget for conv=%s",
                time.monotonic() - ctx.pre_stream_start,
                ctx.conversation_id,
            )
            try:
                await _safe_send(
                    ctx.send,
                    {
                        "type": "error",
                        "message": "Andmebaas vastab aeglaselt. Palun proovi uuesti.",
                    },
                )
            except WebSocketSendTimeout:
                pass
            return False
        except Exception:
            logger.exception("Failed to load conversation %s", ctx.conversation_id)
            try:
                await _safe_send(
                    ctx.send,
                    {"type": "error", "message": "Vestluse laadimine ebaõnnestus."},
                )
            except WebSocketSendTimeout:
                pass
            return False

        if conversation is None:
            try:
                await _safe_send(ctx.send, {"type": "error", "message": "Vestlust ei leitud."})
            except WebSocketSendTimeout:
                pass
            return False

        if not can_access_conversation(ctx.auth, conversation):
            try:
                await _safe_send(
                    ctx.send,
                    {"type": "error", "message": "Puudub õigus sellele vestlusele."},
                )
            except WebSocketSendTimeout:
                pass
            return False

        ctx.conversation = conversation
        return True

    async def _phase_load_history(self, ctx: _TurnContext) -> bool:
        """Step 2 — load message history and resolve regenerate pivot.

        The history load is bounded; on error or timeout we proceed with
        empty history so the turn can still happen.

        Regenerate mode (issues #737 / #738): the prompt is the persisted
        user turn, not a freshly-typed message. Resolve the pivot, strip
        it (and anything after — though the HTTP endpoint already
        trimmed) from ``history`` so :func:`_build_llm_messages` re-adds
        it exactly once at the tail, and use its content as the effective
        ``user_message``. We do NOT persist a new user row — re-inserting
        the pivot is exactly the duplicate-turn bug #737 set out to fix.
        ``history_length_before`` keeps the *pre-trim* count so the
        first-exchange-only auto-title job
        (:func:`_maybe_generate_title`, gated on
        ``history_length_before == 0``) never fires when the
        conversation's opening turn is regenerated.
        """
        logger.debug(
            "chat.handle_message step=2-load-history conv=%s",
            ctx.conversation_id,
        )
        try:
            history = await asyncio.wait_for(
                asyncio.to_thread(_load_history, ctx.conversation_id),
                timeout=_step_deadline(ctx.pre_stream_start),
            )
        except TimeoutError:
            logger.warning(
                "History load timed out after %.1fs of pre-stream budget for conv=%s; using empty",
                time.monotonic() - ctx.pre_stream_start,
                ctx.conversation_id,
            )
            history = []
        except Exception:
            logger.exception("Failed to load messages for conv=%s", ctx.conversation_id)
            history = []

        ctx.history_length_before = len(history)

        if ctx.regenerating:
            pivot_index = _find_regenerate_pivot_index(history, ctx.regenerate_pivot_message_id)
            if pivot_index is None:
                logger.info(
                    "Regenerate requested for conv=%s but no user turn found "
                    "(pivot=%s) — nothing to replay",
                    ctx.conversation_id,
                    ctx.regenerate_pivot_message_id,
                )
                try:
                    await _safe_send(
                        ctx.send,
                        {"type": "error", "message": "Pole midagi uuesti genereerida."},
                    )
                except WebSocketSendTimeout:
                    pass
                return False
            ctx.user_message = history[pivot_index].content or ""
            history = history[:pivot_index]
            logger.info(
                "chat.handle_message step=regenerate conv=%s pivot_index=%s",
                ctx.conversation_id,
                pivot_index,
            )

        ctx.history = history
        return True

    async def _phase_persist_user_message(self, ctx: _TurnContext) -> bool:
        """Step 3a — persist the user message FIRST in its own transaction.

        Decoupled from the budget check so a stuck
        ``pg_advisory_xact_lock`` or any downstream failure cannot lose
        user input (#658). Bounded by a step deadline so a stalled pool
        surfaces as an error instead of a silent hang. The persist runs
        in a thread because psycopg is sync; ``asyncio.to_thread`` keeps
        the event loop responsive.

        Reordered (post-review fix to #658): persist now precedes the
        impact-summary load + system-prompt build. Previously a future
        contributor adding an early return from the impact-summary path
        would silently re-introduce the data-loss bug. Persist is now
        unconditionally the first DB write of the turn after the read-
        only setup steps.

        Skipped in regenerate mode: the user message already lives in the
        DB (it is the pivot), so re-inserting it would duplicate the turn.
        """
        if ctx.regenerating:
            return True

        logger.info(
            "chat.handle_message step=3a-persist-user conv=%s user=%s",
            ctx.conversation_id,
            ctx.user_id,
        )
        try:
            await asyncio.wait_for(
                asyncio.to_thread(_persist_user_message, ctx.conversation_id, ctx.user_message),
                timeout=_step_deadline(ctx.pre_stream_start),
            )
        except TimeoutError:
            logger.warning(
                "User-message persist timed out after %.1fs of pre-stream budget for conv=%s",
                time.monotonic() - ctx.pre_stream_start,
                ctx.conversation_id,
            )
            try:
                await _safe_send(
                    ctx.send,
                    {
                        "type": "error",
                        "message": "Andmebaas vastab aeglaselt. Palun proovi uuesti.",
                    },
                )
            except WebSocketSendTimeout:
                pass
            return False
        except Exception:
            logger.exception("Failed to persist user message for conv=%s", ctx.conversation_id)
            try:
                await _safe_send(
                    ctx.send,
                    {"type": "error", "message": "Sõnumi salvestamine ebaõnnestus."},
                )
            except WebSocketSendTimeout:
                pass
            return False
        return True

    async def _phase_check_budget(self, ctx: _TurnContext) -> bool:
        """Step 3b — per-org cost-budget check in a separate transaction.

        The user message is already safely persisted; if the budget is
        over the user simply sees an error (and can see what they tried
        to ask on reload). Fail open on timeout or unexpected DB error —
        letting the turn through is preferable to losing data.
        """
        if not ctx.org_id:
            return True

        logger.info(
            "chat.handle_message step=3b-budget conv=%s org=%s",
            ctx.conversation_id,
            ctx.org_id,
        )
        try:
            await asyncio.wait_for(
                asyncio.to_thread(_check_budget_in_own_tx, ctx.org_id),
                timeout=_step_deadline(ctx.pre_stream_start),
            )
        except CostBudgetExceededError as exc:
            try:
                await _safe_send(ctx.send, {"type": "error", "message": str(exc)})
            except WebSocketSendTimeout:
                pass
            return False
        except TimeoutError:
            logger.warning(
                "Budget check timed out after %.1fs of pre-stream budget "
                "for conv=%s; failing open",
                time.monotonic() - ctx.pre_stream_start,
                ctx.conversation_id,
            )
            # Fail open — user message already persisted; better to
            # let this turn through than to lose data.
        except Exception:
            logger.exception("Budget check failed for conv=%s", ctx.conversation_id)
            # Fail open
        return True

    async def _phase_build_system_prompt(self, ctx: _TurnContext) -> None:
        """Step 4 — build the system prompt with optional draft context.

        The impact-summary load is a sync DB call; wrap it in the same
        ``wait_for`` + ``to_thread`` pattern as the other pre-stream
        loads. Moved AFTER persist (post-review fix to #658): an early
        return from this load can no longer regress the data-loss
        guarantee. Fail-open with no summary on timeout — the chat still
        works, it just won't reference impact details.
        """
        impact_summary: str | None = None
        draft_context_id: str | None = None
        if ctx.conversation is not None and ctx.conversation.context_draft_id:
            draft_context_id = str(ctx.conversation.context_draft_id)
            try:
                impact_summary = await asyncio.wait_for(
                    asyncio.to_thread(_load_impact_summary, draft_context_id, str(ctx.org_id)),
                    timeout=_step_deadline(ctx.pre_stream_start),
                )
            except TimeoutError:
                logger.warning(
                    "Impact summary load timed out after %.1fs of pre-stream "
                    "budget for draft=%s; proceeding without context",
                    time.monotonic() - ctx.pre_stream_start,
                    draft_context_id,
                )
                impact_summary = None
            except Exception:
                logger.exception("Failed to load impact summary for draft=%s", draft_context_id)
                impact_summary = None

        ctx.impact_summary = impact_summary
        ctx.draft_context_id = draft_context_id
        ctx.system_prompt = build_system_prompt(
            draft_context_id=draft_context_id,
            impact_summary=impact_summary,
        )

        # Issue #352: capture the live ontology snapshot tag exactly once
        # per turn. We do this even when no RAG / tool grounding ends up
        # being used — the snapshot describes "what the assistant could
        # see when it spoke", not "what it actually consulted". Stamped
        # onto the assistant row in the persist step below. Fail-open:
        # any error inside the helper resolves to ``"unknown"``.
        try:
            ctx.ontology_version = await asyncio.wait_for(
                asyncio.to_thread(get_current_ontology_version),
                timeout=_step_deadline(ctx.pre_stream_start),
            )
        except TimeoutError:
            logger.warning(
                "Ontology snapshot lookup timed out after %.1fs of "
                "pre-stream budget for conv=%s; stamping NULL",
                time.monotonic() - ctx.pre_stream_start,
                ctx.conversation_id,
            )
            ctx.ontology_version = None
        except Exception:
            logger.exception(
                "Ontology snapshot lookup failed for conv=%s; stamping NULL",
                ctx.conversation_id,
            )
            ctx.ontology_version = None

    # ------------------------------------------------------------------
    # RAG + streaming + terminal phase methods
    # ------------------------------------------------------------------

    async def _phase_rag_retrieval(self, ctx: _TurnContext) -> bool:
        """Step 5 — RAG retrieval.

        Now that the user message is safely persisted, we can afford to
        have RAG fail or time out without losing data. Tenant scoping
        landed in #576: retriever filters by caller's org_id (public
        NULL-scoped rows + caller's own private rows).

        Returns ``False`` only when the WS send has gone dead and the
        whole turn should bail. ``CancelledError`` is re-raised so the
        outer task records the partial state.
        """
        if Retriever is None:
            return True

        retriever = self._get_retriever()
        if retriever is not None:
            ctx.retrieval_attempted = True
            try:
                await _safe_send(ctx.send, {"type": "retrieval_started"})
                # #576: pass the caller's org_id so private chunks from
                # other tenants are never retrieved.
                # #652: bound the retrieval call so an unresponsive
                # Voyage AI embedding endpoint can't hang the turn.
                ctx.rag_chunks = await asyncio.wait_for(
                    retriever.retrieve(
                        ctx.user_message,
                        k=10,
                        org_id=str(ctx.org_id) if ctx.org_id else None,
                    ),
                    timeout=_RAG_RETRIEVE_TIMEOUT_SECONDS,
                )
            except WebSocketSendTimeout:
                return False
            except asyncio.CancelledError:
                # #659: client cancelled while we were awaiting RAG.
                # Emit ``retrieval_done`` (shielded so the cancel can't
                # abort the send) so the UI still exits its "Otsin
                # konteksti…" state, then re-raise so the outer
                # CancelledError handler can persist partial state.
                try:
                    await asyncio.shield(
                        _safe_send(
                            ctx.send,
                            {"type": "retrieval_done", "chunk_count": 0},
                        )
                    )
                except (WebSocketSendTimeout, asyncio.CancelledError, Exception):
                    # Best-effort: a second cancel or a wedged socket
                    # must not mask the original CancelledError.
                    pass
                raise
            except TimeoutError:
                # #652: don't let a stalled retriever escalate to a hung
                # turn. Log, continue without RAG — downstream code
                # already handles an empty chunk list.
                logger.warning(
                    "RAG retrieval timed out after %.1fs for conversation %s; "
                    "proceeding without context",
                    _RAG_RETRIEVE_TIMEOUT_SECONDS,
                    ctx.conversation_id,
                )
                ctx.rag_chunks = []
                ctx.rag_timed_out = True
            except Exception:
                logger.warning(
                    "RAG retrieval failed for conversation %s; proceeding without",
                    ctx.conversation_id,
                    exc_info=True,
                )
                ctx.rag_chunks = []

        if ctx.rag_chunks:
            chunks_text = "\n---\n".join(chunk.content for chunk in ctx.rag_chunks)
            ctx.system_prompt += "\n\nRelevant legal context:\n" + chunks_text
            ctx.rag_context_json = [
                {
                    "content": chunk.content,
                    "source_uri": chunk.metadata.get("source_uri"),
                    "score": chunk.score,
                }
                for chunk in ctx.rag_chunks
            ]

        # Always emit retrieval_done when we attempted (or skipped) RAG
        # so the UI can move out of the "Otsin konteksti…" state
        # predictably.
        if ctx.retrieval_attempted:
            try:
                await _safe_send(
                    ctx.send,
                    {"type": "retrieval_done", "chunk_count": len(ctx.rag_chunks)},
                )
            except WebSocketSendTimeout:
                return False

            # #652: surface a soft hint when retrieval timed out so the
            # UI can show "konteksti ei leitud" instead of silently
            # pretending everything is fine.
            if ctx.rag_timed_out:
                try:
                    await _safe_send(
                        ctx.send,
                        {
                            "type": "warning",
                            "message": (
                                "Konteksti otsing aegus — vastus antakse ilma "
                                "täiendava kontekstita."
                            ),
                        },
                    )
                except WebSocketSendTimeout:
                    return False
        return True

    async def _phase_stream_llm(self, ctx: _TurnContext) -> bool:
        """Step 6 — LLM streaming with tool use, wrapped in turn-deadline
        and recovery.

        Mutable state lives on ``ctx`` so that timeout / cancel paths can
        still see whatever partial content was streamed before the
        deadline fired.

        Returns ``False`` when the turn is fully terminated by an error /
        timeout / ws-dead path (the caller should ``return`` immediately).
        Returns ``True`` to continue to the terminal-events phase, even
        when ``ctx.completed`` is False (which means we ran out of tool
        rounds or had no content — the terminal-events phase handles
        those cases). ``ctx.cancelled = True`` plus a re-raise of
        ``CancelledError`` is how a user-initiated Stop propagates upward.
        """
        # Build conversation messages for the LLM.
        messages = _build_llm_messages(ctx.history, ctx.user_message)
        tool_rounds = 0

        async def _run_stream_loop() -> None:
            """Run the LLM streaming + tool-use loop.

            Writes progress into the enclosing ``ctx`` so the outer
            handler can recover partial content on timeout / cancel /
            error.
            """
            nonlocal tool_rounds
            while tool_rounds <= MAX_TOOL_ROUNDS:
                event: StreamEvent
                # #636 review: a single Claude turn can contain multiple
                # ``tool_use`` blocks. We MUST execute every one and feed
                # back a ``tool_result`` for every ``tool_use_id`` in the
                # next user message, otherwise Anthropic's API rejects
                # the follow-up turn (and the model loses context for
                # the tools it asked for). Tracking only the latest
                # pending tool_use silently drops the earlier calls.
                pending_tools: list[dict[str, Any]] = []

                stream = self.llm.astream(
                    prompt=_messages_to_prompt(messages),
                    system=ctx.system_prompt,
                    max_tokens=4096,
                    temperature=0.3,
                    feature="chat",
                    user_id=ctx.user_id,
                    org_id=ctx.org_id,
                    tools=CHAT_TOOLS,
                )
                # ``aclosing`` guarantees the upstream HTTP connection is
                # released on every exit path (timeout, cancel,
                # exception). ``astream`` is declared to return an
                # ``AsyncIterator`` but concrete implementations are
                # always async generators, which expose ``aclose`` —
                # hence the local type-ignore.
                async with aclosing(stream) as managed_stream:  # type: ignore[type-var]
                    async for event in managed_stream:
                        if event.type == "content":
                            ctx.full_content += event.delta or ""
                            await _safe_send(
                                ctx.send,
                                {
                                    "type": "content_delta",
                                    "delta": event.delta or "",
                                },
                            )
                        elif event.type == "tool_use":
                            tool_call_id = uuid.uuid4().hex[:12]
                            pending_tools.append(
                                {
                                    "name": event.tool_name,
                                    "input": event.tool_input or {},
                                    "id": tool_call_id,
                                    "tool_use_id": event.tool_use_id,
                                }
                            )
                            await _safe_send(
                                ctx.send,
                                {
                                    "type": "tool_use",
                                    "tool": event.tool_name,
                                    "input": event.tool_input or {},
                                    "tool_call_id": tool_call_id,
                                },
                            )
                        elif event.type == "stop":
                            # #662 / post-review fix: capture per-round
                            # token counts and OVERWRITE rather than
                            # accumulate. Anthropic's
                            # ``message_start.input_tokens`` reports the
                            # FULL prompt for that round, which on a
                            # multi-round tool-use turn includes the
                            # original prompt PLUS every prior assistant
                            # message PLUS every tool_result block.
                            # Accumulating across N rounds would sum the
                            # prompt-prefix N times.
                            #
                            # The persisted message-row tokens reflect
                            # the FINAL round (the assistant content
                            # actually shown to the user). Per-round
                            # API-call costs are separately recorded one
                            # row per call in ``llm_usage`` by
                            # ``_log_cost`` inside the provider.
                            if event.tokens_input is not None:
                                ctx.tokens_in = event.tokens_input
                            if event.tokens_output is not None:
                                ctx.tokens_out = event.tokens_output
                            # Continue — the post-loop logic decides
                            # whether we're truly done (no pending
                            # tools).

                # If no tool use requested, we're done.
                if not pending_tools:
                    ctx.completed = True
                    break

                # Execute every tool in this turn. A turn that asks for
                # N tools still counts as ONE round against
                # MAX_TOOL_ROUNDS — the budget bounds *assistant turns*,
                # not tool fan-out.
                tool_rounds += 1
                tool_call_segments: list[str] = []

                # #315: persist the assistant turn that requested the
                # tools BEFORE running them, so each tool row can link
                # back via ``parent_message_id``. We use whatever
                # content has streamed so far (may be empty when the
                # turn opens with a tool_use); the terminal-events
                # phase UPDATEs this row with the final content + token
                # counts. Skipped when an assistant row already exists
                # for this turn (multi-round tool use: the same parent
                # links every tool the turn requested).
                if ctx.assistant_msg_id is None:
                    try:
                        with get_connection() as conn:
                            assistant_msg = create_message(
                                conn,
                                ctx.conversation_id,
                                "assistant",
                                ctx.full_content,
                                model=getattr(self.llm, "_model", None),
                                # #352: stamp the snapshot tag on the
                                # placeholder so the assistant row carries
                                # it even if a later UPDATE never runs
                                # (turn timeout, cancel, etc.).
                                ontology_version=ctx.ontology_version,
                            )
                            conn.commit()
                            ctx.assistant_msg_id = assistant_msg.id
                    except Exception:
                        logger.exception(
                            "Failed to persist parent assistant message for tool turn"
                        )
                        # Continue without a parent link rather than
                        # losing the tool rows entirely; they will be
                        # stored with parent_message_id=NULL and still
                        # carry tool_use_id for replay.

                for pending in pending_tools:
                    tool_name = pending["name"]
                    tool_input = pending["input"]
                    tool_call_id = pending["id"]
                    tool_use_id = pending["tool_use_id"]

                    tool_result = await execute_tool(
                        tool_name, tool_input, self.sparql, auth=ctx.auth
                    )

                    await _safe_send(
                        ctx.send,
                        {
                            "type": "tool_result",
                            "tool": tool_name,
                            "result_count": _tool_result_count(tool_result),
                            "result": tool_result,
                            "tool_call_id": tool_call_id,
                        },
                    )

                    # Persist tool message.
                    # #315: link to the parent assistant row via
                    # ``parent_message_id`` and store Claude's
                    # ``tool_use_id`` so the renderer can group the
                    # tool under its parent and the replay path can
                    # rebuild the ``tool_use → tool_result`` pairing
                    # the Anthropic API requires for multi-turn tool
                    # use.
                    try:
                        with get_connection() as conn:
                            create_message(
                                conn,
                                ctx.conversation_id,
                                "tool",
                                json.dumps(tool_result),
                                tool_name=tool_name,
                                tool_input=tool_input,
                                tool_output=tool_result,
                                tool_use_id=tool_use_id,
                                parent_message_id=ctx.assistant_msg_id,
                            )
                            conn.commit()
                    except Exception:
                        logger.exception("Failed to persist tool message")

                    # Build a ``tool_result`` block tagged with the
                    # provider's ``tool_use_id`` so the next-turn prompt
                    # carries one block per tool_use, in order.
                    # Anthropic matches results to calls by id;
                    # preserving the id is what makes a multi-tool turn
                    # legal.
                    tool_call_segments.append(
                        f"[Tool call id={tool_use_id or tool_call_id} "
                        f"name={tool_name}]({json.dumps(tool_input)})\n"
                        f"[Tool result for id={tool_use_id or tool_call_id}]"
                        f"({json.dumps(tool_result)})"
                    )

                # Append ALL tool interactions from this turn as a
                # single follow-up "user" segment. This mirrors
                # Anthropic's API contract: one assistant message
                # containing N tool_use blocks is answered by one user
                # message containing N tool_result blocks.
                messages.append("\n".join(tool_call_segments))

                if tool_rounds >= MAX_TOOL_ROUNDS:
                    ctx.full_content += "\n\n(Tööriistade kasutamise limiit saavutatud.)"
                    await _safe_send(
                        ctx.send,
                        {
                            "type": "content_delta",
                            "delta": "\n\n(Tööriistade kasutamise limiit saavutatud.)",
                        },
                    )
                    ctx.completed = True
                    break

        try:
            # #652: bound the whole streaming loop so a stuck upstream
            # LLM call can never leave the user staring at a spinner
            # forever. ``asyncio.wait_for`` cancels the inner coroutine
            # on timeout, which surfaces as ``TimeoutError`` here (not
            # CancelledError in the outer task) so we can distinguish a
            # deadline-hit from a user-initiated Stop.
            await asyncio.wait_for(_run_stream_loop(), timeout=_TURN_DEADLINE_SECONDS)
        except TimeoutError:
            # #652: turn deadline exceeded. Persist whatever partial
            # content was streamed so the user isn't left with a
            # dangling spinner, then emit an error with a friendly
            # Estonian message.
            logger.warning(
                "Chat turn deadline (%ss) exceeded for conversation %s",
                _TURN_DEADLINE_SECONDS,
                ctx.conversation_id,
            )
            if ctx.full_content or ctx.assistant_msg_id is not None:
                # #660: psycopg is sync; offload the partial-persist so
                # a cancellation handler doesn't block the event loop.
                # Post-review fix to #688: forward the per-turn token
                # counts captured so far so the assistant message row
                # records billable tokens even on the deadline path.
                # #315 review fix: when a placeholder already exists
                # (tool turn that timed out after the parent INSERT but
                # before the final UPDATE), UPDATE the placeholder
                # rather than INSERTing a second assistant row.
                # #315 review follow-up: keep calling the persist helper
                # even when no content has been streamed yet, as long as
                # a placeholder exists — that way the error_suffix lands
                # on the placeholder row (or the placeholder gets deleted
                # if the helper has nothing to write).
                await asyncio.to_thread(
                    _persist_partial_assistant,
                    ctx.conversation_id,
                    ctx.full_content,
                    getattr(self.llm, "_model", None),
                    ctx.rag_context_json,
                    is_truncated=True,
                    error_suffix=" [Viga: serveri vastus võttis liiga kaua aega]",
                    tokens_input=ctx.tokens_in,
                    tokens_output=ctx.tokens_out,
                    placeholder_message_id=ctx.assistant_msg_id,
                    ontology_version=ctx.ontology_version,
                )
            try:
                await _safe_send(
                    ctx.send,
                    {
                        "type": "error",
                        "message": "Serveri vastus võttis liiga kaua aega. Palun proovi uuesti.",
                    },
                )
            except WebSocketSendTimeout:
                pass
            return False
        except asyncio.CancelledError:
            # Client asked us to stop — persist partial content flagged
            # as truncated, emit a stopped event, then re-raise so the
            # caller's task.cancel() propagates correctly.
            ctx.cancelled = True
            # #660: shield the partial-persist from a second cancel — we
            # already chose to record the partial; let it complete. Also
            # offload to a thread so psycopg I/O doesn't block the event
            # loop during the cancellation path.
            # #315 review fix: when a placeholder already exists (tool
            # turn cancelled mid-flight), UPDATE it rather than INSERTing
            # a second assistant row.
            ctx.assistant_msg_id = await asyncio.shield(
                asyncio.to_thread(
                    _persist_partial_assistant,
                    ctx.conversation_id,
                    ctx.full_content,
                    getattr(self.llm, "_model", None),
                    ctx.rag_context_json,
                    is_truncated=True,
                    tokens_input=ctx.tokens_in,
                    tokens_output=ctx.tokens_out,
                    placeholder_message_id=ctx.assistant_msg_id,
                    ontology_version=ctx.ontology_version,
                )
            )
            # #661: shield the stopped-event send from a second cancel
            # so the UI still receives the stop acknowledgement even if
            # the task is cancelled again while we're flushing the frame.
            try:
                await asyncio.shield(
                    _safe_send(
                        ctx.send,
                        {
                            "type": "stopped",
                            "message_id": str(ctx.assistant_msg_id)
                            if ctx.assistant_msg_id
                            else None,
                        },
                    )
                )
            except WebSocketSendTimeout:
                pass
            except asyncio.CancelledError:
                # A second cancel arrived mid-send. Swallow it so the
                # original CancelledError below is the one that
                # propagates.
                pass
            raise
        except WebSocketSendTimeout:
            # Client socket is unresponsive — persist whatever we have
            # and bail without sending further events.
            # #660: offload the sync persist so the event loop is free
            # to drain other connections while we save the partial.
            # #315 review fix: forward the placeholder id so a tool turn
            # interrupted by an unresponsive socket UPDATEs the parent
            # row rather than inserting a sibling.
            await asyncio.to_thread(
                _persist_partial_assistant,
                ctx.conversation_id,
                ctx.full_content,
                getattr(self.llm, "_model", None),
                ctx.rag_context_json,
                is_truncated=True,
                tokens_input=ctx.tokens_in,
                tokens_output=ctx.tokens_out,
                placeholder_message_id=ctx.assistant_msg_id,
                ontology_version=ctx.ontology_version,
            )
            return False
        except Exception:
            logger.exception("LLM streaming failed for conversation %s", ctx.conversation_id)
            # M1: persist partial content with error suffix when streaming fails.
            if ctx.full_content or ctx.assistant_msg_id is not None:
                # #660: offload the sync persist so the
                # cancellation/error path doesn't block the event loop.
                # #315 review fix: forward the placeholder id so a tool
                # turn that errored after the parent INSERT UPDATEs that
                # row rather than inserting a sibling assistant row.
                # #315 review follow-up: keep calling the persist helper
                # even when no content has been streamed yet, as long as
                # a placeholder exists — that way the error_suffix lands
                # on the placeholder row (or the placeholder gets deleted
                # if the helper has nothing to write).
                await asyncio.to_thread(
                    _persist_partial_assistant,
                    ctx.conversation_id,
                    ctx.full_content,
                    getattr(self.llm, "_model", None),
                    ctx.rag_context_json,
                    is_truncated=True,
                    error_suffix=" [Viga: vastus katkestati]",
                    tokens_input=ctx.tokens_in,
                    tokens_output=ctx.tokens_out,
                    placeholder_message_id=ctx.assistant_msg_id,
                    ontology_version=ctx.ontology_version,
                )
            try:
                await _safe_send(
                    ctx.send,
                    {"type": "error", "message": "Vastuse genereerimine ebaõnnestus."},
                )
            except WebSocketSendTimeout:
                pass
            return False
        return True

    async def _phase_emit_terminal_events(self, ctx: _TurnContext) -> None:
        """Steps 7 + 8 — persist assistant message, then emit
        ``done`` / ``sources`` / ``follow_ups``, then schedule the
        fire-and-forget auto-title.
        """
        # 7. Persist assistant message (only when streaming completed
        # successfully).
        #
        # #315: when the turn invoked tools, an assistant row was
        # already inserted up-front so the tool rows could link to it
        # via ``parent_message_id``. In that case we UPDATE the
        # placeholder with the final content / tokens / rag_context
        # rather than INSERTing a second row. Pure-text turns still
        # hit the INSERT path below.
        if ctx.completed:
            if ctx.assistant_msg_id is not None:
                try:
                    with get_connection() as conn:
                        _update_assistant_payload(
                            conn,
                            ctx.assistant_msg_id,
                            content=ctx.full_content,
                            tokens_input=ctx.tokens_in if ctx.tokens_in else None,
                            tokens_output=ctx.tokens_out if ctx.tokens_out else None,
                            rag_context=ctx.rag_context_json,
                        )
                        # Also bump conversation updated_at.
                        conn.execute(
                            "UPDATE conversations SET updated_at = now() WHERE id = %s",
                            (str(ctx.conversation_id),),
                        )
                        conn.commit()
                except Exception:
                    logger.exception(
                        "Failed to update placeholder assistant message %s",
                        ctx.assistant_msg_id,
                    )
            else:
                try:
                    with get_connection() as conn:
                        assistant_msg = create_message(
                            conn,
                            ctx.conversation_id,
                            "assistant",
                            ctx.full_content,
                            tokens_input=ctx.tokens_in if ctx.tokens_in else None,
                            tokens_output=ctx.tokens_out if ctx.tokens_out else None,
                            model=getattr(self.llm, "_model", None),
                            rag_context=ctx.rag_context_json,
                            # #352: stamp the ontology snapshot tag so
                            # the conversation view can later detect
                            # drift relative to the live sync_log
                            # snapshot. NULL when the lookup failed.
                            ontology_version=ctx.ontology_version,
                        )
                        # Also bump conversation updated_at.
                        conn.execute(
                            "UPDATE conversations SET updated_at = now() WHERE id = %s",
                            (str(ctx.conversation_id),),
                        )
                        conn.commit()
                        ctx.assistant_msg_id = assistant_msg.id
                except Exception:
                    logger.exception("Failed to persist assistant message")

        # 8. Emit done, then sources, then (optionally) follow_ups.
        try:
            await _safe_send(
                ctx.send,
                {
                    "type": "done",
                    "message_id": str(ctx.assistant_msg_id) if ctx.assistant_msg_id else None,
                },
            )
        except WebSocketSendTimeout:
            return

        # sources — always emitted when we attempted retrieval OR when
        # we have chunks, so the client can show "Allikaid ei leitud"
        # state.
        try:
            await _safe_send(
                ctx.send,
                {
                    "type": "sources",
                    "message_id": str(ctx.assistant_msg_id) if ctx.assistant_msg_id else None,
                    "sources": _build_sources_payload(ctx.rag_chunks),
                },
            )
        except WebSocketSendTimeout:
            return

        # follow_ups — feature-flag-gated Haiku suggestion call.
        if ctx.completed and ctx.full_content and _is_follow_ups_enabled():
            try:
                suggestions = await _generate_follow_ups(
                    ctx.user_message,
                    ctx.full_content,
                    user_id=ctx.user_id,
                    org_id=ctx.org_id,
                )
            except Exception:
                logger.debug("Follow-up helper raised unexpectedly", exc_info=True)
                suggestions = []
            if suggestions:
                try:
                    await _safe_send(
                        ctx.send,
                        {
                            "type": "follow_ups",
                            "message_id": str(ctx.assistant_msg_id)
                            if ctx.assistant_msg_id
                            else None,
                            "suggestions": suggestions,
                        },
                    )
                except WebSocketSendTimeout:
                    return

        # Auto-title: fire-and-forget. Only runs on the first exchange.
        if ctx.completed and ctx.full_content and ctx.assistant_msg_id is not None:
            try:
                title_task = asyncio.create_task(
                    _maybe_generate_title(
                        ctx.conversation_id,
                        ctx.conversation,
                        ctx.history_length_before,
                        ctx.user_message,
                        ctx.full_content,
                        ctx.auth,
                    )
                )
                _track_background_task(title_task)
            except RuntimeError:
                # No running event loop (e.g. sync test driver) — skip.
                logger.debug("Could not schedule auto-title task", exc_info=True)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _find_regenerate_pivot_index(
    history: list[Any], pivot_message_id: uuid.UUID | None
) -> int | None:
    """Return the index in *history* of the user turn to regenerate from.

    Resolution mirrors :func:`app.chat.handlers._resolve_regenerate_pivot`
    so the orchestrator is robust even if it loads a slightly different
    view of the conversation than the HTTP endpoint did:

    * *pivot_message_id* points at a ``user`` message → that index.
    * *pivot_message_id* points at an ``assistant`` / ``tool`` reply →
      walk back to the nearest preceding ``user`` message.
    * *pivot_message_id* is ``None``, unknown, or has no preceding user
      turn → fall back to the conversation's **last** ``user`` message.

    Returns ``None`` when the conversation contains no ``user`` message at
    all (nothing to regenerate).
    """

    def _last_user_index() -> int | None:
        for i in range(len(history) - 1, -1, -1):
            if getattr(history[i], "role", None) == "user":
                return i
        return None

    if pivot_message_id is not None:
        idx = next(
            (i for i, m in enumerate(history) if getattr(m, "id", None) == pivot_message_id),
            None,
        )
        if idx is not None:
            if getattr(history[idx], "role", None) == "user":
                return idx
            for j in range(idx - 1, -1, -1):
                if getattr(history[j], "role", None) == "user":
                    return j
    return _last_user_index()


def _tool_result_count(result: Any) -> int:
    """Return the number of rows/items a tool result carries.

    - ``len(result["results"])`` when the tool returned a mapping with a
      ``results`` list (the convention used by our SPARQL tools).
    - ``1`` for any other non-error mapping.
    - ``0`` for explicit error payloads or unrecognised shapes.
    """
    if not isinstance(result, dict):
        return 0
    if "error" in result and result.get("error"):
        return 0
    inner = result.get("results")
    if isinstance(inner, list):
        return len(inner)
    return 1


def _build_llm_messages(
    history: list[Any],
    user_message: str,
) -> list[str]:
    """Convert DB message history into a flat list of prompt strings.

    The LLM's ``astream`` currently takes a single prompt string, so we
    concatenate history into a multi-turn prompt format that the model
    can follow.

    History is capped to the most recent ``_MAX_HISTORY_MESSAGES``
    entries to prevent context window overflow for long conversations.

    #315: ``tool`` rows in history are rendered with their
    ``tool_use_id`` + ``tool_name`` + ``tool_input`` so the LLM can
    reconstruct what it asked for and what it got back. The textual
    shape mirrors the live tool-call segments emitted inside
    :meth:`ChatOrchestrator._phase_stream_llm` so the model sees a
    consistent transcript whether the tool ran in this turn or a
    previous one. Without this, history-replayed conversations dropped
    every persisted tool turn from the prompt and Claude lost the
    grounding for the answer it gave originally.
    """
    # M2: cap history to most recent N messages
    if len(history) > _MAX_HISTORY_MESSAGES:
        capped_history = history[-_MAX_HISTORY_MESSAGES:]
    else:
        capped_history = history

    parts: list[str] = []
    for msg in capped_history:
        role_label = msg.role.upper()
        if msg.role == "tool":
            # Render a persisted tool turn so it can replay through the
            # same prompt shape the live tool loop uses. We include the
            # ``tool_use_id`` (Anthropic's ``toolu_...``) when present so
            # any future shift to a structured messages array carries
            # the pairing intact; today the flat-string prompt only
            # needs a stable textual representation.
            tool_use_id = getattr(msg, "tool_use_id", None) or ""
            tool_name = getattr(msg, "tool_name", None) or "tool"
            tool_input = getattr(msg, "tool_input", None) or {}
            try:
                tool_input_json = json.dumps(tool_input)
            except (TypeError, ValueError):
                tool_input_json = "{}"
            tool_output = msg.content or ""
            id_marker = f" id={tool_use_id}" if tool_use_id else ""
            parts.append(
                f"[TOOL_CALL{id_marker} name={tool_name}]({tool_input_json})\n"
                f"[TOOL_RESULT{id_marker}]({tool_output})"
            )
        else:
            parts.append(f"[{role_label}]: {msg.content}")
    parts.append(f"[USER]: {user_message}")
    return parts


def _messages_to_prompt(messages: list[str]) -> str:
    """Join message parts into a single prompt string."""
    return "\n\n".join(messages)
