"""Unit tests for ``app.analyysikeskus.intent_extractor`` (#814 prep).

These tests never make real Anthropic API calls: they inject a
``MagicMock`` :class:`app.llm.LLMProvider` via ``provider=`` or patch
``get_default_provider``. The dev-mode stub path (no API key) is also
exercised against the real :class:`ClaudeProvider` so we know synthetic
candidates flow through the whole pipeline end-to-end in CI.

The intent_extractor is the **semantic-inference** counterpart to
``app.docs.entity_extractor`` (which is literal-only). The tests below
lock in:

* The prompt invites *semantic inference* (the literal-only extractor
  must NOT — this is the file-distinct invariant from #814).
* Every LLM call passes ``feature="intent_analysis"`` so cost-tracker
  attribution is correct (the existing literal extractor does NOT, per
  the TODO at ``app/docs/entity_extractor.py:180`` — we don't inherit
  that bug).
* The reply is parsed into :class:`IntentCandidate` with the
  ``reasoning`` field populated when the model emits one.
* Malformed JSON → graceful empty list + warning log.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.analyysikeskus.intent_extractor import (
    _INTENT_PROMPT,
    INTENT_FEATURE_LABEL,
    IntentCandidate,
    extract_intent_candidates,
)

# ---------------------------------------------------------------------------
# Prompt invariants
# ---------------------------------------------------------------------------


class TestPromptStructure:
    """Snapshot the prompt's invariants — these are load-bearing for #814."""

    def test_prompt_invites_semantic_inference(self):
        """The prompt must invite semantic inference, not literal extraction.

        This is the key distinction between ``intent_extractor`` and
        ``entity_extractor`` (which says "Never invent references —
        extract only what is literally in the text"). If this assertion
        ever flips, someone has accidentally regressed the intent
        extractor into the literal-extraction pattern.
        """
        prompt = _INTENT_PROMPT.lower()
        # Estonian for "use semantic inference"
        assert "semantilist järeldamist" in prompt
        # Estonian for "propose candidates"
        assert "tee ettepanekuid" in prompt
        # Negative assertion: the literal-only language from
        # entity_extractor must NOT appear here.
        assert "never invent" not in prompt
        assert "literally in the text" not in prompt
        # Estonian negative assertion: explicit "this is NOT a literal extract".
        assert "ei ole sõnasõnaline" in prompt

    def test_prompt_asks_for_reasoning(self):
        """The schema example must include a ``reasoning`` field.

        Reasoning is what makes the confirmation UI useful — without it
        the user has to evaluate each candidate blind.
        """
        assert '"reasoning"' in _INTENT_PROMPT
        assert "põhjendus" in _INTENT_PROMPT.lower()

    def test_prompt_lists_all_ref_types(self):
        """The prompt must enumerate every ref_type the resolver understands."""
        for ref_type in ("law", "provision", "eu_act", "court_decision", "concept"):
            assert ref_type in _INTENT_PROMPT

    def test_prompt_estonian_user_facing(self):
        """The prompt should be primarily Estonian since users describe intent in Estonian.

        We accept some English markers (JSON / keys / ref_type values are
        deliberately English to keep the LLM's structured output stable
        across providers) but the prose-level guidance must be Estonian.
        """
        # Estonian markers we expect.
        for marker in ("Eesti", "õigusakt", "kandidaat", "kasutaja"):
            assert marker.lower() in _INTENT_PROMPT.lower(), (
                f"Expected Estonian marker {marker!r} in prompt"
            )

    def test_prompt_has_intent_placeholder(self):
        """The template must keep its ``{intent}`` placeholder for str.replace."""
        assert "{intent}" in _INTENT_PROMPT


# ---------------------------------------------------------------------------
# Cost tracking
# ---------------------------------------------------------------------------


class TestCostTrackingFeatureTag:
    """The whole reason for not reusing ``entity_extractor`` is correct
    cost attribution. Every LLM call must pass ``feature="intent_analysis"``."""

    def test_feature_label_constant_is_correct(self):
        """The module-level constant is the canonical feature tag."""
        assert INTENT_FEATURE_LABEL == "intent_analysis"

    def test_extract_passes_intent_analysis_feature_tag(self):
        """``extract_intent_candidates`` must forward ``feature="intent_analysis"``."""
        provider = MagicMock()
        provider.extract_json.return_value = {"candidates": []}

        extract_intent_candidates("Soovin lihtsustada toetuse taotlemist.", provider=provider)

        provider.extract_json.assert_called_once()
        kwargs = provider.extract_json.call_args.kwargs
        assert kwargs.get("feature") == "intent_analysis", (
            f"Expected feature='intent_analysis', got {kwargs.get('feature')!r}. "
            "This breaks per-feature cost attribution in the admin dashboard."
        )

    def test_extract_forwards_user_and_org_ids(self):
        """User + org IDs ride through so per-user/per-org budgets are enforced."""
        provider = MagicMock()
        provider.extract_json.return_value = {"candidates": []}

        extract_intent_candidates(
            "Soovin lihtsustada toetuse taotlemist.",
            provider=provider,
            user_id="user-123",
            org_id="org-456",
        )

        kwargs = provider.extract_json.call_args.kwargs
        assert kwargs.get("user_id") == "user-123"
        assert kwargs.get("org_id") == "org-456"


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------


