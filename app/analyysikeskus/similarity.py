"""Hybrid similarity engine for the A5 workflow + Koostaja Step 3/4.

Task A5 from ``docs/2026-05-15-ontology-six-use-cases-plan.md`` (section 5,
Direction A — "Similarity workflow + Koostaja integration"). The plan
calls for **three** similarity sources, merged and de-duplicated with
"why this matched" badges:

1. **Ontology-declared similarity** — ``estleg:semanticallySimilarTo``
   with the optional inline ``estleg:similarityScore`` literal
   (populated corpus-wide via the keyword_jaccard v2 pipeline).
2. **Same topic cluster** — UNION on ``estleg:requestedCluster`` (the
   populated predicate) and ``estleg:topicCluster`` (SHACL alias, kept
   for forward compatibility per C0).
3. **Embedding cosine** — Voyage embeddings against
   ``rag_chunks`` via :class:`app.rag.retriever.Retriever`. Chunks are
   filtered to the **public corpus only** (``org_id IS NULL``); the
   retriever's tenant predicate already implements this when the caller
   passes ``org_id=None``. Chunks are aggregated by ``source_uri`` —
   entity score = ``max(chunk_cosine)`` with the top-3 average as the
   tie-breaker (per the plan's RAG-handling rules).

Predicate URIs come from :mod:`app.ontology.relations` so a future
rename propagates with no edits here.

**Privacy posture (CRITICAL).** Free-text input from a user's draft is
the privacy-sensitive case. This module:

* **Never persists or indexes the query text.** Embeddings are computed
  in-memory by :class:`VoyageProvider`; nothing is written to
  ``rag_chunks`` or any other table from this code path. There is no
  call to any "log query" / "save search" helper.
* **Embeds via :class:`VoyageProvider`** (which is a SaaS call subject
  to the project's approved LLM/vendor data-processing controls, per
  ``app/llm/cost_tracker.py`` + ``app/config.py::is_stub_allowed()``).
* **Filters cosine search to the public corpus.** The retriever is
  called with ``org_id=None`` so the SQL predicate becomes
  ``WHERE (org_id IS NULL OR org_id = NULL)`` which, because ``NULL =
  NULL`` is NULL (not true), keeps only public rows visible. See
  ``app/rag/retriever.py`` for the canonical implementation.

**Score-merge formula (A5 design).** Each candidate URI's final score is
the maximum of its per-source contributions, weighted ontology 1.0× over
embedding 0.8× (the plan's exact weights). All matched reasons are
preserved so a UI row can show every "why this matched" badge. Cluster
matches contribute as ontology-track signals (weight 1.0) because they
are deterministically declared, not statistical.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from app.ontology.queries import PREFIXES
from app.ontology.relations import PREDICATES
from app.ontology.sparql_client import SparqlClient

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

# Per-source row caps. Each track returns at most this many candidates;
# the merge layer then deduplicates by URI. Keep these small so the
# combined result stays scannable (UI shows ~10 rows in the result block).
MAX_ONTOLOGY_ROWS = 30
MAX_CLUSTER_ROWS = 30
MAX_EMBEDDING_CHUNKS = 50

# The chunker emits multiple chunks per source URI. We aggregate by
# entity using ``max(chunk_cosine)`` for the entity's headline score
# and ``avg(top-N chunk_cosine)`` as a tie-breaker. The plan calls for
# top-3.
EMBEDDING_TIEBREAK_TOP_N = 3

# Score-merge weights (plan-mandated).
ONTOLOGY_WEIGHT = 1.0
EMBEDDING_WEIGHT = 0.8

# Default top-N returned to the caller after merging all three tracks.
DEFAULT_RESULT_LIMIT = 10


# ---------------------------------------------------------------------------
# Estonian badge labels — single source of truth for the UI
# ---------------------------------------------------------------------------

REASON_ONTOLOGY = "ontology_declared"
REASON_CLUSTER = "same_cluster"
REASON_EMBEDDING = "embedding_cosine"

REASON_LABELS_ET: dict[str, str] = {
    REASON_ONTOLOGY: "ontoloogias deklareeritud",
    REASON_CLUSTER: "sama temaatika",
    REASON_EMBEDDING: "sarnane sõnastus",
}


# ---------------------------------------------------------------------------
# SimilarityRow — what the UI / Koostaja consume
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SimilarityRow:
    """One merged similarity candidate.

    Attributes:
        entity_uri: The matched entity's URI. Always non-empty after
            merging (rows lacking a URI are dropped pre-merge).
        label: ``rdfs:label`` / metadata-derived display label. Empty
            string when none of the contributing rows carried a label
            (the route degrades to the URI tail).
        score: The merged final score in roughly [0, 1] but unbounded —
            it's the max of the per-source weighted contributions, so
            ontology (1.0×) caps at the ontology score itself and
            embedding (0.8×) caps at 0.8. The merge layer does not
            normalise across sources beyond the weight multiplier.
        reasons: A sorted list of reason codes (subset of
            :data:`REASON_ONTOLOGY` / :data:`REASON_CLUSTER` /
            :data:`REASON_EMBEDDING`). Always at least one entry.
        snippet: For embedding-matched rows, the highest-scoring chunk's
            text trimmed for display. Empty string for ontology /
            cluster-only matches.
        ontology_score: The raw ontology-declared score (when the
            ``similarityScore`` literal was present). ``None`` when only
            the unweighted ``semanticallySimilarTo`` edge was seen — the
            row still surfaces but contributes a default 1.0× weight
            without a numeric score, so the merge falls back to the
            embedding side or other contributing tracks for ranking.
        embedding_score: The max chunk cosine score on the entity, or
            ``None`` when not embedding-matched.
    """

    entity_uri: str
    label: str = ""
    score: float = 0.0
    reasons: tuple[str, ...] = ()
    snippet: str = ""
    ontology_score: float | None = None
    embedding_score: float | None = None
    extras: dict[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# SPARQL templates — one per ontology track
# ---------------------------------------------------------------------------
#
# We bind ``?seed`` via :meth:`SparqlClient._inject_uri_bindings`. Both
# directions of ``semanticallySimilarTo`` are queried via UNION so the
# data layer can populate either side without bias. ``rdfs:label`` is
# OPTIONAL because the corpus does not guarantee labels on every node;
# ``estleg:similarityScore`` is OPTIONAL because the population is
# best-effort per the audit (most pairs have a score; some do not).

_ONTOLOGY_DECLARED_QUERY = (
    PREFIXES
    + f"""
