"""Tenant-scoping regression tests for institution-label lookup (#844 A2).

DoD verification:
- ``get_institution_label`` only resolves valid ``estleg:`` institution
  URIs and requires ``?institution a estleg:Institution``.
- The lookup rejects non-institution and non-``estleg:`` URIs.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from app.analyysikeskus.competency import (
    _INSTITUTION_LABEL_QUERY,
    get_institution_label,
    is_estleg_institution_uri,
)


def _make_client(rows: list[dict[str, str]]) -> MagicMock:
    client = MagicMock()
    client.query.return_value = rows
    return client


class TestInstitutionUriGuard:
    def test_estleg_uri_accepted(self):
        assert is_estleg_institution_uri("https://data.riik.ee/ontology/estleg#Institution_AKI")

    def test_foreign_http_uri_rejected(self):
        assert is_estleg_institution_uri("https://evil.example.com/x") is False

    def test_other_org_draft_graph_uri_rejected(self):
        assert (
            is_estleg_institution_uri(
                "https://data.riik.ee/ontology/estleg/drafts/"
                "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb#self"
            )
            is False
        )

    def test_empty_rejected(self):
        assert is_estleg_institution_uri("") is False


class TestGetInstitutionLabel:
    def test_query_requires_institution_type(self):
        # The SPARQL template must pin ``a estleg:Institution`` so a
        # non-Institution estleg URI can't resolve a label.
        assert "a estleg:Institution" in _INSTITUTION_LABEL_QUERY

    def test_estleg_institution_resolves(self):
        client = _make_client([{"label": "Andmekaitse Inspektsioon"}])
        label = get_institution_label(
            "https://data.riik.ee/ontology/estleg#Institution_AKI",
            sparql_client=client,
        )
        assert label == "Andmekaitse Inspektsioon"
        client.query.assert_called_once()

    def test_non_estleg_uri_never_hits_jena(self):
        client = _make_client([{"label": "SHOULD NOT BE RETURNED"}])
        label = get_institution_label(
            "https://evil.example.com/institution",
            sparql_client=client,
        )
        assert label == ""
        # Crucially, the foreign URI never reached the triplestore.
        client.query.assert_not_called()

    def test_foreign_draft_graph_uri_never_hits_jena(self):
        client = _make_client([{"label": "ORG B DRAFT TITLE"}])
        label = get_institution_label(
            "https://data.riik.ee/ontology/estleg/drafts/"
            "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb#self",
            sparql_client=client,
        )
        assert label == ""
        client.query.assert_not_called()

    def test_empty_uri_returns_empty(self):
        client = _make_client([])
        assert get_institution_label("", sparql_client=client) == ""
        client.query.assert_not_called()

    def test_non_institution_estleg_uri_yields_no_label(self):
        """An in-namespace URI that is not an Institution: the type
        constraint in the query means Jena returns no rows, so the label
        is empty even though the namespace guard passed."""
        client = _make_client([])  # query returns nothing (type mismatch)
        label = get_institution_label(
            "https://data.riik.ee/ontology/estleg#KarS_Par_133",
            sparql_client=client,
        )
        assert label == ""
        # The query DID run (namespace guard passed) but matched nothing.
        client.query.assert_called_once()
