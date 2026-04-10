"""Unit tests for ``app.chat.tools``.

Tests the tool schemas, SPARQL safety checks, and each executor.
All SPARQL and DB access is mocked.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

from app.chat.tools import CHAT_TOOLS, _is_read_only_sparql, execute_tool
from app.ontology.sparql_client import SparqlClient

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_sparql() -> SparqlClient:
    """Return a SparqlClient with a mocked query method."""
    client = SparqlClient.__new__(SparqlClient)
    client.jena_url = "http://localhost:3030"
    client.dataset = "ontology"
    client.timeout = 5.0
    client.query = MagicMock(return_value=[])  # type: ignore[assignment]
    return client


# ---------------------------------------------------------------------------
# CHAT_TOOLS schema
# ---------------------------------------------------------------------------


class TestToolSchemas:
    def test_all_tools_have_required_keys(self):
        """Every tool schema has name, description, and input_schema."""
        assert len(CHAT_TOOLS) == 4
        for tool in CHAT_TOOLS:
            assert "name" in tool
            assert "description" in tool
            assert "input_schema" in tool
            assert tool["input_schema"]["type"] == "object"

    def test_tool_names(self):
        names = {t["name"] for t in CHAT_TOOLS}
        assert names == {
            "query_ontology",
            "search_provisions",
            "get_draft_impact",
            "get_provision_details",
        }


# ---------------------------------------------------------------------------
# SPARQL safety
# ---------------------------------------------------------------------------


class TestSparqlSafety:
    def test_select_is_read_only(self):
        assert _is_read_only_sparql("SELECT ?s ?p ?o WHERE { ?s ?p ?o }") is True

    def test_ask_is_read_only(self):
        assert _is_read_only_sparql("ASK { ?s ?p ?o }") is True

    def test_describe_is_read_only(self):
        assert _is_read_only_sparql("DESCRIBE <http://example.org/e1>") is True

    def test_insert_is_rejected(self):
        assert _is_read_only_sparql("INSERT DATA { <a> <b> <c> }") is False

    def test_delete_is_rejected(self):
        assert _is_read_only_sparql("DELETE WHERE { ?s ?p ?o }") is False

    def test_drop_is_rejected(self):
        assert _is_read_only_sparql("DROP GRAPH <http://example.org/g>") is False

    def test_case_insensitive_rejection(self):
        assert _is_read_only_sparql("insert data { <a> <b> <c> }") is False


# ---------------------------------------------------------------------------
# query_ontology executor
# ---------------------------------------------------------------------------


class TestQueryOntology:
    def test_executes_select(self):
        sparql = _make_sparql()
        sparql.query.return_value = [{"s": "http://example.org/e1"}]  # type: ignore[attr-defined]

        result = asyncio.run(
            execute_tool("query_ontology", {"query": "SELECT ?s WHERE { ?s ?p ?o }"}, sparql)
        )

        assert "results" in result
        assert len(result["results"]) == 1
        sparql.query.assert_called_once()  # type: ignore[attr-defined]

    def test_rejects_update(self):
        sparql = _make_sparql()

        result = asyncio.run(
            execute_tool("query_ontology", {"query": "DELETE WHERE { ?s ?p ?o }"}, sparql)
        )

        assert "error" in result
        assert "read-only" in result["error"]
        sparql.query.assert_not_called()  # type: ignore[attr-defined]

    def test_rejects_empty_query(self):
        sparql = _make_sparql()

        result = asyncio.run(execute_tool("query_ontology", {"query": "  "}, sparql))

        assert "error" in result
        assert "Empty" in result["error"]


# ---------------------------------------------------------------------------
# search_provisions executor
# ---------------------------------------------------------------------------


class TestSearchProvisions:
    def test_builds_filter_query(self):
        sparql = _make_sparql()
        sparql.query.return_value = [  # type: ignore[attr-defined]
            {
                "uri": "http://ontology.seadusloome.ee/KarS_Norm_001",
                "paragrahv": "KarS 121",
                "summary": "Kehaline vaerkohtlemine",
                "source_act": "Karistusseadustik",
            }
        ]

        result = asyncio.run(execute_tool("search_provisions", {"keywords": "kehaline"}, sparql))

        assert "provisions" in result
        assert len(result["provisions"]) == 1
        assert result["provisions"][0]["paragrahv"] == "KarS 121"

    def test_empty_keywords_returns_error(self):
        sparql = _make_sparql()

        result = asyncio.run(execute_tool("search_provisions", {"keywords": ""}, sparql))

        assert "error" in result


# ---------------------------------------------------------------------------
# get_draft_impact executor
# ---------------------------------------------------------------------------


class TestGetDraftImpact:
    @patch("app.chat.tools.get_connection")
    def test_returns_report_data(self, mock_get_conn: MagicMock):
        sparql = _make_sparql()
        mock_conn = MagicMock()
        mock_get_conn.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_get_conn.return_value.__exit__ = MagicMock(return_value=False)

        report_data = {"conflicts": [], "gaps": []}
        mock_conn.execute.return_value.fetchone.return_value = (report_data,)

        result = asyncio.run(
            execute_tool(
                "get_draft_impact",
                {"draft_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"},
                sparql,
            )
        )

        assert "report" in result
        assert result["report"] == report_data

    @patch("app.chat.tools.get_connection")
    def test_missing_report_returns_error(self, mock_get_conn: MagicMock):
        sparql = _make_sparql()
        mock_conn = MagicMock()
        mock_get_conn.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_get_conn.return_value.__exit__ = MagicMock(return_value=False)
        mock_conn.execute.return_value.fetchone.return_value = None

        result = asyncio.run(
            execute_tool(
                "get_draft_impact",
                {"draft_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"},
                sparql,
            )
        )

        assert "error" in result
        assert "No impact report" in result["error"]


# ---------------------------------------------------------------------------
# get_provision_details executor
# ---------------------------------------------------------------------------


class TestGetProvisionDetails:
    def test_returns_provision(self):
        sparql = _make_sparql()
        sparql.query.return_value = [  # type: ignore[attr-defined]
            {
                "uri": "http://ontology.seadusloome.ee/KarS_Norm_001",
                "text": "Teise inimese tervise kahjustamine...",
                "sourceAct": "Karistusseadustik",
                "paragrahv": "121",
                "related": "",
                "relLabel": "",
            }
        ]

        result = asyncio.run(
            execute_tool(
                "get_provision_details",
                {"provision_uri": "estleg:KarS_Norm_001"},
                sparql,
            )
        )

        assert "provision" in result
        assert result["provision"]["paragrahv"] == "121"

    def test_empty_uri_returns_error(self):
        sparql = _make_sparql()

        result = asyncio.run(execute_tool("get_provision_details", {"provision_uri": ""}, sparql))

        assert "error" in result


# ---------------------------------------------------------------------------
# Executor dispatch
# ---------------------------------------------------------------------------


class TestExecuteToolDispatch:
    def test_unknown_tool_returns_error(self):
        sparql = _make_sparql()

        result = asyncio.run(execute_tool("nonexistent_tool", {}, sparql))

        assert result == {"error": "Unknown tool: nonexistent_tool"}

    def test_executor_exception_returns_error(self):
        """An executor that raises is caught and returned as error dict."""
        sparql = _make_sparql()
        sparql.query.side_effect = RuntimeError("Connection refused")  # type: ignore[attr-defined]

        result = asyncio.run(
            execute_tool(
                "query_ontology",
                {"query": "SELECT ?s WHERE { ?s ?p ?o }"},
                sparql,
            )
        )

        assert "error" in result
        assert "Connection refused" in result["error"]
