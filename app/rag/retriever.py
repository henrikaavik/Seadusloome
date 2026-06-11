"""Vector-similarity retriever for RAG chunks.

Retrieves the most relevant chunks from the ``rag_chunks`` table
using pgvector's cosine distance operator (``<=>``) against an
embedded query string.

Usage:

    from app.rag.retriever import Retriever

    retriever = Retriever()
    results = await retriever.retrieve("Tsiviilseadustiku muudatus", k=5)
    for chunk in results:
        print(chunk.score, chunk.content[:80])
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, LiteralString, cast

from app.db import get_connection
from app.metrics import record_metric
from app.rag.embedding import EmbeddingProvider, get_default_embedding_provider

logger = logging.getLogger(__name__)

# Whitelist of filter keys accepted by :meth:`Retriever.retrieve`,
# mapped to the SQL column expression they translate to.
#
# - ``source_type``, ``source_uri`` are real top-level columns on
#   ``rag_chunks`` (migration 009) and are indexed.
# - ``entity_type`` is NOT a top-level column; it lives inside the
#   ``metadata`` JSONB blob. We translate it to ``metadata->>'entity_type'``.
#   This is intentionally limited to a single hand-picked JSONB key — see
#   the issue body's note that general ``metadata.x`` filtering is a
#   follow-up. Anything else is rejected so callers can't smuggle arbitrary
#   JSONB paths or fresh column names through the filter dict.
#
# Hard-coding the expression strings here (rather than building them with
# an f-string at call time) keeps every fragment a ``LiteralString``, which
# psycopg's ``execute()`` requires for static-analysis safety.
_FILTER_COLUMN_EXPRS: dict[str, LiteralString] = {
    "source_type": "source_type",
    "source_uri": "source_uri",
    "entity_type": "metadata->>'entity_type'",
}


@dataclass(frozen=True)
class RetrievedChunk:
    """A chunk retrieved from the vector store.

    Attributes:
        content: The chunk text.
        metadata: Original metadata dict from the chunk.
        score: Similarity score (1 - cosine distance). Higher is better.
            Range: [0, 2] in theory but typically [0, 1] for normalized vectors.
    """

    content: str
    metadata: dict
    score: float


def _build_filter_clauses(
    filters: dict[str, Any] | None,
) -> tuple[list[LiteralString], list[Any]]:
    """Translate a whitelisted filter dict into SQL fragments + params.

    Every value is bound through ``%s`` placeholders — no string formatting
    of caller-controlled data. List / tuple values become ``= ANY(%s)``
    (Postgres array). ``None`` becomes ``IS NULL``.

    Args:
        filters: Mapping of column name → value. Allowed keys are listed
            in :data:`_FILTER_COLUMN_EXPRS`. Unknown keys raise
            ``ValueError``. ``None`` or an empty dict means "no extra
            filters" and returns ``([], [])``.

    Returns:
        ``(clauses, params)`` where ``clauses`` is a list of SQL
        fragments (each suitable for joining with ``" AND "``) and
        ``params`` is the matching list of bound values in left-to-right
        order.

    Raises:
        ValueError: If any key in *filters* is not in the whitelist.
            Rejecting unknown keys (rather than silently dropping them)
            ensures callers see typos / drift fast and prevents a
            future schema-rename from quietly disabling a filter.
    """
    if not filters:
        return [], []

    unknown = set(filters) - set(_FILTER_COLUMN_EXPRS)
    if unknown:
        raise ValueError(
            "Unknown filter key(s): "
            + ", ".join(sorted(unknown))
            + f". Allowed keys: {sorted(_FILTER_COLUMN_EXPRS)}"
        )

    clauses: list[LiteralString] = []
    params: list[Any] = []

    # Per-shape pre-built fragments. Concatenating two LiteralStrings via
    # ``+`` preserves the LiteralString type; f-strings do not. Keeping
    # these in a closed lookup is what lets pyright accept the final SQL
    # as ``LiteralString`` at the ``execute()`` call site.
    eq_suffix: LiteralString = " = %s"
    any_suffix: LiteralString = " = ANY(%s)"
    null_suffix: LiteralString = " IS NULL"

    for key, value in filters.items():
        # Whitelist lookup; the value is always a LiteralString because
        # _FILTER_COLUMN_EXPRS is hand-authored above.
        col_expr: LiteralString = _FILTER_COLUMN_EXPRS[key]

        if value is None:
            clauses.append(col_expr + null_suffix)
            continue

        if isinstance(value, (list, tuple, set)):
            seq = list(value)
            if not seq:
                # An empty list means "no allowed values" → match nothing.
                # Emit a contradictory predicate so the result set is empty
                # without raising. Callers that wanted "ignore this filter"
                # should pass ``None`` or omit the key.
                clauses.append("FALSE")
                continue
            clauses.append(col_expr + any_suffix)
            params.append(seq)
            continue

        clauses.append(col_expr + eq_suffix)
        params.append(value)

    return clauses, params


class Retriever:
    """Vector-similarity retriever backed by pgvector.

    Args:
        embedding_provider: Provider to embed query strings. Falls back
            to the module-level default if not provided.
    """

    def __init__(self, embedding_provider: EmbeddingProvider | None = None) -> None:
        self.embedder = embedding_provider or get_default_embedding_provider()

    async def retrieve(
        self,
        query: str,
        *,
        k: int = 10,
        source_type: str | None = None,
        org_id: str | None = None,
        user_id: str | None = None,
        filters: dict[str, Any] | None = None,
        feature: str = "unknown",
    ) -> list[RetrievedChunk]:
        """Retrieve the top-k most similar chunks to *query*.

        Args:
            query: Natural language query to search for.
            k: Maximum number of results to return.
            source_type: Optional shortcut for filtering to a single
                source type (``'ontology'``, ``'draft'``, ``'law_text'``,
                ``'court_decision'``). Kept for back-compat; prefer
                ``filters={"source_type": ...}`` for new code. The shortcut
                is folded into the filter dict — so it ANDs cleanly with
                other filter keys (e.g. ``entity_type``). If ``filters``
                already contains a ``"source_type"`` key, the dict wins
                on collision and the shortcut is ignored.
            org_id: Tenant scope. Public-corpus chunks (``org_id IS NULL``
                in the DB) are always visible. If a non-``None`` value is
                supplied, private chunks owned by that org are also
                visible. If ``None``, the caller is treated as "no org"
                and only public chunks are returned. Callers must pass
                the authenticated user's ``org_id`` — failing to do so is
                a data-leak bug, not a convenience.
            user_id: Optional authenticated user id, used purely for
                cost attribution of the query-embedding call in
                ``llm_usage`` (#854). Unlike ``org_id`` it has no effect
                on which chunks are visible.
            filters: Optional metadata filter dict. Allowed keys are
                ``source_type``, ``source_uri``, ``entity_type``. Values
                may be a scalar (``=``), a list/tuple/set (``= ANY``),
                or ``None`` (``IS NULL``). Unknown keys raise
                ``ValueError``. Combines additively with the tenant
                predicate — filters NEVER bypass org scoping. (#311)
            feature: Caller-supplied label used as the ``feature`` tag on
                the ``rag_retrieval_ms`` metric (e.g. ``"chat"``,
                ``"drafter"``, ``"analyysikeskus_similarity"``). Defaults
                to ``"unknown"`` so legacy callers still record metrics;
                new callers should always pass an explicit label. (#323)
                Also drives the cost-attribution label on the embedding
                call: a known feature ``X`` logs the Voyage spend as
                ``X_embedding`` (#854).

        Returns:
            List of :class:`RetrievedChunk` ordered by descending
            similarity score. Empty list if no results or if the
            table has no data.

        Raises:
            ValueError: If *filters* contains a key not in the whitelist.
        """
        # Tenant scoping: `(org_id IS NULL OR org_id = $1)` keeps public
        # corpus (NULL) visible to everyone and gates private chunks on
        # the caller's org. See migration 016 and issue #576.

        # #323: instrument the full retrieve() body — embed + ANN search
        # + result parsing — and tag every emission with the caller's
        # ``feature`` label plus an ``ok`` / ``error`` ``status``. We use
        # ``record_metric`` directly rather than ``track_duration`` so the
        # status label reflects the *outcome*, not a snapshot taken at
        # context-manager entry time. Exceptions are re-raised so callers
        # see the failure; only the metric write is wrapped.
        start = time.perf_counter()
        status = "ok"
        try:
            return await self._retrieve_inner(
                query,
                k=k,
                source_type=source_type,
                org_id=org_id,
                user_id=user_id,
                filters=filters,
                feature=feature,
            )
        except Exception:
            status = "error"
            raise
        finally:
            duration_ms = (time.perf_counter() - start) * 1000
            record_metric(
                "rag_retrieval_ms",
                round(duration_ms, 2),
                {"feature": feature, "status": status},
            )

    async def _retrieve_inner(
        self,
        query: str,
        *,
        k: int,
        source_type: str | None,
        org_id: str | None,
        user_id: str | None,
        filters: dict[str, Any] | None,
        feature: str,
    ) -> list[RetrievedChunk]:
        """Body of :meth:`retrieve`, kept separate so the metric wrapper
        is the only place that needs the try/except scaffolding."""
        if not query.strip():
            return []

        # Normalise: fold the legacy `source_type=` shortcut into the
        # filter dict so the SQL builder is the single source of truth.
        # The dict wins on collision (documented "filters wins" semantics);
        # otherwise the shortcut is injected so callers can mix the two
        # (e.g. `source_type="draft"` + `filters={"entity_type": "Provision"}`
        # ANDs both predicates rather than silently dropping the shortcut).
        effective_filters: dict[str, Any] = dict(filters) if filters else {}
        if source_type is not None and "source_type" not in effective_filters:
            effective_filters["source_type"] = source_type

        # Validate / translate filters BEFORE we spend an embedding call,
        # so an invalid filter dict doesn't burn API quota.
        try:
            extra_clauses, extra_params = _build_filter_clauses(effective_filters)
        except ValueError:
            # Re-raise so callers see the misuse loudly; we deliberately
            # do not swallow this to an empty result list (that would mask
            # bugs like typo'd filter keys).
            raise

        # Short-circuit: if any filter produced a contradictory predicate
        # (empty-list value -> "FALSE"), the SQL is guaranteed to return
        # zero rows. Skip the embedding call so we don't burn Voyage AI
        # quota on a query that can't match anything.
        if any(clause == "FALSE" for clause in extra_clauses):
            return []

        # 1. Embed the query. Thread the caller's identity + feature into
        # the embedding cost row (#854) so Voyage spend lands in per-org
        # budget enforcement instead of an unattributed "embedding"
        # bucket. A known feature ``X`` becomes ``X_embedding`` to keep
        # the provider="claude" feature labels distinct from the Voyage
        # spend they trigger.
        embed_feature = "embedding" if feature == "unknown" else f"{feature}_embedding"
        embeddings = await self.embedder.embed(
            [query],
            user_id=user_id,
            org_id=org_id,
            feature=embed_feature,
        )
        if not embeddings:
            return []
        query_embedding = embeddings[0]

        # 2. Build the SQL query
        embedding_str = "[" + ",".join(str(v) for v in query_embedding) + "]"

        # Tenant predicate: NULL org_id always matches (public); otherwise
        # match the caller's own org. psycopg binds Python `None` as SQL
        # NULL, and `NULL = NULL` is NULL (not true), so the `org_id IS
        # NULL` branch correctly keeps public rows visible even when the
        # caller has no org.
        where_parts: list[LiteralString] = ["(org_id IS NULL OR org_id = %s)"]
        where_parts.extend(extra_clauses)
        # ``str.join`` on a literal separator preserves LiteralString when
        # all elements are LiteralString — but pyright can't infer that
        # through ``list[LiteralString]``, so we cast. Every element of
        # ``where_parts`` is provably a LiteralString (the tenant predicate
        # is a literal; each extra clause is built from
        # ``_FILTER_COLUMN_EXPRS`` values which are LiteralString),
        # so the cast is sound.
        where_sql = cast(LiteralString, " AND ".join(where_parts))

        sql = (
            "SELECT content, metadata, "
            "1 - (embedding <=> %s::vector) AS score "
            "FROM rag_chunks "
            "WHERE " + where_sql + " "
            "ORDER BY embedding <=> %s::vector "
            "LIMIT %s"
        )
        # Parameter order must match `%s` order in the SQL above:
        #   1. embedding_str (SELECT score)
        #   2. org_id (tenant predicate)
        #   3..n. each extra filter param, in the order the clauses were emitted
        #   n+1. embedding_str (ORDER BY)
        #   n+2. k (LIMIT)
        params: list[Any] = [embedding_str, org_id]
        params.extend(extra_params)
        params.extend([embedding_str, k])

        # 3. Execute
        try:
            with get_connection() as conn:
                cursor = conn.execute(sql, tuple(params))
                rows = cursor.fetchall()
        except Exception:
            logger.exception("Failed to retrieve RAG chunks")
            return []

        # 4. Parse results
        import json

        if not rows:
            logger.info(
                "RAG retrieval returned 0 results for query (first 80 chars): %.80s "
                "— the rag_chunks table may be empty. Run "
                "'uv run python scripts/ingest_rag.py' to populate it.",
                query,
            )
            return []

        results: list[RetrievedChunk] = []
        for row in rows:
            content = row[0]
            metadata_raw = row[1]
            score = float(row[2])

            # metadata may come back as a dict or a JSON string
            if isinstance(metadata_raw, str):
                try:
                    metadata = json.loads(metadata_raw)
                except json.JSONDecodeError:
                    metadata = {}
            elif isinstance(metadata_raw, dict):
                metadata = metadata_raw
            else:
                metadata = {}

            results.append(
                RetrievedChunk(
                    content=content,
                    metadata=metadata,
                    score=score,
                )
            )

        return results


def delete_chunks_for_draft(conn, draft_id) -> int:
    """Delete every ``rag_chunks`` row owned by the given draft.

    This is the application-level cascade for the polymorphic
    ``(source_type, source_id)`` reference documented in migration 016.
    PostgreSQL can't enforce the FK itself because ``source_id`` is
    polymorphic across source types, so every caller that deletes a
    draft row must also call this helper. See
    ``app/docs/routes.py::delete_draft_handler`` for the canonical
    wiring.

    Parameters
    ----------
    conn:
        An open psycopg connection. Commit is the caller's
        responsibility — this helper is expected to run inside the same
        transaction as the ``DELETE FROM drafts`` statement so either
        both succeed or neither does.
    draft_id:
        UUID (or string/UUID-compatible) identifying the draft. Compared
        against ``rag_chunks.source_id``.

    Returns
    -------
    int
        Number of rows removed. Zero is a legitimate outcome — e.g. a
        draft that was never RAG-ingested.
    """
    cursor = conn.execute(
        "DELETE FROM rag_chunks WHERE source_type = 'draft' AND source_id = %s",
        (str(draft_id),),
    )
    return cursor.rowcount or 0