SELECT DISTINCT ?candidate ?label ?score
WHERE {{
  {{
    ?seed <{PREDICATES.SEMANTICALLY_SIMILAR_TO}> ?candidate .
    OPTIONAL {{ ?seed <{PREDICATES.SIMILARITY_SCORE}> ?score }}
  }} UNION {{
    ?candidate <{PREDICATES.SEMANTICALLY_SIMILAR_TO}> ?seed .
    OPTIONAL {{ ?candidate <{PREDICATES.SIMILARITY_SCORE}> ?score }}
  }}
  OPTIONAL {{ ?candidate rdfs:label ?label }}
  FILTER(?candidate != ?seed)
}}
LIMIT {MAX_ONTOLOGY_ROWS}
"""
)

# Same-cluster track — UNION across requestedCluster (populated) and
# topicCluster (SHACL alias). We resolve the seed's cluster(s) then walk
# back out to every other provision in the same cluster. The forward and
# back hops are independently UNIONed so a corpus that uses requestedCluster
# on one side and topicCluster on the other still produces matches.
_SAME_CLUSTER_QUERY = (
    PREFIXES
    + f"""
SELECT DISTINCT ?candidate ?label ?cluster
WHERE {{
  {{
    ?seed <{PREDICATES.REQUESTED_CLUSTER}> ?cluster .
  }} UNION {{
    ?seed <{PREDICATES.TOPIC_CLUSTER}> ?cluster .
  }}
  {{
    ?candidate <{PREDICATES.REQUESTED_CLUSTER}> ?cluster .
  }} UNION {{
    ?candidate <{PREDICATES.TOPIC_CLUSTER}> ?cluster .
  }}
  OPTIONAL {{ ?candidate rdfs:label ?label }}
  FILTER(?candidate != ?seed)
}}
LIMIT {MAX_CLUSTER_ROWS}
"""
)


# ---------------------------------------------------------------------------
# Helpers — score coercion + per-track lookups
# ---------------------------------------------------------------------------


def _as_float(value: Any) -> float | None:
    """Coerce a SPARQL string literal to ``float``; ``None`` on missing / non-numeric."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def find_ontology_similar(
    seed_uri: str,
    *,
    sparql_client: SparqlClient | None = None,
) -> list[SimilarityRow]:
    """Return ``estleg:semanticallySimilarTo`` candidates for *seed_uri*.

    Empty / whitespace input ⇒ ``[]`` without hitting Jena. A SPARQL
    error degrades to ``[]`` (the route surfaces only the other tracks
    rather than 500). The score, when present, is carried on the row's
    ``ontology_score`` field; the merge layer uses it to rank ontology
    matches and otherwise falls back to the unweighted default.

    Args:
        seed_uri: The seed entity URI — a provision / act / concept.
        sparql_client: Optional :class:`SparqlClient` override for tests.

    Returns:
        Deduplicated list of :class:`SimilarityRow` carrying only the
        ontology reason; the merge layer adds reasons from other tracks.
    """
    uri = (seed_uri or "").strip()
    if not uri:
        return []
    client = sparql_client if sparql_client is not None else SparqlClient()
    try:
        rows = client.query(
            _ONTOLOGY_DECLARED_QUERY,
            uri_bindings={"seed": uri},
        )
    except Exception:
        logger.warning("find_ontology_similar: SPARQL query failed for %r", uri, exc_info=True)
        return []
    seen: dict[str, SimilarityRow] = {}
    for row in rows or []:
        cand = (row.get("candidate") or "").strip()
        if not cand or cand == uri:
            continue
        score = _as_float(row.get("score"))
        existing = seen.get(cand)
        if existing is not None and (existing.ontology_score or 0.0) >= (score or 0.0):
            continue
        seen[cand] = SimilarityRow(
            entity_uri=cand,
            label=(row.get("label") or "").strip(),
            score=score if score is not None else 1.0,
            reasons=(REASON_ONTOLOGY,),
            ontology_score=score,
        )
    return list(seen.values())


