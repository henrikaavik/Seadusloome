"""Tests for the draft similarity module (issue #621).

Covers:
- compute_entity_set_hash — stable hash, deduplication
- get_similarity_threshold — default, env override, clamping
- find_similar_drafts — Jaccard correctness, threshold filtering, top-N cap,
  empty URI set
- list_similar_drafts_for_view — cross-org masking
- update_uri_index — DELETE then INSERT shape (via mock)
- persist_similarities — writes rows + hash; replaces existing rows (via mock)
"""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock

import pytest

from app.docs.similarity import (
    DEFAULT_SIMILARITY_THRESHOLD,
    TOP_N_SIMILAR,
    compute_entity_set_hash,
    find_similar_drafts,
    get_similarity_threshold,
    list_similar_drafts_for_view,
    persist_similarities,
    update_uri_index,
)

# ---------------------------------------------------------------------------
# compute_entity_set_hash
# ---------------------------------------------------------------------------


def test_hash_same_uris_different_order():
    """Same logical set in any order → identical digest."""
    uris_a = ["http://example.com/1", "http://example.com/2", "http://example.com/3"]
    uris_b = list(reversed(uris_a))
    assert compute_entity_set_hash(uris_a) == compute_entity_set_hash(uris_b)


def test_hash_deduplicates():
    """Duplicate URIs are collapsed before hashing."""
    uris_with_dups = ["http://example.com/1", "http://example.com/1", "http://example.com/2"]
    uris_clean = ["http://example.com/1", "http://example.com/2"]
    assert compute_entity_set_hash(uris_with_dups) == compute_entity_set_hash(uris_clean)


def test_hash_different_uris_differ():
    """Different sets produce different hashes."""
    a = ["http://example.com/1"]
    b = ["http://example.com/2"]
    assert compute_entity_set_hash(a) != compute_entity_set_hash(b)


def test_hash_empty_list():
    """Empty list produces a deterministic hash (sha256 of empty string)."""
    h = compute_entity_set_hash([])
    assert len(h) == 64  # sha256 hex digest
    assert compute_entity_set_hash([]) == h


# ---------------------------------------------------------------------------
# get_similarity_threshold
# ---------------------------------------------------------------------------


def test_threshold_default(monkeypatch):
    """Returns DEFAULT_SIMILARITY_THRESHOLD when env var is absent."""
    monkeypatch.delenv("SEADUSLOOME_SIMILARITY_THRESHOLD", raising=False)
    assert get_similarity_threshold() == DEFAULT_SIMILARITY_THRESHOLD


def test_threshold_env_override(monkeypatch):
    """Respects a valid float in the env var."""
    monkeypatch.setenv("SEADUSLOOME_SIMILARITY_THRESHOLD", "0.30")
    assert get_similarity_threshold() == pytest.approx(0.30)


def test_threshold_clamps_below_zero(monkeypatch):
    """Negative value is clamped to 0.0."""
    monkeypatch.setenv("SEADUSLOOME_SIMILARITY_THRESHOLD", "-0.5")
    assert get_similarity_threshold() == 0.0


def test_threshold_clamps_above_one(monkeypatch):
    """Value above 1.0 is clamped to 1.0."""
    monkeypatch.setenv("SEADUSLOOME_SIMILARITY_THRESHOLD", "2.5")
    assert get_similarity_threshold() == 1.0


def test_threshold_invalid_falls_back_to_default(monkeypatch):
    """Non-numeric env var falls back to the default without crashing."""
    monkeypatch.setenv("SEADUSLOOME_SIMILARITY_THRESHOLD", "banana")
    assert get_similarity_threshold() == DEFAULT_SIMILARITY_THRESHOLD


# ---------------------------------------------------------------------------
# Helpers for building fake connections
# ---------------------------------------------------------------------------


def _make_conn(candidate_rows: list, uri_rows_by_id: dict) -> MagicMock:
    """Build a minimal mock connection for find_similar_drafts.

    candidate_rows: list of (draft_id,) tuples returned by the inverted-
        index query.
    uri_rows_by_id: mapping of draft_id → list of (uri,) tuples returned
        when the per-candidate URI query runs.
    """
    conn = MagicMock()

    def execute_side_effect(sql, params=None):
        cursor = MagicMock()
        sql_stripped = " ".join(sql.split())
        # Inverted index query
        if "draft_uri_index" in sql_stripped and "DISTINCT" in sql_stripped:
            cursor.fetchall.return_value = candidate_rows
        # Per-candidate URI fetch
        elif "draft_uri_index" in sql_stripped and params:
            cand_id = params[0] if isinstance(params, (list, tuple)) else None
            cursor.fetchall.return_value = uri_rows_by_id.get(str(cand_id), [])
        else:
            cursor.fetchall.return_value = []
            cursor.fetchone.return_value = None
        return cursor

    conn.execute.side_effect = execute_side_effect
    return conn


# ---------------------------------------------------------------------------
# find_similar_drafts
# ---------------------------------------------------------------------------


