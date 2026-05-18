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

        # Check reshaping: affected entities become (uri, label, type, relation) dicts.
        # ``relation`` is the canonical predicate URI projected by C0 — empty
        # string when the canned row didn't supply one.
        first = findings.affected_entities[0]
        assert set(first.keys()) == {"uri", "label", "type", "relation"}
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


class TestAnalyzePartialMatch:
    """Wave 2 Step 5A (docs/2026-05-18-bugfix-plan.md, P2 review
    follow-up): the AFFECTED_ENTITIES query unions an
    ``estleg:referencesAct "<title>"`` literal-edge branch on top of
    the URI-shaped branches. The analyzer's ``_find_affected`` must
    reshape the polymorphic SPARQL result so partial-match rows arrive
    at the renderer with a sensible ``label`` and the renderer-side
    ``relation``-keyed branching to render them as plain text instead
    of explorer links.
    """

    _REFERENCES_ACT_URI = "https://data.riik.ee/ontology/estleg#referencesAct"

    def test_find_affected_reshapes_literal_partial_match_row(self):
        """A ``referencesAct`` row carries the literal act title in
        ``?entity`` with no ``?label`` / ``?type`` bindings. The
        analyzer must populate ``label`` from the title so the
        renderer's "Nimetus" column shows the act title verbatim.
        """
        client = _client(
            {
                "SELECT DISTINCT ?entity ?label ?type": [
                    {
                        # Literal-edge partial match — no URI, no
                        # label, no type. The SPARQL arm only binds
                        # ?entity and ?relation.
                        "entity": "Riigieelarve seadus",
                        "label": "",
                        "type": "",
                        "relation": self._REFERENCES_ACT_URI,
                    },
                ]
            }
        )
        analyzer = ImpactAnalyzer(sparql_client=client)
        rows = analyzer._find_affected(_GRAPH_URI)
        assert len(rows) == 1
        row = rows[0]
        # Polymorphic-entity handling: ``uri`` carries the literal
        # title (the renderer keys row identity off this field for
        # annotation threads), ``label`` is populated from the title
        # so the "Nimetus" column has something to show, ``type``
        # stays empty so the type column falls back to the "Akt
        # (sätet ei leitud)" phrase via the renderer's
        # ``_is_partial_match_row`` branch.
        assert row["uri"] == "Riigieelarve seadus"
        assert row["label"] == "Riigieelarve seadus"
        assert row["type"] == ""
        assert row["relation"] == self._REFERENCES_ACT_URI

    def test_find_affected_preserves_uri_shaped_rows(self):
        """URI-shaped rows must NOT be reshaped — the existing path
        for full URI matches stays untouched.
        """
        client = _client(
            {
                "SELECT DISTINCT ?entity ?label ?type": [
                    {
                        "entity": "https://data.riik.ee/ontology/estleg#KarS_Par_133",
                        "label": "KarS § 133",
                        "type": "https://data.riik.ee/ontology/estleg#LegalProvision",
                        "relation": "https://data.riik.ee/ontology/estleg#references",
                    },
                ]
            }
        )
        analyzer = ImpactAnalyzer(sparql_client=client)
        rows = analyzer._find_affected(_GRAPH_URI)
        assert len(rows) == 1
        row = rows[0]
        # ``label`` came from the SPARQL projection, NOT from
        # ``?entity`` — the reshape only triggers on a partial-match
        # relation.
        assert row["uri"].endswith("KarS_Par_133")
        assert row["label"] == "KarS § 133"
        assert row["type"].endswith("LegalProvision")
        assert row["relation"].endswith("references")

    def test_find_affected_mixes_uri_and_literal_rows(self):
        """Both shapes coexist in the same result list — the
        analyzer must produce a homogeneous ``list[dict[str, str]]``
        regardless of which branch a row came from.
        """
        client = _client(
            {
                "SELECT DISTINCT ?entity ?label ?type": [
                    {
                        "entity": "https://data.riik.ee/ontology/estleg#KarS_Par_133",
                        "label": "KarS § 133",
                        "type": "https://data.riik.ee/ontology/estleg#LegalProvision",
                        "relation": "https://data.riik.ee/ontology/estleg#references",
                    },
                    {
                        "entity": "Riigieelarve seadus",
                        "label": "",
                        "type": "",
                        "relation": self._REFERENCES_ACT_URI,
                    },
                ]
            }
        )
        analyzer = ImpactAnalyzer(sparql_client=client)
        rows = analyzer._find_affected(_GRAPH_URI)
        assert len(rows) == 2
        # Existing URI row preserved unchanged.
        assert rows[0]["uri"].endswith("KarS_Par_133")
        assert rows[0]["label"] == "KarS § 133"
        # New literal row reshaped: label populated from the title.
        assert rows[1]["uri"] == "Riigieelarve seadus"
        assert rows[1]["label"] == "Riigieelarve seadus"
        assert rows[1]["relation"].endswith("referencesAct")
