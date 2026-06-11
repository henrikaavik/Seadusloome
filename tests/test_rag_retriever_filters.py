"""Tests for the metadata-filter API on :class:`app.rag.retriever.Retriever`.

Issue #311 — adds a ``filters`` kwarg to ``retrieve()`` so callers can
constrain results by ``source_type`` / ``source_uri`` / ``entity_type``.
Everything goes through psycopg's ``%s`` parameterization — no values
are interpolated into the SQL string.

All tests mock the DB and embedder. No real DB or API calls.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.rag.retriever import Retriever, _build_filter_clauses


def _make_stub_embedder():
    """Mock embedding provider returning deterministic 1024d vectors."""
    embedder = MagicMock()

    async def fake_embed(texts, **kwargs):
        return [[0.1] * 1024 for _ in texts]

    embedder.embed = fake_embed
    embedder.dimensions = 1024
    return embedder


def _make_assertable_embedder():
    """Like ``_make_stub_embedder`` but the ``embed`` method is an
    :class:`AsyncMock`, so tests can assert call counts / call args.
    """
    embedder = MagicMock()
    embedder.embed = AsyncMock(return_value=[[0.1] * 1024])
    embedder.dimensions = 1024
    return embedder


def _make_mock_conn(rows: list[tuple]) -> MagicMock:
    """Build a context-manager-compatible mock connection."""
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.fetchall.return_value = rows
    mock_conn.execute.return_value = mock_cursor
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)
    return mock_conn


class TestBuildFilterClauses:
    """Pure-Python tests for the SQL fragment builder."""

    def test_empty_filters_returns_empty(self):
        assert _build_filter_clauses(None) == ([], [])
        assert _build_filter_clauses({}) == ([], [])

    def test_scalar_value_emits_equality(self):
        clauses, params = _build_filter_clauses({"source_type": "law_text"})
        assert clauses == ["source_type = %s"]
        assert params == ["law_text"]

    def test_list_value_emits_any(self):
        clauses, params = _build_filter_clauses({"source_type": ["law_text", "court_decision"]})
        assert clauses == ["source_type = ANY(%s)"]
        assert params == [["law_text", "court_decision"]]

    def test_tuple_value_emits_any(self):
        clauses, params = _build_filter_clauses({"source_type": ("law_text", "court_decision")})
        assert clauses == ["source_type = ANY(%s)"]
        # tuples are normalised to lists for psycopg array binding
        assert params == [["law_text", "court_decision"]]

    def test_empty_list_emits_false(self):
        clauses, params = _build_filter_clauses({"source_type": []})
        # Empty list => match nothing, no params bound
        assert clauses == ["FALSE"]
        assert params == []

    def test_none_value_emits_is_null(self):
        clauses, params = _build_filter_clauses({"entity_type": None})
        assert clauses == ["metadata->>'entity_type' IS NULL"]
        assert params == []

    def test_entity_type_uses_jsonb_extract(self):
        clauses, params = _build_filter_clauses({"entity_type": "Provision"})
        # entity_type lives in metadata JSONB, not as a top-level column.
        assert clauses == ["metadata->>'entity_type' = %s"]
        assert params == ["Provision"]

    def test_source_uri_uses_top_level_column(self):
        clauses, params = _build_filter_clauses({"source_uri": "https://example.test/law/1"})
        assert clauses == ["source_uri = %s"]
        assert params == ["https://example.test/law/1"]

    def test_unknown_key_raises_value_error(self):
        with pytest.raises(ValueError, match="Unknown filter key"):
            _build_filter_clauses({"not_a_column": "x"})

    def test_unknown_key_message_lists_allowed(self):
        with pytest.raises(ValueError, match="Allowed keys"):
            _build_filter_clauses({"bogus": "x"})

    def test_multiple_filters_combine(self):
        clauses, params = _build_filter_clauses(
            {"source_type": "law_text", "entity_type": "Provision"}
        )
        assert "source_type = %s" in clauses
        assert "metadata->>'entity_type' = %s" in clauses
        assert "law_text" in params
        assert "Provision" in params


class TestRetrieveBackCompat:
    """The new ``filters`` kwarg must not break existing call sites."""

    @patch("app.rag.retriever.get_connection")
    def test_no_filter_matches_previous_behavior(self, mock_get_conn: MagicMock):
        embedder = _make_stub_embedder()
        retriever = Retriever(embedding_provider=embedder)
        mock_conn = _make_mock_conn([("text", json.dumps({"x": 1}), 0.9)])
        mock_get_conn.return_value = mock_conn

        results = asyncio.run(retriever.retrieve("test"))

        assert len(results) == 1
        sql = mock_conn.execute.call_args[0][0]
        # No extra filter clauses besides the tenant guard.
        assert "source_type" not in sql
        assert "entity_type" not in sql
        # Tenant scope is still applied.
        assert "org_id IS NULL OR org_id = %s" in sql

    @patch("app.rag.retriever.get_connection")
    def test_legacy_source_type_shortcut_still_works(self, mock_get_conn: MagicMock):
        embedder = _make_stub_embedder()
        retriever = Retriever(embedding_provider=embedder)
        mock_conn = _make_mock_conn([])
        mock_get_conn.return_value = mock_conn

        asyncio.run(retriever.retrieve("test", source_type="court_decision"))

        sql, params = mock_conn.execute.call_args[0]
        assert "source_type = %s" in sql
        assert "court_decision" in params


class TestRetrieveFilters:
    """End-to-end-ish tests that the SQL + params reach the DB correctly."""

    @patch("app.rag.retriever.get_connection")
    def test_filter_by_source_type_scalar(self, mock_get_conn: MagicMock):
        embedder = _make_stub_embedder()
        retriever = Retriever(embedding_provider=embedder)
        mock_conn = _make_mock_conn([("law text", json.dumps({"source_type": "law_text"}), 0.95)])
        mock_get_conn.return_value = mock_conn

        results = asyncio.run(retriever.retrieve("test", filters={"source_type": "law_text"}))

        assert len(results) == 1
        sql, params = mock_conn.execute.call_args[0]
        assert "source_type = %s" in sql
        assert "law_text" in params
        # Tenant scope still present.
        assert "org_id IS NULL OR org_id = %s" in sql

    @patch("app.rag.retriever.get_connection")
    def test_filter_by_source_type_list(self, mock_get_conn: MagicMock):
        embedder = _make_stub_embedder()
        retriever = Retriever(embedding_provider=embedder)
        mock_conn = _make_mock_conn(
            [
                ("law text", json.dumps({"source_type": "law_text"}), 0.9),
                ("court text", json.dumps({"source_type": "court_decision"}), 0.8),
            ]
        )
        mock_get_conn.return_value = mock_conn

        results = asyncio.run(
            retriever.retrieve(
                "test",
                filters={"source_type": ["law_text", "court_decision"]},
            )
        )

        assert len(results) == 2
        sql, params = mock_conn.execute.call_args[0]
        assert "source_type = ANY(%s)" in sql
        assert ["law_text", "court_decision"] in params

    @patch("app.rag.retriever.get_connection")
    def test_filter_by_entity_type(self, mock_get_conn: MagicMock):
        embedder = _make_stub_embedder()
        retriever = Retriever(embedding_provider=embedder)
        mock_conn = _make_mock_conn([("p", json.dumps({"entity_type": "Provision"}), 0.9)])
        mock_get_conn.return_value = mock_conn

        asyncio.run(retriever.retrieve("test", filters={"entity_type": "Provision"}))

        sql, params = mock_conn.execute.call_args[0]
        # entity_type lives in JSONB, not as a column.
        assert "metadata->>'entity_type' = %s" in sql
        assert "Provision" in params

    @patch("app.rag.retriever.get_connection")
    def test_filter_by_source_uri(self, mock_get_conn: MagicMock):
        embedder = _make_stub_embedder()
        retriever = Retriever(embedding_provider=embedder)
        mock_conn = _make_mock_conn([])
        mock_get_conn.return_value = mock_conn

        asyncio.run(
            retriever.retrieve(
                "test",
                filters={"source_uri": "https://example.test/law/1"},
            )
        )

        sql, params = mock_conn.execute.call_args[0]
        assert "source_uri = %s" in sql
        assert "https://example.test/law/1" in params

    @patch("app.rag.retriever.get_connection")
    def test_filter_combines_with_org_scope(self, mock_get_conn: MagicMock):
        """Filters must AND with the tenant predicate, not replace it."""
        embedder = _make_stub_embedder()
        retriever = Retriever(embedding_provider=embedder)
        mock_conn = _make_mock_conn([])
        mock_get_conn.return_value = mock_conn

        asyncio.run(
            retriever.retrieve(
                "test",
                org_id="org-abc",
                filters={"source_type": "draft"},
            )
        )

        sql, params = mock_conn.execute.call_args[0]
        # Both predicates present.
        assert "org_id IS NULL OR org_id = %s" in sql
        assert "source_type = %s" in sql
        # Connected by AND.
        assert " AND " in sql
        # Both values bound.
        assert "org-abc" in params
        assert "draft" in params

    @patch("app.rag.retriever.get_connection")
    def test_sql_injection_attempt_is_parameterised(self, mock_get_conn: MagicMock):
        """A malicious value flows as a bound param, not into the SQL text."""
        embedder = _make_stub_embedder()
        retriever = Retriever(embedding_provider=embedder)
        mock_conn = _make_mock_conn([])
        mock_get_conn.return_value = mock_conn

        evil = "'; DROP TABLE rag_chunks; --"
        results = asyncio.run(retriever.retrieve("test", filters={"source_type": evil}))

        # No exception, no rows (no chunk has that source_type).
        assert results == []
        sql, params = mock_conn.execute.call_args[0]
        # The dangerous payload is NEVER substituted into the SQL string.
        assert "DROP TABLE" not in sql
        assert evil not in sql
        # It IS bound as a parameter — that's the whole point.
        assert evil in params

    @patch("app.rag.retriever.get_connection")
    def test_unknown_filter_key_raises_before_db_call(self, mock_get_conn: MagicMock):
        """Unknown keys should raise ValueError without hitting the DB."""
        embedder = _make_stub_embedder()
        retriever = Retriever(embedding_provider=embedder)
        mock_conn = _make_mock_conn([])
        mock_get_conn.return_value = mock_conn

        with pytest.raises(ValueError, match="Unknown filter key"):
            asyncio.run(retriever.retrieve("test", filters={"definitely_not_a_column": "x"}))

        # DB was never touched.
        mock_conn.execute.assert_not_called()

    @patch("app.rag.retriever.get_connection")
    def test_filters_takes_precedence_over_legacy_shortcut(self, mock_get_conn: MagicMock):
        """When both ``filters`` and ``source_type=`` are given, filters wins.

        This avoids accidentally AND'ing two competing predicates against
        the same column (e.g. ``source_type = 'a' AND source_type = 'b'``
        which would always be empty).
        """
        embedder = _make_stub_embedder()
        retriever = Retriever(embedding_provider=embedder)
        mock_conn = _make_mock_conn([])
        mock_get_conn.return_value = mock_conn

        asyncio.run(
            retriever.retrieve(
                "test",
                source_type="ignored",
                filters={"source_type": "winner"},
            )
        )

        sql, params = mock_conn.execute.call_args[0]
        # The filter value is bound; the legacy shortcut is dropped.
        assert "winner" in params
        assert "ignored" not in params
        # Only one source_type predicate.
        assert sql.count("source_type = %s") == 1

    @patch("app.rag.retriever.get_connection")
    def test_none_filter_value_emits_is_null(self, mock_get_conn: MagicMock):
        embedder = _make_stub_embedder()
        retriever = Retriever(embedding_provider=embedder)
        mock_conn = _make_mock_conn([])
        mock_get_conn.return_value = mock_conn

        asyncio.run(retriever.retrieve("test", filters={"entity_type": None}))

        sql, _params = mock_conn.execute.call_args[0]
        assert "metadata->>'entity_type' IS NULL" in sql

    @patch("app.rag.retriever.get_connection")
    def test_param_order_matches_sql_placeholders(self, mock_get_conn: MagicMock):
        """The bound params must line up with %s positions in the SQL.

        Order: embedding (SELECT), org_id, extra filter params,
        embedding (ORDER BY), k.
        """
        embedder = _make_stub_embedder()
        retriever = Retriever(embedding_provider=embedder)
        mock_conn = _make_mock_conn([])
        mock_get_conn.return_value = mock_conn

        asyncio.run(
            retriever.retrieve(
                "test",
                k=7,
                org_id="org-xyz",
                filters={"source_type": "law_text"},
            )
        )

        sql, params = mock_conn.execute.call_args[0]
        # Position-sensitive structure check.
        assert params[1] == "org-xyz"  # tenant slot
        assert params[2] == "law_text"  # filter slot
        assert params[-1] == 7  # LIMIT slot
        # The two %s::vector slots share the same embedding string.
        assert params[0] == params[-2]
        # Sanity: number of %s in SQL matches number of params.
        assert sql.count("%s") == len(params)


class TestLegacySourceTypeMergeWithFilters:
    """Regression tests for the P2 review fix: the legacy ``source_type=``
    shortcut must be folded INTO the filter dict whenever the dict does
    not already contain a ``source_type`` key. The old code dropped the
    shortcut as soon as ``filters`` was non-empty, which silently widened
    the result set to all source types.
    """

    @patch("app.rag.retriever.get_connection")
    def test_legacy_kwarg_merges_with_non_colliding_filters(self, mock_get_conn: MagicMock):
        """``source_type="draft"`` + ``filters={"entity_type": "Provision"}``
        must AND both predicates — not silently drop the shortcut.
        """
        embedder = _make_stub_embedder()
        retriever = Retriever(embedding_provider=embedder)
        mock_conn = _make_mock_conn([])
        mock_get_conn.return_value = mock_conn

        asyncio.run(
            retriever.retrieve(
                "test",
                source_type="draft",
                filters={"entity_type": "Provision"},
            )
        )

        sql, params = mock_conn.execute.call_args[0]
        # BOTH predicates must be present in the SQL.
        assert "source_type = %s" in sql
        assert "metadata->>'entity_type' = %s" in sql
        # BOTH bound values must reach the DB.
        assert "draft" in params
        assert "Provision" in params
        # They must be AND'd, not OR'd, alongside the tenant guard.
        assert " AND " in sql

    @patch("app.rag.retriever.get_connection")
    def test_filters_wins_on_source_type_collision(self, mock_get_conn: MagicMock):
        """When ``filters`` already has ``source_type``, the dict value
        wins and the legacy kwarg is ignored. (Unchanged collision case.)
        """
        embedder = _make_stub_embedder()
        retriever = Retriever(embedding_provider=embedder)
        mock_conn = _make_mock_conn([])
        mock_get_conn.return_value = mock_conn

        asyncio.run(
            retriever.retrieve(
                "test",
                source_type="draft",
                filters={"source_type": "law"},
            )
        )

        sql, params = mock_conn.execute.call_args[0]
        # Only one source_type predicate (no double-AND).
        assert sql.count("source_type = %s") == 1
        # The filter dict value wins.
        assert "law" in params
        assert "draft" not in params

    @patch("app.rag.retriever.get_connection")
    def test_legacy_source_type_only_unchanged(self, mock_get_conn: MagicMock):
        """``source_type="draft"`` with no ``filters`` keeps the legacy
        single-predicate behaviour intact.
        """
        embedder = _make_stub_embedder()
        retriever = Retriever(embedding_provider=embedder)
        mock_conn = _make_mock_conn([])
        mock_get_conn.return_value = mock_conn

        asyncio.run(retriever.retrieve("test", source_type="draft"))

        sql, params = mock_conn.execute.call_args[0]
        assert "source_type = %s" in sql
        assert "draft" in params
        # No JSONB filter clause leaked in.
        assert "metadata->>" not in sql

    @patch("app.rag.retriever.get_connection")
    def test_filters_only_no_implicit_source_type(self, mock_get_conn: MagicMock):
        """``filters={"entity_type": "X"}`` with no legacy kwarg must
        emit ONLY the entity_type predicate — no source_type clause and
        no source_type bound param.
        """
        embedder = _make_stub_embedder()
        retriever = Retriever(embedding_provider=embedder)
        mock_conn = _make_mock_conn([])
        mock_get_conn.return_value = mock_conn

        asyncio.run(retriever.retrieve("test", filters={"entity_type": "X"}))

        sql, params = mock_conn.execute.call_args[0]
        assert "metadata->>'entity_type' = %s" in sql
        assert "X" in params
        # No source_type predicate at all.
        assert "source_type" not in sql


class TestEmptyListShortCircuit:
    """Regression tests for the P3 review fix: an empty-list filter value
    must short-circuit BEFORE the embedding call, so we don't burn Voyage
    AI quota on a query whose SQL is guaranteed to return zero rows.
    """

    @patch("app.rag.retriever.get_connection")
    def test_empty_list_filter_skips_embed(self, mock_get_conn: MagicMock):
        """``filters={"source_type": []}`` returns ``[]`` without
        invoking the embedder OR touching the DB."""
        embedder = _make_assertable_embedder()
        retriever = Retriever(embedding_provider=embedder)
        mock_conn = _make_mock_conn([])
        mock_get_conn.return_value = mock_conn

        results = asyncio.run(retriever.retrieve("test", filters={"source_type": []}))

        assert results == []
        # The whole point of the fix: no embedding API call.
        embedder.embed.assert_not_called()
        # And no DB call either — we returned before building the SQL.
        mock_conn.execute.assert_not_called()

    @patch("app.rag.retriever.get_connection")
    def test_scalar_filter_still_calls_embed_once(self, mock_get_conn: MagicMock):
        """Sanity: the happy path (single-value filter) still embeds the
        query exactly once. Guards against an over-eager short-circuit
        regression.
        """
        embedder = _make_assertable_embedder()
        retriever = Retriever(embedding_provider=embedder)
        mock_conn = _make_mock_conn([])
        mock_get_conn.return_value = mock_conn

        asyncio.run(retriever.retrieve("test", filters={"source_type": "draft"}))

        embedder.embed.assert_called_once_with(
            ["test"], user_id=None, org_id=None, feature="embedding"
        )
        mock_conn.execute.assert_called_once()
