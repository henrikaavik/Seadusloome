"""Unit tests for the SPARQL client."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from app.ontology.sparql_client import SparqlClient, _extract_bindings, _sanitize_sparql_value

# ---------------------------------------------------------------------------
# _extract_bindings
# ---------------------------------------------------------------------------


class TestExtractBindings:
    def test_basic_extraction(self):
        raw = {
            "results": {
                "bindings": [
                    {
                        "entity": {"type": "uri", "value": "http://example.org/e1"},
                        "label": {"type": "literal", "value": "Test Entity"},
                    },
                    {
                        "entity": {"type": "uri", "value": "http://example.org/e2"},
                        "label": {"type": "literal", "value": "Another Entity"},
                    },
                ]
            }
        }
        result = _extract_bindings(raw)
        assert len(result) == 2
        assert result[0] == {"entity": "http://example.org/e1", "label": "Test Entity"}
        assert result[1] == {"entity": "http://example.org/e2", "label": "Another Entity"}

    def test_empty_bindings(self):
        raw = {"results": {"bindings": []}}
        assert _extract_bindings(raw) == []

    def test_missing_results_key(self):
        assert _extract_bindings({}) == []

    def test_missing_bindings_key(self):
        assert _extract_bindings({"results": {}}) == []

    def test_missing_value_key(self):
        raw = {
            "results": {
                "bindings": [
                    {"entity": {"type": "uri"}},  # no "value" key
                ]
            }
        }
        result = _extract_bindings(raw)
        assert result == [{"entity": ""}]

    def test_estonian_characters(self):
        """Ensure Estonian characters (ä, ö, ü, õ, š, ž) pass through."""
        raw = {
            "results": {
                "bindings": [
                    {"label": {"type": "literal", "value": "Määrustik šokeeriv žürii"}},
                    {"label": {"type": "literal", "value": "Õigusakt üldine"}},
                ]
            }
        }
        result = _extract_bindings(raw)
        assert result[0]["label"] == "Määrustik šokeeriv žürii"
        assert result[1]["label"] == "Õigusakt üldine"


# ---------------------------------------------------------------------------
# _sanitize_sparql_value
# ---------------------------------------------------------------------------


class TestSanitizeSparqlValue:
    def test_escapes_quotes(self):
        assert _sanitize_sparql_value('say "hello"') == 'say \\"hello\\"'

    def test_escapes_backslash(self):
        assert _sanitize_sparql_value("a\\b") == "a\\\\b"

    def test_escapes_newlines(self):
        assert _sanitize_sparql_value("line1\nline2") == "line1\\nline2"

    def test_plain_text_unchanged(self):
        assert _sanitize_sparql_value("hello world") == "hello world"

    def test_estonian_chars_unchanged(self):
        text = "Töötasu käsitlemine žanriüleselt"
        assert _sanitize_sparql_value(text) == text


# ---------------------------------------------------------------------------
# SparqlClient.__init__
# ---------------------------------------------------------------------------


class TestSparqlClientInit:
    def test_defaults(self):
        client = SparqlClient()
        assert "localhost:3030" in client.jena_url
        assert client.dataset == "ontology"

    def test_custom_values(self):
        client = SparqlClient(jena_url="http://jena:3030", dataset="laws")
        assert client.jena_url == "http://jena:3030"
        assert client.dataset == "laws"

    def test_endpoint_property(self):
        client = SparqlClient(jena_url="http://jena:3030", dataset="laws")
        assert client.endpoint == "http://jena:3030/laws/sparql"

    def test_env_var_override(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("JENA_URL", "http://env-jena:3030")
        monkeypatch.setenv("JENA_DATASET", "env-ds")
        client = SparqlClient()
        assert client.jena_url == "http://env-jena:3030"
        assert client.dataset == "env-ds"


# ---------------------------------------------------------------------------
# SparqlClient._inject_bindings
# ---------------------------------------------------------------------------


class TestInjectBindings:
    def test_no_bindings(self):
        client = SparqlClient()
        query = "SELECT ?s WHERE { ?s ?p ?o }"
        assert client._inject_bindings(query, {}) == query

    def test_single_binding(self):
        client = SparqlClient()
        query = "SELECT ?s WHERE { ?s ?p ?o }"
        result = client._inject_bindings(query, {"name": "test"})
        assert 'VALUES ?name { "test" }' in result

    def test_binding_escapes_quotes(self):
        client = SparqlClient()
        query = "SELECT ?s WHERE { ?s ?p ?o }"
        result = client._inject_bindings(query, {"name": 'val"ue'})
        assert 'VALUES ?name { "val\\"ue" }' in result

    def test_binding_sanitizes_var_name(self):
        client = SparqlClient()
        query = "SELECT ?s WHERE { ?s ?p ?o }"
        result = client._inject_bindings(query, {"na;me": "test"})
        # Semicolons should be stripped from variable name
        assert "?name" in result


# ---------------------------------------------------------------------------
# SparqlClient.query — mocked HTTP
# ---------------------------------------------------------------------------


class TestSparqlClientQuery:
    def test_successful_query(self):
        client = SparqlClient()
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "results": {
                "bindings": [
                    {
                        "s": {"type": "uri", "value": "http://example.org/e1"},
                        "label": {"type": "literal", "value": "Test"},
                    }
                ]
            }
        }
        mock_response.raise_for_status = MagicMock()

        with patch("app.ontology.sparql_client.httpx.post", return_value=mock_response):
            result = client.query("SELECT ?s ?label WHERE { ?s rdfs:label ?label }")

        assert len(result) == 1
        assert result[0]["s"] == "http://example.org/e1"
        assert result[0]["label"] == "Test"

    def test_connection_error_returns_empty(self):
        client = SparqlClient()

        with patch(
            "app.ontology.sparql_client.httpx.post",
            side_effect=httpx.ConnectError("Connection refused"),
        ):
            result = client.query("SELECT ?s WHERE { ?s ?p ?o }")

        assert result == []

    def test_timeout_returns_empty(self):
        client = SparqlClient()

        with patch(
            "app.ontology.sparql_client.httpx.post",
            side_effect=httpx.ReadTimeout("Timed out"),
        ):
            result = client.query("SELECT ?s WHERE { ?s ?p ?o }")

        assert result == []

    def test_http_error_returns_empty(self):
        client = SparqlClient()
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "Server error", request=MagicMock(), response=mock_response
        )

        with patch("app.ontology.sparql_client.httpx.post", return_value=mock_response):
            result = client.query("SELECT ?s WHERE { ?s ?p ?o }")

        assert result == []

    def test_empty_results(self):
        client = SparqlClient()
        mock_response = MagicMock()
        mock_response.json.return_value = {"results": {"bindings": []}}
        mock_response.raise_for_status = MagicMock()

        with patch("app.ontology.sparql_client.httpx.post", return_value=mock_response):
            result = client.query("SELECT ?s WHERE { ?s ?p ?o }")

        assert result == []


# ---------------------------------------------------------------------------
# SparqlClient.ask — mocked HTTP
# ---------------------------------------------------------------------------


class TestSparqlClientAsk:
    def test_ask_true(self):
        client = SparqlClient()
        mock_response = MagicMock()
        mock_response.json.return_value = {"boolean": True}
        mock_response.raise_for_status = MagicMock()

        with patch("app.ontology.sparql_client.httpx.post", return_value=mock_response):
            assert client.ask("ASK { ?s ?p ?o }") is True

    def test_ask_false(self):
        client = SparqlClient()
        mock_response = MagicMock()
        mock_response.json.return_value = {"boolean": False}
        mock_response.raise_for_status = MagicMock()

        with patch("app.ontology.sparql_client.httpx.post", return_value=mock_response):
            assert client.ask("ASK { ?s ?p ?o }") is False

    def test_ask_error_returns_false(self):
        client = SparqlClient()

        with patch(
            "app.ontology.sparql_client.httpx.post",
            side_effect=httpx.ConnectError("Connection refused"),
        ):
            assert client.ask("ASK { ?s ?p ?o }") is False


# ---------------------------------------------------------------------------
# SparqlClient.count — mocked HTTP
# ---------------------------------------------------------------------------


class TestSparqlClientCount:
    def test_count_returns_integer(self):
        client = SparqlClient()
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "results": {"bindings": [{"count": {"type": "literal", "value": "42"}}]}
        }
        mock_response.raise_for_status = MagicMock()

        with patch("app.ontology.sparql_client.httpx.post", return_value=mock_response):
            assert client.count("SELECT (COUNT(*) AS ?count) WHERE { ?s ?p ?o }") == 42

    def test_count_empty_returns_zero(self):
        client = SparqlClient()
        mock_response = MagicMock()
        mock_response.json.return_value = {"results": {"bindings": []}}
        mock_response.raise_for_status = MagicMock()

        with patch("app.ontology.sparql_client.httpx.post", return_value=mock_response):
            assert client.count("SELECT (COUNT(*) AS ?count) WHERE { ?s ?p ?o }") == 0

    def test_count_invalid_value_returns_zero(self):
        client = SparqlClient()
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "results": {"bindings": [{"count": {"type": "literal", "value": "not_a_number"}}]}
        }
        mock_response.raise_for_status = MagicMock()

        with patch("app.ontology.sparql_client.httpx.post", return_value=mock_response):
            assert client.count("SELECT (COUNT(*) AS ?count) WHERE { ?s ?p ?o }") == 0
