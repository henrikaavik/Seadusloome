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

C6 (#791) — sanctions + burden delta:
    Two additional analyzer helpers project the sanctions and burden
    delta over a draft's affected provisions. They sit alongside the
    main :class:`ImpactAnalyzer` rather than inside it because they
    reuse the A1/A2 SPARQL helpers (which already know how to talk to
    Jena, cap row counts, and degrade gracefully on Jena failures),
    so threading them through :meth:`ImpactAnalyzer.analyze` would
    duplicate that work. The result types are kept module-local
    (:class:`SanctionsDelta`, :class:`BurdenDeltaReport`) so legacy
    impact reports without these keys still deserialise cleanly via
    the JSONB ``.get("sanctions_delta", None)`` fallback used by the
    renderer + docx_export.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from app.docs.impact.queries import (
    build_affected_entities_query,
    build_conflicts_query,
    build_eu_compliance_query,
    build_gaps_query,
)
from app.ontology.sparql_client import SparqlClient

if TYPE_CHECKING:
    # Lazy import — at runtime ``app.analyysikeskus.*`` triggers
    # ``app.analyysikeskus.__init__`` which pulls in routes.py, which
    # imports back into ``app.docs.impact``. The TYPE_CHECKING guard
    # gives pyright the type info without the circular runtime cost;
    # the function bodies below import the helpers locally.
    from app.analyysikeskus.burden import BurdenRow
    from app.analyysikeskus.sanctions import SanctionRow

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
        """Run the 2-hop BFS query and shape the results.

        Each row carries a ``relation`` field — the canonical predicate
        URI that linked the entity to the draft reference. C5 uses this
        to render the relation type in legal language; older callers can
        ignore it.

        Wave 2 Step 5A (P2 review follow-up,
        docs/2026-05-18-bugfix-plan.md): the AFFECTED_ENTITIES query
        now unions a ``estleg:referencesAct "<title>"`` LITERAL branch
        on top of the URI-shaped branches. A row coming from that
        branch carries ``?entity`` as a literal act title (e.g.
        ``"Riigieelarve seadus"``) with no ``?label`` or ``?type``. The
        downstream renderer + .docx export expect a homogeneous list
        shape with ``uri`` / ``label`` populated, so we reshape the
        partial-match row here: the act title doubles as both ``uri``
        (the renderer keys off this for the row identity / annotation
        thread) AND ``label`` (the renderer's "Nimetus" column reads
        ``label``). The renderer detects partial-match rows by the
        relation predicate (``referencesAct``) and renders the URI
        column as plain text instead of an explorer link.
        """
        try:
            query = build_affected_entities_query(graph_uri)
            rows = self.client.query(query)
        except Exception as exc:  # noqa: BLE001 — pass-level isolation
            logger.warning("ImpactAnalyzer._find_affected failed: %s", exc)
            return []
        out: list[dict[str, str]] = []
        for row in rows:
            entity = str(row.get("entity", "") or "").strip()
            if not entity:
                continue
            relation = str(row.get("relation", "") or "")
            label = str(row.get("label", "") or "")
            entity_type = str(row.get("type", "") or "")
            # Partial-match (act-level literal) row: the entity itself
            # is the act title — populate ``label`` from it so the
            # "Nimetus" column shows the title verbatim, and leave
            # ``type`` empty so the type column falls back to the
            # short "—". The renderer keys on ``relation`` ==
            # ``…#referencesAct`` to decide whether to render the URI
            # column as a link or as plain text.
            if relation.endswith("referencesAct"):
                if not label:
                    label = entity
                # ``type`` is intentionally left empty — there's no
                # rdf:type for a literal; ``_short_type("")`` returns
                # "—" in the renderer.
            out.append(
                {
                    "uri": entity,
                    "label": label,
                    "type": entity_type,
                    "relation": relation,
                }
            )
        return out

    def _detect_conflicts(self, graph_uri: str) -> list[dict[str, str]]:
        """Run the conflict query and return one dict per hit.

        Each row carries a ``relation`` field for C5 rendering.
        """
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
                "relation": row.get("relation", ""),
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
        """Run the EU compliance query and return one dict per link.

        Each row carries a ``relation`` field — typically
        ``estleg:transposesDirective``, ``estleg:transposedBy``, or
        ``estleg:harmonisedWith`` — so C5 can render the relation type.
        """
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
                "relation": row.get("relation", ""),
            }
            for row in rows
            if row.get("euAct")
        ]


