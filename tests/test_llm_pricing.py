"""Tests for ``app.llm.pricing`` (#854, review E4).

Covers:
* Corrected per-model rates for every configured production model
  (verified 2026-06-11 against platform.claude.com and docs.voyageai.com).
* Voyage embedding entries exist and produce non-zero costs.
* Unknown ``(provider, model)`` pairs warn loudly (once per model) and
  return 0.0 instead of failing — the warn-and-record policy.
* ``log_usage`` still records a row (cost 0) for unknown models so the
  usage itself is never lost.
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

import pytest

from app.llm.pricing import (
    PRICING,
    _reset_unknown_model_warnings,
    calculate_cost,
)


@pytest.fixture(autouse=True)
def _fresh_warn_state():
    """Each test starts with a clean warn-once registry."""
    _reset_unknown_model_warnings()
    yield
    _reset_unknown_model_warnings()


class TestClaudePricing:
    """Rates from https://platform.claude.com/docs/en/docs/about-claude/pricing."""

    @pytest.mark.parametrize(
        ("model", "expected_input", "expected_output"),
        [
            ("claude-fable-5", 10.00, 50.00),
            ("claude-opus-4-8", 5.00, 25.00),
            ("claude-opus-4-7", 5.00, 25.00),
            # The pre-#854 table had opus-4-6 at $15/$75 — Opus 4.0/4.1
            # pricing — a 3x overcharge.
            ("claude-opus-4-6", 5.00, 25.00),
            ("claude-opus-4-5", 5.00, 25.00),
            ("claude-sonnet-4-6", 3.00, 15.00),
            ("claude-sonnet-4-5", 3.00, 15.00),
            # The pre-#854 table had haiku-4-5 at $0.80/$4.00 — Haiku 3.5
            # pricing — an undercharge.
            ("claude-haiku-4-5", 1.00, 5.00),
        ],
    )
    def test_rates_match_provider_docs(
        self, model: str, expected_input: float, expected_output: float
    ):
        rates = PRICING[("claude", model)]
        assert rates["input"] == expected_input
        assert rates["output"] == expected_output

    def test_default_model_cost_per_million_tokens(self):
        """1M in + 1M out on the CLAUDE_MODEL default (sonnet-4-6) = $18."""
        cost = calculate_cost("claude", "claude-sonnet-4-6", 1_000_000, 1_000_000)
        assert cost == pytest.approx(18.00)

    def test_opus_4_6_no_longer_triple_charged(self):
        """100k in + 10k out on opus-4-6 = $0.75, not $2.25."""
        cost = calculate_cost("claude", "claude-opus-4-6", 100_000, 10_000)
        assert cost == pytest.approx(0.75)

    def test_haiku_4_5_cost(self):
        """1M in + 200k out on haiku-4-5 = $1 + $1 = $2."""
        cost = calculate_cost("claude", "claude-haiku-4-5", 1_000_000, 200_000)
        assert cost == pytest.approx(2.00)

    def test_zero_tokens_zero_cost(self):
        assert calculate_cost("claude", "claude-sonnet-4-6", 0, 0) == 0.0


class TestVoyagePricing:
    """Rates from https://docs.voyageai.com/docs/pricing."""

    def test_every_voyage_entry_has_zero_output_rate(self):
        """Embeddings only bill input tokens."""
        voyage_entries = {k: v for k, v in PRICING.items() if k[0] == "voyage"}
        assert voyage_entries, "expected voyage entries in PRICING"
        for rates in voyage_entries.values():
            assert rates["output"] == 0.0

    def test_default_embedding_model_present_and_priced(self):
        """The VOYAGE_MODEL default (voyage-multilingual-2) is $0.12/MTok."""
        rates = PRICING[("voyage", "voyage-multilingual-2")]
        assert rates["input"] == 0.12

    def test_embedding_cost_is_non_zero(self):
        """Pre-#854 every embedding logged cost_usd=0 — must be real now."""
        cost = calculate_cost("voyage", "voyage-multilingual-2", 1_000_000, 0)
        assert cost == pytest.approx(0.12)
        assert cost > 0.0

    @pytest.mark.parametrize(
        ("model", "expected_input"),
        [
            ("voyage-multilingual-2", 0.12),
            ("voyage-law-2", 0.12),
            ("voyage-3-large", 0.18),
            ("voyage-3.5", 0.06),
            ("voyage-3.5-lite", 0.02),
        ],
    )
    def test_voyage_rates_match_provider_docs(self, model: str, expected_input: float):
        assert PRICING[("voyage", model)]["input"] == expected_input


