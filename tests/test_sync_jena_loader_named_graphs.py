"""Unit tests for the named-graph helpers in ``app.sync.jena_loader``.

Every test patches ``httpx`` so the tests never talk to a real Fuseki
instance. The patterns mirror ``tests/test_sync_orchestrator.py`` —
``patch`` the transport and drive the helper from the outside.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.sync import jena_loader

# #480: the URI must match the ``_SAFE_GRAPH_URI`` allowlist in
# ``jena_loader`` — ``put_named_graph`` / ``delete_named_graph``
# now reject anything outside the production ``drafts/<uuid>`` shape
# before the HTTP call.
_GRAPH_URI = "https://data.riik.ee/ontology/estleg/drafts/11111111-1111-1111-1111-111111111111"


class TestPutNamedGraph:
    @patch("app.sync.jena_loader.httpx.put")
    def test_put_named_graph_happy_path(self, mock_put: MagicMock):
        """A 2xx response must return True and encode the graph URI."""
        response = MagicMock()
        response.status_code = 200
        mock_put.return_value = response

        assert jena_loader.put_named_graph(_GRAPH_URI, "@prefix ex: <urn:ex#> .") is True

        mock_put.assert_called_once()
        called_url = mock_put.call_args.args[0]
        # The graph URI must be URL-encoded (colons, slashes escaped).
        assert "graph=" in called_url
        assert "%3A%2F%2F" in called_url or "%3A" in called_url
        # Content-Type hints at Turtle + UTF-8 for Estonian characters.
        headers = mock_put.call_args.kwargs["headers"]
        assert "text/turtle" in headers["Content-Type"]
        assert "utf-8" in headers["Content-Type"]

    @patch("app.sync.jena_loader.httpx.put")
    def test_put_named_graph_204_no_content(self, mock_put: MagicMock):
        """204 is also a success per the Graph Store Protocol spec."""
        response = MagicMock()
        response.status_code = 204
        mock_put.return_value = response

        assert jena_loader.put_named_graph(_GRAPH_URI, "# turtle") is True

    @patch("app.sync.jena_loader.httpx.put")
    def test_put_named_graph_server_error(self, mock_put: MagicMock):
        """500 response must return False and log a warning."""
        response = MagicMock()
        response.status_code = 500
        response.text = "boom"
        mock_put.return_value = response

        assert jena_loader.put_named_graph(_GRAPH_URI, "# turtle") is False

    @patch("app.sync.jena_loader.httpx.put")
    def test_put_named_graph_transport_error(self, mock_put: MagicMock):
        """httpx.HTTPError must be caught and surfaced as False."""
        import httpx

        mock_put.side_effect = httpx.ConnectError("jena down")
        assert jena_loader.put_named_graph(_GRAPH_URI, "# turtle") is False


class TestDeleteNamedGraph:
    @patch("app.sync.jena_loader.httpx.delete")
    def test_delete_named_graph_happy_path(self, mock_delete: MagicMock):
        """204 is the canonical success response for GSP DELETE."""
        response = MagicMock()
        response.status_code = 204
        mock_delete.return_value = response

        assert jena_loader.delete_named_graph(_GRAPH_URI) is True

    @patch("app.sync.jena_loader.httpx.delete")
    def test_delete_named_graph_idempotent_404(self, mock_delete: MagicMock):
        """404 must be treated as success (idempotent delete)."""
        response = MagicMock()
        response.status_code = 404
        mock_delete.return_value = response

        assert jena_loader.delete_named_graph(_GRAPH_URI) is True

    @patch("app.sync.jena_loader.httpx.delete")
    def test_delete_named_graph_server_error(self, mock_delete: MagicMock):
        """500 must be reported as failure."""
        response = MagicMock()
        response.status_code = 500
        response.text = "db dead"
        mock_delete.return_value = response

        assert jena_loader.delete_named_graph(_GRAPH_URI) is False

    @patch("app.sync.jena_loader.httpx.delete")
    def test_delete_named_graph_transport_error(self, mock_delete: MagicMock):
        """Connection errors must return False, not raise."""
        import httpx

        mock_delete.side_effect = httpx.ConnectError("fuseki unreachable")
        assert jena_loader.delete_named_graph(_GRAPH_URI) is False


class TestGraphUriValidation:
    """#480: PUT/DELETE must reject URIs outside the draft allowlist."""

    @patch("app.sync.jena_loader.httpx.put")
    def test_put_rejects_unsafe_uri(self, mock_put: MagicMock):
        """Garbage URIs must never reach httpx."""
        with pytest.raises(ValueError, match="Unsafe graph URI"):
            jena_loader.put_named_graph("urn:not-a-draft", "# turtle")
        mock_put.assert_not_called()

    @patch("app.sync.jena_loader.httpx.put")
    def test_put_rejects_default_graph_uri(self, mock_put: MagicMock):
        """The default Jena graph URI must be rejected too."""
        with pytest.raises(ValueError, match="Unsafe graph URI"):
            jena_loader.put_named_graph(
                "http://jena.apache.org/Default",
                "# turtle",
            )
        mock_put.assert_not_called()

    @patch("app.sync.jena_loader.httpx.delete")
    def test_delete_rejects_unsafe_uri(self, mock_delete: MagicMock):
        """Garbage URIs must never reach httpx on delete either."""
        with pytest.raises(ValueError, match="Unsafe graph URI"):
            jena_loader.delete_named_graph("not a uri at all")
        mock_delete.assert_not_called()

    def test_validator_re_exported_from_queries(self):
        """#480: ``app.docs.impact.queries`` must re-export the validator.

        This guards against future drift — someone refactoring the
        queries module could otherwise inline a second regex that
        drifts from the jena_loader canonical definition.
        """
        from app.docs.impact import queries

        assert queries._validate_graph_uri is jena_loader._validate_graph_uri
        assert queries._SAFE_GRAPH_URI is jena_loader._SAFE_GRAPH_URI


