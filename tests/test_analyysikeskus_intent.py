"""Unit tests for ``app.analyysikeskus.intent_analysis`` (#814 prep).

Covers the orchestration helpers that bridge:
    intent text
        -> intent_extractor.extract_intent_candidates()
        -> ReferenceResolver.resolve()
        -> per-URI run_adhoc_impact_analysis()

These tests do NOT hit Jena, do NOT hit Anthropic, and do NOT touch
PostgreSQL. Everything is stubbed at the module boundary:

* ``extract_candidates`` — patched at the ``intent_extractor`` boundary.
* ``resolve_candidates`` — passed a stub resolver with a canned
  ``resolve()`` method.
* ``run_aggregated_analysis`` — patched at the ``run_adhoc_impact_analysis``
  call site so we don't hit Jena.

The flow integration test composes them all to assert end-to-end
shape without paying network costs.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from app.analyysikeskus.adhoc_analysis import AdhocAnalysisResult
from app.analyysikeskus.intent_analysis import (
    AggregatedResult,
    FormContext,
    PerUriResult,
    ResolvedCandidate,
    extract_candidates,
    prepare_intent_form_context,
    resolve_candidates,
    run_aggregated_analysis,
)
from app.analyysikeskus.intent_extractor import IntentCandidate
from app.docs.entity_extractor import ExtractedRef
from app.docs.impact import ImpactFindings
from app.docs.reference_resolver import ResolvedRef

# ---------------------------------------------------------------------------
# Form context
# ---------------------------------------------------------------------------


class TestPrepareIntentFormContext:
    def test_returns_form_context_dataclass(self):
        ctx = prepare_intent_form_context()
        assert isinstance(ctx, FormContext)

    def test_target_groups_are_estonian_strings(self):
        ctx = prepare_intent_form_context()
        # Non-empty, all-Estonian chip labels.
        assert len(ctx.target_groups) >= 4
        assert "Lapsed" in ctx.target_groups
        assert "Eakad" in ctx.target_groups
        assert "Puuetega inimesed" in ctx.target_groups
        for chip in ctx.target_groups:
            assert isinstance(chip, str)
            assert chip.strip(), "Target-group chip must be non-empty"

    def test_affected_areas_are_estonian_strings(self):
        ctx = prepare_intent_form_context()
        assert len(ctx.affected_areas) >= 4
        assert "Sotsiaalhoolekanne" in ctx.affected_areas
        assert "Andmekaitse" in ctx.affected_areas
        for chip in ctx.affected_areas:
            assert isinstance(chip, str)
            assert chip.strip(), "Affected-area chip must be non-empty"

    def test_chip_lists_are_tuples_for_immutability(self):
        """FormContext is frozen so callers can safely cache it."""
        ctx = prepare_intent_form_context()
        assert isinstance(ctx.target_groups, tuple)
        assert isinstance(ctx.affected_areas, tuple)


# ---------------------------------------------------------------------------
# extract_candidates wrapper
# ---------------------------------------------------------------------------


class TestExtractCandidates:
    @patch("app.analyysikeskus.intent_analysis.extract_intent_candidates")
    def test_forwards_to_intent_extractor(self, mock_extract: MagicMock):
        """``extract_candidates`` is a thin pass-through to the extractor."""
        mock_extract.return_value = [
            IntentCandidate(
                ref_text="PISTS § 4",
                ref_type="provision",
                confidence=0.9,
                reasoning="põhinorm",
            )
        ]
        provider = MagicMock()

        out = extract_candidates(
            "Soovin lihtsustada toetuse taotlemist.",
            provider=provider,
            user_id="user-1",
            org_id="org-1",
        )

        mock_extract.assert_called_once_with(
            "Soovin lihtsustada toetuse taotlemist.",
            provider=provider,
            user_id="user-1",
            org_id="org-1",
        )
        assert len(out) == 1
        assert out[0].ref_text == "PISTS § 4"


# ---------------------------------------------------------------------------
# resolve_candidates
# ---------------------------------------------------------------------------


class _StubResolver:
    """Minimal :class:`ReferenceResolver` stand-in for the resolve tests.

    Only implements ``resolve()`` since that's the only method
    ``resolve_candidates`` calls.
    """

    def __init__(self, mapping: dict[str, str | None]) -> None:
        self._mapping = mapping

    def resolve(self, refs: list[ExtractedRef]) -> list[ResolvedRef]:
        out: list[ResolvedRef] = []
        for ref in refs:
            entity_uri = self._mapping.get(ref.ref_text)
            out.append(
                ResolvedRef(
                    extracted=ref,
                    entity_uri=entity_uri,
                    matched_label=ref.ref_text if entity_uri else None,
                    match_score=1.0 if entity_uri else 0.0,
                )
            )
        return out


class TestResolveCandidates:
    def test_empty_input_returns_empty(self):
        assert resolve_candidates([], resolver=_StubResolver({})) == []

    def test_resolves_each_candidate_via_stub(self):
        candidates = [
            IntentCandidate(
                ref_text="PISTS § 4",
                ref_type="provision",
                confidence=0.9,
                reasoning="r1",
            ),
            IntentCandidate(
                ref_text="Unknown § 99",
                ref_type="provision",
                confidence=0.4,
                reasoning="r2",
            ),
        ]
        stub = _StubResolver(
            mapping={
                "PISTS § 4": "https://data.riik.ee/ontology/estleg#PISTS_Par_4",
                "Unknown § 99": None,
            }
        )

        results = resolve_candidates(candidates, resolver=stub)

        assert len(results) == 2
        # Order preserved.
        assert results[0].candidate.ref_text == "PISTS § 4"
        assert results[0].resolved.entity_uri == "https://data.riik.ee/ontology/estleg#PISTS_Par_4"
        assert results[1].candidate.ref_text == "Unknown § 99"
        assert results[1].resolved.entity_uri is None

    def test_wraps_candidate_as_extracted_ref_for_resolver(self):
        """The resolver only speaks ExtractedRef — we must wrap correctly."""

        captured: list[ExtractedRef] = []

        class _CapturingResolver:
            def resolve(self, refs: list[ExtractedRef]) -> list[ResolvedRef]:
                captured.extend(refs)
                return [
                    ResolvedRef(
                        extracted=r,
                        entity_uri=None,
                        matched_label=None,
                        match_score=0.0,
                    )
                    for r in refs
                ]

        candidates = [
            IntentCandidate(
                ref_text="PISTS § 4",
                ref_type="provision",
                confidence=0.85,
                reasoning="x",
            )
        ]
        resolve_candidates(candidates, resolver=_CapturingResolver())

        assert len(captured) == 1
        wrapped = captured[0]
        assert isinstance(wrapped, ExtractedRef)
        assert wrapped.ref_text == "PISTS § 4"
        assert wrapped.ref_type == "provision"
        assert wrapped.confidence == 0.85

    def test_resolver_failure_returns_unresolved_entries(self):
        """A crashing resolver yields unresolved entries rather than raising."""

        class _BoomResolver:
            def resolve(self, refs: list[ExtractedRef]) -> list[ResolvedRef]:
                raise RuntimeError("Jena timeout")

        candidates = [
            IntentCandidate(
                ref_text="PISTS § 4",
                ref_type="provision",
                confidence=0.9,
                reasoning="r",
            )
        ]
        results = resolve_candidates(candidates, resolver=_BoomResolver())

        assert len(results) == 1
        assert isinstance(results[0], ResolvedCandidate)
        assert results[0].resolved.entity_uri is None
        assert results[0].resolved.match_score == 0.0
        assert results[0].candidate.ref_text == "PISTS § 4"


# ---------------------------------------------------------------------------
# run_aggregated_analysis
# ---------------------------------------------------------------------------


def _stub_findings(affected: int, conflicts: int, gaps: int) -> ImpactFindings:
    return ImpactFindings(
        affected_entities=[{"label": f"affected-{i}"} for i in range(affected)],
        conflicts=[{"label": f"conflict-{i}"} for i in range(conflicts)],
        gaps=[{"label": f"gap-{i}"} for i in range(gaps)],
        affected_count=affected,
        conflict_count=conflicts,
        gap_count=gaps,
    )


class TestRunAggregatedAnalysis:
    def test_empty_uri_list_returns_friendly_message(self):
        result = run_aggregated_analysis([])
        assert isinstance(result, AggregatedResult)
        assert result.per_uri == []
        assert result.total_affected == 0
        assert result.total_conflicts == 0
        assert result.total_gaps == 0
        assert result.message is not None
        assert "tühi" in result.message.lower() or "vali" in result.message.lower()

    def test_whitespace_only_uris_return_friendly_message(self):
        """All-blank inputs are treated as empty."""
        result = run_aggregated_analysis(["", "   ", "\n\t"])
        assert result.per_uri == []
        assert result.message is not None

    def test_dedupes_repeated_uris(self):
        """The same URI submitted twice doesn't double-count its findings."""
        with patch("app.analyysikeskus.intent_analysis.run_adhoc_impact_analysis") as mock_adhoc:
            mock_adhoc.return_value = AdhocAnalysisResult(
                findings=_stub_findings(2, 1, 0),
                score=50,
                graph_uri="https://data.riik.ee/ontology/estleg/adhoc/test",
            )

            result = run_aggregated_analysis(
                ["https://example.org/uri-a", "https://example.org/uri-a"]
            )

        assert len(result.per_uri) == 1
        assert mock_adhoc.call_count == 1
        # Counts came from the single dedupe-survivor URI.
        assert result.total_affected == 2

    def test_aggregates_counts_across_uris(self):
        """Per-URI counts sum into the headline totals."""
        # Two URIs with different findings — totals must add up.
        calls: list[str] = []

        def _side_effect(entity_uri: str, **_: object) -> AdhocAnalysisResult:
            calls.append(entity_uri)
            if entity_uri == "uri-A":
                return AdhocAnalysisResult(
                    findings=_stub_findings(affected=3, conflicts=1, gaps=2),
                    score=70,
                    graph_uri="g-A",
                )
            return AdhocAnalysisResult(
                findings=_stub_findings(affected=5, conflicts=2, gaps=0),
                score=80,
                graph_uri="g-B",
            )

        with patch(
            "app.analyysikeskus.intent_analysis.run_adhoc_impact_analysis",
            side_effect=_side_effect,
        ):
            result = run_aggregated_analysis(["uri-A", "uri-B"])

        assert len(result.per_uri) == 2
        assert calls == ["uri-A", "uri-B"]
        # Per-URI attribution preserved.
        assert result.per_uri[0].entity_uri == "uri-A"
        assert result.per_uri[1].entity_uri == "uri-B"
        # Totals = sum across URIs.
        assert result.total_affected == 3 + 5
        assert result.total_conflicts == 1 + 2
        assert result.total_gaps == 2 + 0
        assert result.message is None

    def test_source_labels_attach_to_per_uri_results(self):
        """``source_labels`` map provides the human-readable label."""
        with patch("app.analyysikeskus.intent_analysis.run_adhoc_impact_analysis") as mock_adhoc:
            mock_adhoc.return_value = AdhocAnalysisResult(
                findings=_stub_findings(0, 0, 0),
                score=0,
                graph_uri="g",
            )

            result = run_aggregated_analysis(
                ["uri-A", "uri-B"],
                source_labels={"uri-A": "AvTS § 35", "uri-B": "KarS § 121"},
            )

        assert result.per_uri[0].source_label == "AvTS § 35"
        assert result.per_uri[1].source_label == "KarS § 121"

    def test_missing_label_falls_back_to_uri(self):
        """A URI without a source_labels entry falls back to the URI string."""
        with patch("app.analyysikeskus.intent_analysis.run_adhoc_impact_analysis") as mock_adhoc:
            mock_adhoc.return_value = AdhocAnalysisResult(
                findings=_stub_findings(0, 0, 0),
                score=0,
                graph_uri="g",
            )
            result = run_aggregated_analysis(["uri-A"])

        assert result.per_uri[0].source_label == "uri-A"

    def test_per_uri_result_carries_adhoc_findings(self):
        """The per-URI result must surface the underlying findings for the UI."""
        findings = _stub_findings(affected=2, conflicts=1, gaps=0)
        with patch("app.analyysikeskus.intent_analysis.run_adhoc_impact_analysis") as mock_adhoc:
            mock_adhoc.return_value = AdhocAnalysisResult(
                findings=findings,
                score=42,
                graph_uri="g",
            )
            result = run_aggregated_analysis(["uri-A"])

        per = result.per_uri[0]
        assert isinstance(per, PerUriResult)
        assert per.adhoc.score == 42
        assert per.adhoc.findings.affected_count == 2
        assert per.adhoc.findings.conflict_count == 1

    def test_jena_failure_yields_empty_findings_no_crash(self):
        """A Jena failure inside ``run_adhoc_impact_analysis`` yields empty findings."""
        with patch("app.analyysikeskus.intent_analysis.run_adhoc_impact_analysis") as mock_adhoc:
            mock_adhoc.return_value = AdhocAnalysisResult(
                findings=ImpactFindings(),
                score=0,
                graph_uri="",
            )
            result = run_aggregated_analysis(["uri-A"])

        assert len(result.per_uri) == 1
        assert result.total_affected == 0
        assert result.total_conflicts == 0
        assert result.total_gaps == 0
        # Message is None — the empty findings ARE a result, not a "no
        # confirmed URIs" empty state.
        assert result.message is None


