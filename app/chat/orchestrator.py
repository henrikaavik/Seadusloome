"""Chat orchestrator: coordinates LLM streaming, tool use, and persistence.

The :class:`ChatOrchestrator` is the core engine behind the advisory chat.
It loads conversation history, builds the system prompt, calls the LLM
with tool use enabled, executes tools when requested, and persists all
messages. Streaming content is pushed back to the caller via an async
``send`` callback (typically wired to a WebSocket).

Tool-use rounds are capped at :data:`MAX_TOOL_ROUNDS` to prevent
infinite loops.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from app.chat.models import (
    create_message,
    get_conversation,
    list_messages,
)
from app.chat.system_prompt import build_system_prompt
from app.chat.tools import execute_tool
from app.db import get_connection
from app.llm.provider import LLMProvider, StreamEvent
from app.ontology.sparql_client import SparqlClient

logger = logging.getLogger(__name__)

MAX_TOOL_ROUNDS = 5
_MAX_HISTORY_MESSAGES = 50

# ---------------------------------------------------------------------------
# Draft context loader
# ---------------------------------------------------------------------------


def _load_impact_summary(draft_id: str) -> str | None:
    """Load the latest impact report summary for *draft_id* from the DB.

    Returns ``None`` if no report exists or the query fails.
    """
    try:
        with get_connection() as conn:
            row = conn.execute(
                """
                SELECT report_data
                FROM impact_reports
                WHERE draft_id = %s
                ORDER BY generated_at DESC
                LIMIT 1
                """,
                (draft_id,),
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
    """

    def __init__(
        self,
        llm: LLMProvider,
        sparql: SparqlClient | None = None,
    ) -> None:
        self.llm = llm
        self.sparql = sparql or SparqlClient()

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
          8. Send ``done`` event

        Parameters
        ----------
        conversation_id:
            UUID of the conversation this message belongs to.
        user_message:
            The text the user typed.
        auth:
            Auth dict from request scope (must have ``id``, ``org_id``).
        send:
            Async callback for pushing events to the client.
        """
        user_id = auth.get("id")
        org_id = auth.get("org_id")

        # 1. Load conversation
        try:
            with get_connection() as conn:
                conversation = get_conversation(conn, conversation_id)
        except Exception:
            logger.exception("Failed to load conversation %s", conversation_id)
            await send({"type": "error", "message": "Vestluse laadimine ebaonnestus."})
            return

        if conversation is None:
            await send({"type": "error", "message": "Vestlust ei leitud."})
            return

        # Access control: verify org
        if str(conversation.org_id) != str(org_id):
            await send({"type": "error", "message": "Puudub oigus sellele vestlusele."})
            return

        # Load message history
        try:
            with get_connection() as conn:
                history = list_messages(conn, conversation_id)
        except Exception:
            logger.exception("Failed to load messages for conversation %s", conversation_id)
            history = []

        # 2. Build system prompt
        impact_summary: str | None = None
        draft_context_id: str | None = None
        if conversation.context_draft_id:
            draft_context_id = str(conversation.context_draft_id)
            impact_summary = _load_impact_summary(draft_context_id)

        system_prompt = build_system_prompt(
            draft_context_id=draft_context_id,
            impact_summary=impact_summary,
        )

        # 3. Persist user message
        try:
            with get_connection() as conn:
                create_message(conn, conversation_id, "user", user_message)
                conn.commit()
        except Exception:
            logger.exception("Failed to persist user message")
            await send({"type": "error", "message": "Sonum salvestamine ebaonnestus."})
            return

        # 4-6. LLM streaming with tool use
        full_content = ""
        tokens_in = 0
        tokens_out = 0
        tool_rounds = 0
        completed = False

        # Build conversation messages for the LLM
        messages = _build_llm_messages(history, user_message)

        try:
            while tool_rounds <= MAX_TOOL_ROUNDS:
                event: StreamEvent
                pending_tool: dict[str, Any] | None = None

                async for event in self.llm.astream(
                    prompt=_messages_to_prompt(messages),
                    system=system_prompt,
                    max_tokens=4096,
                    temperature=0.3,
                    feature="chat",
                    user_id=user_id,
                    org_id=org_id,
                ):
                    if event.type == "content":
                        full_content += event.delta or ""
                        await send(
                            {
                                "type": "content_delta",
                                "delta": event.delta or "",
                            }
                        )
                    elif event.type == "tool_use":
                        pending_tool = {
                            "name": event.tool_name,
                            "input": event.tool_input or {},
                        }
                        await send(
                            {
                                "type": "tool_use",
                                "tool": event.tool_name,
                                "input": event.tool_input or {},
                            }
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

                tool_result = await execute_tool(tool_name, tool_input, self.sparql, auth=auth)

                await send(
                    {
                        "type": "tool_result",
                        "tool": tool_name,
                        "output": tool_result,
                    }
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
                    await send(
                        {
                            "type": "content_delta",
                            "delta": "\n\n(Tööriistade kasutamise limiit saavutatud.)",
                        }
                    )
                    completed = True
                    break

        except Exception:
            logger.exception("LLM streaming failed for conversation %s", conversation_id)
            # M1: persist partial content with error suffix when streaming fails
            if full_content:
                full_content += " [Viga: vastus katkestati]"
                try:
                    with get_connection() as conn:
                        create_message(
                            conn,
                            conversation_id,
                            "assistant",
                            full_content,
                            model=getattr(self.llm, "_model", None),
                        )
                        conn.commit()
                except Exception:
                    logger.exception("Failed to persist partial assistant message")
            await send({"type": "error", "message": "Vastuse genereerimine ebaonnestus."})
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

        # 8. Send done event
        await send(
            {
                "type": "done",
                "message_id": str(assistant_msg_id) if assistant_msg_id else None,
            }
        )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


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
