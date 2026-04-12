"""Tests for RAG tenant scoping (#576).

Covers the ``org_id`` parameter on :func:`app.rag.retriever.Retriever.retrieve`
and the :func:`app.rag.retriever.delete_chunks_for_draft` cascade helper.

The retriever enforces tenant scoping via a WHERE clause
``(org_id IS NULL OR org_id = $1)``. These tests exercise that contract two
ways:

* **SQL-contract tests** assert the emitted SQL/params shape. They catch
  regressions where someone removes the tenant predicate entirely or forgets
  to bind the caller's ``org_id``.
* **Fake-DB simulation tests** wire a ``MagicMock`` connection that actually
  evaluates the predicate against a small in-memory chunk set. They catch
  logic bugs — e.g. returning every row regardless of the predicate.

No real Postgres, Voyage, or Anthropic calls are made.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock, patch

from app.rag.retriever import Retriever, delete_chunks_for_draft

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_stub_embedder():
    embedder = MagicMock()

    async def fake_embed(texts):
        return [[0.1] * 1024 for _ in texts]

    embedder.embed = fake_embed
    embedder.dimensions = 1024
    return embedder


ORG_A = "11111111-1111-1111-1111-111111111111"
ORG_B = "22222222-2222-2222-2222-222222222222"


# In-memory chunk fixture used by the fake-DB tests. Each tuple is
# ``(content, metadata, org_id, source_type, source_id)``. Scores are
# computed by declaration order: first row wins.
FIXTURE_CHUNKS = [
    ("Public ontology text", {"source_type": "ontology"}, None, "ontology", None),
    ("Org A private draft", {"source_type": "draft"}, ORG_A, "draft", "draft-a"),
    ("Org B private draft", {"source_type": "draft"}, ORG_B, "draft", "draft-b"),
    (
        "Another public court decision",
        {"source_type": "court_decision"},
        None,
        "court_decision",
        None,
    ),
]


def _fake_conn_filtering_by_predicate():
    """Return a MagicMock connection that honours the tenant predicate.

    The real retriever emits ``WHERE (org_id IS NULL OR org_id = %s)`` (plus
    optionally ``AND source_type = %s``) and we want the test to verify that
    contract end-to-end. So this fake actually evaluates the predicate
    against ``FIXTURE_CHUNKS`` based on the bound ``org_id`` parameter.
    """
    conn = MagicMock()

    def execute(sql: str, params):
        # params layout:
        #   no source_type: (embedding_str, org_id, embedding_str, k)
        #   source_type:    (embedding_str, org_id, source_type, embedding_str, k)
        org_param = params[1]
        source_filter: str | None = None
        if "source_type = %s" in sql:
            source_filter = params[2]

        rows = []
        for idx, (content, meta, row_org, row_stype, _sid) in enumerate(FIXTURE_CHUNKS):
            if row_org is not None and row_org != org_param:
                continue
            if source_filter is not None and row_stype != source_filter:
                continue
            # Fake similarity score: higher for earlier rows so ordering is stable.
            score = 1.0 - (idx * 0.1)
            rows.append((content, json.dumps(meta), score))

        cursor = MagicMock()
        cursor.fetchall.return_value = rows
        return cursor

    conn.execute.side_effect = execute
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)
    return conn


# ---------------------------------------------------------------------------
# Retrieval: tenant scoping semantics
# ---------------------------------------------------------------------------


class TestRetrievalTenantScoping:
    @patch("app.rag.retriever.get_connection")
    def test_org_caller_sees_public_plus_own_private(self, mock_get_conn: MagicMock):
        """Org-A caller gets public NULL rows + their own private rows only."""
        mock_get_conn.return_value = _fake_conn_filtering_by_predicate()
        retriever = Retriever(embedding_provider=_make_stub_embedder())

        results = asyncio.run(retriever.retrieve("x", org_id=ORG_A))
        contents = {r.content for r in results}

        assert "Public ontology text" in contents
        assert "Another public court decision" in contents
        assert "Org A private draft" in contents
        # Critically, the other org's private draft must be invisible.
        assert "Org B private draft" not in contents

    @patch("app.rag.retriever.get_connection")
    def test_org_b_caller_never_sees_org_a_private(self, mock_get_conn: MagicMock):
        """Cross-org leakage regression: Org-B can never see Org-A rows."""
        mock_get_conn.return_value = _fake_conn_filtering_by_predicate()
        retriever = Retriever(embedding_provider=_make_stub_embedder())

        results = asyncio.run(retriever.retrieve("x", org_id=ORG_B))
        contents = {r.content for r in results}

        assert "Org A private draft" not in contents
        assert "Org B private draft" in contents
        assert "Public ontology text" in contents

    @patch("app.rag.retriever.get_connection")
    def test_none_org_returns_only_public(self, mock_get_conn: MagicMock):
        """``org_id=None`` means 'no org' — only public NULL-scoped rows."""
        mock_get_conn.return_value = _fake_conn_filtering_by_predicate()
        retriever = Retriever(embedding_provider=_make_stub_embedder())

        results = asyncio.run(retriever.retrieve("x", org_id=None))
        contents = {r.content for r in results}

        assert contents == {"Public ontology text", "Another public court decision"}

    @patch("app.rag.retriever.get_connection")
    def test_default_org_id_is_none(self, mock_get_conn: MagicMock):
        """Omitting org_id is equivalent to passing None — public only."""
        mock_get_conn.return_value = _fake_conn_filtering_by_predicate()
        retriever = Retriever(embedding_provider=_make_stub_embedder())

        results = asyncio.run(retriever.retrieve("x"))
        contents = {r.content for r in results}

        assert contents == {"Public ontology text", "Another public court decision"}


# ---------------------------------------------------------------------------
# Retrieval: SQL contract
# ---------------------------------------------------------------------------


class TestRetrievalSQLContract:
    @patch("app.rag.retriever.get_connection")
    def test_sql_includes_tenant_predicate(self, mock_get_conn: MagicMock):
        """Every SELECT must gate on (org_id IS NULL OR org_id = $1)."""
        conn = MagicMock()
        cursor = MagicMock()
        cursor.fetchall.return_value = []
        conn.execute.return_value = cursor
        conn.__enter__ = MagicMock(return_value=conn)
        conn.__exit__ = MagicMock(return_value=False)
        mock_get_conn.return_value = conn

        retriever = Retriever(embedding_provider=_make_stub_embedder())
        asyncio.run(retriever.retrieve("q", org_id=ORG_A))

        sql, params = conn.execute.call_args[0]
        assert "org_id IS NULL OR org_id = %s" in sql
        assert ORG_A in params

    @patch("app.rag.retriever.get_connection")
    def test_sql_includes_tenant_predicate_with_source_type(self, mock_get_conn: MagicMock):
        """The tenant predicate must still appear when source_type is set."""
        conn = MagicMock()
        cursor = MagicMock()
        cursor.fetchall.return_value = []
        conn.execute.return_value = cursor
        conn.__enter__ = MagicMock(return_value=conn)
        conn.__exit__ = MagicMock(return_value=False)
        mock_get_conn.return_value = conn

        retriever = Retriever(embedding_provider=_make_stub_embedder())
        asyncio.run(retriever.retrieve("q", source_type="draft", org_id=ORG_A))

        sql, params = conn.execute.call_args[0]
        assert "org_id IS NULL OR org_id = %s" in sql
        assert "source_type = %s" in sql
        assert ORG_A in params
        assert "draft" in params


# ---------------------------------------------------------------------------
# Draft-delete cascade
# ---------------------------------------------------------------------------


class TestDeleteChunksForDraft:
    def test_deletes_rows_for_draft(self):
        """Helper issues the expected DELETE and returns affected row count."""
        conn = MagicMock()
        cursor = MagicMock()
        cursor.rowcount = 3
        conn.execute.return_value = cursor

        removed = delete_chunks_for_draft(conn, "draft-uuid-123")

        assert removed == 3
        sql, params = conn.execute.call_args[0]
        assert "DELETE FROM rag_chunks" in sql
        assert "source_type = 'draft'" in sql
        assert "source_id = %s" in sql
        assert params == ("draft-uuid-123",)

    def test_zero_rowcount_returns_zero(self):
        """None / zero rowcount is normalised to 0, not propagated as None."""
        conn = MagicMock()
        cursor = MagicMock()
        cursor.rowcount = None
        conn.execute.return_value = cursor

        assert delete_chunks_for_draft(conn, "draft-uuid-zero") == 0

    def test_stringifies_uuid_argument(self):
        """Non-string draft_id (e.g. uuid.UUID) is coerced to str for the bind."""
        import uuid

        conn = MagicMock()
        cursor = MagicMock()
        cursor.rowcount = 0
        conn.execute.return_value = cursor

        draft_uuid = uuid.UUID("44444444-4444-4444-4444-444444444444")
        delete_chunks_for_draft(conn, draft_uuid)

        _, params = conn.execute.call_args[0]
        assert params == (str(draft_uuid),)
