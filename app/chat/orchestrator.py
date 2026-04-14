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
import uuid
from collections.abc import Awaitable, Callable
from contextlib import aclosing
from typing import Any

from app.auth.policy import can_access_conversation
from app.chat.models import (
    create_message,
    get_conversation,
    list_messages,
    update_conversation_title,
)
from app.chat.rate_limiter import (
    CostBudgetExceededError,
    RateLimitExceededError,
    check_message_rate,
    check_org_cost_budget,
)
from app.chat.system_prompt import build_system_prompt
from app.chat.tools import execute_tool
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
    """Load the latest impact report summary for *draft_id* from the DB.

    The query joins through the ``drafts`` table and filters by
    *org_id* to prevent cross-organisation data access.

    Returns ``None`` if no report exists, the draft belongs to a
    different organisation, or the query fails.
    """
    try:
        with get_connection() as conn:
            row = conn.execute(
                """
                SELECT ir.report_data
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

    report_data = row[0]
    if isinstance(report_data, str):
        try:
            report_data = json.loads(report_data)
        except json.JSONDecodeError:
            return str(report_data)[:500]

    if isinstance(report_data, dict):
        summary = report_data.get("summary", "")
        if summary:
            return str(summary)[:2000]
    return None


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
) -> uuid.UUID | None:
    """Persist a partial / truncated assistant message.

    Attempts to set ``is_truncated`` on the new row; when the column is
    missing (migration 017 not yet applied) we fall back to a plain
    insert + WARNING. Returns the new message id on success or ``None``
    if persistence itself fails.
    """
    if not full_content and not error_suffix:
        return None

    text = full_content
    if error_suffix:
        text = f"{text}{error_suffix}"

    try:
        with get_connection() as conn:
            msg = create_message(
                conn,
                conversation_id,
                "assistant",
                text,
                model=model,
                rag_context=rag_context_json,
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
            The text the user typed.
        auth:
            Auth dict from request scope (must have ``id``, ``org_id``).
        send:
            Async callback for pushing events to the client. Wrapped in
            :func:`_safe_send` with a 5-second timeout.
        """
        user_id = auth.get("id")
        org_id = auth.get("org_id")

        # 0. Per-user message rate limit (cheap pre-check, fail-open on DB
        # error). The per-org cost budget is checked later in step 3 inside
        # the same transaction that persists the user message so the
        # advisory lock covers the read-then-insert window (see below).
        try:
            if user_id:
                check_message_rate(user_id)
        except RateLimitExceededError as exc:
            try:
                await _safe_send(send, {"type": "error", "message": str(exc)})
            except WebSocketSendTimeout:
                pass
            return

        # 1. Load conversation
        try:
            with get_connection() as conn:
                conversation = get_conversation(conn, conversation_id)
        except Exception:
            logger.exception("Failed to load conversation %s", conversation_id)
            try:
                await _safe_send(
                    send, {"type": "error", "message": "Vestluse laadimine ebaonnestus."}
                )
            except WebSocketSendTimeout:
                pass
            return

        if conversation is None:
            try:
                await _safe_send(send, {"type": "error", "message": "Vestlust ei leitud."})
            except WebSocketSendTimeout:
                pass
            return

        # Access control: owner-only per NFR §5 matrix (fix #569).
        # The previous org-level check allowed any same-org colleague
        # to send turns into another user's private conversation.
        if not can_access_conversation(auth, conversation):
            try:
                await _safe_send(
                    send, {"type": "error", "message": "Puudub oigus sellele vestlusele."}
                )
            except WebSocketSendTimeout:
                pass
            return

        # Load message history
        try:
            with get_connection() as conn:
                history = list_messages(conn, conversation_id)
        except Exception:
            logger.exception("Failed to load messages for conversation %s", conversation_id)
            history = []

        history_length_before = len(history)

        # 2. Build system prompt
        impact_summary: str | None = None
        draft_context_id: str | None = None
        if conversation.context_draft_id:
            draft_context_id = str(conversation.context_draft_id)
            impact_summary = _load_impact_summary(draft_context_id, str(org_id))

        system_prompt = build_system_prompt(
            draft_context_id=draft_context_id,
            impact_summary=impact_summary,
        )

        # 2b. RAG retrieval
        # Tenant scoping landed in #576: retriever now filters by caller's
        # org_id (public NULL-scoped rows + caller's own private rows).
        rag_chunks: list[Any] = []
        rag_context_json: list[dict[str, Any]] | None = None
        retrieval_attempted = False

        if Retriever is not None:
            retriever = self._get_retriever()
            if retriever is not None:
                retrieval_attempted = True
                try:
                    await _safe_send(send, {"type": "retrieval_started"})
                    # #576: pass the caller's org_id so private chunks from
                    # other tenants are never retrieved.
                    rag_chunks = await retriever.retrieve(
                        user_message,
                        k=10,
                        org_id=str(org_id) if org_id else None,
                    )
                except WebSocketSendTimeout:
                    return
                except Exception:
                    logger.warning(
                        "RAG retrieval failed for conversation %s; proceeding without",
                        conversation_id,
                        exc_info=True,
                    )
                    rag_chunks = []

            if rag_chunks:
                chunks_text = "\n---\n".join(chunk.content for chunk in rag_chunks)
                system_prompt += "\n\nRelevant legal context:\n" + chunks_text
                rag_context_json = [
                    {
                        "content": chunk.content,
                        "source_uri": chunk.metadata.get("source_uri"),
                        "score": chunk.score,
                    }
                    for chunk in rag_chunks
                ]

        # Always emit retrieval_done when we attempted (or skipped) RAG so
        # the UI can move out of the "Otsin konteksti…" state predictably.
        if retrieval_attempted:
            try:
                await _safe_send(send, {"type": "retrieval_done", "chunk_count": len(rag_chunks)})
            except WebSocketSendTimeout:
                return

        # 3. Budget check + persist user message in a single transaction so
        # the ``pg_advisory_xact_lock`` taken inside ``check_org_cost_budget``
        # serialises concurrent reads-then-inserts for the same org, closing
        # the TOCTOU window that allowed two racing requests to both pass a
        # near-limit budget check.
        try:
            with get_connection() as conn:
                if org_id:
                    check_org_cost_budget(org_id, conn=conn)
                create_message(conn, conversation_id, "user", user_message)
                conn.commit()
        except CostBudgetExceededError as exc:
            try:
                await _safe_send(send, {"type": "error", "message": str(exc)})
            except WebSocketSendTimeout:
                pass
            return
        except Exception:
            logger.exception("Failed to persist user message")
            try:
                await _safe_send(
                    send, {"type": "error", "message": "Sonum salvestamine ebaonnestus."}
                )
            except WebSocketSendTimeout:
                pass
            return

        # 4-6. LLM streaming with tool use
        full_content = ""
        tokens_in = 0
        tokens_out = 0
        tool_rounds = 0
        completed = False
        cancelled = False

        # Build conversation messages for the LLM
        messages = _build_llm_messages(history, user_message)

        try:
            while tool_rounds <= MAX_TOOL_ROUNDS:
                event: StreamEvent
                pending_tool: dict[str, Any] | None = None
                pending_tool_call_id: str | None = None

                stream = self.llm.astream(
                    prompt=_messages_to_prompt(messages),
                    system=system_prompt,
                    max_tokens=4096,
                    temperature=0.3,
                    feature="chat",
                    user_id=user_id,
                    org_id=org_id,
                )
                # ``aclosing`` guarantees the upstream HTTP connection is
                # released on every exit path (timeout, cancel, exception).
                # ``astream`` is declared to return an ``AsyncIterator`` but
                # concrete implementations are always async generators, which
                # expose ``aclose`` — hence the local type-ignore.
                async with aclosing(stream) as managed_stream:  # type: ignore[type-var]
                    async for event in managed_stream:
                        if event.type == "content":
                            full_content += event.delta or ""
                            await _safe_send(
                                send,
                                {
                                    "type": "content_delta",
                                    "delta": event.delta or "",
                                },
                            )
                        elif event.type == "tool_use":
                            pending_tool_call_id = uuid.uuid4().hex[:12]
                            pending_tool = {
                                "name": event.tool_name,
                                "input": event.tool_input or {},
                                "id": pending_tool_call_id,
                            }
                            await _safe_send(
                                send,
                                {
                                    "type": "tool_use",
                                    "tool": event.tool_name,
                                    "input": event.tool_input or {},
                                    "tool_call_id": pending_tool_call_id,
                                },
                            )
                        elif event.type == "stop":
                            pass  # handled after loop

                # If no tool use requested, we're done
                if pending_tool is None:
                    completed = True
                    break

                # Execute tool
                tool_rounds += 1
                tool_name = pending_tool["name"]
                tool_input = pending_tool["input"]
                tool_call_id = pending_tool["id"]

                tool_result = await execute_tool(tool_name, tool_input, self.sparql, auth=auth)

                await _safe_send(
                    send,
                    {
                        "type": "tool_result",
                        "tool": tool_name,
                        "result_count": _tool_result_count(tool_result),
                        "result": tool_result,
                        "tool_call_id": tool_call_id,
                    },
                )

                # Persist tool message
                try:
                    with get_connection() as conn:
                        create_message(
                            conn,
                            conversation_id,
                            "tool",
                            json.dumps(tool_result),
                            tool_name=tool_name,
                            tool_input=tool_input,
                            tool_output=tool_result,
                        )
                        conn.commit()
                except Exception:
                    logger.exception("Failed to persist tool message")

                # Append tool interaction and continue
                messages.append(
                    f"[Tool call: {tool_name}({json.dumps(tool_input)})]\n"
                    f"[Tool result: {json.dumps(tool_result)}]"
                )

                if tool_rounds >= MAX_TOOL_ROUNDS:
                    full_content += "\n\n(Tööriistade kasutamise limiit saavutatud.)"
                    await _safe_send(
                        send,
                        {
                            "type": "content_delta",
                            "delta": "\n\n(Tööriistade kasutamise limiit saavutatud.)",
                        },
                    )
                    completed = True
                    break

        except asyncio.CancelledError:
            # Client asked us to stop — persist partial content flagged
            # as truncated, emit a stopped event, then re-raise so the
            # caller's task.cancel() propagates correctly.
            cancelled = True
            assistant_msg_id = _persist_partial_assistant(
                conversation_id,
                full_content,
                getattr(self.llm, "_model", None),
                rag_context_json,
                is_truncated=True,
            )
            try:
                await _safe_send(
                    send,
                    {
                        "type": "stopped",
                        "message_id": str(assistant_msg_id) if assistant_msg_id else None,
                    },
                )
            except WebSocketSendTimeout:
                pass
            raise
        except WebSocketSendTimeout:
            # Client socket is unresponsive — persist whatever we have and
            # bail without sending further events.
            _persist_partial_assistant(
                conversation_id,
                full_content,
                getattr(self.llm, "_model", None),
                rag_context_json,
                is_truncated=True,
            )
            return
        except Exception:
            logger.exception("LLM streaming failed for conversation %s", conversation_id)
            # M1: persist partial content with error suffix when streaming fails
            if full_content:
                _persist_partial_assistant(
                    conversation_id,
                    full_content,
                    getattr(self.llm, "_model", None),
                    rag_context_json,
                    is_truncated=True,
                    error_suffix=" [Viga: vastus katkestati]",
                )
            try:
                await _safe_send(
                    send, {"type": "error", "message": "Vastuse genereerimine ebaonnestus."}
                )
            except WebSocketSendTimeout:
                pass
            return

        if cancelled:
            # Belt-and-suspenders — CancelledError should have re-raised.
            return

        # 7. Persist assistant message (only when streaming completed successfully)
        assistant_msg_id: uuid.UUID | None = None
        if completed:
            try:
                with get_connection() as conn:
                    assistant_msg = create_message(
                        conn,
                        conversation_id,
                        "assistant",
                        full_content,
                        tokens_input=tokens_in if tokens_in else None,
                        tokens_output=tokens_out if tokens_out else None,
                        model=getattr(self.llm, "_model", None),
                        rag_context=rag_context_json,
                    )
                    # Also bump conversation updated_at
                    conn.execute(
                        "UPDATE conversations SET updated_at = now() WHERE id = %s",
                        (str(conversation_id),),
                    )
                    conn.commit()
                    assistant_msg_id = assistant_msg.id
            except Exception:
                logger.exception("Failed to persist assistant message")

        # 8. Emit done, then sources, then (optionally) follow_ups.
        try:
            await _safe_send(
                send,
                {
                    "type": "done",
                    "message_id": str(assistant_msg_id) if assistant_msg_id else None,
                },
            )
        except WebSocketSendTimeout:
            return

        # sources — always emitted when we attempted retrieval OR when we
        # have chunks, so the client can show "Allikaid ei leitud" state.
        try:
            await _safe_send(
                send,
                {
                    "type": "sources",
                    "message_id": str(assistant_msg_id) if assistant_msg_id else None,
                    "sources": _build_sources_payload(rag_chunks),
                },
            )
        except WebSocketSendTimeout:
            return

        # follow_ups — feature-flag-gated Haiku suggestion call.
        if completed and full_content and _is_follow_ups_enabled():
            try:
                suggestions = await _generate_follow_ups(
                    user_message,
                    full_content,
                    user_id=user_id,
                    org_id=org_id,
                )
            except Exception:
                logger.debug("Follow-up helper raised unexpectedly", exc_info=True)
                suggestions = []
            if suggestions:
                try:
                    await _safe_send(
                        send,
                        {
                            "type": "follow_ups",
                            "message_id": str(assistant_msg_id) if assistant_msg_id else None,
                            "suggestions": suggestions,
                        },
                    )
                except WebSocketSendTimeout:
                    return

        # Auto-title: fire-and-forget. Only runs on the first exchange.
        if completed and full_content and assistant_msg_id is not None:
            try:
                title_task = asyncio.create_task(
                    _maybe_generate_title(
                        conversation_id,
                        conversation,
                        history_length_before,
                        user_message,
                        full_content,
                        auth,
                    )
                )
                _track_background_task(title_task)
            except RuntimeError:
                # No running event loop (e.g. sync test driver) — skip.
                logger.debug("Could not schedule auto-title task", exc_info=True)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


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
    """
    # M2: cap history to most recent N messages
    if len(history) > _MAX_HISTORY_MESSAGES:
        capped_history = history[-_MAX_HISTORY_MESSAGES:]
    else:
        capped_history = history

    parts: list[str] = []
    for msg in capped_history:
        role_label = msg.role.upper()
        parts.append(f"[{role_label}]: {msg.content}")
    parts.append(f"[USER]: {user_message}")
    return parts


def _messages_to_prompt(messages: list[str]) -> str:
    """Join message parts into a single prompt string."""
    return "\n\n".join(messages)
