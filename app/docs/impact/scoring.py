"""Impact score calculation for an :class:`ImpactFindings` result.

The score is a 0-100 integer that summarises the magnitude of the
impact a draft has on the legal ontology. It is surfaced in the
report header as a gauge and drives the colour coding of the drafts
list page:

    0-20    Low impact (routine amendment)
    21-50   Medium impact (requires review)
    51-80   High impact (significant review needed)
    81-100  Critical impact (major legislative change)

Formula (Phase 2 spec §8.5 — simplified variant for Batch 3):

    base              = min(100, affected_count * 2)
    conflict_penalty  = conflict_count * 10
    gap_penalty       = gap_count * 5
    score             = min(100, base + conflict_penalty + gap_penalty)

Notes on the simplification:

* The spec's full formula weights high/medium/low conflicts
  separately; the analyzer in this batch does not yet classify
  conflicts by severity so we use a single weight of 10 per
  conflict — biased high on purpose so any conflict draws attention.
* EU issues are not counted in the base score for Batch 3 because the
  analyzer only links EU instruments, it does not detect missing
  transpositions. Phase 3 will add that pass and start contributing
  to the score.
* The base coefficient ``* 2`` means a draft needs ~50 affected
  entities to saturate the base alone, which matches the "medium
  impact" band in the spec.
"""

from __future__ import annotations

from app.docs.impact.analyzer import ImpactFindings


def calculate_impact_score(findings: ImpactFindings) -> int:
    """Return a 0-100 integer summarising the draft's impact.

    Args:
        findings: The output of :meth:`ImpactAnalyzer.analyze`. The
            function only reads the three count fields
            (``affected_count``, ``conflict_count``, ``gap_count``)
            so it can be used with partial or hand-built
            ``ImpactFindings`` instances in tests.

    Returns:
        An integer in ``[0, 100]``. Both the base and the final
        score are clamped so arbitrarily large finding lists never
        produce values outside the gauge range.
    """
    affected = max(0, int(findings.affected_count))
    conflicts = max(0, int(findings.conflict_count))
    gaps = max(0, int(findings.gap_count))

    base = min(100, affected * 2)
    conflict_penalty = conflicts * 10
    gap_penalty = gaps * 5

    score = base + conflict_penalty + gap_penalty
    return min(100, max(0, score))