def find_cluster_siblings(
    seed_uri: str,
    *,
    sparql_client: SparqlClient | None = None,
) -> list[SimilarityRow]:
    """Return same-topic-cluster candidates for *seed_uri*.

    Walks ``requestedCluster`` / ``topicCluster`` (UNION-ed for forward
    compatibility per C0). Same dead-Jena / empty-input contract as
    :func:`find_ontology_similar`.
    """
    uri = (seed_uri or "").strip()
    if not uri:
        return []
    client = sparql_client if sparql_client is not None else SparqlClient()
    try:
        rows = client.query(
            _SAME_CLUSTER_QUERY,
            uri_bindings={"seed": uri},
        )
    except Exception:
        logger.warning("find_cluster_siblings: SPARQL query failed for %r", uri, exc_info=True)
        return []
    seen: dict[str, SimilarityRow] = {}
    for row in rows or []:
        cand = (row.get("candidate") or "").strip()
        if not cand or cand == uri:
            continue
        if cand in seen:
            continue
        seen[cand] = SimilarityRow(
            entity_uri=cand,
            label=(row.get("label") or "").strip(),
            # Cluster matches have no numeric score in the ontology;
            # they're deterministic membership, so we contribute a flat
            # 1.0 weighted by the ontology track in the merge.
            score=1.0,
            reasons=(REASON_CLUSTER,),
        )
    return list(seen.values())


# ---------------------------------------------------------------------------
# Embedding track
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _ChunkHit:
    """Internal shape for an embedding-matched chunk before entity aggregation."""

    source_uri: str
    score: float
    content: str
    label: str


