"""Unit tests for ``app.docs.impact.scoring.calculate_impact_score``.

The scoring function is pure — no dependencies, no mocks. Every test
hand-builds an :class:`ImpactFindings` and asserts the numeric output
against the formula documented in ``scoring.py`` (spec §8.5).
"""

from __future__ import annotations

from app.docs.impact.analyzer import ImpactFindings
from app.docs.impact.scoring import calculate_impact_score


def _findings(
    *,
    affected: int = 0,
    conflicts: int = 0,
    gaps: int = 0,
) -> ImpactFindings:
    """Build a minimal findings object with only the count fields set."""
    return ImpactFindings(
        affected_entities=[],
        conflicts=[],
        gaps=[],
        eu_compliance=[],
        affected_count=affected,
        conflict_count=conflicts,
        gap_count=gaps,
    )


def test_zero_findings_returns_zero():
    assert calculate_impact_score(_findings()) == 0


def test_small_draft_matches_formula():
    # base = 10 * 2 = 20; no conflicts; no gaps.
    assert calculate_impact_score(_findings(affected=10)) == 20


def test_medium_draft_with_one_conflict():
    # base = 20 * 2 = 40; conflict_penalty = 10; total 50.
    score = calculate_impact_score(_findings(affected=20, conflicts=1))
    assert score == 50


def test_large_draft_clamps_to_100():
    # affected=200 would produce base=400, clamped to 100.
    assert calculate_impact_score(_findings(affected=200)) == 100


def test_just_conflicts_contribute_to_score():
    # 0 affected, 5 conflicts => 50.
    assert calculate_impact_score(_findings(conflicts=5)) == 50


def test_just_gaps_contribute_to_score():
    # 0 affected, 0 conflicts, 4 gaps => 20.
    assert calculate_impact_score(_findings(gaps=4)) == 20


def test_combined_score_clamps_to_100():
    # base=100 + 3*10 + 2*5 => 140, clamped to 100.
    score = calculate_impact_score(_findings(affected=50, conflicts=3, gaps=2))
    assert score == 100


def test_negative_counts_coerced_to_zero():
    # Defensive: someone hand-builds findings with negative numbers.
    score = calculate_impact_score(_findings(affected=-5, conflicts=-1, gaps=-2))
    assert score == 0