def test_jaccard_two_drafts_partial_overlap():
    """3 shared out of 5 + 5 URIs → Jaccard = 3/7 ≈ 0.429."""
    draft_id = str(uuid.uuid4())
    cand_id = str(uuid.uuid4())

    target_uris = [
        "http://example.com/1",
        "http://example.com/2",
        "http://example.com/3",
        "http://example.com/4",
        "http://example.com/5",
    ]
    cand_uris = [
        "http://example.com/1",
        "http://example.com/2",
        "http://example.com/3",
        "http://example.com/6",
        "http://example.com/7",
    ]
    # intersection: {1,2,3}  union: {1,2,3,4,5,6,7}  Jaccard: 3/7

    conn = _make_conn(
        candidate_rows=[(cand_id,)],
        uri_rows_by_id={cand_id: [(u,) for u in cand_uris]},
    )

    results = find_similar_drafts(conn, draft_id, target_uris, threshold=0.0)
    assert len(results) == 1
    result = results[0]
    assert result["similar_draft_id"] == cand_id
    assert result["overlap_count"] == 3
    assert result["score"] == pytest.approx(3 / 7, abs=0.001)


def test_threshold_filters_low_score():
    """A candidate with Jaccard 0.10 is excluded when threshold is 0.15."""
    draft_id = str(uuid.uuid4())
    cand_id = str(uuid.uuid4())

    # 1 shared out of 10 total → Jaccard = 1/10 = 0.10
    target_uris = [f"http://example.com/{i}" for i in range(9)]
    cand_uris = ["http://example.com/0", "http://example.com/99"]

    conn = _make_conn(
        candidate_rows=[(cand_id,)],
        uri_rows_by_id={cand_id: [(u,) for u in cand_uris]},
    )

    results = find_similar_drafts(conn, draft_id, target_uris, threshold=0.15)
    assert results == []


def test_top_n_cap():
    """When there are 15 candidates all above threshold, only TOP_N_SIMILAR are returned."""
    draft_id = str(uuid.uuid4())
    target_uris = [f"http://example.com/shared/{i}" for i in range(10)]

    # 15 candidates each sharing all 10 target URIs → Jaccard = 10/10 = 1.0
    cand_ids = [str(uuid.uuid4()) for _ in range(15)]
    candidate_rows = [(cid,) for cid in cand_ids]
    uri_rows_by_id = {cid: [(u,) for u in target_uris] for cid in cand_ids}

    conn = _make_conn(candidate_rows=candidate_rows, uri_rows_by_id=uri_rows_by_id)

    results = find_similar_drafts(conn, draft_id, target_uris, threshold=0.0)
    assert len(results) == TOP_N_SIMILAR
    assert TOP_N_SIMILAR == 10


def test_empty_uri_set_returns_empty():
    """An empty target URI list short-circuits before any DB calls."""
    conn = MagicMock()
    results = find_similar_drafts(conn, str(uuid.uuid4()), [], threshold=0.0)
    assert results == []
    conn.execute.assert_not_called()


def test_no_candidates_returns_empty():
    """When the inverted index yields no candidates, result is empty."""
    draft_id = str(uuid.uuid4())
    conn = _make_conn(candidate_rows=[], uri_rows_by_id={})
    results = find_similar_drafts(conn, draft_id, ["http://example.com/1"], threshold=0.0)
    assert results == []


# ---------------------------------------------------------------------------
# update_uri_index
# ---------------------------------------------------------------------------


def test_update_uri_index_delete_then_insert():
    """update_uri_index issues DELETE then executemany INSERT."""
    conn = MagicMock()
    draft_id = str(uuid.uuid4())
    uris = ["http://example.com/a", "http://example.com/b"]

    update_uri_index(conn, draft_id, uris)

    # First call must be the DELETE
    first_call = conn.execute.call_args_list[0]
    assert "DELETE FROM draft_uri_index" in first_call[0][0]
    assert first_call[0][1] == (draft_id,)

    # Second call must be executemany INSERT
    conn.executemany.assert_called_once()
    em_sql = conn.executemany.call_args[0][0]
    assert "INSERT INTO draft_uri_index" in em_sql


def test_update_uri_index_deduplicates_before_insert():
    """Duplicate URIs in the input are deduplicated before insertion."""
    conn = MagicMock()
    draft_id = str(uuid.uuid4())
    uris = ["http://example.com/x", "http://example.com/x", "http://example.com/y"]

    update_uri_index(conn, draft_id, uris)

    # executemany args should have 2 rows (not 3)
    inserted_rows = conn.executemany.call_args[0][1]
    uris_inserted = {row[0] for row in inserted_rows}
    assert len(uris_inserted) == 2


def test_update_uri_index_empty_skips_insert():
    """Empty URI list still issues DELETE but skips executemany."""
    conn = MagicMock()
    update_uri_index(conn, str(uuid.uuid4()), [])
    conn.execute.assert_called_once()  # only the DELETE
    conn.executemany.assert_not_called()


