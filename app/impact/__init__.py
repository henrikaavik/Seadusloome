"""Impact Analysis Engine — the neutral legal-analysis layer (#860).

The impact analyser runs SPARQL queries against the union of the
default graph (enacted laws) and a draft's named graph to produce an
:class:`~app.impact.analyzer.ImpactFindings` structure:

* **Affected entities** — 2-hop BFS from every draft reference
* **Conflicts** — rule-based overlaps with other drafts / court
  decisions that interpret the same provisions
* **EU compliance** — EU legislation transposed by referenced
  provisions
* **Gaps** — topic clusters the draft touches without referencing
  their core concepts

The entry point is :class:`ImpactAnalyzer`; the scoring helper lives
in :mod:`app.impact.scoring`.

This package is the **neutral** analysis layer: it depends only on
``app.ontology`` / ``app.sync`` and is imported *by* the
``app.analyysikeskus`` workflow hub and the ``app.docs`` document
pipeline, never the reverse. The Halduskoormus / Sanktsioonide-indeks
SPARQL data layers (:mod:`app.impact.burden`, :mod:`app.impact.sanctions`)
were promoted here from ``app.analyysikeskus`` so the analyzer's C6
sanctions/burden-delta helpers can consume them with normal top-level
imports — the former ``analyysikeskus`` ←→ impact-engine import cycle
is gone. The Estonian workflow / UI / rendering stays in
``app.analyysikeskus``.
"""

from app.impact.analyzer import ImpactAnalyzer, ImpactFindings
from app.impact.scoring import calculate_impact_score

__all__ = [
    "ImpactAnalyzer",
    "ImpactFindings",
    "calculate_impact_score",
]