class TestResponseParsing:
    def test_parses_well_formed_response(self):
        """The model's JSON response is coerced into IntentCandidate."""
        provider = MagicMock()
        provider.extract_json.return_value = {
            "candidates": [
                {
                    "ref_text": "PISTS § 4",
                    "ref_type": "provision",
                    "confidence": 0.85,
                    "reasoning": "Puudega inimeste toetuste taotlemise põhinorm.",
                },
                {
                    "ref_text": "sotsiaalhoolekande seadus",
                    "ref_type": "law",
                    "confidence": 0.7,
                    "reasoning": "Üldine sotsiaaltoetuste raamistik.",
                },
            ]
        }

        candidates = extract_intent_candidates(
            "Soovin lihtsustada puudega inimese toetuse taotlemist.",
            provider=provider,
        )

        # Sorted by (ref_type, ref_text) for deterministic output.
        assert len(candidates) == 2
        types = {c.ref_type for c in candidates}
        assert types == {"law", "provision"}

        provision = next(c for c in candidates if c.ref_type == "provision")
        assert provision.ref_text == "PISTS § 4"
        assert provision.confidence == 0.85
        assert "põhinorm" in provision.reasoning

        law = next(c for c in candidates if c.ref_type == "law")
        assert law.ref_text == "sotsiaalhoolekande seadus"

    def test_returns_intent_candidate_instances(self):
        """The parsed entries must be ``IntentCandidate`` (not ``ExtractedRef``)."""
        provider = MagicMock()
        provider.extract_json.return_value = {
            "candidates": [
                {
                    "ref_text": "PISTS § 4",
                    "ref_type": "provision",
                    "confidence": 0.9,
                    "reasoning": "põhinorm",
                }
            ]
        }
        candidates = extract_intent_candidates("midagi", provider=provider)
        assert len(candidates) == 1
        assert isinstance(candidates[0], IntentCandidate)

    def test_dedupes_candidates(self):
        """Same (ref_text, ref_type) twice → one entry with the higher confidence."""
        provider = MagicMock()
        provider.extract_json.return_value = {
            "candidates": [
                {
                    "ref_text": "PISTS § 4",
                    "ref_type": "provision",
                    "confidence": 0.6,
                    "reasoning": "short",
                },
                {
                    "ref_text": "PISTS § 4",
                    "ref_type": "provision",
                    "confidence": 0.9,
                    "reasoning": "more detailed reasoning here",
                },
            ]
        }
        candidates = extract_intent_candidates("midagi", provider=provider)
        assert len(candidates) == 1
        assert candidates[0].confidence == 0.9
        assert "more detailed" in candidates[0].reasoning

    def test_handles_missing_reasoning_gracefully(self):
        """A candidate without ``reasoning`` falls back to empty string, not error."""
        provider = MagicMock()
        provider.extract_json.return_value = {
            "candidates": [
                {
                    "ref_text": "PISTS § 4",
                    "ref_type": "provision",
                    "confidence": 0.9,
                }
            ]
        }
        candidates = extract_intent_candidates("midagi", provider=provider)
        assert len(candidates) == 1
        assert candidates[0].reasoning == ""

    def test_clamps_confidence_to_valid_range(self):
        """Out-of-range confidence values are clamped to [0.0, 1.0]."""
        provider = MagicMock()
        provider.extract_json.return_value = {
            "candidates": [
                {
                    "ref_text": "PISTS § 4",
                    "ref_type": "provision",
                    "confidence": 1.5,
                    "reasoning": "",
                },
                {
                    "ref_text": "KarS § 121",
                    "ref_type": "provision",
                    "confidence": -0.2,
                    "reasoning": "",
                },
            ]
        }
        candidates = extract_intent_candidates("midagi", provider=provider)
        assert len(candidates) == 2
        for c in candidates:
            assert 0.0 <= c.confidence <= 1.0


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


