"""Impact Analysis Engine (Phase 2 Batch 3).

The impact analyser runs SPARQL queries against the union of the
default graph (enacted laws) and a draft's named graph to produce an
:class:`~app.docs.impact.analyzer.ImpactFindings` structure:

* **Affected entities** — 2-hop BFS from every draft reference
* **Conflicts** — rule-based overlaps with other drafts / court
  decisions that interpret the same provisions
* **EU compliance** — EU legislation transposed by referenced
  provisions
* **Gaps** — topic clusters the draft touches without referencing
  their core concepts

The entry point is :class:`ImpactAnalyzer`; the scoring helper lives
in :mod:`app.docs.impact.scoring`.
"""

from app.docs.impact.analyzer import ImpactAnalyzer, ImpactFindings
from app.docs.impact.scoring import calculate_impact_score

__all__ = [
    "ImpactAnalyzer",
    "ImpactFindings",
    "calculate_impact_score",
]
