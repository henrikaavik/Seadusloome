"""Tests for the ``draft_cleanup`` background job handler (#628, #736).

The handler is deliberately tolerant of partial failure: we keep the
user-visible delete fast by moving external cleanups (encrypted file +
Jena graph purge) off-line, and we only re-raise when EVERYTHING fails
(work was attempted, nothing was cleaned) so a persistently-missing
Jena graph doesn't loop us forever once the files are already gone.

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
        """A missing/erroring file is logged and skipped; siblings still go."""

        def _maybe_boom(path):
            if path == "/tmp/v2.enc":
                raise RuntimeError("disk boom")

        mock_file.side_effect = _maybe_boom
        result = draft_cleanup(
            {
                "draft_id": "d1",
                "storage_paths": ["/tmp/v1.enc", "/tmp/v2.enc", "/tmp/v3.enc"],
                "graph_uris": [],
            }
        )
        # v1 + v3 succeeded, v2 failed — but no exception bubbled up
        # because there was *some* progress.
        assert result["storage_deleted"] == 2
        assert result["storage_total"] == 3

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
    def test_partial_failure_does_not_raise(self, mock_file, mock_graph):
        """Files deleted but Jena still failing — don't loop forever."""
        mock_graph.side_effect = RuntimeError("jena boom")
        result = draft_cleanup(
            {
                "draft_id": "d1",
                "storage_paths": ["/tmp/v1.enc"],
                "graph_uris": ["https://example.org/drafts/d1"],
            }
        )
        assert result["storage_deleted"] == 1
        assert result["graph_deleted"] == 0

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
