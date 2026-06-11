"""Tests for ``app.llm.retry`` (#354).

Covers:
* Retry on retryable statuses (429, 500, 502, 503, 504).
* Fail-fast on permanent statuses (400, 401, 403).
* Retry on network/timeout errors (no status code).
* Exhaustion of the retry budget surfaces the last exception.
* Cost-tracker `_log_cost` is called exactly once on eventual success
  (no double-counting on retried-then-succeeded calls).
* Mid-stream errors are NOT retried (different code path; documented).

All sleeps are monkeypatched so the suite runs in milliseconds.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Direct tests for the retry helper module
# ---------------------------------------------------------------------------


class TestRetryClassification:
    """``_is_retryable`` should encode the DoD policy precisely."""

    def test_429_is_retryable(self):
        from app.llm.retry import _is_retryable

        exc = SimpleNamespace(status_code=429)
        assert _is_retryable(exc) is True  # type: ignore[arg-type]

    @pytest.mark.parametrize("status", [500, 502, 503, 504])
    def test_5xx_is_retryable(self, status: int):
        from app.llm.retry import _is_retryable

        exc = SimpleNamespace(status_code=status)
        assert _is_retryable(exc) is True  # type: ignore[arg-type]

    @pytest.mark.parametrize("status", [400, 401, 403])
    def test_4xx_permanent_is_not_retryable(self, status: int):
        from app.llm.retry import _is_retryable

        exc = SimpleNamespace(status_code=status)
        assert _is_retryable(exc) is False  # type: ignore[arg-type]

    @pytest.mark.parametrize("status", [404, 410, 422])
    def test_other_4xx_is_not_retryable(self, status: int):
        """Errors like 404/410/422 mean a bad request payload — not retryable."""
        from app.llm.retry import _is_retryable

        exc = SimpleNamespace(status_code=status)
        assert _is_retryable(exc) is False  # type: ignore[arg-type]

    # -- #854: expanded transient set + voyageai ``http_status`` shape ------

    def test_529_overloaded_is_retryable(self):
        """529 is Anthropic's ``overloaded_error`` — its most common
        transient failure; it must be retried."""
        from app.llm.retry import _is_retryable

        exc = SimpleNamespace(status_code=529)
        assert _is_retryable(exc) is True  # type: ignore[arg-type]

    @pytest.mark.parametrize("status", [408, 409])
    def test_408_and_409_are_retryable(self, status: int):
        """408 (request timeout) and 409 (conflict) are transient — both
        SDKs treat them as retryable (#854)."""
        from app.llm.retry import _is_retryable

        exc = SimpleNamespace(status_code=status)
        assert _is_retryable(exc) is True  # type: ignore[arg-type]

    @pytest.mark.parametrize("status", [429, 500, 503, 529])
    def test_voyage_http_status_transient_is_retryable(self, status: int):
        """voyageai exceptions carry ``http_status`` (not ``status_code``).

        Before #854 the extractor never read it, so a single Voyage 429
        aborted a whole ingest with zero retries.
        """
        from app.llm.retry import _is_retryable

        exc = SimpleNamespace(http_status=status)
        assert _is_retryable(exc) is True  # type: ignore[arg-type]

    @pytest.mark.parametrize("status", [400, 401, 403])
    def test_voyage_http_status_permanent_is_not_retryable(self, status: int):
        from app.llm.retry import _is_retryable

        exc = SimpleNamespace(http_status=status)
        assert _is_retryable(exc) is False  # type: ignore[arg-type]

    def test_voyage_http_status_numeric_string_is_handled(self):
        """voyageai stores the status verbatim; tolerate numeric strings."""
        from app.llm.retry import _http_status

        assert _http_status(SimpleNamespace(http_status="429")) == 429  # type: ignore[arg-type]

    def test_status_code_takes_precedence_over_http_status(self):
        """Anthropic-shaped ``status_code`` wins when both attrs exist."""
        from app.llm.retry import _http_status

        exc = SimpleNamespace(status_code=401, http_status=429)
        assert _http_status(exc) == 401  # type: ignore[arg-type]

    def test_connection_error_is_retryable(self):
        from app.llm.retry import _is_retryable

        assert _is_retryable(ConnectionError("dns failure")) is True

    def test_timeout_error_is_retryable(self):
        from app.llm.retry import _is_retryable

        assert _is_retryable(TimeoutError("slow")) is True

    def test_anthropic_connection_error_is_retryable(self):
        """Anthropic's APIConnectionError (no status_code) is retryable."""
        import anthropic

        from app.llm.retry import _is_retryable

        exc = anthropic.APIConnectionError(request=MagicMock())
        assert _is_retryable(exc) is True

    def test_anthropic_api_timeout_is_retryable(self):
        import anthropic

        from app.llm.retry import _is_retryable

        exc = anthropic.APITimeoutError(request=MagicMock())
        assert _is_retryable(exc) is True

    def test_anthropic_rate_limit_is_retryable(self):
        import anthropic

        from app.llm.retry import _is_retryable

        exc = anthropic.RateLimitError(
            message="rl",
            response=MagicMock(status_code=429),
            body=None,
        )
        assert _is_retryable(exc) is True

    def test_anthropic_authentication_error_is_not_retryable(self):
        import anthropic

        from app.llm.retry import _is_retryable

        exc = anthropic.AuthenticationError(
            message="bad key",
            response=MagicMock(status_code=401),
            body=None,
        )
        assert _is_retryable(exc) is False

    def test_anthropic_bad_request_is_not_retryable(self):
        import anthropic

        from app.llm.retry import _is_retryable

        exc = anthropic.BadRequestError(
            message="bad",
            response=MagicMock(status_code=400),
            body=None,
        )
        assert _is_retryable(exc) is False

    def test_anthropic_permission_denied_is_not_retryable(self):
        import anthropic

        from app.llm.retry import _is_retryable

        exc = anthropic.PermissionDeniedError(
            message="no",
            response=MagicMock(status_code=403),
            body=None,
        )
        assert _is_retryable(exc) is False

    def test_anthropic_internal_server_error_is_retryable(self):
        import anthropic

        from app.llm.retry import _is_retryable

        exc = anthropic.InternalServerError(
            message="oops",
            response=MagicMock(status_code=500),
            body=None,
        )
        assert _is_retryable(exc) is True

    def test_unknown_exception_is_not_retryable(self):
        """A plain RuntimeError with no status carries no signal — fail fast."""
        from app.llm.retry import _is_retryable

        assert _is_retryable(RuntimeError("???")) is False


class TestRetryBackoffSchedule:
    def test_backoff_schedule_matches_dod(self):
        """DoD says (1s, 5s, 30s)."""
        from app.llm.retry import _BACKOFF

        assert _BACKOFF == (1.0, 5.0, 30.0)

    def test_max_retries_is_three(self):
        from app.llm.retry import MAX_RETRIES

        assert MAX_RETRIES == 3

    def test_wait_for_attempt_returns_each_slot(self):
        from app.llm.retry import _BACKOFF, _wait_for_attempt

        assert _wait_for_attempt(1) == _BACKOFF[0]
        assert _wait_for_attempt(2) == _BACKOFF[1]
        assert _wait_for_attempt(3) == _BACKOFF[2]

    def test_wait_for_attempt_beyond_table_uses_last_slot(self):
        from app.llm.retry import _BACKOFF, _wait_for_attempt

        assert _wait_for_attempt(99) == _BACKOFF[-1]


class TestRetrySyncBehaviour:
    @patch("app.llm.retry.time.sleep")
    def test_succeeds_first_attempt_no_sleep(self, mock_sleep: MagicMock):
        from app.llm.retry import retry_sync

        fn = MagicMock(return_value="ok")
        assert retry_sync(fn) == "ok"
        fn.assert_called_once()
        mock_sleep.assert_not_called()

    @patch("app.llm.retry.time.sleep")
    def test_429_then_success_logs_one_retry(self, mock_sleep: MagicMock):
        import anthropic

        from app.llm.retry import retry_sync

        rl = anthropic.RateLimitError(
            message="rl",
            response=MagicMock(status_code=429),
            body=None,
        )
        fn = MagicMock(side_effect=[rl, rl, "ok"])
        assert retry_sync(fn) == "ok"
        assert fn.call_count == 3
        # Two retries → two sleeps: 1.0s and 5.0s.
        assert mock_sleep.call_args_list == [((1.0,),), ((5.0,),)]

    @patch("app.llm.retry.time.sleep")
    def test_500_exhausts_retries_and_raises(self, mock_sleep: MagicMock):
        import anthropic

        from app.llm.retry import MAX_RETRIES, retry_sync

        err = anthropic.InternalServerError(
            message="boom",
            response=MagicMock(status_code=500),
            body=None,
        )
        fn = MagicMock(side_effect=err)
        with pytest.raises(anthropic.InternalServerError):
            retry_sync(fn)

        # Initial + MAX_RETRIES retries = MAX_RETRIES + 1 calls.
        assert fn.call_count == MAX_RETRIES + 1
        # Sleeps for retries 1, 2, 3 → 1.0, 5.0, 30.0.
        assert mock_sleep.call_args_list == [((1.0,),), ((5.0,),), ((30.0,),)]

    @patch("app.llm.retry.time.sleep")
    def test_401_fails_fast_with_no_sleep(self, mock_sleep: MagicMock):
        import anthropic

        from app.llm.retry import retry_sync

        err = anthropic.AuthenticationError(
            message="bad key",
            response=MagicMock(status_code=401),
            body=None,
        )
        fn = MagicMock(side_effect=err)
        with pytest.raises(anthropic.AuthenticationError):
            retry_sync(fn)
        fn.assert_called_once()
        mock_sleep.assert_not_called()

    @patch("app.llm.retry.time.sleep")
    def test_400_fails_fast_with_no_sleep(self, mock_sleep: MagicMock):
        import anthropic

        from app.llm.retry import retry_sync

        err = anthropic.BadRequestError(
            message="malformed",
            response=MagicMock(status_code=400),
            body=None,
        )
        fn = MagicMock(side_effect=err)
        with pytest.raises(anthropic.BadRequestError):
            retry_sync(fn)
        fn.assert_called_once()
        mock_sleep.assert_not_called()

    @patch("app.llm.retry.time.sleep")
    def test_502_retries(self, mock_sleep: MagicMock):
        """502 (typed via APIStatusError subclass)."""
        from anthropic import APIStatusError

        from app.llm.retry import retry_sync

        err = APIStatusError(
            message="bad gateway",
            response=MagicMock(status_code=502),
            body=None,
        )
        fn = MagicMock(side_effect=[err, "ok"])
        assert retry_sync(fn) == "ok"
        assert fn.call_count == 2

    @patch("app.llm.retry.time.sleep")
    def test_connection_error_retries(self, mock_sleep: MagicMock):
        from app.llm.retry import retry_sync

        fn = MagicMock(side_effect=[ConnectionError("dns"), "ok"])
        assert retry_sync(fn) == "ok"
        assert fn.call_count == 2
        mock_sleep.assert_called_once_with(1.0)

    @patch("app.llm.retry.time.sleep")
    def test_logs_each_retry(self, mock_sleep: MagicMock, caplog):
        import logging

        import anthropic

        from app.llm.retry import retry_sync

        rl = anthropic.RateLimitError(
            message="rl",
            response=MagicMock(status_code=429),
            body=None,
        )
        fn = MagicMock(side_effect=[rl, rl, "ok"])
        with caplog.at_level(logging.WARNING, logger="app.llm.retry"):
            retry_sync(fn, context="test")

        retry_msgs = [r for r in caplog.records if "retry attempt" in r.getMessage()]
        assert len(retry_msgs) == 2
        # First retry log mentions attempt 1 + 1.0s wait.
        first = retry_msgs[0].getMessage()
        assert "attempt 1" in first
        assert "1.0s" in first


class TestRetryAsyncBehaviour:
    """Mirror of the sync tests for the async path."""

    def test_async_succeeds_first_attempt(self, monkeypatch: pytest.MonkeyPatch):
        from app.llm.retry import retry_async

        monkeypatch.setattr("app.llm.retry.asyncio.sleep", AsyncMock())

        async def _fn() -> str:
            return "ok"

        assert asyncio.run(retry_async(_fn)) == "ok"

    def test_async_429_then_success(self, monkeypatch: pytest.MonkeyPatch):
        import anthropic

        from app.llm.retry import retry_async

        sleep_mock = AsyncMock()
        monkeypatch.setattr("app.llm.retry.asyncio.sleep", sleep_mock)

        rl = anthropic.RateLimitError(
            message="rl",
            response=MagicMock(status_code=429),
            body=None,
        )
        seq = [rl, "ok"]

        async def _fn() -> str:
            v = seq.pop(0)
            if isinstance(v, Exception):
                raise v
            return v

        assert asyncio.run(retry_async(_fn)) == "ok"
        sleep_mock.assert_awaited_once_with(1.0)

    def test_async_500_exhausts(self, monkeypatch: pytest.MonkeyPatch):
        import anthropic

        from app.llm.retry import MAX_RETRIES, retry_async

        sleep_mock = AsyncMock()
        monkeypatch.setattr("app.llm.retry.asyncio.sleep", sleep_mock)

        err = anthropic.InternalServerError(
            message="boom",
            response=MagicMock(status_code=500),
            body=None,
        )
        calls = {"n": 0}

        async def _fn() -> str:
            calls["n"] += 1
            raise err

        with pytest.raises(anthropic.InternalServerError):
            asyncio.run(retry_async(_fn))

        assert calls["n"] == MAX_RETRIES + 1
        assert sleep_mock.await_args_list == [((1.0,),), ((5.0,),), ((30.0,),)]

    def test_async_401_fails_fast(self, monkeypatch: pytest.MonkeyPatch):
        import anthropic

        from app.llm.retry import retry_async

        sleep_mock = AsyncMock()
        monkeypatch.setattr("app.llm.retry.asyncio.sleep", sleep_mock)

        err = anthropic.AuthenticationError(
            message="bad key",
            response=MagicMock(status_code=401),
            body=None,
        )

        async def _fn() -> str:
            raise err

        with pytest.raises(anthropic.AuthenticationError):
            asyncio.run(retry_async(_fn))
        sleep_mock.assert_not_called()


# ---------------------------------------------------------------------------
# Integration: ClaudeProvider must call cost-tracker exactly once on retry
# ---------------------------------------------------------------------------


class TestClaudeProviderRetryIntegration:
    def _make_provider(self, monkeypatch: pytest.MonkeyPatch):
        from app.llm.claude import ClaudeProvider

        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.setenv("CLAUDE_MODEL", "claude-sonnet-4-6")
        return ClaudeProvider()

    def _make_response(self, text: str = "ok") -> SimpleNamespace:
        block = SimpleNamespace(text=text)
        usage = SimpleNamespace(input_tokens=10, output_tokens=5)
        return SimpleNamespace(content=[block], usage=usage)

    @patch("app.llm.retry.time.sleep")
    @patch("app.llm.claude.ClaudeProvider._log_cost")
    def test_complete_logs_cost_once_on_retried_success(
        self,
        mock_log_cost: MagicMock,
        mock_sleep: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """Retried-then-succeeded calls log usage exactly once."""
        import anthropic

        provider = self._make_provider(monkeypatch)
        mock_client = MagicMock()
        provider._client = mock_client

        rl = anthropic.RateLimitError(
            message="rl",
            response=MagicMock(status_code=429),
            body=None,
        )
        mock_client.messages.create.side_effect = [rl, self._make_response("ok")]

        provider.complete("hi")

        # The wrapped SDK call was retried, but cost-tracker fires once.
        assert mock_client.messages.create.call_count == 2
        mock_log_cost.assert_called_once()

    @patch("app.llm.retry.time.sleep")
    @patch("app.llm.claude.ClaudeProvider._log_cost")
    def test_complete_does_not_log_cost_on_total_failure(
        self,
        mock_log_cost: MagicMock,
        mock_sleep: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """If all retries fail, no llm_usage row is logged."""
        import anthropic

        from app.llm.retry import MAX_RETRIES

        provider = self._make_provider(monkeypatch)
        mock_client = MagicMock()
        provider._client = mock_client

        err = anthropic.InternalServerError(
            message="boom",
            response=MagicMock(status_code=500),
            body=None,
        )
        mock_client.messages.create.side_effect = err

        with pytest.raises(anthropic.InternalServerError):
            provider.complete("hi")

        assert mock_client.messages.create.call_count == MAX_RETRIES + 1
        mock_log_cost.assert_not_called()

    @patch("app.llm.retry.time.sleep")
    @patch("app.llm.claude.ClaudeProvider._log_cost")
    def test_complete_401_fails_fast(
        self,
        mock_log_cost: MagicMock,
        mock_sleep: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """401 raises after the first attempt with no retries."""
        import anthropic

        provider = self._make_provider(monkeypatch)
        mock_client = MagicMock()
        provider._client = mock_client

        err = anthropic.AuthenticationError(
            message="bad key",
            response=MagicMock(status_code=401),
            body=None,
        )
        mock_client.messages.create.side_effect = err

        with pytest.raises(anthropic.AuthenticationError):
            provider.complete("hi")

        mock_client.messages.create.assert_called_once()
        mock_sleep.assert_not_called()
        mock_log_cost.assert_not_called()

    def test_acomplete_logs_cost_once_on_retried_success(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """Async retry path also logs usage exactly once."""
        import anthropic

        sleep_mock = AsyncMock()
        monkeypatch.setattr("app.llm.retry.asyncio.sleep", sleep_mock)

        provider = self._make_provider(monkeypatch)
        mock_client = MagicMock()
        provider._async_client = mock_client

        rl = anthropic.RateLimitError(
            message="rl",
            response=MagicMock(status_code=429),
            body=None,
        )
        mock_client.messages.create = AsyncMock(
            side_effect=[rl, self._make_response("ok")],
        )

        with patch("app.llm.claude.ClaudeProvider._log_cost") as mock_log_cost:
            result = asyncio.run(provider.acomplete("hi"))

        assert result == "ok"
        assert mock_client.messages.create.call_count == 2
        mock_log_cost.assert_called_once()


# ---------------------------------------------------------------------------
# Streaming: mid-stream failures are NOT retried
# ---------------------------------------------------------------------------


class TestStreamMidStreamErrorNotRetried:
    """A failure *after* the stream begins emitting must not be retried.

    Restarting a stream that has already pushed deltas to the client would
    produce duplicate output the orchestrator can't safely reconcile. We
    surface the error and let the handler decide.
    """

    def test_mid_stream_error_propagates(self, monkeypatch: pytest.MonkeyPatch):
        from app.llm.claude import ClaudeProvider

        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        provider = ClaudeProvider()
        mock_client = MagicMock()
        provider._async_client = mock_client

        # Open succeeds; iteration raises on the second event.
        emit_count = {"n": 0}

        class _BrokenStream:
            def __aiter__(self):
                return self

            async def __anext__(self):
                emit_count["n"] += 1
                if emit_count["n"] == 1:
                    return SimpleNamespace(
                        type="content_block_delta",
                        delta=SimpleNamespace(text="partial"),
                    )
                raise ConnectionError("network died mid-stream")

        class _Ctx:
            async def __aenter__(self):
                return _BrokenStream()

            async def __aexit__(self, *args):
                return False

        mock_client.messages.stream = MagicMock(return_value=_Ctx())

        async def _drain():
            received = []
            with pytest.raises(ConnectionError):
                async for evt in provider.astream("hi"):
                    received.append(evt)
            return received

        events = asyncio.run(_drain())
        # We got the first delta before the error — proves we didn't retry.
        assert any(e.type == "content" for e in events)
        # Stream open was only called once — no retry of the open after
        # the partial deltas, because the failure occurred during iteration.
        mock_client.messages.stream.assert_called_once()

    def test_stream_open_429_retried(self, monkeypatch: pytest.MonkeyPatch):
        """If the open itself raises 429, the open is retried."""
        import anthropic

        from app.llm.claude import ClaudeProvider

        sleep_mock = AsyncMock()
        monkeypatch.setattr("app.llm.retry.asyncio.sleep", sleep_mock)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")

        provider = ClaudeProvider()
        mock_client = MagicMock()
        provider._async_client = mock_client

        rl = anthropic.RateLimitError(
            message="rl",
            response=MagicMock(status_code=429),
            body=None,
        )

        class _OkStream:
            def __aiter__(self):
                return self

            async def __anext__(self):
                raise StopAsyncIteration

        class _OkCtx:
            async def __aenter__(self):
                return _OkStream()

            async def __aexit__(self, *args):
                return False

        class _BadCtx:
            async def __aenter__(self):
                raise rl

            async def __aexit__(self, *args):
                return False

        # First open raises 429; second open returns a normal empty stream.
        mock_client.messages.stream = MagicMock(side_effect=[_BadCtx(), _OkCtx()])

        with patch("app.llm.claude.ClaudeProvider._log_cost"):

            async def _drain():
                async for _ in provider.astream("hi"):
                    pass

            asyncio.run(_drain())

        assert mock_client.messages.stream.call_count == 2
        sleep_mock.assert_awaited_once_with(1.0)


# ---------------------------------------------------------------------------
# Voyage embedding provider shares the same retry policy.
# ---------------------------------------------------------------------------


class TestVoyageEmbeddingRetry:
    """The retry helper is symmetric for Voyage's async embed() call."""

    def test_embed_retries_on_429(self, monkeypatch: pytest.MonkeyPatch):
        from app.rag.embedding import VoyageProvider

        sleep_mock = AsyncMock()
        monkeypatch.setattr("app.llm.retry.asyncio.sleep", sleep_mock)
        monkeypatch.setenv("VOYAGE_API_KEY", "fake-key")

        provider = VoyageProvider()
        # Skip the lazy-init by pre-setting the client.
        mock_client = MagicMock()
        provider._client = mock_client

        # Voyage doesn't expose its own typed errors in our deps, so simulate
        # a status-bearing exception that ``_is_retryable`` treats as transient.
        class _FakeRateLimitError(Exception):
            status_code = 429

        response = MagicMock(embeddings=[[0.1] * 1024], total_tokens=10)
        mock_client.embed = AsyncMock(side_effect=[_FakeRateLimitError(), response])

        result = asyncio.run(provider.embed(["hello"]))

        assert result == [[0.1] * 1024]
        assert mock_client.embed.call_count == 2
        sleep_mock.assert_awaited_once_with(1.0)

    def test_embed_fails_fast_on_401(self, monkeypatch: pytest.MonkeyPatch):
        from app.rag.embedding import VoyageProvider

        sleep_mock = AsyncMock()
        monkeypatch.setattr("app.llm.retry.asyncio.sleep", sleep_mock)
        monkeypatch.setenv("VOYAGE_API_KEY", "fake-key")

        provider = VoyageProvider()
        mock_client = MagicMock()
        provider._client = mock_client

        class _FakeAuthError(Exception):
            status_code = 401

        mock_client.embed = AsyncMock(side_effect=_FakeAuthError())

        with pytest.raises(_FakeAuthError):
            asyncio.run(provider.embed(["hello"]))

        mock_client.embed.assert_called_once()
        sleep_mock.assert_not_called()

    def test_embed_retries_on_voyage_http_status_429(self, monkeypatch: pytest.MonkeyPatch):
        """#854: the real voyageai error shape carries ``http_status``
        (not ``status_code``) — a 429 must now retry instead of aborting
        the whole ingest on the first rate-limit hit."""
        from app.rag.embedding import VoyageProvider

        sleep_mock = AsyncMock()
        monkeypatch.setattr("app.llm.retry.asyncio.sleep", sleep_mock)
        monkeypatch.setenv("VOYAGE_API_KEY", "fake-key")

        provider = VoyageProvider()
        mock_client = MagicMock()
        provider._client = mock_client

        class _FakeVoyageRateLimitError(Exception):
            """Mimics voyageai.error.RateLimitError's attribute shape."""

            http_status = 429

        response = MagicMock(embeddings=[[0.1] * 1024], total_tokens=10)
        mock_client.embed = AsyncMock(side_effect=[_FakeVoyageRateLimitError(), response])

        with patch("app.rag.embedding.VoyageProvider._log_cost"):
            result = asyncio.run(provider.embed(["hello"]))

        assert result == [[0.1] * 1024]
        assert mock_client.embed.call_count == 2
        sleep_mock.assert_awaited_once_with(1.0)

    def test_embed_fails_fast_on_voyage_http_status_401(self, monkeypatch: pytest.MonkeyPatch):
        from app.rag.embedding import VoyageProvider

        sleep_mock = AsyncMock()
        monkeypatch.setattr("app.llm.retry.asyncio.sleep", sleep_mock)
        monkeypatch.setenv("VOYAGE_API_KEY", "fake-key")

        provider = VoyageProvider()
        mock_client = MagicMock()
        provider._client = mock_client

        class _FakeVoyageAuthError(Exception):
            http_status = 401

        mock_client.embed = AsyncMock(side_effect=_FakeVoyageAuthError())

        with pytest.raises(_FakeVoyageAuthError):
            asyncio.run(provider.embed(["hello"]))

        mock_client.embed.assert_called_once()
        sleep_mock.assert_not_called()


# ---------------------------------------------------------------------------
# #854: 529 (Anthropic overloaded) retries end-to-end through the helpers.
# ---------------------------------------------------------------------------


class TestOverloaded529Retry:
    @patch("app.llm.retry.time.sleep")
    def test_sync_529_then_success(self, mock_sleep: MagicMock):
        from app.llm.retry import retry_sync

        class _OverloadedError(Exception):
            status_code = 529

        fn = MagicMock(side_effect=[_OverloadedError(), "ok"])
        assert retry_sync(fn, context="test") == "ok"
        assert fn.call_count == 2
        mock_sleep.assert_called_once_with(1.0)

    def test_async_529_then_success(self, monkeypatch: pytest.MonkeyPatch):
        from app.llm.retry import retry_async

        sleep_mock = AsyncMock()
        monkeypatch.setattr("app.llm.retry.asyncio.sleep", sleep_mock)

        class _OverloadedError(Exception):
            status_code = 529

        fn = AsyncMock(side_effect=[_OverloadedError(), "ok"])

        result = asyncio.run(retry_async(fn, context="test"))

        assert result == "ok"
        assert fn.call_count == 2
        sleep_mock.assert_awaited_once_with(1.0)


# ---------------------------------------------------------------------------
# #854: SDK-internal retries are disabled — the outer wrapper is the single
# retry authority. SDK default (anthropic max_retries=2) would stack under
# the wrapper's 4 attempts → up to 12 HTTP calls.
# ---------------------------------------------------------------------------


class TestSdkRetryStackingDisabled:
    def test_sync_anthropic_client_built_with_zero_retries(self, monkeypatch: pytest.MonkeyPatch):
        from app.llm.claude import ClaudeProvider

        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        provider = ClaudeProvider()

        fake_anthropic = MagicMock()
        with patch.dict("sys.modules", {"anthropic": fake_anthropic}):
            provider._get_client()

        fake_anthropic.Anthropic.assert_called_once_with(api_key="sk-ant-test", max_retries=0)

    def test_async_anthropic_client_built_with_zero_retries(self, monkeypatch: pytest.MonkeyPatch):
        from app.llm.claude import ClaudeProvider

        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        provider = ClaudeProvider()

        fake_anthropic = MagicMock()
        with patch.dict("sys.modules", {"anthropic": fake_anthropic}):
            provider._get_async_client()

        fake_anthropic.AsyncAnthropic.assert_called_once_with(api_key="sk-ant-test", max_retries=0)

    def test_voyage_client_built_with_zero_retries(self, monkeypatch: pytest.MonkeyPatch):
        """voyageai's tenacity-based retries default to 0; pin it so an
        SDK default bump can never reintroduce stacking."""
        from app.rag.embedding import VoyageProvider

        monkeypatch.setenv("VOYAGE_API_KEY", "fake-key")
        provider = VoyageProvider()

        fake_voyageai = MagicMock()
        with patch.dict("sys.modules", {"voyageai": fake_voyageai}):
            provider._get_client()

        fake_voyageai.AsyncClient.assert_called_once_with(api_key="fake-key", max_retries=0)
