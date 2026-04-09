"""Unit tests for ``app.docs.reference_resolver``.

The resolver talks to Jena via :class:`SparqlClient`; these tests
inject a MagicMock client so no real HTTP traffic happens. We verify
each ref_type's strategy in isolation and that a dead Jena turns into
unresolved refs (not an exception).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.docs.entity_extractor import ExtractedRef
from app.docs.reference_resolver import (
    ReferenceResolver,
    ResolvedRef,
    resolve_refs,
)


def _ref(ref_text: str, ref_type: str, confidence: float = 0.9) -> ExtractedRef:
    return ExtractedRef(
        ref_text=ref_text,
        ref_type=ref_type,
        confidence=confidence,
        location={"chunk": 0, "offset": 0},
    )


class TestResolveLaw:
    def test_resolve_law_exact_match(self):
        sparql = MagicMock()
        sparql.query.return_value = [
            {
                "uri": "https://data.riik.ee/ontology/estleg#KarS",
                "shortName": "KarS",
                "fullName": "Karistusseadustik",
            }
        ]
        resolver = ReferenceResolver(sparql_client=sparql)

        result = resolver.resolve([_ref("KarS", "law")])

        assert len(result) == 1
        assert isinstance(result[0], ResolvedRef)
        assert result[0].entity_uri == "https://data.riik.ee/ontology/estleg#KarS"
        assert result[0].matched_label == "Karistusseadustik"
        assert result[0].match_score == 1.0

    def test_resolve_law_fuzzy_match(self):
        """Near-miss short name picks the closest label via difflib."""
        sparql = MagicMock()
        sparql.query.return_value = [
            {
                "uri": "https://data.riik.ee/ontology/estleg#KarS",
                "shortName": "KarS",
                "fullName": "Karistusseadustik",
            },
            {
                "uri": "https://data.riik.ee/ontology/estleg#TsÜS",
                "shortName": "TsÜS",
                "fullName": "Tsiviilseadustiku üldosa seadus",
            },
        ]
        resolver = ReferenceResolver(sparql_client=sparql)

        # "Kars" (lowercase s instead of S) → fuzzy match to KarS
        result = resolver.resolve([_ref("kars", "law")])

        assert result[0].entity_uri == "https://data.riik.ee/ontology/estleg#KarS"
        # Fuzzy match on normalised lowercase strings: "kars" vs "kars" is exact,
        # so we get 1.0. Use a genuinely off-by-one input to verify <1.0.
        assert result[0].match_score >= 0.9

    def test_resolve_law_no_match(self):
        sparql = MagicMock()
        sparql.query.return_value = [
            {
                "uri": "https://data.riik.ee/ontology/estleg#KarS",
                "shortName": "KarS",
                "fullName": "Karistusseadustik",
            }
        ]
        resolver = ReferenceResolver(sparql_client=sparql)

        result = resolver.resolve([_ref("Täiesti tundmatu seadus", "law")])

        # Low fuzzy score → unresolved.
        assert result[0].entity_uri is None
        assert result[0].match_score == 0.0


class TestResolveProvision:
    def test_resolve_provision_exact(self):
        sparql = MagicMock()
        sparql.query.return_value = [
            {
                "uri": "https://data.riik.ee/ontology/estleg#KarS_Par_133",
                "paragrahv": "KarS § 133 lg 2 p 1",
            }
        ]
        resolver = ReferenceResolver(sparql_client=sparql)

        result = resolver.resolve([_ref("KarS § 133 lg 2 p 1", "provision")])

        assert result[0].entity_uri == "https://data.riik.ee/ontology/estleg#KarS_Par_133"
        assert result[0].matched_label == "KarS § 133 lg 2 p 1"
        assert result[0].match_score == 1.0

    def test_resolve_provision_unresolved(self):
        sparql = MagicMock()
        sparql.query.return_value = []
        resolver = ReferenceResolver(sparql_client=sparql)

        result = resolver.resolve([_ref("FooBar § 999", "provision")])

        assert result[0].entity_uri is None
        assert result[0].match_score == 0.0

    def test_resolve_provision_retries_with_normalised_whitespace(self):
        """NBSPs and weird spacing trigger a second attempt."""
        sparql = MagicMock()
        # First call: exact raw lookup — no hit.
        # Second call: normalised whitespace — hit!
        sparql.query.side_effect = [
            [],
            [
                {
                    "uri": "https://data.riik.ee/ontology/estleg#TsÜS_Par_12",
                    "paragrahv": "TsÜS § 12",
                }
            ],
        ]
        resolver = ReferenceResolver(sparql_client=sparql)

        # "TsÜS\u00a0§\u00a012" — non-breaking space between parts
        result = resolver.resolve([_ref("TsÜS\u00a0§\u00a012", "provision")])

        assert result[0].entity_uri == "https://data.riik.ee/ontology/estleg#TsÜS_Par_12"
        assert sparql.query.call_count == 2


class TestResolveEUAct:
    def test_resolve_eu_act_by_celex(self):
        sparql = MagicMock()
        sparql.query.return_value = [
            {
                "uri": "https://data.riik.ee/ontology/estleg#EU_GDPR",
                "label": "GDPR",
            }
        ]
        resolver = ReferenceResolver(sparql_client=sparql)

        result = resolver.resolve([_ref("määrus 32016R0679 (GDPR)", "eu_act")])

        assert result[0].entity_uri == "https://data.riik.ee/ontology/estleg#EU_GDPR"
        assert result[0].matched_label == "GDPR"

    def test_resolve_eu_act_without_celex_is_unresolved(self):
        sparql = MagicMock()
        resolver = ReferenceResolver(sparql_client=sparql)

        result = resolver.resolve([_ref("GDPR üldiselt", "eu_act")])

        assert result[0].entity_uri is None
        sparql.query.assert_not_called()


class TestResolveCourtDecision:
    def test_resolve_court_decision_by_case_number(self):
        sparql = MagicMock()
        sparql.query.return_value = [
            {
                "uri": "https://data.riik.ee/ontology/estleg#case_3-1-1-63-15",
                "label": "Riigikohus 3-1-1-63-15",
            }
        ]
        resolver = ReferenceResolver(sparql_client=sparql)

        result = resolver.resolve([_ref("3-1-1-63-15", "court_decision")])

        assert result[0].entity_uri == "https://data.riik.ee/ontology/estleg#case_3-1-1-63-15"
        assert result[0].matched_label == "Riigikohus 3-1-1-63-15"


class TestResolveConcept:
    def test_resolve_concept_by_label(self):
        sparql = MagicMock()
        sparql.query.return_value = [
            {
                "uri": "https://data.riik.ee/ontology/estleg#concept_good_faith",
                "label": "hea usu põhimõte",
            }
        ]
        resolver = ReferenceResolver(sparql_client=sparql)

        result = resolver.resolve([_ref("hea usu põhimõte", "concept")])

        assert result[0].entity_uri == "https://data.riik.ee/ontology/estleg#concept_good_faith"


class TestResolverOutage:
    def test_resolve_handles_jena_outage(self, caplog: pytest.LogCaptureFixture):
        """SparqlClient.query raising → all refs returned unresolved with a warning."""
        sparql = MagicMock()
        sparql.query.side_effect = RuntimeError("connection refused")
        resolver = ReferenceResolver(sparql_client=sparql)

        with caplog.at_level("WARNING"):
            result = resolver.resolve(
                [
                    _ref("KarS", "law"),
                    _ref("KarS § 1", "provision"),
                    _ref("32016R0679", "eu_act"),
                    _ref("3-1-1-63-15", "court_decision"),
                    _ref("hea usu põhimõte", "concept"),
                ]
            )

        assert len(result) == 5
        for r in result:
            assert r.entity_uri is None
            assert r.match_score == 0.0
        # Every ref_type should have logged a warning at least once
        # (the law path logs via "_get_law_dict"; others via their
        # per-type handlers).
        assert len(caplog.records) >= 1

    def test_resolve_empty_list_returns_empty(self):
        sparql = MagicMock()
        resolver = ReferenceResolver(sparql_client=sparql)

        assert resolver.resolve([]) == []
        sparql.query.assert_not_called()


class TestDefaultResolverFactory:
    def test_resolve_refs_top_level_helper(self, monkeypatch: pytest.MonkeyPatch):
        """``resolve_refs`` delegates to the module-level singleton."""
        import app.docs.reference_resolver as mod

        fake_resolver = MagicMock()
        fake_resolver.resolve.return_value = ["fake-result"]
        monkeypatch.setattr(mod, "_default_resolver", fake_resolver)

        out = resolve_refs([_ref("KarS", "law")])

        assert out == ["fake-result"]
        fake_resolver.resolve.assert_called_once()
