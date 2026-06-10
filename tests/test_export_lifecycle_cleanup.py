"""#845 (B2 + B3) — draft_cleanup export purge + Jena-failure semantics.

B2: deleting a draft must also remove its rendered export artifacts
(``<draft_id>-<report_id>.docx`` / ``.pdf`` / ``-summary.docx``) from
EXPORT_DIR — they are plaintext derivatives of the encrypted draft.

B3: ``delete_named_graph`` reports failure as a ``False`` return (it
only raises on programmer error); pre-#845 the handler counted that as
success, silently orphaning sensitive named graphs with zero retries.
Any failure now raises so the worker's bounded retry budget engages.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.docs.cleanup_handler import draft_cleanup

_DRAFT_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
_OTHER_DRAFT_ID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
_REPORT_ID = "cccccccc-cccc-cccc-cccc-cccccccccccc"


@pytest.fixture
def export_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("EXPORT_DIR", str(tmp_path))
    return tmp_path


class TestExportArtifactPurge:
    @patch("app.docs.cleanup_handler.delete_named_graph", return_value=True)
    @patch("app.docs.cleanup_handler.delete_encrypted_file")
    def test_draft_cleanup_removes_all_export_variants(
        self, mock_file: MagicMock, mock_graph: MagicMock, export_dir: Path
    ):
        """Every rendered artifact for the draft dies; other drafts' stay."""
        mine = [
            export_dir / f"{_DRAFT_ID}-{_REPORT_ID}.docx",
            export_dir / f"{_DRAFT_ID}-{_REPORT_ID}.pdf",
            export_dir / f"{_DRAFT_ID}-{_REPORT_ID}-summary.docx",
        ]
        other = export_dir / f"{_OTHER_DRAFT_ID}-{_REPORT_ID}.docx"
        for path in [*mine, other]:
            path.write_bytes(b"PK fake docx")

        result = draft_cleanup({"draft_id": _DRAFT_ID})

        for path in mine:
            assert not path.exists(), f"{path.name} survived draft deletion"
        assert other.exists(), "another draft's export was wrongly deleted"
        assert result["exports_deleted"] == 3
        assert result["exports_total"] == 3

    @patch("app.docs.cleanup_handler.delete_named_graph", return_value=True)
    @patch("app.docs.cleanup_handler.delete_encrypted_file")
    def test_non_uuid_draft_id_never_widens_the_glob(
        self, mock_file: MagicMock, mock_graph: MagicMock, export_dir: Path
    ):
        """A malformed draft_id must not be interpolated into a glob."""
        stray = export_dir / "d1-something.docx"
        stray.write_bytes(b"x")

        result = draft_cleanup({"draft_id": "d1"})

        assert stray.exists()
        assert result["exports_total"] == 0
        assert result["exports_deleted"] == 0

    @patch("app.docs.cleanup_handler.delete_named_graph", return_value=True)
    @patch("app.docs.cleanup_handler.delete_encrypted_file")
    def test_matching_directory_is_ignored(
        self, mock_file: MagicMock, mock_graph: MagicMock, export_dir: Path
    ):
        """Only files are unlinked — a pattern-matching directory survives."""
        subdir = export_dir / f"{_DRAFT_ID}-weird-dir"
        subdir.mkdir()

        result = draft_cleanup({"draft_id": _DRAFT_ID})

        assert subdir.is_dir()
        assert result["exports_total"] == 0

    @patch("app.docs.cleanup_handler.delete_named_graph", return_value=True)
    @patch("app.docs.cleanup_handler.delete_encrypted_file")
    def test_export_unlink_failure_raises_for_retry(
        self, mock_file: MagicMock, mock_graph: MagicMock, export_dir: Path
    ):
        broken = MagicMock()
        broken.unlink.side_effect = OSError("disk boom")
        with patch(
            "app.docs.cleanup_handler._export_artifacts_for_draft",
            return_value=[broken],
        ):
            with pytest.raises(RuntimeError, match="disk boom"):
                draft_cleanup({"draft_id": _DRAFT_ID})


