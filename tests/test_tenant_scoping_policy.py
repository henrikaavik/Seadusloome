"""Unit tests for the shared tenant-scoping policy (#844).

Covers :mod:`app.ontology.scoping` — the single source of truth for
"public-only SPARQL" and the draft/adhoc graph-URI recognisers that the
conflict-masking paths depend on.
"""

from __future__ import annotations

import pytest

from app.ontology.scoping import (
    ADHOC_GRAPH_PREFIX,
    DRAFT_GRAPH_PREFIX,
    assert_public_only,
    draft_graph_prefix_for,
    draft_id_from_uri,
    is_adhoc_graph_uri,
    is_draft_graph_uri,
    public_subject_filter,
    references_named_graph,
)

_V1 = "https://data.riik.ee/ontology/estleg/drafts/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
_V3 = _V1 + "/v3"
_SELF = _V1 + "#self"
_ADHOC = "https://data.riik.ee/ontology/estleg/adhoc/cccccccc-cccc-cccc-cccc-cccccccccccc"
_PUBLIC = "https://data.riik.ee/ontology/estleg#KarS_Par_133"


# ---------------------------------------------------------------------------
# references_named_graph / assert_public_only
# ---------------------------------------------------------------------------


class TestPublicOnly:
    def test_plain_select_is_public(self):
        assert references_named_graph("SELECT ?s WHERE { ?s ?p ?o }") is False
        # Does not raise.
        assert_public_only("SELECT ?s WHERE { ?s ?p ?o }")

    def test_graph_keyword_rejected(self):
        q = "SELECT ?g ?s WHERE { GRAPH ?g { ?s ?p ?o } }"
        assert references_named_graph(q) is True
        with pytest.raises(ValueError):
            assert_public_only(q)

    def test_from_clause_rejected(self):
        q = "SELECT ?s FROM <https://data.riik.ee/x> WHERE { ?s ?p ?o }"
        assert references_named_graph(q) is True
        with pytest.raises(ValueError):
            assert_public_only(q)

    def test_from_named_rejected(self):
        q = "SELECT ?s FROM NAMED <https://data.riik.ee/x> WHERE { ?s ?p ?o }"
        assert references_named_graph(q) is True
        with pytest.raises(ValueError):
            assert_public_only(q)

    def test_graph_hidden_in_comment_still_rejected(self):
        # Comment-stripping must not let a GRAPH keyword survive on a real
        # line; conversely a GRAPH *only* in a comment is not a real
        # reference. Here the GRAPH is real (the comment is a decoy).
        q = "SELECT ?g WHERE {\n  # harmless\n  GRAPH ?g { ?s ?p ?o }\n}"
        assert references_named_graph(q) is True

    def test_graph_only_in_comment_is_allowed(self):
        # A GRAPH token that appears ONLY inside a comment is not a real
        # named-graph reference, so the query is public.
        q = "SELECT ?s WHERE { ?s ?p ?o }  # not a GRAPH really"
        assert references_named_graph(q) is False
        assert_public_only(q)

    def test_case_insensitive(self):
        assert references_named_graph("select ?s where { graph ?g { ?s ?p ?o } }") is True


# ---------------------------------------------------------------------------
# Draft / adhoc URI recognisers
# ---------------------------------------------------------------------------


class TestRecognisers:
    def test_is_draft_graph_uri_v1_and_versioned(self):
        assert is_draft_graph_uri(_V1) is True
        assert is_draft_graph_uri(_V3) is True
        assert is_draft_graph_uri(_SELF) is True

    def test_is_draft_graph_uri_rejects_public_and_adhoc(self):
        assert is_draft_graph_uri(_PUBLIC) is False
        assert is_draft_graph_uri(_ADHOC) is False
        assert is_draft_graph_uri("") is False
        assert is_draft_graph_uri("urn:foo") is False

    def test_is_adhoc_graph_uri(self):
        assert is_adhoc_graph_uri(_ADHOC) is True
        assert is_adhoc_graph_uri(_ADHOC + "#self") is True
        assert is_adhoc_graph_uri(_V1) is False
        assert is_adhoc_graph_uri(_PUBLIC) is False

    def test_draft_id_from_uri(self):
        did = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        assert draft_id_from_uri(_V1) == did
        assert draft_id_from_uri(_V3) == did
        assert draft_id_from_uri(_SELF) == did
        assert draft_id_from_uri(_PUBLIC) is None
        assert draft_id_from_uri(_ADHOC) is None
        assert draft_id_from_uri("") is None

    def test_draft_graph_prefix_for_strips_version(self):
        # The version-agnostic prefix is what the A5 exclusion keys on.
        assert draft_graph_prefix_for(_V3) == _V1
        assert draft_graph_prefix_for(_V1) == _V1
        # Non-draft URI returned unchanged (exact-match fallback).
        assert draft_graph_prefix_for(_PUBLIC) == _PUBLIC


# ---------------------------------------------------------------------------
# public_subject_filter
# ---------------------------------------------------------------------------


class TestPublicSubjectFilter:
    def test_filter_mentions_both_namespaces(self):
        f = public_subject_filter("entity")
        assert DRAFT_GRAPH_PREFIX in f
        assert ADHOC_GRAPH_PREFIX in f
        assert "?entity" in f
        assert "STRSTARTS" in f

    def test_var_sanitised(self):
        # A var with hostile characters is reduced to word chars only, so
        # it cannot break out of the FILTER context. The injection chars
        # (quote, brace, paren, hash, whitespace) must all be gone from
        # the variable token; remaining letters just form a harmless
        # (if odd) SPARQL variable name like ``?xDROPALL``.
        f = public_subject_filter('x") } ) # drop')
        # The only quotes/parens/braces in the output belong to the fixed
        # FILTER syntax + the two literal namespace strings — never from
        # the injected var. Confirm no stray closing constructs appear
        # immediately after the variable token.
        assert '?x")' not in f
        assert "?x }" not in f
        assert "#" not in f.replace("\n", " ").split('"')[0]  # no comment before the literal
        assert f.count('"') == 4  # exactly the two STR-literal pairs
        assert "?x" in f
