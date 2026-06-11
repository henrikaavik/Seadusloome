"""Orchestration for the "Analüüsi poliitikamõttest" workflow (#814).

The Phase 2b route handler (held out of this PR to avoid a merge
conflict with #805/#815 on ``routes.py``) will call these helpers in
sequence:

    1. ``prepare_intent_form_context()`` — chip lists for the intake
       form (target groups, affected areas).
    2. User submits free-text intent + optional chips + optional manual
       refs. The handler calls
       :func:`extract_candidates` → LLM-driven semantic inference via
       :mod:`app.analyysikeskus.intent_extractor`.
    3. :func:`resolve_candidates` wraps each candidate as an
       :class:`~app.docs.entity_extractor.ExtractedRef` and feeds them
       to the existing :class:`~app.docs.reference_resolver.ReferenceResolver`
       — same path the proven workflows use.
    4. The user **confirms** the resolved candidates in the UI (the
       confirmation step is the only guardrail against LLM hallucination
       — designed in as a non-negotiable per the user's MVP direction).
    5. :func:`run_aggregated_analysis` loops over the confirmed entity
       URIs, calls :func:`app.analyysikeskus.adhoc_analysis.run_adhoc_impact_analysis`
       per URI, and packages the results with per-URI attribution so
       the final report says *"this mõjuahel comes from the analysis
       of provision X"*.

Why per-URI instead of one composite ephemeral graph: the user
explicitly rejected a multi-URI ephemeral graph for the MVP. A composite
graph would blur accountability — when the user later asks *"why does
this conflict appear?"*, the answer must trace back to exactly one
confirmed input. The per-URI loop preserves that traceability.

This module is intentionally Postgres-free. It calls Jena (via
``run_adhoc_impact_analysis``) and the LLM (via ``extract_intent_candidates``)
but never touches PostgreSQL — the route handler in Phase 2b is the
layer that decides whether to persist anything (today's MVP doesn't).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Protocol
from uuid import UUID

from app.analyysikeskus.adhoc_analysis import AdhocAnalysisResult, run_adhoc_impact_analysis
from app.analyysikeskus.intent_extractor import IntentCandidate, extract_intent_candidates
from app.docs.entity_extractor import ExtractedRef
from app.docs.reference_resolver import ReferenceResolver, ResolvedRef
from app.impact import ImpactAnalyzer
from app.llm import LLMProvider

logger = logging.getLogger(__name__)


class _ResolverLike(Protocol):
    """Structural type for the dependency :func:`resolve_candidates` needs.

    The orchestrator only calls ``resolve(list[ExtractedRef])``, so we
    don't require a full :class:`ReferenceResolver` — any object with
    that one method satisfies the contract. Lets tests inject minimal
    stubs without inheriting from the real resolver (which would force
    a SPARQL client at construction time).
    """

    def resolve(self, refs: list[ExtractedRef]) -> list[ResolvedRef]: ...


# ---------------------------------------------------------------------------
# Form chip vocabularies
# ---------------------------------------------------------------------------
#
# The intake form offers two optional chip groups so the user can give
# the LLM extra context without typing it as free text. The vocabularies
# are deliberately small (≤8 entries each) to keep the UI scannable;
# adding rows here is the only change required to extend either group.

# Target-group chips — the population a policy intent typically targets.
# Ordered roughly by how often Social-Ministry lawyers cite each one in
# the usability-testing transcripts (``docs/2026-05-18-social-ministry-
# usability-testing-plan.md``).
_DEFAULT_TARGET_GROUPS: tuple[str, ...] = (
    "Lapsed",
    "Eakad",
    "Puuetega inimesed",
    "Töötajad",
    "Pered",
    "Ettevõtjad",
    "KOV-id",
    "Riigiasutused",
)

# Affected-area chips — the legal/policy field the intent most plausibly
# touches. We keep them broad and let the LLM map them to concrete acts.
_DEFAULT_AFFECTED_AREAS: tuple[str, ...] = (
    "Sotsiaalhoolekanne",
    "Tervishoid",
    "Tööõigus",
    "Maksuõigus",
    "Andmekaitse",
    "Haridus",
    "Kohaliku omavalitsuse korraldus",
    "Riigihange",
)


@dataclass(frozen=True)
class FormContext:
    """Chip lists rendered above the intake form.

    Attributes:
        target_groups: Optional chip labels — the population a policy
            intent typically targets (lapsed, eakad, ...).
        affected_areas: Optional chip labels — the legal/policy area
            the intent most plausibly touches (sotsiaalhoolekanne,
            tervishoid, ...).
    """

    target_groups: tuple[str, ...]
    affected_areas: tuple[str, ...]


@dataclass(frozen=True)
class ResolvedCandidate:
    """A single candidate after the resolver has tried to find a URI.

    The UI lays these out as confirm/remove rows. A candidate with
    ``entity_uri is None`` and ``partial_match is None`` is fully
    unresolved — the user can still confirm it but the per-URI
    analyser cannot run on it; the route surfaces that with a muted
    "ei tuvastatud ontoloogias" badge and a manual-edit affordance.

    Attributes:
        candidate: The original :class:`IntentCandidate` the LLM
            proposed, kept alongside the resolution so the UI has
            both the reasoning AND the matched label.
        resolved: The resolver output. ``entity_uri`` is the URI the
            per-URI analyser will use; ``matched_label`` is the
            human-readable label to show in the confirmation row.
    """

    candidate: IntentCandidate
    resolved: ResolvedRef


@dataclass(frozen=True)
class PerUriResult:
    """One per-URI analysis result with its source attribution.

    Attributes:
        entity_uri: The confirmed entity URI this analysis was run on.
        source_label: Human-readable label of the source — e.g.
            ``"AvTS § 35"`` — drawn from the resolver's
            ``matched_label`` so the UI says *"this mõjuahel comes
            from the analysis of AvTS § 35"*.
        adhoc: The :class:`AdhocAnalysisResult` returned by
            :func:`run_adhoc_impact_analysis`. ``findings`` is empty
            (zero counts, empty lists) on a Jena failure; the
            traceability metadata is still meaningful.
    """

    entity_uri: str
    source_label: str
    adhoc: AdhocAnalysisResult


@dataclass(frozen=True)
class AggregatedResult:
    """The full aggregated result of an intent-driven analysis run.

    Attributes:
        per_uri: One :class:`PerUriResult` per confirmed entity URI,
            preserving input order so the UI can render the findings
            grouped by the source the user confirmed.
        total_affected: Sum of ``affected_count`` across all per-URI
            findings. Surfaced as a single headline number.
        total_conflicts: Sum of ``conflict_count`` across all per-URI
            findings.
        total_gaps: Sum of ``gap_count`` across all per-URI findings.
        message: Optional friendly message for the empty / failure
            states. ``None`` on a successful aggregation.
    """

    per_uri: list[PerUriResult] = field(default_factory=list)
    total_affected: int = 0
    total_conflicts: int = 0
    total_gaps: int = 0
    message: str | None = None


# ---------------------------------------------------------------------------
# Form context
# ---------------------------------------------------------------------------


def prepare_intent_form_context() -> FormContext:
    """Return the chip lists rendered above the intake form.

    Stateless helper — the chip lists are module constants today. Kept
    as a function (not a constant export) so a future iteration can
    swap to a SPARQL-backed vocabulary (e.g. distinct
    ``estleg:targetGroup`` values) without changing the call site in
    the route.
    """
    return FormContext(
        target_groups=_DEFAULT_TARGET_GROUPS,
        affected_areas=_DEFAULT_AFFECTED_AREAS,
    )


# ---------------------------------------------------------------------------
# Extract
# ---------------------------------------------------------------------------


def extract_candidates(
    intent_text: str,
    *,
    provider: LLMProvider | None = None,
    user_id: UUID | str | None = None,
    org_id: UUID | str | None = None,
) -> list[IntentCandidate]:
    """Run the semantic-inference extractor over the policy intent text.

    Thin wrapper around
    :func:`app.analyysikeskus.intent_extractor.extract_intent_candidates`.
    Lives here (not in the extractor module) so the route only depends
    on one orchestration entry point — the extractor stays a focused
    "LLM + prompt + parse" module.

    Args:
        intent_text: User's plain-language policy intent.
        provider: Optional :class:`LLMProvider` override (tests inject a
            ``MagicMock``).
        user_id: Optional user id forwarded to the LLM call for cost
            attribution.
        org_id: Optional org id forwarded for cost attribution.

    Returns:
        Deduplicated :class:`IntentCandidate` list, or ``[]`` on empty
        input / LLM failure.
    """
    return extract_intent_candidates(
        intent_text,
        provider=provider,
        user_id=user_id,
        org_id=org_id,
    )


# ---------------------------------------------------------------------------
# Resolve
# ---------------------------------------------------------------------------


def resolve_candidates(
    candidates: list[IntentCandidate],
    *,
    resolver: _ResolverLike | None = None,
) -> list[ResolvedCandidate]:
    """Resolve each candidate to an ontology URI (or ``None``).

    Wraps each :class:`IntentCandidate` as an :class:`ExtractedRef`
    (the resolver only speaks ``ExtractedRef``) and feeds the list to
    :meth:`ReferenceResolver.resolve` in one batch. The resolver caches
    its abbreviation map per instance, so one batched call is cheaper
    than N single calls.

    A dead Jena / crashing resolver yields unresolved entries (every
    candidate comes back with ``entity_uri=None``, ``match_score=0.0``)
    rather than raising — the route then surfaces a manual-add UI so
    the user is never stuck.

    Args:
        candidates: Output of :func:`extract_candidates`.
        resolver: Optional :class:`ReferenceResolver` override (tests
            inject a stub). Defaults to a fresh instance, which is
            cheap because the abbreviation map only loads on first
            ``resolve()`` call.

    Returns:
        One :class:`ResolvedCandidate` per input candidate, preserving
        input order so the UI can keep them aligned with what the LLM
        produced.
    """
    if not candidates:
        return []

    # Wrap each IntentCandidate as an ExtractedRef so the resolver can
    # consume them. ``confidence`` rides through; ``location`` is empty
    # (the resolver only reads it for log breadcrumbs, never for logic).
    extracted_refs: list[ExtractedRef] = [
        ExtractedRef(
            ref_text=cand.ref_text,
            ref_type=cand.ref_type,
            confidence=cand.confidence,
        )
        for cand in candidates
    ]

    runner = resolver if resolver is not None else ReferenceResolver()
    try:
        resolved_refs: list[ResolvedRef] = runner.resolve(extracted_refs)
    except Exception:
        logger.warning(
            "resolve_candidates: resolver failed for %d candidates; returning unresolved entries",
            len(candidates),
            exc_info=True,
        )
        # Build a list of "unresolved" entries so the route can still
        # render the candidate set with a "ei tuvastatud" badge.
        return [
            ResolvedCandidate(
                candidate=cand,
                resolved=ResolvedRef(
                    extracted=ExtractedRef(
                        ref_text=cand.ref_text,
                        ref_type=cand.ref_type,
                        confidence=cand.confidence,
                    ),
                    entity_uri=None,
                    matched_label=None,
                    match_score=0.0,
                ),
            )
            for cand in candidates
        ]

    # zip is safe — ``ReferenceResolver.resolve`` preserves input order.
    return [
        ResolvedCandidate(candidate=cand, resolved=res)
        for cand, res in zip(candidates, resolved_refs, strict=True)
    ]


# ---------------------------------------------------------------------------
# Aggregate
# ---------------------------------------------------------------------------


def run_aggregated_analysis(
    confirmed_uris: list[str],
    *,
    source_labels: dict[str, str] | None = None,
    analyzer: ImpactAnalyzer | None = None,
) -> AggregatedResult:
    """Run :func:`run_adhoc_impact_analysis` per URI and aggregate.

    Per the MVP design, each confirmed URI gets its **own** ephemeral
    synthetic graph + analysis run. The aggregation here is mechanical
    (sum of counts, list of per-URI results); the actual findings are
    computed by the proven per-URI analyser.

    Args:
        confirmed_uris: List of ontology entity URIs the user confirmed
            in the UI. Empty / all-blank inputs short-circuit to an
            empty result with a friendly Estonian message.
        source_labels: Optional ``{entity_uri -> human-readable label}``
            map drawn from the resolver's ``matched_label`` (e.g.
            ``"AvTS § 35"``). Missing keys fall back to the URI itself
            so the UI always has something to render.
        analyzer: Optional :class:`ImpactAnalyzer` override forwarded
            to each :func:`run_adhoc_impact_analysis` call. Tests inject
            a stub whose ``analyze`` returns canned findings.

    Returns:
        An :class:`AggregatedResult` with one :class:`PerUriResult` per
        URI, the total counts summed across all URIs, and an optional
        ``message`` for the empty-input case.
    """
    # Trim + drop empties up front so the headline counts reflect what
    # actually ran. Preserve original order — the UI groups by source.
    cleaned_uris: list[str] = []
    seen: set[str] = set()
    for uri in confirmed_uris:
        if not uri or not uri.strip():
            continue
        uri_stripped = uri.strip()
        if uri_stripped in seen:
            # Defensive: a UI bug could submit the same URI twice; we
            # don't want to double-count its findings.
            continue
        seen.add(uri_stripped)
        cleaned_uris.append(uri_stripped)

    if not cleaned_uris:
        return AggregatedResult(
            message="Kinnitatud viidete loend on tühi — vali vähemalt üks viide.",
        )

    labels = source_labels or {}

    per_uri: list[PerUriResult] = []
    total_affected = 0
    total_conflicts = 0
    total_gaps = 0

    for uri in cleaned_uris:
        adhoc = run_adhoc_impact_analysis(uri, analyzer=analyzer)
        per_uri.append(
            PerUriResult(
                entity_uri=uri,
                source_label=labels.get(uri, uri),
                adhoc=adhoc,
            )
        )
        total_affected += adhoc.findings.affected_count
        total_conflicts += adhoc.findings.conflict_count
        total_gaps += adhoc.findings.gap_count

    return AggregatedResult(
        per_uri=per_uri,
        total_affected=total_affected,
        total_conflicts=total_conflicts,
        total_gaps=total_gaps,
    )