def _aggregate_chunks_by_entity(chunks: list[Any]) -> list[SimilarityRow]:
    """Group retrieved chunks by ``metadata.source_uri`` and score each entity.

    Per the plan's RAG-handling rules:

    * **Group by ``source_uri``** — for ``source_type='ontology'`` chunks
      this is already the provision / act / concept URI, so no extra
      mapping is needed. Chunks whose metadata lacks ``source_uri`` are
      skipped (they cannot be promoted to an entity-level match without
      a target URI).
    * **Score = max(chunk_cosine)** — the entity's headline score is the
      single best-matching chunk. The top-3 average is computed as a
      deterministic tie-breaker on the merge side (encoded into the
      row's ``score`` via ``max + (avg-tiebreak / 1e6)`` so the merge
      can still treat ``score`` as a single number).
    * **Snippet = the best chunk's content**, trimmed for display.

    Chunks without a usable score (None / NaN) are skipped.
    """
    by_entity: dict[str, list[_ChunkHit]] = {}
    for ch in chunks or []:
        meta = getattr(ch, "metadata", None) or {}
        if not isinstance(meta, dict):
            continue
        source_uri = str(meta.get("source_uri") or "").strip()
        if not source_uri:
            continue
        score = getattr(ch, "score", None)
        if score is None:
            continue
        try:
            score_f = float(score)
        except (TypeError, ValueError):
            continue
        # Guard against NaN — comparisons with NaN are always False so a
        # sneak-in row would corrupt max() ordering.
        if score_f != score_f:  # NaN check
            continue
        content = str(getattr(ch, "content", "") or "")
        label = str(meta.get("label") or meta.get("title") or "").strip()
        by_entity.setdefault(source_uri, []).append(
            _ChunkHit(source_uri=source_uri, score=score_f, content=content, label=label)
        )

    out: list[SimilarityRow] = []
    for uri, hits in by_entity.items():
        hits.sort(key=lambda h: h.score, reverse=True)
        top = hits[0]
        max_score = top.score
        top_n = hits[:EMBEDDING_TIEBREAK_TOP_N]
        avg_top_n = sum(h.score for h in top_n) / len(top_n)
        # Encode the tiebreaker into a tiny ε on the headline so a
        # single ``score`` carries both ranks without an extra field.
        # ε is small enough that the entity's own max(...) still
        # dominates ordering across entities.
        encoded = max_score + (avg_top_n - max_score) / 1e6
        snippet = top.content.strip().replace("\n", " ")
        if len(snippet) > 240:
            snippet = snippet[:237].rstrip() + "…"
        out.append(
            SimilarityRow(
                entity_uri=uri,
                label=top.label,
                score=encoded,
                reasons=(REASON_EMBEDDING,),
                snippet=snippet,
                embedding_score=max_score,
            )
        )
    out.sort(key=lambda r: r.score, reverse=True)
    return out


def find_embedding_similar(
    query_text: str,
    *,
    k: int = MAX_EMBEDDING_CHUNKS,
    retriever: Any | None = None,
) -> list[SimilarityRow]:
    """Return embedding-cosine candidates for *query_text* against the public corpus.

    **Privacy posture:** ``query_text`` is **never persisted or indexed**
    by this function. It is sent to :class:`VoyageProvider` (a SaaS call
    subject to the project's approved data-processing controls, the same
    as every other Voyage embedding call in the system); the cosine
    search runs against rows where ``org_id IS NULL`` (the public
    corpus filter — enforced by the underlying retriever when ``org_id``
    is ``None``). See the module docstring for the full rationale.

    Args:
        query_text: The free-text input from the user's seed. Empty /
            whitespace ⇒ ``[]`` with no embedding call.
        k: Max chunks fetched from pgvector. Aggregated to fewer
            entities — typically 5–15 entities for k=50 chunks.
        retriever: Optional :class:`app.rag.retriever.Retriever` instance
            (tests inject one with ``.retrieve`` mocked); falls back to
            a freshly-constructed default.

    Returns:
        A list of :class:`SimilarityRow` — one per entity URI, sorted by
        descending headline (max-chunk) score. ``[]`` on empty input or
        any embedding / retrieval error.
    """
    text = (query_text or "").strip()
    if not text:
        return []

    if retriever is None:
        try:
            from app.rag.retriever import Retriever

            retriever = Retriever()
        except Exception:
            logger.debug("find_embedding_similar: Retriever not available", exc_info=True)
            return []

    async def _run() -> list[Any]:
        # ``org_id=None`` ⇒ public corpus only. See
        # ``app/rag/retriever.py`` — the SQL predicate is
        # ``(org_id IS NULL OR org_id = NULL)`` and the second clause
        # always evaluates UNKNOWN, so only public rows are returned.
        return await retriever.retrieve(
            text,
            k=k,
            source_type="ontology",
            org_id=None,
            feature="analyysikeskus_similarity",
        )

    try:
        try:
            chunks = asyncio.run(_run())
        except RuntimeError:
            # We're already inside a running event loop (e.g. an async
            # route or a test using anyio). Fall back to a fresh loop
            # in a worker thread so we never block / nest the running one.
            import concurrent.futures as _futures

            def _runner() -> list[Any]:
                loop = asyncio.new_event_loop()
                try:
                    return loop.run_until_complete(_run())
                finally:
                    loop.close()

            with _futures.ThreadPoolExecutor(max_workers=1) as pool:
                chunks = pool.submit(_runner).result()
    except Exception:
        logger.warning(
            "find_embedding_similar: retrieval failed for query (first 60 chars): %.60s",
            text,
            exc_info=True,
        )
        return []

    return _aggregate_chunks_by_entity(chunks)


