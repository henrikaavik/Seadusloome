"""Tests for the ``draft_cleanup`` background job handler (#628).

The handler is deliberately tolerant of partial failure: we keep the
user-visible delete fast by moving external cleanups (encrypted file +
Jena graph purge) off-line, and we only re-raise when BOTH steps
fail so a persistently-missing Jena graph doesn't loop us forever on
an already-deleted file.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from app.docs.cleanup_handler import draft_cleanup


class TestDraftCleanupHandler:
    @patch("app.docs.cleanup_handler.delete_named_graph")
    @patch("app.docs.cleanup_handler.delete_encrypted_file")
    def test_deletes_file_and_graph(self, mock_file, mock_graph):
        payload = {
            "draft_id": "d1",
            "storage_path": "/tmp/cipher.enc",
            "graph_uri": "https://example.org/drafts/d1",
        }
        result = draft_cleanup(payload)
        mock_file.assert_called_once_with("/tmp/cipher.enc")
        mock_graph.assert_called_once_with("https://example.org/drafts/d1")
        assert result["storage_deleted"] is True
        assert result["graph_deleted"] is True

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
        assert result["storage_deleted"] is True

    @patch("app.docs.cleanup_handler.delete_named_graph")
    @patch("app.docs.cleanup_handler.delete_encrypted_file")
    def test_both_failing_raises(self, mock_file, mock_graph):
        mock_file.side_effect = RuntimeError("disk boom")
        mock_graph.side_effect = RuntimeError("jena boom")
        with pytest.raises(RuntimeError):
            draft_cleanup(
                {
                    "draft_id": "d1",
                    "storage_path": "/tmp/cipher.enc",
                    "graph_uri": "https://example.org/drafts/d1",
                }
            )

    @patch("app.docs.cleanup_handler.delete_named_graph")
    @patch("app.docs.cleanup_handler.delete_encrypted_file")
    def test_partial_failure_does_not_raise(self, mock_file, mock_graph):
        """File deleted but Jena still failing — don't loop forever."""
        mock_graph.side_effect = RuntimeError("jena boom")
        result = draft_cleanup(
            {
                "draft_id": "d1",
                "storage_path": "/tmp/cipher.enc",
                "graph_uri": "https://example.org/drafts/d1",
            }
        )
        assert result["storage_deleted"] is True
        assert result["graph_deleted"] is False

    def test_missing_storage_path_is_no_op(self):
        result = draft_cleanup({"draft_id": "d1", "storage_path": None, "graph_uri": None})
        assert result["storage_deleted"] is True
        assert result["graph_deleted"] is True
