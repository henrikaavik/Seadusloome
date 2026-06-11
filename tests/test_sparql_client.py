"""Unit tests for the SPARQL client."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

import app.ontology.sparql_client as sparql_mod
from app.ontology.sparql_client import (
    SparqlClient,
    _extract_bindings,
    _get_shared_http_client,
    _sanitize_sparql_value,
    close_shared_http_client,
)


@pytest.fixture(autouse=True)
def _reset_shared_http_client():
    """Drop the pooled client around each test so a mocked ``post`` from
    one test never leaks into the next, and so the lazy-init path is
    exercised fresh."""
    close_shared_http_client()
    yield
    close_shared_http_client()


def _patch_post(**kwargs):
    """Patch the pooled client's ``post`` method.

    ``_execute`` now routes through the shared ``httpx.Client`` instead
    of the module-level ``httpx.post`` (connection pooling — see the
    SparqlClient docstring), so tests patch the bound method on the
    singleton. ``return_value`` / ``side_effect`` keyword semantics are
    identical to patching a free function.
    """
    return patch.object(_get_shared_http_client(), "post", **kwargs)


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

        with _patch_post(return_value=mock_response):
            result = client.query("SELECT ?s ?label WHERE { ?s rdfs:label ?label }")

        assert len(result) == 1
        assert result[0]["s"] == "http://example.org/e1"
        assert result[0]["label"] == "Test"

    def test_connection_error_returns_empty(self):
        client = SparqlClient()

        with _patch_post(
            side_effect=httpx.ConnectError("Connection refused"),
        ):
            result = client.query("SELECT ?s WHERE { ?s ?p ?o }")

        assert result == []

    def test_timeout_returns_empty(self):
        client = SparqlClient()

        with _patch_post(
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

        with _patch_post(return_value=mock_response):
            result = client.query("SELECT ?s WHERE { ?s ?p ?o }")

        assert result == []

    def test_empty_results(self):
        client = SparqlClient()
        mock_response = MagicMock()
        mock_response.json.return_value = {"results": {"bindings": []}}
        mock_response.raise_for_status = MagicMock()

        with _patch_post(return_value=mock_response):
            result = client.query("SELECT ?s WHERE { ?s ?p ?o }")

        assert result == []


class TestSparqlClientQueryOnErrorRaise:
    """``on_error='raise'`` lets callers distinguish "Jena returned 0 rows"
    from "Jena was unreachable". The default ``'swallow'`` keeps the
    legacy behaviour (return ``[]`` and log) so existing call sites are
    untouched.

    This option exists for callers that cache results and must avoid
    poisoning the cache on a transient outage — see
    ``app/docs/reference_resolver.py::_get_abbrev_maps`` (P2#5).
    """

    def test_connection_error_raises_when_on_error_is_raise(self):
        client = SparqlClient()
        with _patch_post(
            side_effect=httpx.ConnectError("Connection refused"),
        ):
            with pytest.raises(httpx.ConnectError):
                client.query(
                    "SELECT ?s WHERE { ?s ?p ?o }",
                    on_error="raise",
                )

    def test_timeout_raises_when_on_error_is_raise(self):
        client = SparqlClient()
        with _patch_post(
            side_effect=httpx.ReadTimeout("Timed out"),
        ):
            with pytest.raises(httpx.TimeoutException):
                client.query(
                    "SELECT ?s WHERE { ?s ?p ?o }",
                    on_error="raise",
                )

    def test_http_status_error_raises_when_on_error_is_raise(self):
        client = SparqlClient()
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "Server error", request=MagicMock(), response=mock_response
        )
        with _patch_post(return_value=mock_response):
            with pytest.raises(httpx.HTTPStatusError):
                client.query(
                    "SELECT ?s WHERE { ?s ?p ?o }",
                    on_error="raise",
                )

    def test_empty_result_does_not_raise_under_on_error_raise(self):
        """``on_error='raise'`` only fires on httpx-level errors.

        A genuine empty result set (HTTP 200 + zero bindings) must
        still come back as an empty list, not raise. Without this the
        resolver's "Jena exists but has no data yet" deploy state
        would look like an outage.
        """
        client = SparqlClient()
        mock_response = MagicMock()
        mock_response.json.return_value = {"results": {"bindings": []}}
        mock_response.raise_for_status = MagicMock()
        with _patch_post(return_value=mock_response):
            result = client.query(
                "SELECT ?s WHERE { ?s ?p ?o }",
                on_error="raise",
            )
        assert result == []

    def test_default_swallow_preserves_legacy_behaviour(self):
        """Without ``on_error`` the legacy behaviour is preserved: log + empty."""
        client = SparqlClient()
        with _patch_post(
            side_effect=httpx.ConnectError("Connection refused"),
        ):
            # No on_error kwarg → swallows + returns empty (legacy).
            assert client.query("SELECT ?s WHERE { ?s ?p ?o }") == []


# ---------------------------------------------------------------------------
# SparqlClient.ask — mocked HTTP
# ---------------------------------------------------------------------------


class TestSparqlClientAsk:
    def test_ask_true(self):
        client = SparqlClient()
        mock_response = MagicMock()
        mock_response.json.return_value = {"boolean": True}
        mock_response.raise_for_status = MagicMock()

        with _patch_post(return_value=mock_response):
            assert client.ask("ASK { ?s ?p ?o }") is True

    def test_ask_false(self):
        client = SparqlClient()
        mock_response = MagicMock()
        mock_response.json.return_value = {"boolean": False}
        mock_response.raise_for_status = MagicMock()

        with _patch_post(return_value=mock_response):
            assert client.ask("ASK { ?s ?p ?o }") is False

    def test_ask_error_returns_false(self):
        client = SparqlClient()

        with _patch_post(
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

        with _patch_post(return_value=mock_response):
            assert client.count("SELECT (COUNT(*) AS ?count) WHERE { ?s ?p ?o }") == 42

    def test_count_empty_returns_zero(self):
        client = SparqlClient()
        mock_response = MagicMock()
        mock_response.json.return_value = {"results": {"bindings": []}}
        mock_response.raise_for_status = MagicMock()

        with _patch_post(return_value=mock_response):
            assert client.count("SELECT (COUNT(*) AS ?count) WHERE { ?s ?p ?o }") == 0

    def test_count_invalid_value_returns_zero(self):
        client = SparqlClient()
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "results": {"bindings": [{"count": {"type": "literal", "value": "not_a_number"}}]}
        }
        mock_response.raise_for_status = MagicMock()

        with _patch_post(return_value=mock_response):
            assert client.count("SELECT (COUNT(*) AS ?count) WHERE { ?s ?p ?o }") == 0


class TestSparqlClientCountOnError:
    """``count(on_error=...)`` lets a caller tell a real zero apart from an
    outage so pagination never renders a dead Jena as a truthful
    ``total: 0``."""

    def test_default_swallow_returns_zero_on_outage(self):
        """Legacy default: a transport failure yields ``0`` (no raise)."""
        client = SparqlClient()
        with _patch_post(side_effect=httpx.ConnectError("refused")):
            assert client.count("SELECT (COUNT(*) AS ?count) WHERE { ?s ?p ?o }") == 0

    def test_on_error_raise_propagates_outage(self):
        """``on_error='raise'`` re-raises the httpx error instead of 0."""
        client = SparqlClient()
        with _patch_post(side_effect=httpx.ConnectError("refused")):
            with pytest.raises(httpx.ConnectError):
                client.count(
                    "SELECT (COUNT(*) AS ?count) WHERE { ?s ?p ?o }",
                    on_error="raise",
                )

    def test_on_error_raise_still_zero_on_genuine_empty(self):
        """A reachable Jena with zero rows is a real ``0``, not an error."""
        client = SparqlClient()
        mock_response = MagicMock()
        mock_response.json.return_value = {"results": {"bindings": []}}
        mock_response.raise_for_status = MagicMock()
        with _patch_post(return_value=mock_response):
            assert (
                client.count(
                    "SELECT (COUNT(*) AS ?count) WHERE { ?s ?p ?o }",
                    on_error="raise",
                )
                == 0
            )


# ---------------------------------------------------------------------------
# Shared pooled httpx.Client (lazy-init singleton)
# ---------------------------------------------------------------------------


class TestSharedHttpClient:
    def test_lazy_init_builds_once_and_is_shared(self):
        """No client exists until first use; then every caller shares one."""
        # The autouse fixture closed any prior client, so we start cold.
        assert sparql_mod._shared_client is None
        first = _get_shared_http_client()
        second = _get_shared_http_client()
        assert first is second
        assert isinstance(first, httpx.Client)

    def test_pool_limits_configured(self):
        """The pool carries bounded connection limits (FD-exhaustion guard)."""
        # Assert on the module-level limits the pool is built from rather
        # than httpx's private ``Client._limits`` attribute.
        assert sparql_mod._HTTP_LIMITS.max_connections == 20
        assert sparql_mod._HTTP_LIMITS.max_keepalive_connections == 10
        # The built client is a real httpx.Client wired to that pool.
        assert isinstance(_get_shared_http_client(), httpx.Client)

    def test_distinct_sparqlclients_reuse_one_pool(self):
        """Two SparqlClient instances issue requests through the same pool."""
        mock_response = MagicMock()
        mock_response.json.return_value = {"results": {"bindings": []}}
        mock_response.raise_for_status = MagicMock()
        a = SparqlClient(jena_url="http://a:3030", dataset="x")
        b = SparqlClient(jena_url="http://b:3030", dataset="y")
        with _patch_post(return_value=mock_response) as post:
            a.query("SELECT ?s WHERE { ?s ?p ?o }")
            b.query("SELECT ?s WHERE { ?s ?p ?o }")
        # Both calls landed on the single pooled client's post.
        assert post.call_count == 2
        # Each instance targeted its own endpoint via the shared pool.
        endpoints = {call.args[0] for call in post.call_args_list}
        assert endpoints == {"http://a:3030/x/sparql", "http://b:3030/y/sparql"}

    def test_close_is_idempotent_when_never_built(self):
        """Closing before any client was built is a no-op, not an error."""
        close_shared_http_client()
        assert sparql_mod._shared_client is None
        # Calling again must not raise.
        close_shared_http_client()