class TestJenaFalseIsAnError:
    @patch("app.docs.cleanup_handler.delete_named_graph", return_value=False)
    @patch("app.docs.cleanup_handler.delete_encrypted_file")
    def test_false_return_raises_so_retry_budget_engages(
        self, mock_file: MagicMock, mock_graph: MagicMock, export_dir: Path
    ):
        """Storage succeeded but Jena reported failure → the run fails.

        Pre-#845 this combination returned success and the politically
        sensitive named graph stayed orphaned in Fuseki forever.
        """
        with pytest.raises(RuntimeError, match="returned False"):
            draft_cleanup(
                {
                    "draft_id": _DRAFT_ID,
                    "storage_paths": ["/tmp/v1.enc"],
                    "graph_uris": [f"https://example.org/drafts/{_DRAFT_ID}"],
                }
            )
        # The file delete was still attempted (idempotent on retry).
        mock_file.assert_called_once_with("/tmp/v1.enc")
        mock_graph.assert_called_once()

    @patch("app.docs.cleanup_handler.delete_named_graph", return_value=False)
    @patch("app.docs.cleanup_handler.delete_encrypted_file")
    def test_false_does_not_count_toward_graph_deleted(
        self, mock_file: MagicMock, mock_graph: MagicMock, export_dir: Path
    ):
        with pytest.raises(RuntimeError) as excinfo:
            draft_cleanup(
                {
                    "draft_id": _DRAFT_ID,
                    "graph_uris": ["https://example.org/g1", "https://example.org/g2"],
                }
            )
        # Both graphs were attempted and BOTH failures are reported, so
        # the retry/error message names every orphan candidate.
        assert "g1" in str(excinfo.value)
        assert "g2" in str(excinfo.value)

    @patch("app.docs.cleanup_handler.delete_named_graph", return_value=True)
    @patch("app.docs.cleanup_handler.delete_encrypted_file")
    def test_true_return_still_counts_success(
        self, mock_file: MagicMock, mock_graph: MagicMock, export_dir: Path
    ):
        result = draft_cleanup(
            {
                "draft_id": _DRAFT_ID,
                "storage_paths": ["/tmp/v1.enc"],
                "graph_uris": [f"https://example.org/drafts/{_DRAFT_ID}"],
            }
        )
        assert result["graph_deleted"] == 1
        assert result["storage_deleted"] == 1


class TestExportGlobIsUuidSafe:
    def test_export_artifacts_helper_rejects_garbage(self, export_dir: Path):
        from app.docs.cleanup_handler import _export_artifacts_for_draft

        (export_dir / "x-1.docx").write_bytes(b"x")
        assert _export_artifacts_for_draft("../../*") == []
        assert _export_artifacts_for_draft("") == []
        assert _export_artifacts_for_draft("not-a-uuid") == []

    def test_export_artifacts_helper_finds_uuid_prefixed_files(self, export_dir: Path):
        from app.docs.cleanup_handler import _export_artifacts_for_draft

        target = export_dir / f"{_DRAFT_ID}-{_REPORT_ID}.pdf"
        target.write_bytes(b"%PDF")
        found = _export_artifacts_for_draft(_DRAFT_ID)
        assert found == [target]
        # And the canonical-form parse means case variants still match
        # the same files (UUIDs are case-insensitive).
        assert _export_artifacts_for_draft(_DRAFT_ID.upper()) == [target]

    def test_helper_accepts_uuid_instances_via_str(self, export_dir: Path):
        from app.docs.cleanup_handler import _export_artifacts_for_draft

        target = export_dir / f"{_DRAFT_ID}-{_REPORT_ID}.docx"
        target.write_bytes(b"PK")
        assert _export_artifacts_for_draft(str(uuid.UUID(_DRAFT_ID))) == [target]
