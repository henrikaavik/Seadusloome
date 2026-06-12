# pyright: reportOperatorIssue=false
# pyright: reportOptionalSubscript=false
"""Tests for the A5 hybrid similarity engine + workflow + Koostaja integration.

Covers:

1. **SPARQL helpers** — :func:`find_ontology_similar` /
   :func:`find_cluster_siblings`: query shape, empty input, dead-Jena,
   URI binding, score parsing, plus an rdflib end-to-end against the
   canonical ontology fixture.
2. **Embedding adapter** — :func:`find_embedding_similar` chunk → entity
   aggregation, ``max(chunk_cosine)`` headline, top-3 tiebreaker, and
   **privacy posture** (the retriever must be called with
   ``org_id=None`` and the query text must never reach a persistence
   helper).
3. **Score merge** — :func:`merge_similarity_rows` dedup by URI, weight
   formula (1.0× ontology vs 0.8× embedding), reason union, stable
   ordering.
4. **A5a route** — ``/analyysikeskus/sarnasus``: auth gate, landing,
   resolved happy path, free-text path, disambiguation.
5. **Koostaja A5b** — :func:`_find_similar_provisions` enrichment +
   :func:`_similar_provisions_text_for_prompt` injects the snippet
   text into the Step 4 prompt.
6. **Capability registry** — the ``sarnasus`` capability is live and
   registered in ``_ANALYYSIKESKUS_INPUTS``.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Shared URIs / fixtures
# ---------------------------------------------------------------------------

_SEED_URI = "https://data.riik.ee/ontology/estleg#AvTS-p35"
_CAND_A_URI = "https://data.riik.ee/ontology/estleg#KMS-p10"
_CAND_B_URI = "https://data.riik.ee/ontology/estleg#KarS-p211"
_CAND_C_URI = "https://data.riik.ee/ontology/estleg#TsiviilS-p3"


# ---------------------------------------------------------------------------
# 1. SPARQL helpers — find_ontology_similar / find_cluster_siblings
# ---------------------------------------------------------------------------


class TestFindOntologySimilar:
    def test_empty_uri_returns_empty_without_hitting_jena(self):
        from app.analyysikeskus.similarity import find_ontology_similar

        stub_client = MagicMock()
        rows = find_ontology_similar("", sparql_client=stub_client)
        assert rows == []
        stub_client.query.assert_not_called()

    def test_dead_jena_returns_empty(self):
        from app.analyysikeskus.similarity import find_ontology_similar

        stub_client = MagicMock()
        stub_client.query.side_effect = RuntimeError("jena down")
        rows = find_ontology_similar(_SEED_URI, sparql_client=stub_client)
        assert rows == []

    def test_parses_scored_and_unscored_rows(self):
        from app.analyysikeskus.similarity import (
            REASON_ONTOLOGY,
            find_ontology_similar,
        )

        stub_client = MagicMock()
        stub_client.query.return_value = [
            {"candidate": _CAND_A_URI, "label": "KMS § 10", "score": "0.82"},
            {"candidate": _CAND_B_URI, "label": "KarS § 211", "score": ""},
        ]
        rows = find_ontology_similar(_SEED_URI, sparql_client=stub_client)
        by_uri = {r.entity_uri: r for r in rows}
        assert by_uri[_CAND_A_URI].ontology_score == 0.82
        assert by_uri[_CAND_A_URI].reasons == (REASON_ONTOLOGY,)
        assert by_uri[_CAND_B_URI].ontology_score is None
        # Unscored row still surfaces (with a flat 1.0 fallback).
        assert by_uri[_CAND_B_URI].score == 1.0

    def test_binds_seed_as_uri(self):
        """The seed URI must travel via :meth:`SparqlClient._inject_uri_bindings`."""
        from app.analyysikeskus.similarity import find_ontology_similar

        stub_client = MagicMock()
        stub_client.query.return_value = []
        find_ontology_similar(_SEED_URI, sparql_client=stub_client)
        kwargs = stub_client.query.call_args.kwargs
        assert kwargs["uri_bindings"] == {"seed": _SEED_URI}

    def test_self_match_is_dropped(self):
        from app.analyysikeskus.similarity import find_ontology_similar

        stub_client = MagicMock()
        stub_client.query.return_value = [
            {"candidate": _SEED_URI, "label": "self"},
            {"candidate": _CAND_A_URI, "label": "KMS § 10"},
        ]
        rows = find_ontology_similar(_SEED_URI, sparql_client=stub_client)
        assert [r.entity_uri for r in rows] == [_CAND_A_URI]


class TestFindClusterSiblings:
    def test_empty_uri_returns_empty_without_hitting_jena(self):
        from app.analyysikeskus.similarity import find_cluster_siblings

        stub_client = MagicMock()
        rows = find_cluster_siblings("", sparql_client=stub_client)
        assert rows == []
        stub_client.query.assert_not_called()

    def test_query_unions_requested_and_topic_cluster_predicates(self):
        """Forward compatibility: query both predicates so a corpus with
        only one populated still produces matches.
        """
        from app.analyysikeskus.similarity import _SAME_CLUSTER_QUERY

        assert "requestedCluster" in _SAME_CLUSTER_QUERY
        assert "topicCluster" in _SAME_CLUSTER_QUERY
        assert "UNION" in _SAME_CLUSTER_QUERY

    def test_parses_cluster_siblings(self):
        from app.analyysikeskus.similarity import (
            REASON_CLUSTER,
            find_cluster_siblings,
        )

        stub_client = MagicMock()
        stub_client.query.return_value = [
            {"candidate": _CAND_A_URI, "label": "KMS § 10"},
            {"candidate": _CAND_B_URI, "label": "KarS § 211"},
        ]
        rows = find_cluster_siblings(_SEED_URI, sparql_client=stub_client)
        assert len(rows) == 2
        assert all(r.reasons == (REASON_CLUSTER,) for r in rows)
        # Deterministic-membership rows carry a flat 1.0 score.
        assert all(r.score == 1.0 for r in rows)

    def test_dead_jena_returns_empty(self):
        from app.analyysikeskus.similarity import find_cluster_siblings

        stub_client = MagicMock()
        stub_client.query.side_effect = RuntimeError("jena down")
        assert find_cluster_siblings(_SEED_URI, sparql_client=stub_client) == []


class TestOntologyQueriesUseCanonicalPredicates:
    """A5 must not hard-code estleg:* strings — predicates come from
    :mod:`app.ontology.relations.PREDICATES` so a C0-style rename
    propagates automatically.
    """

    def test_ontology_query_references_canonical_predicates(self):
        from app.analyysikeskus.similarity import _ONTOLOGY_DECLARED_QUERY
        from app.ontology.relations import PREDICATES

        assert PREDICATES.SEMANTICALLY_SIMILAR_TO in _ONTOLOGY_DECLARED_QUERY
        assert PREDICATES.SIMILARITY_SCORE in _ONTOLOGY_DECLARED_QUERY

    def test_cluster_query_references_canonical_predicates(self):
        from app.analyysikeskus.similarity import _SAME_CLUSTER_QUERY
        from app.ontology.relations import PREDICATES

        assert PREDICATES.REQUESTED_CLUSTER in _SAME_CLUSTER_QUERY
        assert PREDICATES.TOPIC_CLUSTER in _SAME_CLUSTER_QUERY


class TestRdflibFixture:
    """End-to-end SPARQL against the canonical fixture (Provision_1 →
    Provision_2 via semanticallySimilarTo, score 0.82; topicCluster
    siblings via Cluster_1).
    """

    def _load_fixture(self):
        from pathlib import Path

        from rdflib import Graph

        fixture = Path(__file__).resolve().parent / "fixtures" / "ontology_canonical.ttl"
        g = Graph()
        g.parse(str(fixture), format="turtle")
        return g

    def test_ontology_similar_returns_scored_pair(self):
        from app.analyysikeskus.similarity import _ONTOLOGY_DECLARED_QUERY

        g = self._load_fixture()
        seed_uri = "https://data.riik.ee/ontology/estleg#Provision_1"
        # Mirror SparqlClient._inject_uri_bindings: insert before last
        # closing brace.
        values_block = f"VALUES ?seed {{ <{seed_uri}> }}"
        last_brace = _ONTOLOGY_DECLARED_QUERY.rfind("}")
        query = (
            _ONTOLOGY_DECLARED_QUERY[:last_brace]
            + "\n"
            + values_block
            + "\n"
            + _ONTOLOGY_DECLARED_QUERY[last_brace:]
        )
        rows = list(g.query(query))
        # At least Provision_2 (scored 0.82) should appear; the fixture
        # also has Provision_3 reachable via Provision_2's outgoing edge.
        candidate_uris = {str(row[0]) for row in rows}  # type: ignore[index]
        assert "https://data.riik.ee/ontology/estleg#Provision_2" in candidate_uris

    def test_cluster_query_returns_siblings(self):
        from app.analyysikeskus.similarity import _SAME_CLUSTER_QUERY

        g = self._load_fixture()
        # Provision_1 is in Cluster_1 via requestedCluster; Provision_2
        # is in the same Cluster_1 via topicCluster — must surface in
        # the UNION query.
        seed_uri = "https://data.riik.ee/ontology/estleg#Provision_1"
        values_block = f"VALUES ?seed {{ <{seed_uri}> }}"
        last_brace = _SAME_CLUSTER_QUERY.rfind("}")
        query = (
            _SAME_CLUSTER_QUERY[:last_brace]
            + "\n"
            + values_block
            + "\n"
            + _SAME_CLUSTER_QUERY[last_brace:]
        )
        rows = list(g.query(query))
        candidate_uris = {str(row[0]) for row in rows}  # type: ignore[index]
        assert "https://data.riik.ee/ontology/estleg#Provision_2" in candidate_uris


# ---------------------------------------------------------------------------
# 2. Embedding track — find_embedding_similar
# ---------------------------------------------------------------------------


class _StubChunk:
    """Minimal RetrievedChunk-shaped stub for the embedding aggregator."""

    def __init__(self, source_uri: str, score: float, content: str = "", label: str = ""):
        self.score = score
        self.content = content
        self.metadata = {"source_uri": source_uri, "label": label}


class TestEmbeddingAggregation:
    def test_groups_chunks_by_source_uri_with_max_score(self):
        from app.analyysikeskus.similarity import _aggregate_chunks_by_entity

        chunks = [
            _StubChunk(_CAND_A_URI, 0.91, "Esimene tekst", label="KMS § 10"),
            _StubChunk(_CAND_A_URI, 0.85, "Teine tekst"),
            _StubChunk(_CAND_A_URI, 0.74, "Kolmas tekst"),
            _StubChunk(_CAND_B_URI, 0.66, "KarS tekst"),
        ]
        rows = _aggregate_chunks_by_entity(chunks)
        by_uri = {r.entity_uri: r for r in rows}
        # KMS gets max(0.91, 0.85, 0.74) = 0.91 (+tiny tiebreak epsilon).
        cand_a_embed = by_uri[_CAND_A_URI].embedding_score
        assert cand_a_embed is not None
        assert abs(cand_a_embed - 0.91) < 1e-6
        # Snippet is from the top-scoring chunk.
        assert "Esimene tekst" in by_uri[_CAND_A_URI].snippet
        # The label rides through from chunk metadata.
        assert by_uri[_CAND_A_URI].label == "KMS § 10"
        # Sorted by score desc — KMS (0.91) comes before KarS (0.66).
        assert rows[0].entity_uri == _CAND_A_URI

    def test_skips_chunks_without_source_uri(self):
        from app.analyysikeskus.similarity import _aggregate_chunks_by_entity

        chunks = [
            _StubChunk(_CAND_A_URI, 0.9),
            _StubChunk("", 0.99),  # missing URI ⇒ skip
        ]
        rows = _aggregate_chunks_by_entity(chunks)
        assert {r.entity_uri for r in rows} == {_CAND_A_URI}

    def test_skips_chunks_with_invalid_score(self):
        from app.analyysikeskus.similarity import _aggregate_chunks_by_entity

        ch_bad = _StubChunk(_CAND_A_URI, 0.0)
        ch_bad.score = float("nan")  # type: ignore[assignment]
        ch_good = _StubChunk(_CAND_B_URI, 0.5)
        rows = _aggregate_chunks_by_entity([ch_bad, ch_good])
        assert {r.entity_uri for r in rows} == {_CAND_B_URI}

    def test_empty_query_text_returns_empty_without_embedding(self):
        from app.analyysikeskus.similarity import find_embedding_similar

        # No retriever supplied → fail closed if anything tried to run.
        rows = find_embedding_similar("", retriever=MagicMock())
        assert rows == []


class TestEmbeddingPrivacy:
    """Privacy posture: the retriever must be called with ``org_id=None``
    so the SQL predicate restricts results to the public corpus
    (``org_id IS NULL``). Query text must not be persisted or indexed.
    """

    def test_retriever_called_with_public_org_id_only(self):
        from app.analyysikeskus.similarity import find_embedding_similar

        async def _stub_retrieve(*args, **kwargs):
            return []

        retriever = MagicMock()
        retriever.retrieve = MagicMock(side_effect=_stub_retrieve)
        find_embedding_similar("uudne otsing teksti kohta", retriever=retriever)

        retriever.retrieve.assert_called_once()
        call_kwargs = retriever.retrieve.call_args.kwargs
        # CRITICAL: org_id=None ⇒ public-corpus-only filter.
        assert call_kwargs["org_id"] is None
        # The source_type filter pins us to ontology chunks.
        assert call_kwargs["source_type"] == "ontology"

    def test_module_does_not_import_any_log_or_insert_helpers(self):
        """Defence-in-depth: the similarity module must not pull in any
        helper that persists user input. This grep-style assertion
        catches an accidental ``from app.db import insert_log`` or
        similar mistake during refactors.
        """
        import inspect

        from app.analyysikeskus import similarity

        source = inspect.getsource(similarity)
        # No log-table inserts on the similarity path.
        assert "INSERT INTO" not in source.upper()
        # No direct DB connection imports from the similarity layer —
        # all persistence lives in the retriever which we've already
        # constrained via ``org_id=None``.
        assert "from app.db import" not in source
        assert "get_connection" not in source

    def test_query_text_never_logged_via_print_or_logger_info(self):
        """The query text must not appear in stdout / info-level logs
        (debug/warning is fine for diagnostics).
        """
        import inspect

        from app.analyysikeskus import similarity

        source = inspect.getsource(similarity)
        # ``logger.info(...query_text...)`` would leak the input into
        # the operational log stream. We allow ``logger.debug`` /
        # ``logger.warning`` with truncation (".60s") but never an
        # untrimmed info-level emission.
        assert "logger.info(query" not in source
        assert "print(query" not in source


# ---------------------------------------------------------------------------
# 3. Score merge — merge_similarity_rows
# ---------------------------------------------------------------------------


class TestMergeSimilarityRows:
    def test_dedup_by_uri_unions_reasons(self):
        from app.analyysikeskus.similarity import (
            REASON_CLUSTER,
            REASON_EMBEDDING,
            REASON_ONTOLOGY,
            SimilarityRow,
            merge_similarity_rows,
        )

        merged = merge_similarity_rows(
            ontology=[
                SimilarityRow(
                    entity_uri=_CAND_A_URI,
                    label="KMS § 10",
                    score=0.7,
                    reasons=(REASON_ONTOLOGY,),
                    ontology_score=0.7,
                )
            ],
            cluster=[
                SimilarityRow(
                    entity_uri=_CAND_A_URI,
                    label="",
                    score=1.0,
                    reasons=(REASON_CLUSTER,),
                )
            ],
            embedding=[
                SimilarityRow(
                    entity_uri=_CAND_A_URI,
                    label="KMS § 10 (embed)",
                    score=0.85,
                    reasons=(REASON_EMBEDDING,),
                    embedding_score=0.85,
                    snippet="Sarnane sõnastus...",
                )
            ],
        )
        assert len(merged) == 1
        row = merged[0]
        # All three reasons preserved, sorted.
        assert set(row.reasons) == {REASON_ONTOLOGY, REASON_CLUSTER, REASON_EMBEDDING}
        # Snippet from embedding track survives.
        assert "Sarnane sõnastus" in row.snippet
        # First non-empty label wins.
        assert row.label == "KMS § 10"

    def test_weight_formula_ontology_1x_vs_embedding_0_8x(self):
        """A pure ontology(0.7) match should outrank a pure embedding(0.8)
        match because 0.7 * 1.0 == 0.7 > 0.8 * 0.8 == 0.64.
        """
        from app.analyysikeskus.similarity import (
            REASON_EMBEDDING,
            REASON_ONTOLOGY,
            SimilarityRow,
            merge_similarity_rows,
        )

        merged = merge_similarity_rows(
            ontology=[
                SimilarityRow(
                    entity_uri=_CAND_A_URI,
                    score=0.7,
                    reasons=(REASON_ONTOLOGY,),
                    ontology_score=0.7,
                )
            ],
            cluster=[],
            embedding=[
                SimilarityRow(
                    entity_uri=_CAND_B_URI,
                    score=0.8,
                    reasons=(REASON_EMBEDDING,),
                    embedding_score=0.8,
                )
            ],
        )
        # Cand A (0.7 × 1.0 = 0.7) > Cand B (0.8 × 0.8 = 0.64).
        assert merged[0].entity_uri == _CAND_A_URI
        assert merged[1].entity_uri == _CAND_B_URI
        assert abs(merged[0].score - 0.7) < 1e-6
        assert abs(merged[1].score - 0.64) < 1e-6

    def test_cluster_match_contributes_at_ontology_weight(self):
        """Cluster matches are deterministic membership — they get the
        ontology weight (1.0×), not the embedding weight.
        """
        from app.analyysikeskus.similarity import (
            REASON_CLUSTER,
            SimilarityRow,
            merge_similarity_rows,
        )

        merged = merge_similarity_rows(
            ontology=[],
            cluster=[
                SimilarityRow(
                    entity_uri=_CAND_A_URI,
                    score=1.0,
                    reasons=(REASON_CLUSTER,),
                )
            ],
            embedding=[],
        )
        assert merged[0].score == pytest.approx(1.0)

    def test_tie_break_prefers_more_reasons(self):
        from app.analyysikeskus.similarity import (
            REASON_CLUSTER,
            REASON_EMBEDDING,
            REASON_ONTOLOGY,
            SimilarityRow,
            merge_similarity_rows,
        )

        # Two URIs with identical final scores; A has three reasons,
        # B has one — A should rank first.
        merged = merge_similarity_rows(
            ontology=[
                SimilarityRow(
                    entity_uri=_CAND_A_URI,
                    score=0.5,
                    reasons=(REASON_ONTOLOGY,),
                    ontology_score=0.5,
                ),
                SimilarityRow(
                    entity_uri=_CAND_B_URI,
                    score=0.5,
                    reasons=(REASON_ONTOLOGY,),
                    ontology_score=0.5,
                ),
            ],
            cluster=[
                SimilarityRow(
                    entity_uri=_CAND_A_URI,
                    score=0.5,
                    reasons=(REASON_CLUSTER,),
                ),
            ],
            embedding=[
                SimilarityRow(
                    entity_uri=_CAND_A_URI,
                    score=0.5,
                    reasons=(REASON_EMBEDDING,),
                    embedding_score=0.5,
                ),
            ],
        )
        assert merged[0].entity_uri == _CAND_A_URI

    def test_limit_caps_result_count(self):
        from app.analyysikeskus.similarity import (
            REASON_ONTOLOGY,
            SimilarityRow,
            merge_similarity_rows,
        )

        rows = [
            SimilarityRow(
                entity_uri=f"https://data.riik.ee/ontology/estleg#X-{i}",
                score=0.5,
                reasons=(REASON_ONTOLOGY,),
                ontology_score=0.5,
            )
            for i in range(20)
        ]
        merged = merge_similarity_rows(ontology=rows, cluster=[], embedding=[], limit=5)
        assert len(merged) == 5

    def test_empty_inputs_return_empty(self):
        from app.analyysikeskus.similarity import merge_similarity_rows

        assert merge_similarity_rows(ontology=[], cluster=[], embedding=[]) == []


class TestReasonLabelsEt:
    def test_translates_codes_to_estonian(self):
        from app.analyysikeskus.similarity import (
            REASON_CLUSTER,
            REASON_EMBEDDING,
            REASON_ONTOLOGY,
            reason_labels_et,
        )

        labels = reason_labels_et((REASON_ONTOLOGY, REASON_CLUSTER, REASON_EMBEDDING))
        assert labels == [
            "ontoloogias deklareeritud",
            "sama temaatika",
            "sarnane sõnastus",
        ]

    def test_unknown_codes_skipped(self):
        from app.analyysikeskus.similarity import reason_labels_et

        assert reason_labels_et(("bogus", "also-bogus")) == []


# ---------------------------------------------------------------------------
# 4. Route layer — /analyysikeskus/sarnasus
# ---------------------------------------------------------------------------


def _authed_user() -> dict[str, Any]:
    return {
        "id": "33333333-3333-3333-3333-333333333333",
        "email": "kasutaja@seadusloome.ee",
        "full_name": "Test Kasutaja",
        "role": "drafter",
        "org_id": "11111111-1111-1111-1111-111111111111",
    }


def _stub_provider() -> MagicMock:
    provider = MagicMock()
    provider.get_current_user.return_value = _authed_user()
    return provider


def _authed_client(*, raise_server_exceptions: bool = True):
    from starlette.testclient import TestClient

    client = TestClient(
        __import__("app.main", fromlist=["app"]).app,
        follow_redirects=False,
        raise_server_exceptions=raise_server_exceptions,
    )
    client.cookies.set("access_token", "stub-token")
    return client


def _canned_resolved_provision_ref():
    from app.docs.entity_extractor import ExtractedRef
    from app.docs.reference_resolver import ResolvedRef

    return ResolvedRef(
        extracted=ExtractedRef(
            ref_text="AvTS § 35",
            ref_type="provision",
            confidence=1.0,
            location={"source": "analyysikeskus_input"},
        ),
        entity_uri=_SEED_URI,
        matched_label="AvTS § 35 — Avaliku teabe seadus",
        match_score=1.0,
    )


def _canned_similarity_rows():
    from app.analyysikeskus.similarity import (
        REASON_CLUSTER,
        REASON_EMBEDDING,
        REASON_ONTOLOGY,
        SimilarityRow,
    )

    return [
        SimilarityRow(
            entity_uri=_CAND_A_URI,
            label="KMS § 10",
            score=0.91,
            reasons=(REASON_ONTOLOGY, REASON_CLUSTER, REASON_EMBEDDING),
            snippet="Käibemaksukohuslane esitab deklaratsiooni...",
            ontology_score=0.7,
            embedding_score=0.91,
        ),
        SimilarityRow(
            entity_uri=_CAND_B_URI,
            label="KarS § 211",
            score=0.5,
            reasons=(REASON_CLUSTER,),
        ),
    ]


def test_sarnasus_redirects_unauthenticated():
    from starlette.testclient import TestClient

    from app.main import app

    client = TestClient(app, follow_redirects=False)
    resp = client.get("/analyysikeskus/sarnasus")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/auth/login"


@patch("app.auth.middleware._get_provider")
def test_sarnasus_landing_renders_input_form(mock_provider: MagicMock):
    mock_provider.return_value = _stub_provider()
    client = _authed_client()
    resp = client.get("/analyysikeskus/sarnasus")
    assert resp.status_code == 200
    body = resp.text
    # Title + the 5-card shell headings.
    assert "Otsi sarnaseid sätteid" in body
    for heading in ("Sisend", "Ulatus", "Tulemused", "Tõendid", "Soovitatud tegevused"):
        assert heading in body, heading
    # Landing form posts back to the workflow.
    assert 'action="/analyysikeskus/sarnasus"' in body
    assert "Otsi sarnaseid sätteid" in body


@patch("app.analyysikeskus.routes._sarnasus.find_similar")
@patch("app.docs.reference_resolver.ReferenceResolver.resolve")
@patch("app.auth.middleware._get_provider")
def test_sarnasus_resolved_provision_renders_full_result(
    mock_provider: MagicMock,
    mock_resolve: MagicMock,
    mock_find: MagicMock,
):
    """A resolved §-reference triggers find_similar(seed_uri=..., query_text=label)."""
    mock_provider.return_value = _stub_provider()
    mock_resolve.return_value = [_canned_resolved_provision_ref()]
    mock_find.return_value = _canned_similarity_rows()

    client = _authed_client()
    resp = client.get("/analyysikeskus/sarnasus?sisend=AvTS+%C2%A7+35")
    assert resp.status_code == 200
    body = resp.text

    # Estonian reason badges in the body.
    assert "ontoloogias deklareeritud" in body
    assert "sama temaatika" in body
    assert "sarnane sõnastus" in body
    # Result labels.
    assert "KMS § 10" in body
    # Snippet from the embedding track surfaces in Tõendid.
    assert "Käibemaksukohuslane" in body

    # find_similar called with both seed_uri and (label-derived) query_text.
    call_kwargs = mock_find.call_args.kwargs
    assert call_kwargs["seed_uri"] == _SEED_URI
    # The embedding seed is the resolved label, not the raw sisend.
    assert call_kwargs["query_text"] == "AvTS § 35 — Avaliku teabe seadus"


@patch("app.analyysikeskus.routes._sarnasus.find_similar")
@patch("app.docs.reference_resolver.ReferenceResolver.resolve", return_value=[])
@patch("app.auth.middleware._get_provider")
def test_sarnasus_free_text_uses_embedding_only(
    mock_provider: MagicMock,
    mock_resolve: MagicMock,
    mock_find: MagicMock,
):
    """Free-text path: nothing resolved ⇒ embedding track only."""
    mock_provider.return_value = _stub_provider()
    mock_find.return_value = _canned_similarity_rows()

    client = _authed_client()
    resp = client.get("/analyysikeskus/sarnasus?sisend=menetlust%C3%A4htaegade+pikendamine")
    assert resp.status_code == 200

    # No seed URI; query_text is the raw input.
    call_kwargs = mock_find.call_args.kwargs
    assert call_kwargs.get("seed_uri") is None
    assert call_kwargs["query_text"] == "menetlustähtaegade pikendamine"


@patch("app.analyysikeskus.routes._sarnasus.find_similar", return_value=[])
@patch("app.docs.reference_resolver.ReferenceResolver.resolve", return_value=[])
@patch("app.auth.middleware._get_provider")
def test_sarnasus_no_results_shows_warning(
    mock_provider: MagicMock,
    mock_resolve: MagicMock,
    mock_find: MagicMock,
):
    mock_provider.return_value = _stub_provider()
    client = _authed_client()
    resp = client.get("/analyysikeskus/sarnasus?sisend=mingi+tundmatu+jutt")
    assert resp.status_code == 200
    body = resp.text
    assert "Ei tuvastanud õiguslikku viidet" in body


@patch("app.docs.reference_resolver.ReferenceResolver.resolve")
@patch("app.auth.middleware._get_provider")
def test_sarnasus_disambiguation_when_multiple_resolutions(
    mock_provider: MagicMock,
    mock_resolve: MagicMock,
):
    """Multiple resolved entities ⇒ disambiguation card."""
    from app.docs.entity_extractor import ExtractedRef
    from app.docs.reference_resolver import ResolvedRef

    mock_provider.return_value = _stub_provider()
    mock_resolve.return_value = [
        _canned_resolved_provision_ref(),
        ResolvedRef(
            extracted=ExtractedRef(
                ref_text="AvTS",
                ref_type="law",
                confidence=1.0,
                location={"source": "analyysikeskus_input"},
            ),
            entity_uri="https://data.riik.ee/ontology/estleg#AvTS",
            matched_label="Avaliku teabe seadus",
            match_score=1.0,
        ),
    ]
    client = _authed_client()
    resp = client.get("/analyysikeskus/sarnasus?sisend=AvTS+%C2%A7+35")
    assert resp.status_code == 200
    body = resp.text
    assert "Sisend võib viidata mitmele üksusele" in body


# ---------------------------------------------------------------------------
# 5. Koostaja A5b integration
# ---------------------------------------------------------------------------


class TestFindSimilarProvisionsForDrafter:
    def test_empty_research_returns_empty(self):
        from app.drafter.handlers import _find_similar_provisions

        assert _find_similar_provisions({"provisions": []}) == []
        assert _find_similar_provisions({}) == []

    @patch("app.analyysikeskus.similarity.find_similar")
    def test_seeds_on_top_research_provisions(self, mock_find: MagicMock):
        from app.analyysikeskus.similarity import (
            REASON_EMBEDDING,
            REASON_ONTOLOGY,
            SimilarityRow,
        )
        from app.drafter.handlers import _find_similar_provisions

        mock_find.return_value = [
            SimilarityRow(
                entity_uri=_CAND_A_URI,
                label="KMS § 10",
                score=0.91,
                reasons=(REASON_ONTOLOGY, REASON_EMBEDDING),
                snippet="Sõnastuse näide",
                ontology_score=0.7,
                embedding_score=0.91,
            )
        ]

        research = {"provisions": [{"uri": _SEED_URI, "label": "AvTS § 35", "act_label": "AvTS"}]}
        rows = _find_similar_provisions(research)
        assert len(rows) == 1
        assert rows[0]["uri"] == _CAND_A_URI
        # The label, snippet, score, and reasons are serialised flat.
        assert rows[0]["label"] == "KMS § 10"
        assert rows[0]["snippet"] == "Sõnastuse näide"
        assert rows[0]["score"] == pytest.approx(0.91)
        assert "ontology_declared" in rows[0]["reasons"]

    @patch("app.analyysikeskus.similarity.find_similar", side_effect=RuntimeError("dead jena"))
    def test_lookup_failure_returns_empty(self, mock_find: MagicMock):
        from app.drafter.handlers import _find_similar_provisions

        research = {"provisions": [{"uri": _SEED_URI, "label": "AvTS § 35"}]}
        assert _find_similar_provisions(research) == []


class TestSimilarProvisionsTextForPrompt:
    def test_injects_snippet_text(self):
        from app.drafter.handlers import _similar_provisions_text_for_prompt

        text = _similar_provisions_text_for_prompt(
            {
                "similar_provisions": [
                    {
                        "label": "KMS § 10",
                        "snippet": "Käibemaksukohuslane peab esitama deklaratsiooni",
                    }
                ]
            }
        )
        assert "KMS § 10" in text
        assert "Käibemaksukohuslane" in text
        # The verbatim text is the whole point — bullet shape.
        assert text.startswith("-")

    def test_empty_similar_provisions_returns_empty(self):
        from app.drafter.handlers import _similar_provisions_text_for_prompt

        assert _similar_provisions_text_for_prompt({}) == ""
        assert _similar_provisions_text_for_prompt({"similar_provisions": []}) == ""

    def test_label_only_falls_back_to_label(self):
        from app.drafter.handlers import _similar_provisions_text_for_prompt

        text = _similar_provisions_text_for_prompt(
            {"similar_provisions": [{"label": "Karistusseadustik § 211"}]}
        )
        assert "Karistusseadustik § 211" in text
        # No snippet means no "Sõnastus:" sub-line.
        assert "Sõnastus" not in text


class TestStep3RendererSimilarProvisionsCard:
    def test_empty_card_renders_no_results_line(self):
        from app.drafter._step_renderers import _similar_provisions_card

        node = _similar_provisions_card([])
        # Crude assertion — render to string and look for the empty-state copy.
        rendered = str(node)
        assert "Sarnased sätted: 0" in rendered
        assert "Sarnaseid sätteid ei leitud" in rendered

    def test_populated_card_renders_label_and_reason(self):
        from app.drafter._step_renderers import _similar_provisions_card

        node = _similar_provisions_card(
            [
                {
                    "uri": _CAND_A_URI,
                    "label": "KMS § 10",
                    "reasons": ["ontology_declared", "embedding_cosine"],
                    "snippet": "Käibemaksukohuslane peab...",
                    "score": 0.9,
                }
            ]
        )
        rendered = str(node)
        assert "KMS § 10" in rendered
        assert "ontoloogias deklareeritud" in rendered
        assert "sarnane sõnastus" in rendered
        assert "Käibemaksukohuslane" in rendered


# ---------------------------------------------------------------------------
# 6. Capability registry / inputs
# ---------------------------------------------------------------------------


class TestCapabilityAndInputsRegistration:
    def test_sarnasus_capability_is_live(self):
        from app.ui.capabilities import get_capability

        cap = get_capability("sarnasus")
        assert cap is not None
        assert cap.status == "live"
        assert cap.target_url == "/analyysikeskus/sarnasus"

    def test_sarnasus_has_input_metadata(self):
        from app.analyysikeskus.routes import _ANALYYSIKESKUS_INPUTS

        assert "sarnasus" in _ANALYYSIKESKUS_INPUTS
        meta = _ANALYYSIKESKUS_INPUTS["sarnasus"]
        for key in ("placeholder", "aria_label", "examples"):
            assert key in meta
            assert meta[key].strip(), f"Empty {key}"
