"""Anthropic Claude concrete ``LLMProvider`` implementation.

Phase 2 shipped only the scaffolding; Phase 3A fills in real Anthropic
API calls. When ``ANTHROPIC_API_KEY`` is unset AND
:func:`app.config.is_stub_allowed` permits it (``APP_ENV`` in
development/test/ci/staging), the provider switches to
``_stubbed = True`` and returns canned responses so tests, local dev,
and CI never make network calls. In production or any unrecognized
``APP_ENV`` (#847) a missing key raises ``RuntimeError`` at
construction instead — a prod key loss must never silently serve
canned advice.

The real implementation path (when the SDK is installed and a key is
set) calls the Anthropic Messages API and logs token usage via
:mod:`app.llm.cost_tracker`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
import time
from collections.abc import AsyncIterator
from typing import Any

from app.config import STUB_ALLOWED_ENVS, get_app_env, is_stub_allowed
from app.llm.provider import LLMProvider, StreamEvent
from app.llm.retry import retry_async, retry_sync
from app.llm.scrubber import scrub_messages, scrub_prompt
from app.metrics import record_metric

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-sonnet-4-6"


class ClaudeProvider(LLMProvider):
    """Anthropic Claude backend with a dev-mode stub path.

    Attributes:
        _stubbed: True when running with no API key in a stub-allowed
            environment; methods return canned responses instead of
            calling Anthropic.
        _api_key: The ``ANTHROPIC_API_KEY`` value (empty when stubbed).
        _model: Model identifier, from ``CLAUDE_MODEL`` env var.

    Raises:
        RuntimeError: When ``ANTHROPIC_API_KEY`` is missing and
            :func:`app.config.is_stub_allowed` is False (production or
            an unrecognized ``APP_ENV``) — the fail-closed gate of #847.
    """

    def __init__(self) -> None:
        api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()

        if not api_key:
            if not is_stub_allowed():
                raise RuntimeError(
                    "ANTHROPIC_API_KEY is not set and stub mode is disabled "
                    f"for APP_ENV={get_app_env()!r} (stubs are only allowed in "
                    f"{sorted(STUB_ALLOWED_ENVS)}). Refusing to serve canned "
                    "LLM responses — set ANTHROPIC_API_KEY in the Coolify "
                    "environment, or fix APP_ENV if this is not a production "
                    "deployment (#847)."
                )
            logger.warning(
                "ANTHROPIC_API_KEY not set — ClaudeProvider running in STUB mode. "
                "All completions return canned responses. Set ANTHROPIC_API_KEY "
                "in Coolify to enable real LLM extraction."
            )
            self._stubbed = True
            self._api_key = ""
        else:
            self._stubbed = False
            self._api_key = api_key

        self._model = os.environ.get("CLAUDE_MODEL", DEFAULT_MODEL)
        # Lazy-initialised SDK clients; only built on first real call so
        # stub users never need the ``anthropic`` package installed.
        self._client: Any = None
        self._async_client: Any = None

    # -- helpers ------------------------------------------------------------

    def _get_client(self) -> Any:
        """Return a lazily-constructed Anthropic SDK client.

        Raises:
            RuntimeError: If the ``anthropic`` package isn't installed.
        """
        if self._client is not None:
            return self._client
        try:
            import anthropic  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "ClaudeProvider: the 'anthropic' package is not installed. "
                "Run `uv add anthropic` to add it."
            ) from exc
        # #854: max_retries=0 — app/llm/retry.py is the single retry
        # authority. The SDK default of 2 internal retries would stack
        # under the outer wrapper's 4 attempts (up to 12 HTTP calls and
        # a multi-minute blocking worst case).
        self._client = anthropic.Anthropic(api_key=self._api_key, max_retries=0)
        return self._client

    def _log_cost(
        self,
        feature: str,
        tokens_input: int,
        tokens_output: int,
        *,
        user_id: Any = None,
        org_id: Any = None,
    ) -> None:
        """Log usage via cost_tracker. Import deferred to avoid circular deps."""
        from app.llm.cost_tracker import log_usage

        log_usage(
            user_id=user_id,
            org_id=org_id,
            provider="claude",
            model=self._model,
            feature=feature,
            tokens_input=tokens_input,
            tokens_output=tokens_output,
        )

    # -- LLMProvider interface ---------------------------------------------

    def complete(
        self,
        prompt: str,
        *,
        max_tokens: int = 1024,
        temperature: float = 0.0,
        system: str | None = None,
        feature: str = "complete",
        user_id: Any = None,
        org_id: Any = None,
        allow_raw: bool = False,
    ) -> str:
        """Return a completion for *prompt*.

        Stub mode returns a deterministic marker string so tests can
        assert on it without network I/O.

        PII / secret-like tokens in *prompt* and *system* are scrubbed
        via :func:`app.llm.scrubber.scrub_prompt` unless ``allow_raw``
        is ``True`` (reserved for draft-analysis callers).
        """
        if self._stubbed:
            return f"[STUB Claude] {prompt[:40]}..."

        client = self._get_client()

        scrubbed_prompt = scrub_prompt(prompt, allow_raw=allow_raw)
        scrubbed_system = scrub_prompt(system, allow_raw=allow_raw) if system else system

        create_kwargs: dict[str, Any] = {
            "model": self._model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": scrub_messages(
                [{"role": "user", "content": scrubbed_prompt}],
                allow_raw=allow_raw,
            ),
        }
        if scrubbed_system:
            create_kwargs["system"] = scrubbed_system

        # #196: instrument per-call latency. We can't use ``track_duration``
        # here because labels captured at context entry can't reflect the
        # ``status`` outcome — fall back to manual timing + record_metric.
        # #354: retry transient errors (429/5xx/network) with bounded backoff.
        status = "ok"
        _start = time.perf_counter()
        try:
            response = retry_sync(
                lambda: client.messages.create(**create_kwargs),
                context="complete",
            )
        except Exception:
            status = "error"
            raise
        finally:
            _duration_ms = (time.perf_counter() - _start) * 1000
            record_metric(
                "llm_call_ms",
                round(_duration_ms, 2),
                {
                    "feature": feature,
                    "model": self._model,
                    "operation": "complete",
                    "status": status,
                },
            )

        content = response.content[0].text if response.content else ""

        # Cost tracking
        self._log_cost(
            feature=feature,
            tokens_input=response.usage.input_tokens,
            tokens_output=response.usage.output_tokens,
            user_id=user_id,
            org_id=org_id,
        )

        return content

    def extract_json(
        self,
        prompt: str,
        *,
        schema: dict | None = None,
        feature: str = "extract_json",
        user_id: Any = None,
        org_id: Any = None,
        allow_raw: bool = False,
    ) -> dict:
        """Run *prompt* through the model and parse the reply as JSON.

        Stub mode returns a deterministic dict; real mode wraps the
        prompt in a "respond with valid JSON" instruction and json-loads
        the reply. See :meth:`complete` for the ``allow_raw`` contract.
        """
        if self._stubbed:
            return {"stub": True, "prompt": prompt[:40]}

        instruction = "Respond with a single valid JSON object and no surrounding prose."
        if schema is not None:
            instruction += f" The object must conform to this schema: {json.dumps(schema)}"

        raw = self.complete(
            prompt,
            max_tokens=2048,
            temperature=0.0,
            system=instruction,
            feature=feature,
            user_id=user_id,
            org_id=org_id,
            allow_raw=allow_raw,
        )
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            # Try extracting JSON from markdown code blocks
            match = re.search(r"```(?:json)?\s*\n?(.*?)\n?\s*```", raw, re.DOTALL)
            if match:
                try:
                    return json.loads(match.group(1))
                except json.JSONDecodeError:
                    pass

            logger.warning(
                "ClaudeProvider.extract_json: model did not return valid JSON "
                "(response length=%d)",
                len(raw),
            )
            return {"error": "failed to parse"}

    def count_tokens(self, text: str) -> int:
        """Return an approximate token count for *text*.

        Stub mode falls back to ``len(text) // 4``, which is a common
        rule of thumb for English/Estonian latin-script text. Real mode
        uses the Anthropic SDK's ``count_tokens`` helper if available.
        """
        if self._stubbed:
            return len(text) // 4

        client = self._get_client()
        count_fn = getattr(client, "count_tokens", None)
        if callable(count_fn):  # pragma: no cover
            # #196: instrument per-call latency.
            status = "ok"
            _start = time.perf_counter()
            try:
                result: Any = count_fn(text)
            except Exception:
                status = "error"
                raise
            finally:
                _duration_ms = (time.perf_counter() - _start) * 1000
                record_metric(
                    "llm_call_ms",
                    round(_duration_ms, 2),
                    {
                        "feature": "count_tokens",
                        "model": self._model,
                        "operation": "count_tokens",
                        "status": status,
                    },
                )
            return int(result)
        return len(text) // 4

    # -- async helpers ---------------------------------------------------------

    def _get_async_client(self) -> Any:
        """Return a lazily-constructed Anthropic AsyncAnthropic client.

        Raises:
            RuntimeError: If the ``anthropic`` package isn't installed.
        """
        if self._async_client is not None:
            return self._async_client
        try:
            import anthropic  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "ClaudeProvider: the 'anthropic' package is not installed. "
                "Run `uv add anthropic` to add it."
            ) from exc
        # #854: max_retries=0 — see _get_client; the outer retry wrapper
        # in app/llm/retry.py is the single retry authority.
        self._async_client = anthropic.AsyncAnthropic(api_key=self._api_key, max_retries=0)
        return self._async_client

    # -- async LLMProvider interface -------------------------------------------

    async def acomplete(
        self,
        prompt: str,
        *,
        max_tokens: int = 1024,
        temperature: float = 0.0,
        system: str | None = None,
        feature: str = "acomplete",
        user_id: Any = None,
        org_id: Any = None,
        allow_raw: bool = False,
    ) -> str:
        """Async variant of :meth:`complete`.

        Stub mode returns a deterministic marker string so tests can
        assert on it without network I/O. See :meth:`complete` for the
        ``allow_raw`` contract.
        """
        if self._stubbed:
            return f"[STUB Claude async] {prompt[:40]}..."

        client = self._get_async_client()

        scrubbed_prompt = scrub_prompt(prompt, allow_raw=allow_raw)
        scrubbed_system = scrub_prompt(system, allow_raw=allow_raw) if system else system

        create_kwargs: dict[str, Any] = {
            "model": self._model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": scrub_messages(
                [{"role": "user", "content": scrubbed_prompt}],
                allow_raw=allow_raw,
            ),
        }
        if scrubbed_system:
            create_kwargs["system"] = scrubbed_system

        # #196: instrument per-call latency (see ``complete`` for rationale).
        # #354: retry transient errors (429/5xx/network) with bounded backoff.
        status = "ok"
        _start = time.perf_counter()

        async def _call() -> Any:
            return await client.messages.create(**create_kwargs)

        try:
            response = await retry_async(_call, context="acomplete")
        except Exception:
            status = "error"
            raise
        finally:
            _duration_ms = (time.perf_counter() - _start) * 1000
            record_metric(
                "llm_call_ms",
                round(_duration_ms, 2),
                {
                    "feature": feature,
                    "model": self._model,
                    "operation": "acomplete",
                    "status": status,
                },
            )

        content = response.content[0].text if response.content else ""

        self._log_cost(
            feature=feature,
            tokens_input=response.usage.input_tokens,
            tokens_output=response.usage.output_tokens,
            user_id=user_id,
            org_id=org_id,
        )

        return content

    async def astream(
        self,
        prompt: str,
        *,
        max_tokens: int = 1024,
        temperature: float = 0.0,
        system: str | None = None,
        feature: str = "astream",
        user_id: Any = None,
        org_id: Any = None,
        allow_raw: bool = False,
        tools: list[dict] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Async streaming completion yielding :class:`StreamEvent` objects.

        Stub mode yields a few canned events then ``StreamEvent(type="stop")``.
        Real mode wraps ``AsyncAnthropic.messages.stream()``. See
        :meth:`complete` for the ``allow_raw`` contract.

        When *tools* is a non-empty list, it is forwarded to the Anthropic
        Messages API as the ``tools=`` parameter (with ``tool_choice`` set
        to ``auto``). Anthropic's streaming tool-use block shape —
        ``content_block_start`` (``type="tool_use"``) → one or more
        ``content_block_delta`` events carrying ``input_json_delta``
        fragments → ``content_block_stop`` — is translated into a single
        :class:`StreamEvent` with ``type="tool_use"``, ``tool_name``,
        ``tool_input`` and ``tool_use_id``.
        """
        if self._stubbed:
            yield StreamEvent(type="content", delta="[STUB] ")
            yield StreamEvent(type="content", delta="Tere! ")
            yield StreamEvent(type="content", delta="See on stub-vastus.")
            yield StreamEvent(type="stop")
            return

        client = self._get_async_client()

        scrubbed_prompt = scrub_prompt(prompt, allow_raw=allow_raw)
        scrubbed_system = scrub_prompt(system, allow_raw=allow_raw) if system else system

        create_kwargs: dict[str, Any] = {
            "model": self._model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": scrub_messages(
                [{"role": "user", "content": scrubbed_prompt}],
                allow_raw=allow_raw,
            ),
        }
        if scrubbed_system:
            create_kwargs["system"] = scrubbed_system
        if tools:
            # Gate on truthiness so an explicit empty list is treated as
            # "no tools" — matches the abstract contract.
            create_kwargs["tools"] = tools
            create_kwargs["tool_choice"] = {"type": "auto"}

        tokens_input = 0
        tokens_output = 0

        # Per-block accumulator state for streaming tool_use blocks. Anthropic
        # emits ``content_block_start`` (type="tool_use") followed by one or
        # more ``content_block_delta`` (type="input_json_delta") carrying a
        # ``partial_json`` string, terminated by ``content_block_stop``. We
        # key by the event ``index`` so interleaved blocks don't collide.
        tool_blocks: dict[int, dict[str, Any]] = {}

        # #196: instrument per-call latency. We measure wall-time spent in
        # the streaming context (open → final event consumed). The metric is
        # recorded once per call, regardless of consumer cancel timing.
        # #354: retry only the *open* call. Mid-stream failures aren't
        # retried because we'd duplicate already-emitted deltas.
        stream_status = "ok"
        _stream_start = time.perf_counter()

        async def _open_stream() -> Any:
            # ``messages.stream(...)`` returns a context manager; we enter
            # it manually so the retry helper can re-run the open while we
            # keep the resulting stream object for iteration below.
            ctx = client.messages.stream(**create_kwargs)
            stream_obj = await ctx.__aenter__()
            # Stash the ctx on the stream so we can call __aexit__ later.
            stream_obj.__stream_ctx__ = ctx  # type: ignore[attr-defined]
            return stream_obj

        # #854: cost logging happens in the OUTER ``finally`` at the bottom
        # of this method. Previously ``_log_cost`` ran in a ``finally``
        # attached to the trailing ``yield stop``, which a mid-stream
        # upstream error or a client-disconnect cancel (GeneratorExit via
        # ``aclose()``) skipped entirely — already-billed output tokens
        # never reached ``llm_usage``. Once the stream has *opened*,
        # Anthropic bills the input tokens (and any streamed output), so
        # we record whatever usage the events delivered. The counts are
        # event-derived: on an early abort ``message_delta`` may not have
        # arrived yet, so partial-but-honest numbers (often output=0) are
        # logged rather than nothing.
        stream_opened = False
        try:
            try:
                stream = await retry_async(_open_stream, context="astream-open")
            except Exception:
                # Open never succeeded → nothing was billed; skip the
                # usage row entirely (stream_opened stays False).
                stream_status = "error"
                raise
            stream_opened = True
            ctx = stream.__stream_ctx__  # type: ignore[attr-defined]
            try:
                async for event in stream:
                    event_type = getattr(event, "type", None)
                    if event_type is None:
                        continue

                    if event_type == "content_block_start":
                        content_block = getattr(event, "content_block", None)
                        if content_block is None:
                            continue
                        if getattr(content_block, "type", None) == "tool_use":
                            idx = getattr(event, "index", len(tool_blocks))
                            tool_blocks[idx] = {
                                "id": getattr(content_block, "id", ""),
                                "name": getattr(content_block, "name", ""),
                                "json_buf": "",
                            }
                    elif event_type == "content_block_delta":
                        delta_obj = getattr(event, "delta", None)
                        if delta_obj is None:
                            continue
                        delta_type = getattr(delta_obj, "type", None)
                        if delta_type == "input_json_delta":
                            idx = getattr(event, "index", None)
                            block = tool_blocks.get(idx) if idx is not None else None
                            if block is not None:
                                block["json_buf"] += getattr(delta_obj, "partial_json", "") or ""
                        elif hasattr(delta_obj, "text"):
                            # Plain text delta (text_delta for SSE, or legacy
                            # shape where .text is set directly).
                            text = delta_obj.text
                            if text:
                                yield StreamEvent(type="content", delta=text)
                    elif event_type == "content_block_stop":
                        idx = getattr(event, "index", None)
                        block = tool_blocks.pop(idx, None) if idx is not None else None
                        if block is None:
                            continue
                        buf = block["json_buf"]
                        try:
                            parsed_input: dict = json.loads(buf) if buf else {}
                        except json.JSONDecodeError:
                            logger.warning(
                                "ClaudeProvider.astream: failed to parse tool_use "
                                "input JSON for tool=%s (buf length=%d); emitting empty dict",
                                block["name"],
                                len(buf),
                            )
                            parsed_input = {}
                        if not isinstance(parsed_input, dict):
                            parsed_input = {}
                        yield StreamEvent(
                            type="tool_use",
                            tool_name=block["name"],
                            tool_input=parsed_input,
                            tool_use_id=block["id"] or None,
                        )
                    elif event_type == "message_delta":
                        usage = getattr(event, "usage", None)
                        if usage:
                            tokens_output = getattr(usage, "output_tokens", 0)
                    elif event_type == "message_start":
                        msg = getattr(event, "message", None)
                        if msg:
                            usage = getattr(msg, "usage", None)
                            if usage:
                                tokens_input = getattr(usage, "input_tokens", 0)
            except (GeneratorExit, asyncio.CancelledError):
                # Consumer cancelled mid-stream — client disconnect
                # (``aclose()`` → GeneratorExit) or an enclosing
                # ``wait_for`` timeout (CancelledError). The tokens
                # streamed so far are still billed; the outer finally
                # records them (#854).
                stream_status = "cancelled"
                raise
            except Exception:
                stream_status = "error"
                raise
            finally:
                # Close the context we manually entered above so the
                # underlying httpx stream is released on every path —
                # clean stop, upstream error, and GeneratorExit from a
                # consumer ``aclose()``.
                await ctx.__aexit__(None, None, None)

            # #662 / post-review fix: yield the stop event so the
            # orchestrator can read the per-turn tokens off it and persist
            # them on the assistant message row. The llm_usage row is
            # recorded in the OUTER finally below, so a consumer cancel
            # immediately after receiving the stop frame (or at any
            # earlier point) still produces exactly one usage row.
            yield StreamEvent(
                type="stop",
                tokens_input=tokens_input,
                tokens_output=tokens_output,
            )
        finally:
            _stream_duration_ms = (time.perf_counter() - _stream_start) * 1000
            record_metric(
                "llm_call_ms",
                round(_stream_duration_ms, 2),
                {
                    "feature": feature,
                    "model": self._model,
                    "operation": "astream",
                    "status": stream_status,
                },
            )
            if stream_opened:
                # #854: meter the stream no matter how it ended — clean
                # stop, mid-stream upstream error, or consumer cancel.
                # ``log_usage`` swallows its own failures, so this never
                # masks the in-flight exception.
                self._log_cost(
                    feature=feature,
                    tokens_input=tokens_input,
                    tokens_output=tokens_output,
                    user_id=user_id,
                    org_id=org_id,
                )


_default_provider: ClaudeProvider | None = None
_default_provider_lock = threading.Lock()


def _reset_default_provider() -> None:
    """Reset the singleton (for tests only)."""
    global _default_provider
    with _default_provider_lock:
        _default_provider = None


def get_default_provider() -> LLMProvider:
    """Return the project default ``LLMProvider`` singleton.

    Uses a module-level lock + singleton pattern (same approach as the
    Phase 2 #453 fix) to avoid creating a new ``ClaudeProvider`` on
    every call.

    Today that's Claude. Phase 3+ may read a ``LLM_PROVIDER`` env var
    here to choose between Claude and alternatives without changing
    every call site.
    """
    global _default_provider
    if _default_provider is None:
        with _default_provider_lock:
            if _default_provider is None:
                _default_provider = ClaudeProvider()
    return _default_provider
