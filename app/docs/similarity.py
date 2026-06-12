"""Draft similarity computation for issue #621.

Computes Jaccard similarity between drafts based on the set of resolved
``entity_uri`` values stored in ``draft_entities``.  No LLM calls, no
vector embeddings — pure set intersection over existing extracted data.

Algorithm overview
------------------
1. For each draft, maintain an **inverted index** (``draft_uri_index``):
   uri → [draft_id, ...].  Rebuilt atomically on every analyze run.

2. At analyze time, look up the target draft's URIs in the index to find
   *candidate* drafts — those that share at least one URI.  This shrinks
   the comparison set from O(N) to O(avg_candidates), keeping per-draft
   compute well inside the < 5 s P95 budget even at 22 000+ drafts.

3. Compute Jaccard for each candidate:
   score = |A ∩ B| / |A ∪ B|

4. Persist the top-10 candidates with score >= threshold into
   ``draft_similarities``.  Rows are stamped with ``entity_set_hash``
   (sha256 of the sorted URI list) so a re-run with no entity changes
   is a cheap no-op.
"""

from __future__ import annotations

import hashlib
import logging

from app import config

logger = logging.getLogger(__name__)

DEFAULT_SIMILARITY_THRESHOLD = 0.15
TOP_N_SIMILAR = 10


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def get_similarity_threshold() -> float:
    """Return the configured Jaccard threshold, clamped to [0, 1].

    Reads ``SEADUSLOOME_SIMILARITY_THRESHOLD`` from the environment.
    Defaults to :data:`DEFAULT_SIMILARITY_THRESHOLD` (0.15) when the
    variable is absent or unparseable.
    """
    v = config.env_float("SEADUSLOOME_SIMILARITY_THRESHOLD")
    return max(0.0, min(1.0, v))


# ---------------------------------------------------------------------------
# Hashing
# ---------------------------------------------------------------------------


def compute_entity_set_hash(uris: list[str]) -> str:
    """Return the sha256 hex-digest of the deduplicated, sorted URI list.

    The hash is stable across reorderings and duplicates — two calls with
    the same logical set always return the same digest regardless of how
    the caller assembled the list.
    """
    normalized = "\n".join(sorted(set(uris)))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Inverted index maintenance
# ---------------------------------------------------------------------------


def update_uri_index(conn, draft_id: str, uris: list[str]) -> None:
    """Replace the inverted-index rows for *draft_id* atomically.

    Deletes the old rows first, then bulk-inserts the new set.
    The caller is responsible for committing the transaction.

    Args:
        conn: Active psycopg2-style connection (not committed here).
        draft_id: UUID string of the draft whose index to rebuild.
        uris: The draft's current entity URI list (may contain duplicates;
              they are deduplicated before insertion).
    """
    draft_id_str = str(draft_id)
    conn.execute(
        "DELETE FROM draft_uri_index WHERE draft_id = %s",
        (draft_id_str,),
    )
    unique_uris = list(set(uris))
    if unique_uris:
        args = [(uri, draft_id_str) for uri in unique_uris]
        conn.executemany(
            "INSERT INTO draft_uri_index (uri, draft_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
            args,
        )
    logger.debug(
        "similarity: updated uri_index for draft=%s with %d unique URIs",
        draft_id_str,
        len(unique_uris),
    )


# ---------------------------------------------------------------------------
# Similarity computation
# ---------------------------------------------------------------------------