class TestEveryConfiguredModelIsPriced:
    """Every PRICING row must produce a non-zero, warning-free cost."""

    @pytest.mark.parametrize(("provider", "model"), sorted(PRICING))
    def test_table_entry_yields_positive_cost_without_warning(
        self, provider: str, model: str, caplog: pytest.LogCaptureFixture
    ):
        with caplog.at_level(logging.WARNING, logger="app.llm.pricing"):
            cost = calculate_cost(provider, model, 1_000_000, 1_000_000)
        assert cost > 0.0
        assert not caplog.records


class TestUnknownModelPolicy:
    """Warn-and-record: loud warning, cost 0, billing paths never crash."""

    def test_unknown_model_returns_zero(self):
        assert calculate_cost("claude", "claude-nonexistent-9-9", 1000, 1000) == 0.0

    def test_unknown_model_warns(self, caplog: pytest.LogCaptureFixture):
        with caplog.at_level(logging.WARNING, logger="app.llm.pricing"):
            calculate_cost("claude", "claude-nonexistent-9-9", 1000, 1000)
        assert len(caplog.records) == 1
        message = caplog.records[0].getMessage()
        assert "claude-nonexistent-9-9" in message
        assert "cost_usd=0" in message

    def test_unknown_provider_warns(self, caplog: pytest.LogCaptureFixture):
        with caplog.at_level(logging.WARNING, logger="app.llm.pricing"):
            assert calculate_cost("openai", "gpt-x", 1000, 0) == 0.0
        assert len(caplog.records) == 1

    def test_warns_once_per_model_per_process(self, caplog: pytest.LogCaptureFixture):
        """A busy ingest must not flood the logs with one line per call."""
        with caplog.at_level(logging.WARNING, logger="app.llm.pricing"):
            for _ in range(5):
                calculate_cost("voyage", "voyage-unknown-99", 1000, 0)
        assert len(caplog.records) == 1

    def test_distinct_unknown_models_each_warn(self, caplog: pytest.LogCaptureFixture):
        with caplog.at_level(logging.WARNING, logger="app.llm.pricing"):
            calculate_cost("claude", "claude-mystery-a", 1, 1)
            calculate_cost("claude", "claude-mystery-b", 1, 1)
        assert len(caplog.records) == 2

    @patch("app.llm.cost_tracker.get_connection")
    def test_log_usage_still_records_row_for_unknown_model(
        self, mock_get_connection: MagicMock, caplog: pytest.LogCaptureFixture
    ):
        """The usage row must survive with cost 0 — never dropped or crashed."""
        from app.llm.cost_tracker import log_usage

        mock_conn = MagicMock()
        mock_get_connection.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_get_connection.return_value.__exit__ = MagicMock(return_value=False)

        with caplog.at_level(logging.WARNING, logger="app.llm.pricing"):
            log_usage(
                user_id=None,
                org_id=None,
                provider="claude",
                model="claude-brand-new-model",
                feature="chat",
                tokens_input=1234,
                tokens_output=56,
            )

        # Warned loudly...
        assert any("claude-brand-new-model" in r.getMessage() for r in caplog.records)
        # ...and still inserted the row with cost 0.
        mock_conn.execute.assert_called_once()
        insert_params = mock_conn.execute.call_args[0][1]
        assert insert_params[-1] == 0.0  # cost_usd
        assert insert_params[5] == 1234  # tokens_input
        mock_conn.commit.assert_called_once()

    @patch("app.llm.cost_tracker.get_connection")
    def test_log_usage_records_real_cost_for_known_voyage_model(
        self, mock_get_connection: MagicMock
    ):
        """Voyage usage rows carry a non-zero cost_usd now (#854)."""
        from app.llm.cost_tracker import log_usage

        mock_conn = MagicMock()
        mock_get_connection.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_get_connection.return_value.__exit__ = MagicMock(return_value=False)

        log_usage(
            user_id="22222222-2222-2222-2222-222222222222",
            org_id="33333333-3333-3333-3333-333333333333",
            provider="voyage",
            model="voyage-multilingual-2",
            feature="chat_embedding",
            tokens_input=500_000,
            tokens_output=0,
        )

        insert_params = mock_conn.execute.call_args[0][1]
        assert insert_params[-1] == pytest.approx(0.06)  # 0.5M * $0.12/M
        assert insert_params[0] == "22222222-2222-2222-2222-222222222222"
        assert insert_params[1] == "33333333-3333-3333-3333-333333333333"
        assert insert_params[4] == "chat_embedding"