# ---------------------------------------------------------------------------
# C6 (#791) — sanctions delta + burden delta + executive-summary helpers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SanctionsDeltaRow:
    """One sanction row in the impact report's sanctions-delta section.

    Mirrors the SPARQL projection of :class:`app.analyysikeskus.sanctions.SanctionRow`
    flattened to JSON-serialisable primitives so the row can survive a
    round-trip through Postgres JSONB and back into the renderer / docx
    export. The ``change`` field labels the row as new / modified /
    removed for the one-line summary.

    For the v1 implementation we surface every sanction attached to an
    affected provision as a "new" row (``change="new"``) — the draft
    itself does not yet carry sanction-level edges in current data, so
    a richer before/after diff lands in v2 once draft provisions
    declare their own ``estleg:hasSanction`` triples. The dataclass is
    forward-compatible: ``before_*`` fields default empty and are filled
    by v2 without churning callers.
    """

    change: str = "new"  # "new" | "modified" | "removed"
    provision_uri: str = ""
    provision_label: str = ""
    sanction_type: str = ""
    sanction_type_label: str = ""
    penalty_range: str = ""
    min_amount: float | None = None
    max_amount: float | None = None
    min_unit: str = ""
    max_unit: str = ""
    currency: str | None = None
    before_summary: str = ""
    after_summary: str = ""


@dataclass(frozen=True)
class SanctionsDelta:
    """Aggregate sanctions-delta for an impact report.

    Stored in the ``impact_reports.report_data`` JSONB under the
    ``sanctions_delta`` key. Legacy reports without this key resolve
    to ``None`` via the renderer's ``.get("sanctions_delta")`` fallback
    so the page renders unchanged.

    Attributes:
        rows: Flat list of :class:`SanctionsDeltaRow`. The renderer
            shows the first N inline and adds a "Näita rohkem" affordance
            if there's overflow (parity with the other report sections).
        new_count: Count of rows whose ``change == "new"``. Surfaced
            in the one-line summary "{n} uut sanktsiooni · …".
        modified_count: Count of rows whose ``change == "modified"``.
        removed_count: Count of rows whose ``change == "removed"``.
    """

    rows: list[SanctionsDeltaRow] = field(default_factory=list)
    new_count: int = 0
    modified_count: int = 0
    removed_count: int = 0


@dataclass(frozen=True)
class BurdenDeltaRow:
    """One burden row in the impact report's burden-delta section."""

    provision_uri: str = ""
    provision_label: str = ""
    burden_key: str = "unknown"
    burden_label: str = ""
    duty_holder: str = ""


@dataclass(frozen=True)
class BurdenDeltaReport:
    """Aggregate burden-delta for an impact report (C6, #791).

    Wraps the A2 :class:`app.analyysikeskus.burden.BurdenDelta` into a
    JSON-friendly shape with derived totals for the one-line summary
    "{obligations} uut kohustust · {prohibitions} keeldu · {rights}
    õigus — koormus skoor {±N}% vs current law".

    The percent delta (``score_delta_pct``) is rounded to the nearest
    integer and sign-prefixed by the renderer. When the prior law has
    zero burden-bearing rows we surface ``None`` so the renderer can
    show a neutral "—" instead of dividing by zero (the same
    convention the dashboard already uses for missing counters).

    Attributes:
        rows: One :class:`BurdenDeltaRow` per affected provision (the
            "what does this draft touch" view — the same v1 caveat as
            :class:`app.analyysikeskus.burden.BurdenDelta`).
        counts: Per-bucket count of rows (always populated for every
            canonical key so the renderer can iterate the canonical
            order without ``dict.get`` defaults).
        affected_count: Number of distinct provisions the draft
            references / amends.
        before_score: Sum of burden-bearing rows in the prior-law
            baseline (obligation + prohibition). The v1 fallback for
            "burden weight" until a proper score lands in v2.
        after_score: Same metric over the draft's projected post-change
            baseline. ``None`` for v1 (no draft-side normativeType
            edges yet — see :class:`BurdenDelta`).
        score_delta_pct: ``(after - before) / before * 100`` rounded
            to the nearest integer. ``None`` when the baseline is zero
            or ``after_score`` is unavailable.
    """

    rows: list[BurdenDeltaRow] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    affected_count: int = 0
    before_score: int = 0
    after_score: int | None = None
    score_delta_pct: int | None = None