def find_similar_drafts(
    conn,
    draft_id: str,
    uris: list[str],
    threshold: float | None = None,
) -> list[dict]:
    """Find and score similar drafts using inverted-index candidate generation.

    Steps:
    1. Look up candidate draft IDs that share at least one URI with
       *draft_id* via ``draft_uri_index``.
    2. For each candidate, fetch its URI set and compute Jaccard.
    3. Filter by *threshold*, sort DESC, return top :data:`TOP_N_SIMILAR`.

    Returns:
        List of dicts with keys ``similar_draft_id`` (str), ``score``
        (float, 3 decimal places), ``overlap_count`` (int).
        Empty when *uris* is empty or no candidate exceeds the threshold.

    Args:
        conn: Active database connection.
        draft_id: UUID string of the target draft (excluded from results).
        uris: The target draft's entity URI list.
        threshold: Minimum Jaccard score; defaults to
            :func:`get_similarity_threshold`.
    """
    if not uris:
        return []

    if threshold is None:
        threshold = get_similarity_threshold()

    draft_id_str = str(draft_id)
    target_set = set(uris)

    # Step 1: candidate generation via inverted index.
    # Any draft that shares at least one URI is a candidate.
    placeholders = ",".join(["%s"] * len(target_set))
    candidate_rows = conn.execute(
        f"""
        SELECT DISTINCT draft_id
        FROM draft_uri_index
        WHERE uri IN ({placeholders})
          AND draft_id <> %s
        """,
        (*target_set, draft_id_str),
    ).fetchall()

    if not candidate_rows:
        return []

    candidate_ids = [str(row[0]) for row in candidate_rows]
    logger.debug(
        "similarity: draft=%s has %d candidates from inverted index",
        draft_id_str,
        len(candidate_ids),
    )

    # Step 2: fetch each candidate's URI set and compute Jaccard.
    results: list[dict] = []
    for cand_id in candidate_ids:
        cand_uri_rows = conn.execute(
            "SELECT uri FROM draft_uri_index WHERE draft_id = %s",
            (cand_id,),
        ).fetchall()
        cand_set = {row[0] for row in cand_uri_rows}
        if not cand_set:
            continue

        intersection = target_set & cand_set
        union = target_set | cand_set
        if not union:
            continue

        score = len(intersection) / len(union)
        if score < threshold:
            continue

        results.append(
            {
                "similar_draft_id": cand_id,
                "score": round(score, 3),
                "overlap_count": len(intersection),
            }
        )

    # Step 3: sort DESC, cap at TOP_N_SIMILAR.
    results.sort(key=lambda r: r["score"], reverse=True)
    return results[:TOP_N_SIMILAR]


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def persist_similarities(
    conn,
    draft_id: str,
    similarities: list[dict],
    entity_set_hash: str,
) -> None:
    """Replace ``draft_similarities`` rows for *draft_id* with *similarities*.

    Deletes the old rows first, then inserts the new top-N.  The caller
    is responsible for committing.

    Args:
        conn: Active database connection.
        draft_id: UUID string of the source draft.
        similarities: Output of :func:`find_similar_drafts`.
        entity_set_hash: sha256 of the draft's current entity URI set.
    """
    draft_id_str = str(draft_id)
    conn.execute(
        "DELETE FROM draft_similarities WHERE draft_id = %s",
        (draft_id_str,),
    )
    for row in similarities:
        conn.execute(
            """
            INSERT INTO draft_similarities
                (draft_id, similar_draft_id, score, overlap_count, entity_set_hash)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (draft_id, similar_draft_id) DO UPDATE
                SET score = EXCLUDED.score,
                    overlap_count = EXCLUDED.overlap_count,
                    entity_set_hash = EXCLUDED.entity_set_hash,
                    computed_at = now()
            """,
            (
                draft_id_str,
                str(row["similar_draft_id"]),
                row["score"],
                row["overlap_count"],
                entity_set_hash,
            ),
        )
    logger.debug(
        "similarity: persisted %d rows for draft=%s (hash=%s)",
        len(similarities),
        draft_id_str,
        entity_set_hash[:12],
    )


# ---------------------------------------------------------------------------
# Detail-page query with cross-org masking
# ---------------------------------------------------------------------------


def list_similar_drafts_for_view(
    conn,
    draft_id: str,
    viewer_org_id: str,
) -> list[dict]:
    """Return similar drafts for the detail page with cross-org masking.

    Executes a single LEFT JOIN so the DB layer itself returns NULL for
    title/id fields on cross-org rows — defence in depth so the renderer
    cannot accidentally expose another org's draft title even if the
    assertion check is skipped.

    Returns:
        List of dicts, one per ``draft_similarities`` row ordered by
        ``score DESC``.  Fields:

        Within-org row:
            ``similar_draft_id`` (str), ``title`` (str), ``score`` (float),
            ``overlap_count`` (int), ``masked`` (False).

        Cross-org row:
            ``similar_draft_id`` (None), ``title`` (None),
            ``score`` (float), ``overlap_count`` (int), ``masked`` (True).
    """
    draft_id_str = str(draft_id)
    viewer_org_id_str = str(viewer_org_id)

    rows = conn.execute(
        """
        SELECT
            CASE WHEN d.org_id::text = %s THEN ds.similar_draft_id::text ELSE NULL END
                AS similar_draft_id,
            CASE WHEN d.org_id::text = %s THEN d.title ELSE NULL END
                AS title,
            ds.score,
            ds.overlap_count,
            d.org_id::text <> %s AS masked
        FROM draft_similarities ds
        LEFT JOIN drafts d ON d.id = ds.similar_draft_id
        WHERE ds.draft_id = %s
        ORDER BY ds.score DESC
        """,
        (viewer_org_id_str, viewer_org_id_str, viewer_org_id_str, draft_id_str),
    ).fetchall()

    result: list[dict] = []
    for row in rows:
        similar_draft_id, title, score, overlap_count, masked = row
        result.append(
            {
                "similar_draft_id": similar_draft_id,
                "title": title,
                "score": float(score) if score is not None else 0.0,
                "overlap_count": overlap_count,
                "masked": bool(masked),
            }
        )
    return result
