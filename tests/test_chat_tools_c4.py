"""Tests for the C4 specialised chat-tool helpers (2026-05-16).

The four helpers (``get_court_decisions_for_provision``,
``get_eu_transposition_for_provision``, ``get_provision_amendments``,
``get_related_concepts``) build SPARQL templates that traverse the
canonical predicates from :mod:`app.ontology.relations`. We exercise
them at the same depth as :mod:`tests.test_impact_queries_canonical` —
the helper builds a real query, we run it against an
:class:`rdflib.Graph` seeded from the canonical fixture, and assert the
expected rows come back.

The :class:`SparqlClient` is monkey-patched so its ``query`` method
runs the SPARQL string against the rdflib graph instead of issuing an
HTTP call. That keeps the test deterministic and offline while still
exercising the real SPARQL bytes the helper would send to Fuseki.

Coverage per helper:

* Happy path — non-empty URI, fixture data present, expected row returned.
* Empty input — ``provision_uri`` missing / blank → ``error`` returned.
* Non-existent URI — well-formed URI with no triples → empty result list.

Plus a guard that the system prompt advertises every C4 helper by name
so the LLM is steered toward them and not back to ``query_ontology``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock

from rdflib import Graph

from app.chat.system_prompt import build_system_prompt
from app.chat.tools import CHAT_TOOLS, execute_tool
from app.ontology.sparql_client import SparqlClient

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "ontology_canonical.ttl"

# A well-formed estleg URI that intentionally has no triples in the
# fixture — used to test the "no results" path.
NON_EXISTENT_URI = "estleg:DoesNotExist_999"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_sparql_against_fixture() -> SparqlClient:
    """Return a SparqlClient whose ``query`` runs SPARQL against the fixture.

    Bypasses :meth:`SparqlClient.__init__` (no HTTP / no Fuseki) and
    replaces ``query`` with a function that delegates to a freshly-loaded
    rdflib :class:`Graph`. The fixture is parsed once per call so each
    test starts from a clean baseline.
    """
    graph = Graph()
    graph.parse(FIXTURE_PATH, format="turtle")

    def _run(sparql_text: str, *args: object, **kwargs: object) -> list[dict[str, str]]:
        rows: list[dict[str, str]] = []
        for row in graph.query(sparql_text):
            d: dict[str, str] = {}
            for var in row.labels:  # type: ignore[attr-defined,union-attr]
                value = row[var]  # type: ignore[index]
                d[str(var)] = str(value) if value is not None else ""
            rows.append(d)
        return rows

    client = SparqlClient.__new__(SparqlClient)
    client.jena_url = "http://localhost:3030"
    client.dataset = "ontology"
    client.timeout = 5.0
    client.query = MagicMock(side_effect=_run)  # type: ignore[assignment]
    return client


def _run(tool: str, payload: dict[str, object]) -> dict[str, object]:
    return asyncio.run(execute_tool(tool, payload, _make_sparql_against_fixture()))


# ---------------------------------------------------------------------------
# get_court_decisions_for_provision
# ---------------------------------------------------------------------------


class TestGetCourtDecisionsForProvision:
    """Walk ``interpretsLaw`` (forward) + ``interpretedBy`` (inverse)."""

    def test_happy_path_returns_court_decision(self):
        result = _run(
            "get_court_decisions_for_provision",
            {"provision_uri": "estleg:Provision_1"},
        )
        assert "decisions" in result
        decisions = result["decisions"]
        assert isinstance(decisions, list) and len(decisions) >= 1
        # Both UNION arms should resolve to CourtDecision_1 in the fixture.
        decision_uris = {d["uri"] for d in decisions}
        assert any(uri.endswith("CourtDecision_1") for uri in decision_uris)
        # Each row carries a relation (interpretsLaw or interpretedBy) and a source.
        for d in decisions:
            assert d["relation"], f"row missing relation: {d}"
            assert d["source"], f"row missing source provision: {d}"
            assert d["relation"].endswith("interpretsLaw") or d["relation"].endswith(
                "interpretedBy"
            )

    def test_empty_provision_uri_returns_error(self):
        result = _run("get_court_decisions_for_provision", {"provision_uri": ""})
        assert "error" in result
        assert "Empty" in str(result["error"])

    def test_non_existent_uri_returns_empty_list(self):
        result = _run(
            "get_court_decisions_for_provision",
            {"provision_uri": NON_EXISTENT_URI},
        )
        assert result == {"decisions": [], "count": 0}


# ---------------------------------------------------------------------------
# get_eu_transposition_for_provision
# ---------------------------------------------------------------------------


class TestGetEuTranspositionForProvision:
    """Walk ``transposesDirective``, ``transposedBy``, ``harmonisedWith``."""

    def test_happy_path_returns_eu_acts(self):
        result = _run(
            "get_eu_transposition_for_provision",
            {"provision_uri": "estleg:Provision_1"},
        )
        assert "eu_acts" in result
        eu_acts = result["eu_acts"]
        assert isinstance(eu_acts, list)
        # Fixture has Provision_1 ──transposesDirective──▶ EU_Dir_1
        # and Provision_1 ──harmonisedWith──▶ EU_Dir_1 → two rows minimum.
        relations = {row["relation"] for row in eu_acts}
        assert any(r.endswith("transposesDirective") for r in relations)
        assert any(r.endswith("harmonisedWith") for r in relations)
        # Every returned URI must be EU_Dir_1 (the only direct EU peer).
        for row in eu_acts:
            assert row["uri"].endswith("EU_Dir_1")
            assert row["source"], f"row missing source: {row}"

    def test_empty_provision_uri_returns_error(self):
        result = _run("get_eu_transposition_for_provision", {"provision_uri": "   "})
        assert "error" in result

    def test_non_existent_uri_returns_empty_list(self):
        result = _run(
            "get_eu_transposition_for_provision",
            {"provision_uri": NON_EXISTENT_URI},
        )
        assert result == {"eu_acts": [], "count": 0}


# ---------------------------------------------------------------------------
# get_provision_amendments
# ---------------------------------------------------------------------------


class TestGetProvisionAmendments:
    """Walk ``amends`` (AmendmentEvent → Provision) + ``amendedBy`` (inverse)."""

    def test_happy_path_returns_amendment_history(self):
        result = _run(
            "get_provision_amendments",
            {"provision_uri": "estleg:Provision_1"},
        )
        assert "amendments" in result
        amendments = result["amendments"]
        assert isinstance(amendments, list) and len(amendments) >= 2
        uris = {row["uri"] for row in amendments}
        # AmendmentEvent_1 ──amends──▶ Provision_1 (forward arm)
        assert any(u.endswith("AmendmentEvent_1") for u in uris)
        # Provision_1 ──amendedBy──▶ Act_2 (inverse arm)
        assert any(u.endswith("Act_2") for u in uris)
        # Each row exposes the relation that produced it.
        relations = {row["relation"] for row in amendments}
        assert any(r.endswith("amends") for r in relations)
        assert any(r.endswith("amendedBy") for r in relations)

    def test_empty_provision_uri_returns_error(self):
        result = _run("get_provision_amendments", {"provision_uri": ""})
        assert "error" in result

    def test_non_existent_uri_returns_empty_list(self):
        result = _run(
            "get_provision_amendments",
            {"provision_uri": NON_EXISTENT_URI},
        )
        assert result == {"amendments": [], "count": 0}


# ---------------------------------------------------------------------------
# get_related_concepts
# ---------------------------------------------------------------------------


class TestGetRelatedConcepts:
    """Walk ``definesConcept`` + ``requestedCluster`` UNION ``topicCluster``."""

    def test_happy_path_returns_concept_and_cluster(self):
        result = _run(
            "get_related_concepts",
            {"provision_uri": "estleg:Provision_1"},
        )
        assert "related" in result
        related = result["related"]
        assert isinstance(related, list) and len(related) >= 2
        # Fixture: Provision_1 definesConcept Concept_1
        # and Provision_1 requestedCluster Cluster_1.
        kinds = {row["kind"] for row in related}
        assert kinds == {"concept", "cluster"}
        uris = {row["uri"] for row in related}
        assert any(u.endswith("Concept_1") for u in uris)
        assert any(u.endswith("Cluster_1") for u in uris)
        relations = {row["relation"] for row in related}
        assert any(r.endswith("definesConcept") for r in relations)
        # requestedCluster is the canonical populated edge — must be projected.
        assert any(r.endswith("requestedCluster") for r in relations)

    def test_empty_provision_uri_returns_error(self):
        result = _run("get_related_concepts", {"provision_uri": ""})
        assert "error" in result

    def test_non_existent_uri_returns_empty_list(self):
        result = _run(
            "get_related_concepts",
            {"provision_uri": NON_EXISTENT_URI},
        )
        assert result == {"related": [], "count": 0}


# ---------------------------------------------------------------------------
# Schema-level guards (cheap and protect the LLM contract)
# ---------------------------------------------------------------------------


class TestC4SchemaRegistration:
    """All four helpers must be registered as Claude tool schemas."""

    def test_each_helper_is_in_chat_tools(self):
        names = {t["name"] for t in CHAT_TOOLS}
        assert "get_court_decisions_for_provision" in names
        assert "get_eu_transposition_for_provision" in names
        assert "get_provision_amendments" in names
        assert "get_related_concepts" in names

    def test_each_helper_schema_requires_provision_uri(self):
        wanted = {
            "get_court_decisions_for_provision",
            "get_eu_transposition_for_provision",
            "get_provision_amendments",
            "get_related_concepts",
        }
        for tool in CHAT_TOOLS:
            if tool["name"] in wanted:
                schema = tool["input_schema"]
                assert "provision_uri" in schema["properties"]
                assert "provision_uri" in schema["required"]


# ---------------------------------------------------------------------------
# System prompt steers the LLM toward the new helpers
# ---------------------------------------------------------------------------


class TestSystemPromptListsC4Helpers:
    """The prefer-specialised guidance must mention each new helper by name."""

    def test_prompt_mentions_every_new_helper(self):
        prompt = build_system_prompt()
        assert "get_court_decisions_for_provision" in prompt
        assert "get_eu_transposition_for_provision" in prompt
        assert "get_provision_amendments" in prompt
        assert "get_related_concepts" in prompt

    def test_prompt_instructs_preference_over_query_ontology(self):
        prompt = build_system_prompt()
        # The guidance block must call out query_ontology as the last-resort
        # tool so the LLM doesn't default to it.
        assert "query_ontology" in prompt
        # Estonian guidance keyword for "prefer / preference".
        assert "EELISTUS" in prompt or "eelista" in prompt.lower()
