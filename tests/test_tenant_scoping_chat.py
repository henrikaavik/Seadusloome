"""Tenant-scoping regression tests for the chat ``query_ontology`` tool (#844).

DoD verification:
- Chat ``query_ontology`` cannot read private draft named graphs outside
  the caller's org — proven here by asserting any ``GRAPH`` / ``FROM`` /
  ``FROM NAMED`` query is rejected *before* it reaches Jena (the only way
  to reach a draft named graph in the shared dataset).
- Chat SPARQL responses have a hard row cap + serialised-byte cap, and a
  query without a LIMIT gets a safe enforced LIMIT.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock

from app.chat.tools import (
    _QUERY_ONTOLOGY_BYTE_CAP,
    _QUERY_ONTOLOGY_ROW_CAP,
    _enforce_query_limit,
    execute_tool,
)
from app.ontology.sparql_client import SparqlClient


def _make_sparql(return_value=None) -> SparqlClient:
    client = SparqlClient.__new__(SparqlClient)
    client.jena_url = "http://localhost:3030"
    client.dataset = "ontology"
    client.timeout = 5.0
    client.query = MagicMock(return_value=return_value or [])  # type: ignore[assignment]
    return client


# ---------------------------------------------------------------------------
# C1 — named-graph queries are rejected (two orgs, two draft graphs)
# ---------------------------------------------------------------------------


class TestQueryOntologyGraphScoping:
    def test_graph_listing_query_is_rejected(self):
        """The canonical exfiltration query — ``SELECT ?g ?s ?p ?o WHERE
        { GRAPH ?g { ... } }`` — must never reach Jena. This is the query
        that would dump *every* org's private draft graph contents."""
        sparql = _make_sparql(
            [
                {
                    "g": "https://data.riik.ee/ontology/estleg/drafts/"
                    "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                    "s": "secret-subject",
                    "p": "estleg:title",
                    "o": "ORG A SECRET DRAFT TITLE",
                },
            ]
        )
        result = asyncio.run(
            execute_tool(
                "query_ontology",
                {"query": "SELECT ?g ?s ?p ?o WHERE { GRAPH ?g { ?s ?p ?o } }"},
                sparql,
            )
        )
        assert "error" in result
        assert "named graph" in result["error"].lower()
        # The query never executed — no org's draft data was read.
        sparql.query.assert_not_called()  # type: ignore[attr-defined]

    def test_targeted_other_org_draft_graph_is_rejected(self):
        """Even a query naming a *specific* other-org draft graph (the
        attacker knows the UUID) is blocked by the FROM keyword check."""
        other_org_graph = (
            "https://data.riik.ee/ontology/estleg/drafts/bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
        )
        sparql = _make_sparql([{"o": "ORG B SECRET"}])
        q = f"SELECT ?o FROM <{other_org_graph}> WHERE {{ ?s ?p ?o }}"
        result = asyncio.run(execute_tool("query_ontology", {"query": q}, sparql))
        assert "error" in result
        sparql.query.assert_not_called()  # type: ignore[attr-defined]

    def test_from_named_rejected(self):
        sparql = _make_sparql()
        q = (
            "SELECT ?o FROM NAMED <https://data.riik.ee/ontology/estleg/drafts/"
            "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb> WHERE { ?s ?p ?o }"
        )
        result = asyncio.run(execute_tool("query_ontology", {"query": q}, sparql))
        assert "error" in result
        sparql.query.assert_not_called()  # type: ignore[attr-defined]

    def test_graph_hidden_behind_comment_rejected(self):
        sparql = _make_sparql()
        q = "SELECT ?s WHERE {\n# decoy\nGRAPH ?g { ?s ?p ?o }\n}"
        result = asyncio.run(execute_tool("query_ontology", {"query": q}, sparql))
        assert "error" in result
        sparql.query.assert_not_called()  # type: ignore[attr-defined]

    def test_plain_public_query_still_runs(self):
        """A default-graph (public) query is unaffected — it executes."""
        sparql = _make_sparql([{"s": "https://data.riik.ee/ontology/estleg#KarS_Par_1"}])
        result = asyncio.run(
            execute_tool(
                "query_ontology",
                {"query": "SELECT ?s WHERE { ?s a estleg:LegalProvision }"},
                sparql,
            )
        )
        assert "results" in result
        assert len(result["results"]) == 1
        sparql.query.assert_called_once()  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# G1 — LIMIT injection + row cap + byte cap
# ---------------------------------------------------------------------------


class TestEnforceQueryLimit:
    def test_select_without_limit_gets_one(self):
        out = _enforce_query_limit("SELECT ?s WHERE { ?s ?p ?o }")
        assert "LIMIT" in out.upper()

    def test_select_with_limit_unchanged(self):
        q = "SELECT ?s WHERE { ?s ?p ?o } LIMIT 5"
        assert _enforce_query_limit(q) == q

    def test_ask_not_limited(self):
        q = "ASK { ?s ?p ?o }"
        assert _enforce_query_limit(q) == q

    def test_describe_not_limited(self):
        q = "DESCRIBE <https://data.riik.ee/ontology/estleg#KarS_Par_1>"
        assert _enforce_query_limit(q) == q

    def test_limit_injected_after_prefixes(self):
        q = (
            "PREFIX estleg: <https://data.riik.ee/ontology/estleg#>\n"
            "SELECT ?s WHERE { ?s a estleg:LegalProvision }"
        )
        out = _enforce_query_limit(q)
        assert out.rstrip().upper().endswith(f"LIMIT {200}".upper()) or "LIMIT 200" in out


class TestQueryOntologyResultCaps:
    def test_row_cap_truncates(self):
        # Jena returns more rows than the cap (e.g. unbounded EU acts).
        big = [{"s": f"https://data.riik.ee/ontology/estleg#E{i}"} for i in range(500)]
        sparql = _make_sparql(big)
        result = asyncio.run(
            execute_tool("query_ontology", {"query": "SELECT ?s WHERE { ?s ?p ?o }"}, sparql)
        )
        assert len(result["results"]) == _QUERY_ONTOLOGY_ROW_CAP
        assert result.get("truncated") is True

    def test_byte_cap_truncates_wide_rows(self):
        # A handful of very wide rows blow the byte cap even under the row
        # cap; the byte guard shrinks the list further.
        wide_value = "x" * 5000
        rows = [{"a": wide_value, "b": wide_value} for _ in range(_QUERY_ONTOLOGY_ROW_CAP)]
        sparql = _make_sparql(rows)
        result = asyncio.run(
            execute_tool("query_ontology", {"query": "SELECT ?a ?b WHERE { ?s ?p ?o }"}, sparql)
        )
        serialized = json.dumps({"results": result["results"]}, ensure_ascii=False)
        assert len(serialized.encode("utf-8")) <= _QUERY_ONTOLOGY_BYTE_CAP
        assert result.get("truncated") is True

    def test_small_result_not_flagged_truncated(self):
        sparql = _make_sparql([{"s": "https://data.riik.ee/ontology/estleg#KarS_Par_1"}])
        result = asyncio.run(
            execute_tool("query_ontology", {"query": "SELECT ?s WHERE { ?s ?p ?o }"}, sparql)
        )
        assert "truncated" not in result
        assert len(result["results"]) == 1