def _sanction_penalty_range(row: SanctionRow) -> str:
    """Render a penalty-range cell text from a :class:`SanctionRow`.

    Mirrors the route-layer rendering for the Sanktsioonide indeks
    workflow so the impact-report row reads identically to its
    Analüüsikeskus counterpart. Examples (Estonian):

      * ``"1 – 5 aastat"`` (imprisonment)
      * ``"100 – 5000 EUR"`` (monetary fine)
      * ``"kuni 5000 EUR"`` (max-only)
      * ``"alates 100 EUR"`` (min-only)
      * ``"—"`` (both bounds missing)
    """
    from app.analyysikeskus.sanctions import sanction_unit_label

    def _fmt(amount: float | None) -> str:
        if amount is None:
            return ""
        if amount == int(amount):
            return str(int(amount))
        return f"{amount:g}"

    min_s = _fmt(row.min_amount)
    max_s = _fmt(row.max_amount)
    unit = sanction_unit_label(row.max_unit or row.min_unit, row.max_currency or row.min_currency)
    unit_suffix = f" {unit}" if unit else ""

    if min_s and max_s:
        if min_s == max_s:
            return f"{min_s}{unit_suffix}".strip()
        return f"{min_s} – {max_s}{unit_suffix}".strip()
    if max_s:
        return f"kuni {max_s}{unit_suffix}".strip()
    if min_s:
        return f"alates {min_s}{unit_suffix}".strip()
    return "—"


def _sanction_summary_text(row: SanctionRow) -> str:
    """One-line "type + range" summary for a sanction row (before/after labels)."""
    from app.analyysikeskus.sanctions import sanction_type_label

    type_label = sanction_type_label(row.sanction_type)
    range_text = _sanction_penalty_range(row)
    if range_text and range_text != "—":
        return f"{type_label}: {range_text}"
    return type_label


def analyze_sanctions_delta(
    draft_uri: str,
    affected_provision_uris: list[str],
    *,
    sparql_client: SparqlClient | None = None,
) -> SanctionsDelta:
    """Return the sanctions delta over a draft's affected provisions.

    Args:
        draft_uri: The draft URI (kept for future v2 use — the v1
            implementation aggregates per-provision Sanction rows
            regardless of the draft's own edges).
        affected_provision_uris: List of provision URIs the draft
            references / amends (typically derived from the
            ``affected_entities`` finding rows). Empty / whitespace
            URIs are skipped so the caller doesn't need to pre-filter.
        sparql_client: Optional :class:`SparqlClient` override (tests
            inject a mocked one).

    Returns:
        A :class:`SanctionsDelta` with one row per Sanction attached to
        an affected provision. v1 marks every row as ``"new"`` — the
        before/after diff is a v2 follow-up. ``modified_count`` /
        ``removed_count`` are always ``0`` in v1.

    Errors are swallowed (per the analyzer's "partial > none" contract);
    a dead Jena returns an empty delta rather than raising.
    """
    if not affected_provision_uris:
        return SanctionsDelta()

    # Lazy import — see TYPE_CHECKING block above for the circular-import rationale.
    from app.analyysikeskus.sanctions import list_sanctions_for_provision

    seen_sanction_keys: set[str] = set()
    out_rows: list[SanctionsDeltaRow] = []
    for raw_uri in affected_provision_uris:
        provision_uri = (raw_uri or "").strip()
        if not provision_uri:
            continue
        try:
            sanction_rows = list_sanctions_for_provision(
                provision_uri, sparql_client=sparql_client
            )
        except Exception as exc:  # noqa: BLE001 — pass-level isolation
            logger.warning(
                "analyze_sanctions_delta: list_sanctions_for_provision failed for %r: %s",
                provision_uri,
                exc,
            )
            continue
        for sanction in sanction_rows:
            # Dedup on (provision, sanction) — the same SanctionRow can
            # surface multiple times if the affected-entities pass reports
            # the same provision via several relations.
            key = f"{sanction.provision_uri}|{sanction.sanction_uri or sanction.sanction_type}"
            if key in seen_sanction_keys:
                continue
            seen_sanction_keys.add(key)
            out_rows.append(_sanction_to_delta_row(sanction))

    new_count = sum(1 for r in out_rows if r.change == "new")
    modified_count = sum(1 for r in out_rows if r.change == "modified")
    removed_count = sum(1 for r in out_rows if r.change == "removed")
    return SanctionsDelta(
        rows=out_rows,
        new_count=new_count,
        modified_count=modified_count,
        removed_count=removed_count,
    )


