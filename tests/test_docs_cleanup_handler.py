"""Tests for the ``draft_cleanup`` background job handler (#628, #736, #845).

The handler keeps the user-visible delete fast by moving external
cleanups (encrypted file + Jena graph + rendered export purge)
off-line. Within a run it attempts every item even when one fails, but
any failure makes the run raise at the end (#845 B3): each step is
idempotent (missing file / absent graph count as success), so the
worker's bounded retry budget can re-run the whole payload instead of
silently reporting success over orphaned sensitive artifacts.

#736 widened the payload from a single ``storage_path`` / ``graph_uri``
to ``storage_paths`` / ``graph_uris`` arrays — one entry per draft
version — while still honouring the legacy singular keys for any job
enqueued by an older app build that was in flight at deploy time.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from app.docs.cleanup_handler import draft_cleanup


class TestDraftCleanupHandler:
    # -- legacy singular-key payloads (backward compat) --------------------

    @patch("app.docs.cleanup_handler.delete_named_graph")
    @patch("app.docs.cleanup_handler.delete_encrypted_file")
    def test_legacy_singular_payload_deletes_file_and_graph(self, mock_file, mock_graph):
        payload = {
            "draft_id": "d1",
            "storage_path": "/tmp/cipher.enc",
            "graph_uri": "https://example.org/drafts/d1",
        }
        result = draft_cleanup(payload)
        mock_file.assert_called_once_with("/tmp/cipher.enc")
        mock_graph.assert_called_once_with("https://example.org/drafts/d1")
        assert result["storage_deleted"] == 1
        assert result["graph_deleted"] == 1

    @patch("app.docs.cleanup_handler.delete_named_graph")
    @patch("app.docs.cleanup_handler.delete_encrypted_file")
    def test_missing_storage_counts_as_success(self, mock_file, mock_graph):
        mock_file.side_effect = FileNotFoundError()
        payload = {
            "draft_id": "d1",
            "storage_path": "/tmp/cipher.enc",
            "graph_uri": "https://example.org/drafts/d1",
        }
        result = draft_cleanup(payload)
        assert result["storage_deleted"] == 1

    # -- array payloads (#736) --------------------------------------------

    @patch("app.docs.cleanup_handler.delete_named_graph")
    @patch("app.docs.cleanup_handler.delete_encrypted_file")
    def test_deletes_every_version_file_and_graph(self, mock_file, mock_graph):
        payload = {
            "draft_id": "d1",
            "storage_paths": ["/tmp/v1.enc", "/tmp/v2.enc", "/tmp/v3.enc"],
            "graph_uris": [
                "https://example.org/drafts/d1/v1",
                "https://example.org/drafts/d1/v2",
                "https://example.org/drafts/d1/v3",
            ],
        }
        result = draft_cleanup(payload)
        assert {c.args[0] for c in mock_file.call_args_list} == {
            "/tmp/v1.enc",
            "/tmp/v2.enc",
            "/tmp/v3.enc",
        }
        assert {c.args[0] for c in mock_graph.call_args_list} == {
            "https://example.org/drafts/d1/v1",
            "https://example.org/drafts/d1/v2",
            "https://example.org/drafts/d1/v3",
        }
        assert result["storage_deleted"] == 3
        assert result["graph_deleted"] == 3
        assert result["storage_total"] == 3
        assert result["graph_total"] == 3

    @patch("app.docs.cleanup_handler.delete_named_graph")
    @patch("app.docs.cleanup_handler.delete_encrypted_file")
    def test_array_and_legacy_keys_are_merged_and_deduped(self, mock_file, mock_graph):
        """A transitional payload may carry both shapes — union, no dups."""
        payload = {
            "draft_id": "d1",
            "storage_paths": ["/tmp/v1.enc", "/tmp/v2.enc"],
            "graph_uris": ["g1", "g2"],
            # legacy keys repeat the latest version — must not double-delete
            "storage_path": "/tmp/v2.enc",
            "graph_uri": "g2",
        }
        result = draft_cleanup(payload)
        assert sorted(c.args[0] for c in mock_file.call_args_list) == [
            "/tmp/v1.enc",
            "/tmp/v2.enc",
        ]
        assert sorted(c.args[0] for c in mock_graph.call_args_list) == ["g1", "g2"]
        assert result["storage_deleted"] == 2
        assert result["graph_deleted"] == 2

    @patch("app.docs.cleanup_handler.delete_named_graph")
    @patch("app.docs.cleanup_handler.delete_encrypted_file")
    def test_one_bad_path_does_not_abort_the_rest(self, mock_file, mock_graph):
        """An erroring file is logged and the siblings are still attempted.

        #845 (B3): the run now raises at the END so the worker's retry
        budget engages on the failed path — but only after every other
        path got its delete attempt (idempotent re-runs converge).
        """

        def _maybe_boom(path):
            if path == "/tmp/v2.enc":
                raise RuntimeError("disk boom")

        mock_file.side_effect = _maybe_boom
        with pytest.raises(RuntimeError, match="disk boom"):
            draft_cleanup(
                {
                    "draft_id": "d1",
                    "storage_paths": ["/tmp/v1.enc", "/tmp/v2.enc", "/tmp/v3.enc"],
                    "graph_uris": [],
                }
            )
        # v2 failing did NOT abort v3 — all three were attempted.
        assert [c.args[0] for c in mock_file.call_args_list] == [
            "/tmp/v1.enc",
            "/tmp/v2.enc",
            "/tmp/v3.enc",
        ]

    # -- failure semantics ------------------------------------------------

    @patch("app.docs.cleanup_handler.delete_named_graph")
    @patch("app.docs.cleanup_handler.delete_encrypted_file")
    def test_everything_failing_raises(self, mock_file, mock_graph):
        mock_file.side_effect = RuntimeError("disk boom")
        mock_graph.side_effect = RuntimeError("jena boom")
        with pytest.raises(RuntimeError):
            draft_cleanup(
                {
                    "draft_id": "d1",
                    "storage_paths": ["/tmp/v1.enc"],
                    "graph_uris": ["https://example.org/drafts/d1"],
                }
            )

    @patch("app.docs.cleanup_handler.delete_named_graph")
    @patch("app.docs.cleanup_handler.delete_encrypted_file")
    def test_partial_failure_raises_so_retry_budget_engages(self, mock_file, mock_graph):
        """Files deleted but Jena still failing must raise (#845 B3).

        Pre-#845 a partial success returned cleanly, which silently
        orphaned the sensitive named graph with zero retries. Re-running
        is safe (file deletes are idempotent: missing file == success),
        so the run fails and the worker retries until the bounded budget
        is exhausted — at which point the job is *visibly* failed.
        """
        mock_graph.side_effect = RuntimeError("jena boom")
        with pytest.raises(RuntimeError, match="jena boom"):
            draft_cleanup(
                {
                    "draft_id": "d1",
                    "storage_paths": ["/tmp/v1.enc"],
                    "graph_uris": ["https://example.org/drafts/d1"],
                }
            )
        # The storage delete was still attempted before the raise.
        mock_file.assert_called_once_with("/tmp/v1.enc")

    def test_empty_payload_is_a_no_op(self):
        result = draft_cleanup({"draft_id": "d1"})
        assert result["storage_deleted"] == 0
        assert result["graph_deleted"] == 0
        assert result["storage_total"] == 0
        assert result["graph_total"] == 0

    def test_explicit_empty_arrays_is_a_no_op(self):
        result = draft_cleanup(
            {"draft_id": "d1", "storage_paths": [], "graph_uris": [], "storage_path": None}
        )
        assert result["storage_deleted"] == 0
        assert result["graph_deleted"] == 0
