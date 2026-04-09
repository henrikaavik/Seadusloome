"""Unit tests for ``app.docs.entity_extractor``.

These tests never make real Anthropic API calls: they inject a
MagicMock :class:`app.llm.LLMProvider` via ``provider=`` or patch
``get_default_provider``. The dev-mode stub path (no API key) is
also exercised against the real :class:`ClaudeProvider` so we know
synthetic refs flow through the whole pipeline end-to-end in CI.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.docs.entity_extractor import (
    ExtractedRef,
    extract_refs_from_text,
)


class TestStubMode:
    def test_stub_mode_returns_synthetic_refs(self, monkeypatch: pytest.MonkeyPatch):
        """Dev + no API key → stub refs with ``[STUB`` prefix and valid shape."""
        monkeypatch.setenv("APP_ENV", "development")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        text = "§ 1. Test. " + ("x" * 200)
        refs = extract_refs_from_text(text)

        assert len(refs) >= 2
        for ref in refs:
            assert isinstance(ref, ExtractedRef)
            assert ref.ref_text.startswith("[STUB")
            assert ref.ref_type == "provision"
            assert 0.0 <= ref.confidence <= 1.0
            # Location dict must record the chunk index the ref came from.
            assert "chunk" in ref.location


class TestProviderInteraction:
    def test_provider_receives_extraction_prompt(self):
        """The prompt handed to ``extract_json`` must contain the extraction ask."""
        provider = MagicMock()
        provider.extract_json.return_value = {"refs": []}

        extract_refs_from_text("§ 1. Testtekst.", provider=provider)

        provider.extract_json.assert_called_once()
        prompt_arg = provider.extract_json.call_args.args[0]
        assert "extract every legal reference" in prompt_arg.lower()
        # The source text must also be embedded in the prompt.
        assert "§ 1. Testtekst." in prompt_arg

    def test_dedupes_refs_across_chunks(self):
        """The same (ref_text, ref_type) across two chunks collapses to one entry."""
        provider = MagicMock()
        # Two calls → two chunks; both return the same reference but
        # with different confidences. Dedupe should keep the higher
        # confidence.
        provider.extract_json.side_effect = [
            {"refs": [{"ref_text": "KarS § 133", "ref_type": "provision", "confidence": 0.6}]},
            {"refs": [{"ref_text": "KarS § 133", "ref_type": "provision", "confidence": 0.9}]},
        ]

        # Force the chunker into two chunks: no paragraph/sentence
        # boundaries, 2500 raw 'x' chars, target=1000.
        text = "x" * 2500
        with patch("app.docs.entity_extractor.chunk_text") as mock_chunk:
            from app.docs.chunking import ChunkSpan

            mock_chunk.return_value = [
                ChunkSpan(start=0, end=1250, text=text[:1250]),
                ChunkSpan(start=1250, end=2500, text=text[1250:]),
            ]
            refs = extract_refs_from_text(text, provider=provider)

        assert len(refs) == 1
        assert refs[0].ref_text == "KarS § 133"
        assert refs[0].ref_type == "provision"
        assert refs[0].confidence == 0.9

    def test_drops_malformed_json_response(self, caplog: pytest.LogCaptureFixture):
        """Missing ``refs`` key → warning logged, chunk contributes nothing."""
        provider = MagicMock()
        provider.extract_json.return_value = {"nonsense": "value"}

        with caplog.at_level("WARNING"):
            refs = extract_refs_from_text("Lühike tekst.", provider=provider)

        assert refs == []
        assert any("missing 'refs' key" in rec.message for rec in caplog.records)

    def test_empty_text_returns_empty(self):
        """Empty / whitespace input must not call the provider."""
        provider = MagicMock()

        assert extract_refs_from_text("", provider=provider) == []
        assert extract_refs_from_text("   \n\n  \t", provider=provider) == []

        provider.extract_json.assert_not_called()

    def test_invalid_ref_type_is_dropped(self):
        """Refs with a ref_type outside the allowed set are silently skipped."""
        provider = MagicMock()
        provider.extract_json.return_value = {
            "refs": [
                {"ref_text": "Valid", "ref_type": "law", "confidence": 0.9},
                {"ref_text": "Bad", "ref_type": "garbage", "confidence": 0.8},
            ]
        }
        refs = extract_refs_from_text("tekst", provider=provider)
        assert len(refs) == 1
        assert refs[0].ref_text == "Valid"

    def test_provider_exception_chunk_returns_empty(self, caplog: pytest.LogCaptureFixture):
        """A provider exception on one chunk must not crash the extractor."""
        provider = MagicMock()
        provider.extract_json.side_effect = RuntimeError("connection reset")

        with caplog.at_level("WARNING"):
            refs = extract_refs_from_text("Lühike tekst.", provider=provider)

        assert refs == []
        assert any("LLM call failed" in rec.message for rec in caplog.records)