def _sanction_to_delta_row(sanction: SanctionRow) -> SanctionsDeltaRow:
    """Project a :class:`SanctionRow` into a :class:`SanctionsDeltaRow`."""
    from app.analyysikeskus.sanctions import sanction_type_label

    after_summary = _sanction_summary_text(sanction)
    return SanctionsDeltaRow(
        change="new",
        provision_uri=sanction.provision_uri,
        provision_label=sanction.provision_label or sanction.provision_uri,
        sanction_type=sanction.sanction_type,
        sanction_type_label=sanction_type_label(sanction.sanction_type),
        penalty_range=_sanction_penalty_range(sanction),
        min_amount=sanction.min_amount,
        max_amount=sanction.max_amount,
        min_unit=sanction.min_unit,
        max_unit=sanction.max_unit,
        currency=sanction.max_currency or sanction.min_currency,
        before_summary="",
        after_summary=after_summary,
    )


def analyze_burden_delta(
    draft_uri: str,
    *,
    sparql_client: SparqlClient | None = None,
) -> BurdenDeltaReport:
    """Return the burden delta for a draft in impact-report-friendly form.

    Wraps :func:`app.analyysikeskus.burden.burden_delta_for_draft` with
    the additional fields the impact-report renderer needs (per-bucket
    counts in JSON-friendly form, a burden score, and a percent-delta).

    Args:
        draft_uri: The ``DraftLegislation`` URI. An empty URI yields an
            empty :class:`BurdenDeltaReport` with all zero counters.
        sparql_client: Optional :class:`SparqlClient` override.

    Returns:
        A :class:`BurdenDeltaReport`. v1 leaves ``after_score`` /
        ``score_delta_pct`` as ``None`` because draft provisions do
        not yet carry their own ``normativeType`` edges — see the
        :class:`BurdenDelta` docstring for the v2 backfill dependency.
    """
    uri = (draft_uri or "").strip()
    if not uri:
        return BurdenDeltaReport(counts=_empty_burden_counts())

    # Lazy import — see TYPE_CHECKING block above.
    from app.analyysikeskus.burden import burden_delta_for_draft

    try:
        delta = burden_delta_for_draft(uri, sparql_client=sparql_client)
    except Exception as exc:  # noqa: BLE001 — pass-level isolation
        logger.warning("analyze_burden_delta: burden_delta_for_draft failed: %s", exc)
        return BurdenDeltaReport(counts=_empty_burden_counts())

    before = delta.before
    rows = [_burden_to_delta_row(r) for r in before.rows]
    # Burden-bearing rows = obligations + prohibitions (rights / permissions
    # are burden-relieving; "unknown" doesn't contribute to the score).
    counts = {str(k): int(v) for k, v in before.counts.items()}
    before_score = int(counts.get("obligation", 0)) + int(counts.get("prohibition", 0))
    return BurdenDeltaReport(
        rows=rows,
        counts=counts,
        affected_count=delta.affected_count,
        before_score=before_score,
        # v1: after_score / score_delta_pct stay None until ontology issue
        # #214's data backfill populates draft-side normativeType edges.
        after_score=None,
        score_delta_pct=None,
    )


def _burden_to_delta_row(row: BurdenRow) -> BurdenDeltaRow:
    from app.analyysikeskus.burden import burden_label

    return BurdenDeltaRow(
        provision_uri=row.provision_uri,
        provision_label=row.provision_label or row.provision_uri,
        burden_key=str(row.burden_key),
        burden_label=burden_label(row.burden_key),
        duty_holder=row.duty_holder,
    )


def _empty_burden_counts() -> dict[str, int]:
    """Return the canonical zero-filled bucket counts dict (JSON-friendly keys)."""
    return {
        "obligation": 0,
        "prohibition": 0,
        "permission": 0,
        "right": 0,
        "unknown": 0,
    }


__all__ = [
    "ImpactAnalyzer",
    "ImpactFindings",
    "SanctionsDelta",
    "SanctionsDeltaRow",
    "BurdenDeltaReport",
    "BurdenDeltaRow",
    "analyze_sanctions_delta",
    "analyze_burden_delta",
]