# ---------------------------------------------------------------------------
# Score merge + dedup
# ---------------------------------------------------------------------------


def merge_similarity_rows(
    *,
    ontology: list[SimilarityRow],
    cluster: list[SimilarityRow],
    embedding: list[SimilarityRow],
    limit: int = DEFAULT_RESULT_LIMIT,
) -> list[SimilarityRow]:
    """Merge per-source rows, deduplicate by URI, and apply the weighted score.

    For each unique URI the merged row carries:

    * **Final score** — max of per-source weighted contributions:

        ``score = max(``
            ``ontology_score × ONTOLOGY_WEIGHT,``
            ``cluster_score × ONTOLOGY_WEIGHT,``
            ``embedding_score × EMBEDDING_WEIGHT``
        ``)``

      Ontology and cluster share the ``ONTOLOGY_WEIGHT`` (1.0×) because
      both are deterministically declared, not statistical. Embedding
      contributes ``EMBEDDING_WEIGHT`` (0.8×). Cluster matches without
      a numeric score contribute a flat ``1.0 × ONTOLOGY_WEIGHT``.
    * **Reasons** — the union of every track that contributed. Sorted
      deterministically so a UI render is stable across requests.
    * **Label / snippet** — first non-empty wins; embedding-track
      snippets are preserved even when an ontology label exists, since
      the snippet adds independent context.

    Ties (within a small epsilon) are broken by reason count
    (3-source > 2-source > 1-source) so a candidate matched by all
    three tracks ranks above a single-source match with the same score.

    Args:
        ontology / cluster / embedding: Per-source candidate lists.
            Each row's ``score`` and per-source ``ontology_score`` /
            ``embedding_score`` fields are read.
        limit: Result cap. Defaults to :data:`DEFAULT_RESULT_LIMIT`.

    Returns:
        The merged candidates, sorted by descending final score,
        truncated to *limit*.
    """
    merged: dict[str, dict[str, Any]] = {}

    def _bucket(uri: str) -> dict[str, Any]:
        bucket = merged.get(uri)
        if bucket is None:
            bucket = {
                "uri": uri,
                "label": "",
                "snippet": "",
                "reasons": set(),
                "ontology_score": None,
                "embedding_score": None,
                "cluster_score": None,
            }
            merged[uri] = bucket
        return bucket

    for row in ontology or []:
        if not row.entity_uri:
            continue
        b = _bucket(row.entity_uri)
        if not b["label"] and row.label:
            b["label"] = row.label
        b["reasons"].add(REASON_ONTOLOGY)
        # Use the explicit ontology_score when present; otherwise the
        # row's flat 1.0 fallback.
        prev = b["ontology_score"]
        new_val = row.ontology_score if row.ontology_score is not None else row.score
        if prev is None or new_val > prev:
            b["ontology_score"] = new_val

    for row in cluster or []:
        if not row.entity_uri:
            continue
        b = _bucket(row.entity_uri)
        if not b["label"] and row.label:
            b["label"] = row.label
        b["reasons"].add(REASON_CLUSTER)
        prev = b["cluster_score"]
        new_val = row.score
        if prev is None or new_val > prev:
            b["cluster_score"] = new_val

    for row in embedding or []:
        if not row.entity_uri:
            continue
        b = _bucket(row.entity_uri)
        if not b["label"] and row.label:
            b["label"] = row.label
        if not b["snippet"] and row.snippet:
            b["snippet"] = row.snippet
        b["reasons"].add(REASON_EMBEDDING)
        prev = b["embedding_score"]
        new_val = row.embedding_score if row.embedding_score is not None else row.score
        if prev is None or new_val > prev:
            b["embedding_score"] = new_val

    out: list[SimilarityRow] = []
    for uri, b in merged.items():
        contributions: list[float] = []
        ontology_score = b["ontology_score"]
        cluster_score = b["cluster_score"]
        embedding_score = b["embedding_score"]
        if ontology_score is not None:
            contributions.append(float(ontology_score) * ONTOLOGY_WEIGHT)
        if cluster_score is not None:
            contributions.append(float(cluster_score) * ONTOLOGY_WEIGHT)
        if embedding_score is not None:
            contributions.append(float(embedding_score) * EMBEDDING_WEIGHT)
        final_score = max(contributions) if contributions else 0.0
        # Sorted-tuple for stable UI rendering.
        reasons = tuple(sorted(b["reasons"]))
        out.append(
            SimilarityRow(
                entity_uri=uri,
                label=b["label"],
                score=final_score,
                reasons=reasons,
                snippet=b["snippet"],
                ontology_score=(float(ontology_score) if ontology_score is not None else None),
                embedding_score=(float(embedding_score) if embedding_score is not None else None),
            )
        )

    # Sort: primary by final score (desc), secondary by reason count
    # (more reasons = stronger signal), tertiary by URI for determinism.
    out.sort(
        key=lambda r: (-r.score, -len(r.reasons), r.entity_uri),
    )
    return out[: max(0, limit)]


