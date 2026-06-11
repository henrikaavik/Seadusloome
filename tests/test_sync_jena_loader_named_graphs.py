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

    # --- #722 (epic #714): the allowlist widening to ``adhoc/<uuid>`` -------
    #
    # The Analüüsikeskus "Normi mõjuahel" workflow mints an ephemeral
    # ``…/estleg/adhoc/<uuid4>`` graph, PUTs one ``estleg:references``
    # triple into it, runs the impact analyser, then deletes it. That
    # graph URI must pass ``_validate_graph_uri`` — but a ``urn:…`` /
    # arbitrary URI must still raise.

    _ADHOC_URI = "https://data.riik.ee/ontology/estleg/adhoc/22222222-2222-2222-2222-222222222222"

    def test_validate_accepts_adhoc_uri(self):
        """An ``adhoc/<uuid>`` URI must pass ``_validate_graph_uri`` unchanged."""
        assert jena_loader._validate_graph_uri(self._ADHOC_URI) == self._ADHOC_URI
        # And the regex itself fullmatches it.
        assert jena_loader._SAFE_GRAPH_URI.fullmatch(self._ADHOC_URI)

    def test_validate_still_accepts_drafts_uri(self):
        """The pre-existing ``drafts/<uuid>`` arm must keep working."""
        assert jena_loader._validate_graph_uri(_GRAPH_URI) == _GRAPH_URI

    def test_validate_rejects_urn_uri(self):
        """A ``urn:…`` URI is outside both arms and must raise ``ValueError``."""
        with pytest.raises(ValueError, match="Unsafe graph URI"):
            jena_loader._validate_graph_uri("urn:estleg:adhoc:not-a-uuid")

    def test_validate_rejects_arbitrary_uri(self):
        """An arbitrary HTTPS URI that isn't a drafts/adhoc graph must raise."""
        with pytest.raises(ValueError, match="Unsafe graph URI"):
            jena_loader._validate_graph_uri("https://example.com/whatever/123")
        # A near-miss (right host + path prefix, but a non-UUID tail) too.
        with pytest.raises(ValueError, match="Unsafe graph URI"):
            jena_loader._validate_graph_uri(
                "https://data.riik.ee/ontology/estleg/adhoc/not-a-uuid"
            )

    @patch("app.sync.jena_loader.httpx.put")
    def test_put_accepts_adhoc_uri(self, mock_put: MagicMock):
        """``put_named_graph`` must accept an ``adhoc/<uuid>`` graph (it reaches httpx)."""
        response = MagicMock()
        response.status_code = 204
        mock_put.return_value = response
        assert jena_loader.put_named_graph(self._ADHOC_URI, "# turtle") is True
        mock_put.assert_called_once()

    @patch("app.sync.jena_loader.httpx.delete")
    def test_delete_accepts_adhoc_uri(self, mock_delete: MagicMock):
        """``delete_named_graph`` must accept an ``adhoc/<uuid>`` graph."""
        response = MagicMock()
        response.status_code = 204
        mock_delete.return_value = response
        assert jena_loader.delete_named_graph(self._ADHOC_URI) is True
        mock_delete.assert_called_once()

    def test_validator_re_exported_from_queries(self):
        """#480: ``app.impact.queries`` must re-export the validator.

        This guards against future drift — someone refactoring the
        queries module could otherwise inline a second regex that
        drifts from the jena_loader canonical definition.
        """
        from app.impact import queries

        assert queries._validate_graph_uri is jena_loader._validate_graph_uri
        assert queries._SAFE_GRAPH_URI is jena_loader._SAFE_GRAPH_URI