class TestErrorHandling:
    def test_empty_intent_returns_empty_no_llm_call(self):
        """Whitespace-only intent must not call the LLM (saves a token)."""
        provider = MagicMock()
        assert extract_intent_candidates("", provider=provider) == []
        assert extract_intent_candidates("   \n\n  \t", provider=provider) == []
        provider.extract_json.assert_not_called()

    def test_llm_raises_returns_empty_list_with_warning(self, caplog: pytest.LogCaptureFixture):
        """A provider exception → graceful empty list + warning log.

        The route renders an "empty state with manual-add affordance" so
        the user is never stuck on an LLM failure.
        """
        provider = MagicMock()
        provider.extract_json.side_effect = RuntimeError("connection reset")

        with caplog.at_level("WARNING"):
            candidates = extract_intent_candidates("midagi", provider=provider)

        assert candidates == []
        assert any("LLM call failed" in rec.message for rec in caplog.records)

    def test_missing_candidates_key_returns_empty(self, caplog: pytest.LogCaptureFixture):
        """Reply missing the ``candidates`` key → warning + empty list."""
        provider = MagicMock()
        provider.extract_json.return_value = {"nonsense": "value"}

        with caplog.at_level("WARNING"):
            candidates = extract_intent_candidates("midagi", provider=provider)

        assert candidates == []
        assert any("missing 'candidates' key" in rec.message for rec in caplog.records)

    def test_non_dict_reply_returns_empty(self, caplog: pytest.LogCaptureFixture):
        """A non-dict reply → warning + empty list."""
        provider = MagicMock()
        provider.extract_json.return_value = "not a dict"

        with caplog.at_level("WARNING"):
            candidates = extract_intent_candidates("midagi", provider=provider)

        assert candidates == []
        assert any("non-dict reply" in rec.message for rec in caplog.records)

    def test_candidates_not_list_returns_empty(self, caplog: pytest.LogCaptureFixture):
        """``candidates`` value isn't a list → warning + empty list."""
        provider = MagicMock()
        provider.extract_json.return_value = {"candidates": "should be a list"}

        with caplog.at_level("WARNING"):
            candidates = extract_intent_candidates("midagi", provider=provider)

        assert candidates == []
        assert any("not a list" in rec.message for rec in caplog.records)

    def test_invalid_ref_type_is_dropped(self):
        """Candidate with ref_type outside the allowed set is silently skipped."""
        provider = MagicMock()
        provider.extract_json.return_value = {
            "candidates": [
                {
                    "ref_text": "Valid",
                    "ref_type": "law",
                    "confidence": 0.9,
                    "reasoning": "",
                },
                {
                    "ref_text": "Bad",
                    "ref_type": "garbage",
                    "confidence": 0.8,
                    "reasoning": "",
                },
            ]
        }
        candidates = extract_intent_candidates("midagi", provider=provider)
        assert len(candidates) == 1
        assert candidates[0].ref_text == "Valid"

    def test_empty_ref_text_is_dropped(self):
        """A candidate with empty/whitespace ref_text is skipped."""
        provider = MagicMock()
        provider.extract_json.return_value = {
            "candidates": [
                {
                    "ref_text": "  ",
                    "ref_type": "law",
                    "confidence": 0.9,
                    "reasoning": "",
                },
                {
                    "ref_text": "Valid",
                    "ref_type": "law",
                    "confidence": 0.7,
                    "reasoning": "",
                },
            ]
        }
        candidates = extract_intent_candidates("midagi", provider=provider)
        assert len(candidates) == 1
        assert candidates[0].ref_text == "Valid"


# ---------------------------------------------------------------------------
# Stub mode
# ---------------------------------------------------------------------------


class TestStubMode:
    def test_stub_mode_returns_synthetic_candidates(self, monkeypatch: pytest.MonkeyPatch):
        """Dev + no API key → stub candidates with ``[STUB`` prefix."""
        monkeypatch.setenv("APP_ENV", "development")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        candidates = extract_intent_candidates(
            "Soovin lihtsustada puudega inimese toetuse taotlemist."
        )

        assert len(candidates) >= 2
        for cand in candidates:
            assert isinstance(cand, IntentCandidate)
            assert cand.ref_text.startswith("[STUB")
            assert cand.ref_type in {"law", "provision", "eu_act", "court_decision", "concept"}
            assert 0.0 <= cand.confidence <= 1.0
            # Stub candidates carry a reasoning so the UI has something
            # to render even in dev mode.
            assert cand.reasoning


# ---------------------------------------------------------------------------
# Intent text in prompt
# ---------------------------------------------------------------------------


class TestIntentInPrompt:
    def test_intent_text_is_embedded_in_prompt(self):
        """The user's intent must be inside the prompt sent to the LLM."""
        provider = MagicMock()
        provider.extract_json.return_value = {"candidates": []}

        intent = "Soovin lihtsustada puudega inimese toetuse taotlemist."
        extract_intent_candidates(intent, provider=provider)

        prompt = provider.extract_json.call_args.args[0]
        assert intent in prompt

    def test_intent_text_is_quoted_for_isolation(self):
        """The intent must be wrapped in triple backticks (treat as data)."""
        provider = MagicMock()
        provider.extract_json.return_value = {"candidates": []}

        extract_intent_candidates("midagi", provider=provider)

        prompt = provider.extract_json.call_args.args[0]
        assert "```" in prompt
