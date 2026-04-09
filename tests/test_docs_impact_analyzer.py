"""Unit tests for ``app.docs.impact.analyzer.ImpactAnalyzer``.

The analyzer only talks to a :class:`SparqlClient`, so every test
patches the client to return canned rows and asserts on the shape of
the :class:`ImpactFindings` result. No real Jena, no real network.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from app.docs.impact.analyzer import ImpactAnalyzer, ImpactFindings

# #476: the URI must match the tightened ``_SAFE_GRAPH_URI`` allowlist
# in ``app.docs.impact.queries`` — any string outside the production
# ``drafts/<uuid>`` shape is now rejected as an injection guard.
_GRAPH_URI = "https://data.riik.ee/ontology/estleg/drafts/11111111-1111-1111-1111-111111111111"


def _client(responses: dict[str, list[dict[str, str]]]) -> MagicMock:
    """Build a SparqlClient mock that returns canned rows by query fingerprint.

    ``responses`` keys are substring fingerprints looked up against the
    submitted SPARQL text. The first matching key wins; queries that
    don't match any key return an empty list so unrelated passes do not
    contribute.
    """
    mock = MagicMock()

    def side_effect(sparql: str, *args, **kwargs) -> list[dict[str, str]]:
        for needle, rows in responses.items():
            if needle in sparql:
                return rows
        return []

    mock.query.side_effect = side_effect
    return mock


class TestAnalyzeHappyPath:
    def test_all_passes_populated(self):
        client = _client(
            {
                "SELECT DISTINCT ?entity ?label ?type": [
                    {
                        "entity": "https://data.riik.ee/ontology/estleg#KarS_Par_133",
                        "label": "KarS § 133",
                        "type": "https://data.riik.ee/ontology/estleg#LegalProvision",
                    },
                    {
                        "entity": "https://data.riik.ee/ontology/estleg#TsUS_Par_12",
                        "label": "TsÜS § 12",
                        "type": "https://data.riik.ee/ontology/estleg#LegalProvision",
                    },
                ],
                "?draftRef ?conflictEntity": [
                    {
                        "draftRef": "https://data.riik.ee/ontology/estleg#KarS_Par_133",
                        "conflictEntity": "urn:case:3-1-1-63-15",
                        "conflictLabel": "Riigikohus 3-1-1-63-15",
                        "reason": "Court decision interprets this provision",
                    },
                ],
                "SELECT ?cluster": [
                    {
                        "cluster": "https://data.riik.ee/ontology/estleg#topic/karistusoigus",
                        "clusterLabel": "Karistusõigus",
                        "totalProvisions": "20",
                        "referencedProvisions": "1",
                    }
                ],
                "SELECT DISTINCT ?euAct": [
                    {
                        "euAct": "https://data.riik.ee/ontology/estleg#EU_Dir_2019_790",
                        "euLabel": "DSM Directive",
                        "estonianProvision": "https://data.riik.ee/ontology/estleg#AuthorLaw_Par_80",
                        "provisionLabel": "AutÕS § 80",
                    }
                ],
            }
        )

        analyzer = ImpactAnalyzer(sparql_client=client)
        findings = analyzer.analyze(_GRAPH_URI)

        assert isinstance(findings, ImpactFindings)
        assert findings.affected_count == 2
        assert findings.conflict_count == 1
        assert findings.gap_count == 1
        assert len(findings.eu_compliance) == 1

        # Check reshaping: affected entities become (uri, label, type) dicts.
        first = findings.affected_entities[0]
        assert set(first.keys()) == {"uri", "label", "type"}
        assert first["label"] == "KarS § 133"

        # Conflicts reshape draftRef/conflictEntity into snake_case.
        conflict = findings.conflicts[0]
        assert conflict["draft_ref"].endswith("KarS_Par_133")
        assert conflict["reason"] == "Court decision interprets this provision"

        # Gaps include derived description.
        gap = findings.gaps[0]
        assert gap["topic_cluster_label"] == "Karistusõigus"
        assert "1 of 20" in gap["description"]

        # EU compliance keeps both the EU act and the provision.
        eu = findings.eu_compliance[0]
        assert eu["eu_act"].endswith("EU_Dir_2019_790")
        assert eu["transposition_status"] == "linked"


class TestAnalyzePartialFailure:
    def test_one_pass_raising_still_returns_others(self):
        client = MagicMock()

        def side_effect(sparql: str, *args, **kwargs) -> list[dict[str, str]]:
            if "?draftRef ?conflictEntity" in sparql:
                raise RuntimeError("planner exploded")
            if "SELECT DISTINCT ?entity ?label ?type" in sparql:
                return [
                    {
                        "entity": "urn:foo",
                        "label": "foo",
                        "type": "urn:type",
                    }
                ]
            return []

        client.query.side_effect = side_effect

        findings = ImpactAnalyzer(sparql_client=client).analyze(_GRAPH_URI)

        # Affected pass succeeded; conflicts pass failed; gap/EU empty.
        assert findings.affected_count == 1
        assert findings.conflict_count == 0
        assert findings.gap_count == 0
        assert findings.eu_compliance == []


class TestAnalyzeEmpty:
    def test_all_empty_returns_zero_counts(self):
        client = _client({})
        findings = ImpactAnalyzer(sparql_client=client).analyze(_GRAPH_URI)

        assert findings.affected_entities == []
        assert findings.conflicts == []
        assert findings.gaps == []
        assert findings.eu_compliance == []
        assert findings.affected_count == 0
        assert findings.conflict_count == 0
        assert findings.gap_count == 0


class TestIndividualPasses:
    def test_find_affected_drops_rows_without_entity(self):
        client = _client(
            {
                "SELECT DISTINCT ?entity ?label ?type": [
                    {"entity": "", "label": "no uri"},
                    {"entity": "urn:keep", "label": "keep"},
                ]
            }
        )
        analyzer = ImpactAnalyzer(sparql_client=client)
        rows = analyzer._find_affected(_GRAPH_URI)
        assert len(rows) == 1
        assert rows[0]["uri"] == "urn:keep"

    def test_detect_conflicts_drops_rows_without_draftref(self):
        client = _client(
            {
                "?draftRef ?conflictEntity": [
                    {"draftRef": "", "conflictEntity": "urn:nope"},
                    {"draftRef": "urn:ref", "conflictEntity": "urn:c", "reason": "r"},
                ]
            }
        )
        analyzer = ImpactAnalyzer(sparql_client=client)
        rows = analyzer._detect_conflicts(_GRAPH_URI)
        assert len(rows) == 1
        assert rows[0]["draft_ref"] == "urn:ref"

    def test_analyze_gaps_drops_rows_without_cluster(self):
        client = _client(
            {
                "SELECT ?cluster": [
                    {"clusterLabel": "orphan"},  # no cluster URI
                    {
                        "cluster": "urn:cluster",
                        "clusterLabel": "kept",
                        "totalProvisions": "10",
                        "referencedProvisions": "2",
                    },
                ]
            }
        )
        analyzer = ImpactAnalyzer(sparql_client=client)
        rows = analyzer._analyze_gaps(_GRAPH_URI)
        assert len(rows) == 1
        assert rows[0]["topic_cluster"] == "urn:cluster"

    def test_check_eu_compliance_drops_rows_without_euact(self):
        client = _client(
            {
                "SELECT DISTINCT ?euAct": [
                    {"euAct": ""},
                    {
                        "euAct": "urn:eu:1",
                        "euLabel": "Dir",
                        "estonianProvision": "urn:est:1",
                    },
                ]
            }
        )
        analyzer = ImpactAnalyzer(sparql_client=client)
        rows = analyzer._check_eu_compliance(_GRAPH_URI)
        assert len(rows) == 1
        assert rows[0]["eu_act"] == "urn:eu:1"
