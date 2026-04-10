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


_TEST_AUTH = {
    "id": "11111111-1111-1111-1111-111111111111",
    "org_id": "22222222-2222-2222-2222-222222222222",
}


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
                auth=_TEST_AUTH,
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
                auth=_TEST_AUTH,
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


# ---------------------------------------------------------------------------
# H1: SPARQL whitelist — additional tests for comment stripping, SERVICE, etc.
# ---------------------------------------------------------------------------


class TestSparqlWhitelist:
    def test_select_with_prefixes_passes(self):
        query = """
        PREFIX estleg: <http://ontology.seadusloome.ee/>
        PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
        SELECT ?s ?p ?o WHERE { ?s ?p ?o }
        """
        assert _is_read_only_sparql(query) is True

    def test_ask_passes(self):
        assert _is_read_only_sparql("ASK { ?s ?p ?o }") is True

    def test_describe_passes(self):
        assert _is_read_only_sparql("DESCRIBE <http://example.org/e1>") is True

    def test_construct_passes(self):
        assert _is_read_only_sparql("CONSTRUCT { ?s ?p ?o } WHERE { ?s ?p ?o }") is True

    def test_insert_rejected(self):
        assert _is_read_only_sparql("INSERT DATA { <a> <b> <c> }") is False

    def test_service_rejected(self):
        """SERVICE keyword should be blocked (SSRF prevention)."""
        query = "SELECT ?s WHERE { SERVICE <http://evil.com/sparql> { ?s ?p ?o } }"
        assert _is_read_only_sparql(query) is False

    def test_comment_wrapped_mutation_rejected(self):
        """Mutation hidden behind a comment-like prefix should be caught."""
        # The comment is stripped, leaving just the INSERT
        query = "# This is a SELECT query\nINSERT DATA { <a> <b> <c> }"
        assert _is_read_only_sparql(query) is False

    def test_comment_only_query_rejected(self):
        """A query that is all comments (no query form) should be rejected."""
        query = "# just a comment"
        assert _is_read_only_sparql(query) is False

    def test_select_after_comment_passes(self):
        """A legitimate SELECT with a comment line should pass."""
        query = "# Find all entities\nSELECT ?s WHERE { ?s ?p ?o }"
        assert _is_read_only_sparql(query) is True

    def test_service_in_comment_with_select_passes(self):
        """SERVICE in a comment should not block a legitimate SELECT."""
        query = "# Uses SERVICE pattern\nSELECT ?s WHERE { ?s ?p ?o }"
        assert _is_read_only_sparql(query) is True

    def test_bare_update_rejected(self):
        """A query that doesn't start with an allowed form is rejected."""
        query = "DROP GRAPH <http://example.org/g>"
        assert _is_read_only_sparql(query) is False


# ---------------------------------------------------------------------------
# H2: get_draft_impact org-scoping
# ---------------------------------------------------------------------------


class TestGetDraftImpactOrgScoping:
    def test_no_auth_returns_error(self):
        """get_draft_impact without auth should return an error."""
        sparql = _make_sparql()

        result = asyncio.run(
            execute_tool(
                "get_draft_impact",
                {"draft_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"},
                sparql,
                auth=None,
            )
        )

        assert "error" in result
        assert "org_id" in result["error"].lower() or "authentication" in result["error"].lower()

    def test_auth_without_org_id_returns_error(self):
        """get_draft_impact with auth but no org_id should return an error."""
        sparql = _make_sparql()

        result = asyncio.run(
            execute_tool(
                "get_draft_impact",
                {"draft_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"},
                sparql,
                auth={"id": "some-user"},
            )
        )

        assert "error" in result

    @patch("app.chat.tools.get_connection")
    def test_cross_org_draft_returns_empty(self, mock_get_conn: MagicMock):
        """Querying a draft from another org should return 'not found' (SQL filters it out)."""
        sparql = _make_sparql()
        mock_conn = MagicMock()
        mock_get_conn.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_get_conn.return_value.__exit__ = MagicMock(return_value=False)

        # The JOIN + org_id filter returns no rows for a cross-org draft
        mock_conn.execute.return_value.fetchone.return_value = None

        other_org_auth = {
            "id": "11111111-1111-1111-1111-111111111111",
            "org_id": "99999999-9999-9999-9999-999999999999",
        }

        result = asyncio.run(
            execute_tool(
                "get_draft_impact",
                {"draft_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"},
                sparql,
                auth=other_org_auth,
            )
        )

        assert "error" in result
        assert "No impact report" in result["error"]

        # Verify the SQL included both draft_id and org_id parameters
        call_args = mock_conn.execute.call_args
        sql = call_args[0][0]
        params = call_args[0][1]
        assert "d.org_id" in sql
        assert params == (
            "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            "99999999-9999-9999-9999-999999999999",
        )


# ---------------------------------------------------------------------------
# M3: UUID validation for draft_id
# ---------------------------------------------------------------------------


class TestDraftIdUUIDValidation:
    def test_non_uuid_draft_id_returns_error(self):
        """A non-UUID draft_id should return an error without hitting the DB."""
        sparql = _make_sparql()

        result = asyncio.run(
            execute_tool(
                "get_draft_impact",
                {"draft_id": "not-a-valid-uuid"},
                sparql,
                auth=_TEST_AUTH,
            )
        )

        assert "error" in result
        assert "Invalid draft_id format" in result["error"]

    def test_sql_injection_attempt_returns_error(self):
        """SQL injection attempt via draft_id should be blocked by UUID validation."""
        sparql = _make_sparql()

        result = asyncio.run(
            execute_tool(
                "get_draft_impact",
                {"draft_id": "'; DROP TABLE drafts; --"},
                sparql,
                auth=_TEST_AUTH,
            )
        )

        assert "error" in result
        assert "Invalid draft_id format" in result["error"]

    def test_valid_uuid_draft_id_accepted(self):
        """A valid UUID draft_id should pass the UUID check (may hit DB)."""
        sparql = _make_sparql()

        # The UUID check passes but the DB call would fail without a mock;
        # we just verify it doesn't return "Invalid draft_id format"
        with patch("app.chat.tools.get_connection") as mock_get_conn:
            mock_conn = MagicMock()
            mock_get_conn.return_value.__enter__ = MagicMock(return_value=mock_conn)
            mock_get_conn.return_value.__exit__ = MagicMock(return_value=False)
            mock_conn.execute.return_value.fetchone.return_value = None

            result = asyncio.run(
                execute_tool(
                    "get_draft_impact",
                    {"draft_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"},
                    sparql,
                    auth=_TEST_AUTH,
                )
            )

            # Should not be UUID format error — should proceed to DB query
            assert result.get("error") != "Invalid draft_id format"


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
