"""Unit tests for :mod:`app.docs.error_mapping` (#609).

The mapper turns raw exception text into a short actionable Estonian
message shown to ministry lawyers, plus a raw debug detail stored for
admins. These tests cover every mapped failure mode plus the generic
fallback so a future refactor cannot silently drop a rule.
"""

from __future__ import annotations

import pytest

from app.docs.error_mapping import (
    MSG_ENCRYPTED_PDF,
    MSG_FILE_MISSING,
    MSG_LLM_UNAVAILABLE,
    MSG_SPARQL_BUSY,
    MSG_TIKA_CAPACITY,
    MSG_UNKNOWN,
    map_failure_to_user_message,
)

# ---------------------------------------------------------------------------
# Fake library-specific exception classes
# ---------------------------------------------------------------------------
# We can't import ``anthropic`` in tests (stub-mode deployments run
# without the package), so we synthesise lookalike classes whose
# ``__module__`` starts with ``anthropic.`` — the mapper keys on
# class-path substring so these are indistinguishable from the real
# thing at the mapping layer.


def _fake_anthropic_exc(name: str, message: str) -> Exception:
    cls = type(name, (Exception,), {"__module__": "anthropic._errors"})
    return cls(message)


def _fake_requests_timeout(message: str = "timed out") -> Exception:
    cls = type("Timeout", (Exception,), {"__module__": "requests.exceptions"})
    return cls(message)


def _fake_httpx_timeout(message: str = "Read timed out") -> Exception:
    cls = type("TimeoutException", (Exception,), {"__module__": "httpx"})
    return cls(message)


# ---------------------------------------------------------------------------
# Individual failure modes
# ---------------------------------------------------------------------------


class TestEncryptedPdf:
    @pytest.mark.parametrize(
        "message",
        [
            "PDFBox: document is encrypted",
            "TikaException: encountered password-protected PDF",
            "Eelnõu on krüpteeritud",
        ],
    )
    def test_encrypted_pdf_markers(self, message: str):
        user, debug = map_failure_to_user_message(Exception(message), "parse")
        assert user == MSG_ENCRYPTED_PDF
        assert message in debug


class TestTikaCapacity:
    def test_memory_error_class(self):
        user, debug = map_failure_to_user_message(MemoryError("Java heap space"), "parse")
        assert user == MSG_TIKA_CAPACITY
        assert "MemoryError" in debug

    @pytest.mark.parametrize(
        "message",
        [
            "Read timed out after 60s",
            "java.lang.OutOfMemoryError: Java heap space",
            "OutOfMemoryError",
        ],
    )
    def test_tika_capacity_markers(self, message: str):
        user, _ = map_failure_to_user_message(RuntimeError(message), "parse")
        assert user == MSG_TIKA_CAPACITY

    def test_generic_timeout_outside_analyze_stage(self):
        """A ``requests.Timeout`` during *parse* reads as a capacity
        issue (Tika took too long), not a SPARQL outage."""
        user, _ = map_failure_to_user_message(_fake_requests_timeout("upload timed out"), "parse")
        assert user == MSG_TIKA_CAPACITY


class TestSparqlBusy:
    def test_sparql_timeout_during_analyze(self):
        """During the analyze stage a generic HTTP timeout points at
        Jena/Fuseki, so the Estonian message nudges the user to wait."""
        user, _ = map_failure_to_user_message(_fake_httpx_timeout(), "analyze")
        assert user == MSG_SPARQL_BUSY

    @pytest.mark.parametrize(
        "message",
        [
            "SPARQL query execution failed",
            "Fuseki returned 503",
            "QueryParseException: bad syntax",
        ],
    )
    def test_sparql_markers(self, message: str):
        user, _ = map_failure_to_user_message(RuntimeError(message), "analyze")
        assert user == MSG_SPARQL_BUSY


class TestLlmUnavailable:
    def test_anthropic_rate_limit_class(self):
        exc = _fake_anthropic_exc("RateLimitError", "429 Too Many Requests")
        user, debug = map_failure_to_user_message(exc, "extract")
        assert user == MSG_LLM_UNAVAILABLE
        assert "RateLimitError" in debug

    def test_anthropic_auth_class(self):
        exc = _fake_anthropic_exc("AuthenticationError", "invalid api key")
        user, _ = map_failure_to_user_message(exc, "extract")
        assert user == MSG_LLM_UNAVAILABLE

    @pytest.mark.parametrize(
        "message",
        [
            "rate limit exceeded",
            "429 Too Many Requests",
            "Invalid API key",
            "401 Unauthorized",
        ],
    )
    def test_llm_message_markers(self, message: str):
        user, _ = map_failure_to_user_message(RuntimeError(message), "extract")
        assert user == MSG_LLM_UNAVAILABLE


class TestFileMissing:
    def test_file_not_found_error(self):
        exc = FileNotFoundError("/srv/drafts/abc.enc missing")
        user, debug = map_failure_to_user_message(exc, "parse")
        assert user == MSG_FILE_MISSING
        assert "/srv/drafts/abc.enc" in debug


class TestUnknownFallback:
    def test_unmapped_runtime_error(self):
        user, debug = map_failure_to_user_message(RuntimeError("mystery bug"), "parse")
        assert user == MSG_UNKNOWN
        assert "mystery bug" in debug
        assert "RuntimeError" in debug

    def test_unmapped_value_error(self):
        user, _ = map_failure_to_user_message(ValueError("nope"), "extract")
        assert user == MSG_UNKNOWN


class TestDebugDetail:
    def test_debug_detail_includes_stage_and_class(self):
        _, debug = map_failure_to_user_message(RuntimeError("boom"), "analyze")
        assert debug.startswith("[analyze]")
        assert "RuntimeError" in debug
        assert "boom" in debug

    def test_debug_detail_is_truncated(self):
        """Very long exception messages must not bloat the drafts row."""
        long_msg = "x" * 10_000
        _, debug = map_failure_to_user_message(RuntimeError(long_msg), "parse")
        # The mapper truncates at 2000 chars; the header adds ~30
        # chars so the total is under ~2050.
        assert len(debug) <= 2000
