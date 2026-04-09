"""Run the impact analysis passes and return a findings structure.

:class:`ImpactAnalyzer` is a thin orchestrator: each analysis pass is
a private method that executes a SPARQL query from
:mod:`app.docs.impact.queries` and parses the result into a list of
dicts. Every pass is wrapped in a try/except so a failure in one pass
(e.g. a malformed predicate URI) does not kill the whole analysis —
partial results are strictly better than no results, and the user can
re-run the pipeline once the underlying issue is fixed.

The analyzer never touches Postgres itself; persistence of the
findings lives in :func:`app.docs.analyze_handler.analyze_impact`.
That separation keeps the analyzer unit-testable against a fake
``SparqlClient`` without needing a DB fixture.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from app.docs.impact.queries import (
    build_affected_entities_query,
    build_conflicts_query,
    build_eu_compliance_query,
    build_gaps_query,
)
from app.ontology.sparql_client import SparqlClient

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ImpactFindings:
    """Structured output of :meth:`ImpactAnalyzer.analyze`.

    Every list contains plain ``dict[str, str]`` rows so the whole
    structure is trivially JSON-serialisable for the ``impact_reports.
    report_data`` column. The counts are stored separately (rather
    than computed from the lists on the fly) because the SQL
    aggregates ``affected_count`` / ``conflict_count`` / ``gap_count``
    are kept in their own columns for fast dashboard queries.
    """

    affected_entities: list[dict[str, str]] = field(default_factory=list)
    conflicts: list[dict[str, str]] = field(default_factory=list)
    gaps: list[dict[str, str]] = field(default_factory=list)
    eu_compliance: list[dict[str, str]] = field(default_factory=list)
    affected_count: int = 0
    conflict_count: int = 0
    gap_count: int = 0


class ImpactAnalyzer:
    """Run all analysis passes against a draft's named graph."""

    def __init__(self, sparql_client: SparqlClient | None = None) -> None:
        self.client = sparql_client if sparql_client is not None else SparqlClient()

    def analyze(self, draft_graph_uri: str) -> ImpactFindings:
        """Execute every pass and return the aggregated findings.

        Args:
            draft_graph_uri: The Jena named graph URI for the draft.
                Must already be loaded into Jena — the analyzer does
                not put the graph itself (that is the responsibility
                of :func:`app.docs.analyze_handler.analyze_impact`).

        Returns:
            An :class:`ImpactFindings` populated with whatever each
            pass produced. Failed passes contribute empty lists and
            zero counts rather than raising.
        """
        logger.info("ImpactAnalyzer.analyze start graph=%s", draft_graph_uri)

        affected = self._find_affected(draft_graph_uri)
        conflicts = self._detect_conflicts(draft_graph_uri)
        gaps = self._analyze_gaps(draft_graph_uri)
        eu_compliance = self._check_eu_compliance(draft_graph_uri)

        findings = ImpactFindings(
            affected_entities=affected,
            conflicts=conflicts,
            gaps=gaps,
            eu_compliance=eu_compliance,
            affected_count=len(affected),
            conflict_count=len(conflicts),
            gap_count=len(gaps),
        )
        logger.info(
            "ImpactAnalyzer.analyze done graph=%s affected=%d conflicts=%d gaps=%d eu=%d",
            draft_graph_uri,
            findings.affected_count,
            findings.conflict_count,
            findings.gap_count,
            len(findings.eu_compliance),
        )
        return findings

    # ------------------------------------------------------------------
    # Individual passes
    # ------------------------------------------------------------------

    def _find_affected(self, graph_uri: str) -> list[dict[str, str]]:
        """Run the 2-hop BFS query and shape the results."""
        try:
            query = build_affected_entities_query(graph_uri)
            rows = self.client.query(query)
        except Exception as exc:  # noqa: BLE001 — pass-level isolation
            logger.warning("ImpactAnalyzer._find_affected failed: %s", exc)
            return []
        return [
            {
                "uri": row.get("entity", ""),
                "label": row.get("label", ""),
                "type": row.get("type", ""),
            }
            for row in rows
            if row.get("entity")
        ]

    def _detect_conflicts(self, graph_uri: str) -> list[dict[str, str]]:
        """Run the conflict query and return one dict per hit."""
        try:
            query = build_conflicts_query(graph_uri)
            rows = self.client.query(query)
        except Exception as exc:  # noqa: BLE001
            logger.warning("ImpactAnalyzer._detect_conflicts failed: %s", exc)
            return []
        return [
            {
                "draft_ref": row.get("draftRef", ""),
                "conflicting_entity": row.get("conflictEntity", ""),
                "conflicting_label": row.get("conflictLabel", ""),
                "reason": row.get("reason", ""),
            }
            for row in rows
            if row.get("draftRef")
        ]

    def _analyze_gaps(self, graph_uri: str) -> list[dict[str, str]]:
        """Run the gap-analysis query and return one dict per cluster."""
        try:
            query = build_gaps_query(graph_uri)
            rows = self.client.query(query)
        except Exception as exc:  # noqa: BLE001
            logger.warning("ImpactAnalyzer._analyze_gaps failed: %s", exc)
            return []
        out: list[dict[str, str]] = []
        for row in rows:
            cluster = row.get("cluster")
            if not cluster:
                continue
            out.append(
                {
                    "topic_cluster": cluster,
                    "topic_cluster_label": row.get("clusterLabel", ""),
                    "total_provisions": row.get("totalProvisions", "0"),
                    "referenced_provisions": row.get("referencedProvisions", "0"),
                    "description": (
                        f"Draft references {row.get('referencedProvisions', '0')} of "
                        f"{row.get('totalProvisions', '0')} provisions in this cluster"
                    ),
                }
            )
        return out

    def _check_eu_compliance(self, graph_uri: str) -> list[dict[str, str]]:
        """Run the EU compliance query and return one dict per link."""
        try:
            query = build_eu_compliance_query(graph_uri)
            rows = self.client.query(query)
        except Exception as exc:  # noqa: BLE001
            logger.warning("ImpactAnalyzer._check_eu_compliance failed: %s", exc)
            return []
        return [
            {
                "eu_act": row.get("euAct", ""),
                "eu_label": row.get("euLabel", ""),
                "estonian_provision": row.get("estonianProvision", ""),
                "provision_label": row.get("provisionLabel", ""),
                "transposition_status": "linked",
            }
            for row in rows
            if row.get("euAct")
        ]