# ---------------------------------------------------------------------------
# End-to-end orchestration
# ---------------------------------------------------------------------------


class TestEndToEndFlow:
    def test_full_pipeline_extract_resolve_aggregate(self):
        """Compose the three helpers end-to-end with stubbed dependencies.

        This is the integration shape Phase 2b's route will follow.
        """
        # Stub the extractor's LLM call.
        provider = MagicMock()
        provider.extract_json.return_value = {
            "candidates": [
                {
                    "ref_text": "PISTS § 4",
                    "ref_type": "provision",
                    "confidence": 0.9,
                    "reasoning": "põhinorm",
                },
                {
                    "ref_text": "Unknown § 99",
                    "ref_type": "provision",
                    "confidence": 0.4,
                    "reasoning": "kõrvalpõik",
                },
            ]
        }

        candidates = extract_candidates(
            "Soovin lihtsustada puudega inimese toetuse taotlemist.",
            provider=provider,
        )
        assert len(candidates) == 2

        # Stub resolver maps one URI cleanly, leaves one unresolved.
        stub_resolver = _StubResolver(
            mapping={
                "PISTS § 4": "https://data.riik.ee/ontology/estleg#PISTS_Par_4",
                "Unknown § 99": None,
            }
        )
        resolved = resolve_candidates(candidates, resolver=stub_resolver)
        assert len(resolved) == 2

        # User confirms only the resolved URI (mimicking the UI step).
        confirmed_uris = [r.resolved.entity_uri for r in resolved if r.resolved.entity_uri]
        source_labels = {
            r.resolved.entity_uri: r.resolved.matched_label
            for r in resolved
            if r.resolved.entity_uri and r.resolved.matched_label
        }
        assert len(confirmed_uris) == 1

        # Stub the analyser.
        with patch("app.analyysikeskus.intent_analysis.run_adhoc_impact_analysis") as mock_adhoc:
            mock_adhoc.return_value = AdhocAnalysisResult(
                findings=_stub_findings(affected=4, conflicts=1, gaps=0),
                score=70,
                graph_uri="g",
            )
            result = run_aggregated_analysis(
                confirmed_uris,
                source_labels=source_labels,
            )

        # End-to-end assertions.
        assert isinstance(result, AggregatedResult)
        assert len(result.per_uri) == 1
        assert result.per_uri[0].entity_uri == "https://data.riik.ee/ontology/estleg#PISTS_Par_4"
        assert result.per_uri[0].source_label == "PISTS § 4"
        assert result.total_affected == 4
        assert result.total_conflicts == 1
        assert result.total_gaps == 0
        assert result.message is None

    def test_full_pipeline_with_zero_resolutions_returns_empty_message(self):
        """If the user submits no resolved URIs we get the friendly message."""
        provider = MagicMock()
        provider.extract_json.return_value = {
            "candidates": [
                {
                    "ref_text": "Unknown § 99",
                    "ref_type": "provision",
                    "confidence": 0.4,
                    "reasoning": "x",
                }
            ]
        }
        candidates = extract_candidates("midagi", provider=provider)
        stub_resolver = _StubResolver(mapping={"Unknown § 99": None})
        resolved = resolve_candidates(candidates, resolver=stub_resolver)
        # No URIs confirmed.
        confirmed_uris = [r.resolved.entity_uri for r in resolved if r.resolved.entity_uri]
        assert confirmed_uris == []

        result = run_aggregated_analysis(confirmed_uris)
        assert result.message is not None
        assert result.per_uri == []