class TestNamedGraphExists:
    @patch("app.sync.jena_loader.httpx.post")
    def test_named_graph_exists_true(self, mock_post: MagicMock):
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {"head": {}, "boolean": True}
        response.raise_for_status.return_value = None
        mock_post.return_value = response

        assert jena_loader.named_graph_exists(_GRAPH_URI) is True

        # The query must have been an ASK with the graph URI embedded.
        sent = mock_post.call_args.kwargs["data"]
        assert "ASK" in sent["query"]
        assert _GRAPH_URI in sent["query"]

    @patch("app.sync.jena_loader.httpx.post")
    def test_named_graph_exists_false(self, mock_post: MagicMock):
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {"head": {}, "boolean": False}
        response.raise_for_status.return_value = None
        mock_post.return_value = response

        assert jena_loader.named_graph_exists(_GRAPH_URI) is False

    @patch("app.sync.jena_loader.httpx.post")
    def test_named_graph_exists_transport_error(self, mock_post: MagicMock):
        import httpx

        mock_post.side_effect = httpx.ConnectError("no jena")
        assert jena_loader.named_graph_exists(_GRAPH_URI) is False


class TestGetNamedGraphTripleCount:
    @patch("app.sync.jena_loader.httpx.post")
    def test_triple_count_happy_path(self, mock_post: MagicMock):
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {
            "head": {"vars": ["count"]},
            "results": {"bindings": [{"count": {"value": "42"}}]},
        }
        response.raise_for_status.return_value = None
        mock_post.return_value = response

        assert jena_loader.get_named_graph_triple_count(_GRAPH_URI) == 42

        sent = mock_post.call_args.kwargs["data"]
        # The query must be wrapped in a GRAPH clause.
        assert "GRAPH <" + _GRAPH_URI + ">" in sent["query"]

    @patch("app.sync.jena_loader.httpx.post")
    def test_triple_count_empty_bindings(self, mock_post: MagicMock):
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {
            "head": {"vars": ["count"]},
            "results": {"bindings": []},
        }
        response.raise_for_status.return_value = None
        mock_post.return_value = response

        assert jena_loader.get_named_graph_triple_count(_GRAPH_URI) == 0

    @patch("app.sync.jena_loader.httpx.post")
    def test_triple_count_transport_error(self, mock_post: MagicMock):
        import httpx

        mock_post.side_effect = httpx.ConnectError("nope")
        assert jena_loader.get_named_graph_triple_count(_GRAPH_URI) == 0