class TestVersionedGraphUriValidation:
    """#849: the allowlist must accept ``drafts/<uuid>/v<n>`` version graphs.

    v1 uploads mint ``…/drafts/<uuid>``; v2+ uploads mint
    ``…/drafts/<uuid>/v<n>`` (``app.docs.upload`` §9.5). Before this fix the
    allowlist rejected the versioned shape, so every v2+ upload's analyze
    + cleanup raised ``ValueError("Unsafe graph URI")``, retries
    exhausted, and the draft flipped to ``failed``.
    """

    _UUID = "11111111-1111-1111-1111-111111111111"
    _PREFIX = "https://data.riik.ee/ontology/estleg/"
    _V1 = f"{_PREFIX}drafts/{_UUID}"
    _V2 = f"{_PREFIX}drafts/{_UUID}/v2"
    _V10 = f"{_PREFIX}drafts/{_UUID}/v10"
    _ADHOC = f"{_PREFIX}adhoc/22222222-2222-2222-2222-222222222222"

    # --- acceptance matrix -------------------------------------------------

    def test_accepts_v1_bare_uuid(self):
        assert jena_loader._validate_graph_uri(self._V1) == self._V1

    def test_accepts_v2_version_graph(self):
        assert jena_loader._validate_graph_uri(self._V2) == self._V2
        assert jena_loader._SAFE_GRAPH_URI.fullmatch(self._V2)

    def test_accepts_v10_double_digit_version_graph(self):
        assert jena_loader._validate_graph_uri(self._V10) == self._V10

    def test_accepts_adhoc(self):
        assert jena_loader._validate_graph_uri(self._ADHOC) == self._ADHOC

    # --- rejection matrix (malformed / external / injection) ---------------

    @pytest.mark.parametrize(
        "bad",
        [
            "urn:not-a-draft",
            "http://jena.apache.org/Default",
            # non-estleg host with an otherwise valid-looking path
            "https://evil.com/ontology/estleg/drafts/11111111-1111-1111-1111-111111111111",
            # bad uuid
            "https://data.riik.ee/ontology/estleg/drafts/not-a-uuid",
            # version arm with no digits
            "https://data.riik.ee/ontology/estleg/drafts/11111111-1111-1111-1111-111111111111/v",
            # extra path segment after the version
            "https://data.riik.ee/ontology/estleg/drafts/11111111-1111-1111-1111-111111111111/v2/extra",
            # adhoc graphs are never versioned
            "https://data.riik.ee/ontology/estleg/adhoc/22222222-2222-2222-2222-222222222222/v2",
            # fragment at the graph level is not allowed
            "https://data.riik.ee/ontology/estleg/drafts/11111111-1111-1111-1111-111111111111#self",
            # query-string injection
            f"{_PREFIX}drafts/{_UUID}/v2?x=1",
            # SPARQL-injection-shaped tail (angle-bracket break-out attempt)
            f"{_PREFIX}drafts/{_UUID}/v2> }} GRAPH ?g {{",
        ],
    )
    def test_rejects_malformed_external_and_injection(self, bad: str):
        with pytest.raises(ValueError, match="Unsafe graph URI"):
            jena_loader._validate_graph_uri(bad)

    # --- transport accepts the versioned URI end to end --------------------

    @patch("app.sync.jena_loader.httpx.put")
    def test_put_accepts_v2_graph(self, mock_put: MagicMock):
        response = MagicMock()
        response.status_code = 204
        mock_put.return_value = response
        assert jena_loader.put_named_graph(self._V2, "# turtle") is True
        mock_put.assert_called_once()
        # The versioned URI is URL-encoded into the ?graph= param.
        called_url = mock_put.call_args.args[0]
        assert "graph=" in called_url

    @patch("app.sync.jena_loader.httpx.delete")
    def test_delete_accepts_v2_graph(self, mock_delete: MagicMock):
        """#849: version-graph cleanup deletes must pass validation."""
        response = MagicMock()
        response.status_code = 204
        mock_delete.return_value = response
        assert jena_loader.delete_named_graph(self._V2) is True
        mock_delete.assert_called_once()

    @patch("app.sync.jena_loader.httpx.post")
    def test_named_graph_exists_accepts_v2_graph(self, mock_post: MagicMock):
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {"head": {}, "boolean": True}
        response.raise_for_status.return_value = None
        mock_post.return_value = response
        assert jena_loader.named_graph_exists(self._V2) is True

    # --- impact builders + lineage writer share the one regula -------------

    def test_impact_builders_accept_v2_graph(self):
        """All four impact query builders must accept a versioned graph URI.

        They re-use the canonical ``_validate_graph_uri`` (re-exported in
        ``app.impact.queries``), so the #849 widening must reach them
        without a second edit. A versioned URI that previously raised must
        now build a query string containing the GRAPH clause.
        """
        from app.impact.queries import (
            build_affected_entities_query,
            build_conflicts_query,
            build_eu_compliance_query,
            build_gaps_query,
        )

        for builder in (
            build_affected_entities_query,
            build_gaps_query,
            build_eu_compliance_query,
            build_conflicts_query,
        ):
            q = builder(self._V2)
            assert f"GRAPH <{self._V2}>" in q

    def test_conflicts_builder_excludes_own_prior_versions_for_v2(self):
        """#849 + #868 (A5): a v2 graph's conflict query must exclude the
        whole ``…/drafts/<uuid>`` namespace so it never self-conflicts
        against its own v1."""
        from app.impact.queries import build_conflicts_query

        q = build_conflicts_query(self._V2)
        # The version-agnostic prefix (no ``/v2``) is what the exclusion
        # keys on, so v1 and any other version are excluded.
        assert f'!STRSTARTS(str(?otherGraph), "{self._V1}")' in q
        assert "/v2" not in q.split("!STRSTARTS")[1].split("\n")[0]


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