# ---------------------------------------------------------------------------
# persist_similarities
# ---------------------------------------------------------------------------


def test_persist_similarities_writes_top_n_rows():
    """persist_similarities issues DELETE + one INSERT per row."""
    conn = MagicMock()
    draft_id = str(uuid.uuid4())
    similarities = [
        {"similar_draft_id": str(uuid.uuid4()), "score": 0.8, "overlap_count": 8},
        {"similar_draft_id": str(uuid.uuid4()), "score": 0.6, "overlap_count": 6},
    ]
    entity_set_hash = "abc123"

    persist_similarities(conn, draft_id, similarities, entity_set_hash)

    # First execute = DELETE
    first = conn.execute.call_args_list[0]
    assert "DELETE FROM draft_similarities" in first[0][0]

    # Subsequent executes = INSERTs (2 rows)
    insert_calls = [c for c in conn.execute.call_args_list if "INSERT" in c[0][0]]
    assert len(insert_calls) == 2

    # Hash must appear in each insert
    for c in insert_calls:
        assert entity_set_hash in c[0][1]


def test_persist_similarities_replaces_existing_rows():
    """Calling persist_similarities twice on the same draft replaces rows."""
    conn = MagicMock()
    draft_id = str(uuid.uuid4())
    first_batch = [{"similar_draft_id": str(uuid.uuid4()), "score": 0.5, "overlap_count": 5}]
    second_batch = [
        {"similar_draft_id": str(uuid.uuid4()), "score": 0.7, "overlap_count": 7},
        {"similar_draft_id": str(uuid.uuid4()), "score": 0.3, "overlap_count": 3},
    ]

    persist_similarities(conn, draft_id, first_batch, "hash1")
    delete_count_after_first = sum(1 for c in conn.execute.call_args_list if "DELETE" in c[0][0])
    persist_similarities(conn, draft_id, second_batch, "hash2")
    delete_count_after_second = sum(1 for c in conn.execute.call_args_list if "DELETE" in c[0][0])

    # Each call must issue its own DELETE
    assert delete_count_after_first == 1
    assert delete_count_after_second == 2


# ---------------------------------------------------------------------------
# list_similar_drafts_for_view
# ---------------------------------------------------------------------------


def _make_view_conn(rows: list[tuple]) -> MagicMock:
    """Build a mock conn whose execute().fetchall() returns *rows*."""
    conn = MagicMock()
    cursor = MagicMock()
    cursor.fetchall.return_value = rows
    conn.execute.return_value = cursor
    return conn


def test_list_similar_drafts_within_org():
    """Within-org row returns title and draft_id, masked=False."""
    cand_id = str(uuid.uuid4())
    viewer_org = str(uuid.uuid4())

    # (similar_draft_id, title, score, overlap_count, masked)
    rows = [(cand_id, "Eelnõu A", 0.5, 5, False)]
    conn = _make_view_conn(rows)

    result = list_similar_drafts_for_view(conn, str(uuid.uuid4()), viewer_org)

    assert len(result) == 1
    row = result[0]
    assert row["similar_draft_id"] == cand_id
    assert row["title"] == "Eelnõu A"
    assert row["score"] == pytest.approx(0.5)
    assert row["overlap_count"] == 5
    assert row["masked"] is False


def test_list_similar_drafts_cross_org_masked():
    """Cross-org row returns title=None, similar_draft_id=None, masked=True."""
    viewer_org = str(uuid.uuid4())

    # DB returns NULL for protected fields on cross-org rows
    rows = [(None, None, 0.3, 3, True)]
    conn = _make_view_conn(rows)

    result = list_similar_drafts_for_view(conn, str(uuid.uuid4()), viewer_org)

    assert len(result) == 1
    row = result[0]
    assert row["similar_draft_id"] is None, "cross-org draft_id must be masked to None"
    assert row["title"] is None, "cross-org title must be masked to None"
    assert row["masked"] is True
    assert row["score"] == pytest.approx(0.3)


def test_list_similar_drafts_empty():
    """No similar drafts → empty list."""
    conn = _make_view_conn([])
    result = list_similar_drafts_for_view(conn, str(uuid.uuid4()), str(uuid.uuid4()))
    assert result == []


def test_list_similar_drafts_mixed_orgs():
    """Mix of within-org and cross-org rows is correctly categorised."""
    cand_id_same = str(uuid.uuid4())
    viewer_org = str(uuid.uuid4())

    rows = [
        (cand_id_same, "Same Org Draft", 0.8, 8, False),
        (None, None, 0.4, 4, True),
    ]
    conn = _make_view_conn(rows)

    result = list_similar_drafts_for_view(conn, str(uuid.uuid4()), viewer_org)

    assert len(result) == 2
    within = [r for r in result if not r["masked"]]
    cross = [r for r in result if r["masked"]]
    assert len(within) == 1
    assert len(cross) == 1
    assert within[0]["title"] == "Same Org Draft"
    assert cross[0]["title"] is None
    assert cross[0]["similar_draft_id"] is None
