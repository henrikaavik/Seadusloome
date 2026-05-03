-- Migration 028: draft similarity signal (issue #621)
--
-- Adds two tables that support the "sarnased eelnõud" feature:
--
--   draft_similarities  — precomputed Jaccard scores between drafts.
--                         Stamped with entity_set_hash so an unchanged
--                         entity set can skip the expensive recompute.
--
--   draft_uri_index     — inverted index: uri → [draft_id] rows.
--                         Turns the per-draft similarity compute from
--                         O(N²) (all-pairs scan) into
--                         O(|uris| · avg_candidates), which stays fast
--                         even at 22 000+ drafts.
--
-- Both tables are derived entirely from draft_entities and can be
-- rebuilt from scratch at any time — they contain no user-entered data.
--
-- ROLLBACK (manual; run *before* reverting the app to pre-028 code):
--   DROP TABLE IF EXISTS draft_similarities;
--   DROP TABLE IF EXISTS draft_uri_index;
--   DELETE FROM schema_migrations WHERE version = '028_draft_similarities';
--
-- FORWARD-FIX: re-running this migration is safe via IF NOT EXISTS.

CREATE TABLE IF NOT EXISTS draft_similarities (
    draft_id          uuid NOT NULL REFERENCES drafts(id) ON DELETE CASCADE,
    similar_draft_id  uuid NOT NULL REFERENCES drafts(id) ON DELETE CASCADE,
    score             numeric(4,3) NOT NULL,        -- 0.000–1.000 Jaccard
    overlap_count     int NOT NULL,                  -- |A ∩ B| URI count
    entity_set_hash   text NOT NULL,                 -- sha256 of sorted entity_uri list
    computed_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (draft_id, similar_draft_id),
    CHECK (draft_id <> similar_draft_id),
    CHECK (score >= 0 AND score <= 1)
);

-- Fast lookup: all similar drafts for a given draft, ordered by score
CREATE INDEX IF NOT EXISTS idx_draft_similarities_lookup
    ON draft_similarities (draft_id, score DESC);

-- Inverted index for candidate generation:
-- "given a URI, list every draft that contains it"
CREATE TABLE IF NOT EXISTS draft_uri_index (
    uri       text NOT NULL,
    draft_id  uuid NOT NULL REFERENCES drafts(id) ON DELETE CASCADE,
    PRIMARY KEY (uri, draft_id)
);

-- Secondary index to efficiently DELETE all rows for a given draft
-- when its entity set changes and the index needs to be rebuilt.
CREATE INDEX IF NOT EXISTS idx_draft_uri_index_draft
    ON draft_uri_index (draft_id);