# ---------------------------------------------------------------------------
# Top-level orchestrator
# ---------------------------------------------------------------------------


def find_similar(
    *,
    seed_uri: str | None = None,
    query_text: str | None = None,
    limit: int = DEFAULT_RESULT_LIMIT,
    sparql_client: SparqlClient | None = None,
    retriever: Any | None = None,
) -> list[SimilarityRow]:
    """Find similar entities using all three tracks; merge with the plan's weights.

    At least one of *seed_uri* / *query_text* must be supplied. When
    both are present:

    * *seed_uri* drives the two ontology tracks (declared similarity +
      same-cluster).
    * *query_text* drives the embedding track. When omitted but the
      seed URI is present and labels are available, the caller is
      expected to supply *query_text* explicitly (a future iteration
      may auto-derive it from the seed entity's label / content; we
      keep that decision in the route so the privacy boundary stays
      visible at the call site).

    Args:
        seed_uri: Optional ontology entity URI. Drives the SPARQL
            tracks.
        query_text: Optional free-text query. Drives the embedding
            track. **Never persisted** — see the module docstring's
            privacy posture section.
        limit: Cap on returned rows.
        sparql_client: Optional override for tests.
        retriever: Optional override for tests.

    Returns:
        A list of :class:`SimilarityRow` — ranked by the merged score.
        ``[]`` when no track produced any results.
    """
    ontology_rows: list[SimilarityRow] = []
    cluster_rows: list[SimilarityRow] = []
    embedding_rows: list[SimilarityRow] = []

    if seed_uri and seed_uri.strip():
        ontology_rows = find_ontology_similar(seed_uri, sparql_client=sparql_client)
        cluster_rows = find_cluster_siblings(seed_uri, sparql_client=sparql_client)

    if query_text and query_text.strip():
        embedding_rows = find_embedding_similar(query_text, retriever=retriever)

    return merge_similarity_rows(
        ontology=ontology_rows,
        cluster=cluster_rows,
        embedding=embedding_rows,
        limit=limit,
    )


def reason_labels_et(reasons: tuple[str, ...] | list[str]) -> list[str]:
    """Translate reason codes into Estonian badge labels (preserving order)."""
    out: list[str] = []
    for r in reasons or []:
        label = REASON_LABELS_ET.get(r)
        if label:
            out.append(label)
    return out


__all__ = [
    "DEFAULT_RESULT_LIMIT",
    "EMBEDDING_WEIGHT",
    "MAX_CLUSTER_ROWS",
    "MAX_EMBEDDING_CHUNKS",
    "MAX_ONTOLOGY_ROWS",
    "ONTOLOGY_WEIGHT",
    "REASON_CLUSTER",
    "REASON_EMBEDDING",
    "REASON_LABELS_ET",
    "REASON_ONTOLOGY",
    "SimilarityRow",
    "find_cluster_siblings",
    "find_embedding_similar",
    "find_ontology_similar",
    "find_similar",
    "merge_similarity_rows",
    "reason_labels_et",
]
