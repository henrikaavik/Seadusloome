"""Tests for the migration runner."""

from pathlib import Path

import pytest

from scripts.migrate import get_migration_files, validate_migration_files


def test_get_migration_files_sorted(tmp_path: Path):
    (tmp_path / "002_second.sql").write_text("SELECT 1;")
    (tmp_path / "001_first.sql").write_text("SELECT 1;")
    (tmp_path / "003_third.sql").write_text("SELECT 1;")

    files = get_migration_files(tmp_path)
    names = [f.stem for f in files]
    assert names == ["001_first", "002_second", "003_third"]


def test_get_migration_files_empty(tmp_path: Path):
    files = get_migration_files(tmp_path)
    assert files == []


def test_migration_files_exist():
    migrations_dir = Path(__file__).parent.parent / "migrations"
    files = get_migration_files(migrations_dir)
    assert len(files) >= 2
    assert files[0].stem == "001_initial"
    assert files[1].stem == "002_seed"


# ---------------------------------------------------------------------------
# validate_migration_files — runner pre-run validation unit tests
# ---------------------------------------------------------------------------


def _make_files(tmp_path: Path, stems: list[str]) -> list[Path]:
    """Write stub SQL files and return sorted Path list."""
    paths = []
    for stem in stems:
        p = tmp_path / f"{stem}.sql"
        p.write_text("SELECT 1;")
        paths.append(p)
    return sorted(paths)


def test_validate_clean_sequence_passes(tmp_path: Path):
    """A clean 001→002→003 sequence must pass without SystemExit."""
    files = _make_files(tmp_path, ["001_alpha", "002_beta", "003_gamma"])
    # Should not raise
    validate_migration_files(files)


def test_validate_new_duplicate_aborts(tmp_path: Path):
    """A NEW (non-whitelisted) duplicate prefix must cause sys.exit(1)."""
    files = _make_files(tmp_path, ["041_foo", "041_bar"])
    with pytest.raises(SystemExit) as exc_info:
        validate_migration_files(files)
    assert exc_info.value.code == 1


def test_validate_whitelisted_duplicate_warns_not_aborts(
    tmp_path: Path, capsys: pytest.CaptureFixture
):
    """The known duplicate prefix 036 must warn but not abort."""
    files = _make_files(
        tmp_path,
        [
            "001_initial",
            "036_draft_shared_notification_type",
            "036_message_tool_use_tracking",
        ],
    )
    # Must not raise
    validate_migration_files(files)
    captured = capsys.readouterr()
    assert "WARNING" in captured.out
    assert "036" in captured.out
    assert "whitelisted" in captured.out


def test_validate_whitelisted_gap_warns(tmp_path: Path, capsys: pytest.CaptureFixture):
    """Gap at 029 (whitelisted) must warn with 'whitelisted' text, not abort."""
    files = _make_files(
        tmp_path,
        ["028_foo", "030_bar"],
    )
    validate_migration_files(files)
    captured = capsys.readouterr()
    assert "WARNING" in captured.out
    assert "029" in captured.out
    assert "whitelisted" in captured.out


def test_validate_non_whitelisted_gap_warns(tmp_path: Path, capsys: pytest.CaptureFixture):
    """An unwhitelisted gap warns but does not abort."""
    files = _make_files(tmp_path, ["001_a", "003_b"])
    validate_migration_files(files)
    captured = capsys.readouterr()
    assert "WARNING" in captured.out
    assert "002" in captured.out


def test_validate_empty_list_passes(tmp_path: Path):
    """An empty file list is valid (fresh repo)."""
    validate_migration_files([])


def test_validate_real_migrations_pass():
    """The current migrations/ directory must pass runner validation
    (the 036 duplicate is whitelisted, 029 gap is whitelisted)."""
    migrations_dir = Path(__file__).parent.parent / "migrations"
    files = get_migration_files(migrations_dir)
    # Should not raise SystemExit
    validate_migration_files(files)


# ---------------------------------------------------------------------------
# Migration 019 idempotency — IF NOT EXISTS on all DDL
# ---------------------------------------------------------------------------


def test_migration_019_is_idempotent():
    """Migration 019 must use IF NOT EXISTS / ADD COLUMN IF NOT EXISTS on
    every CREATE INDEX and ADD COLUMN statement so a double-apply is safe."""
    migrations_dir = Path(__file__).parent.parent / "migrations"
    migration = migrations_dir / "019_draft_doc_type_and_vtk_lineage.sql"
    assert migration.exists()

    body = migration.read_text()
    body_lower = body.lower()

    # All ADD COLUMN statements must use IF NOT EXISTS.
    # Strip SQL comment lines before checking so that example code in comments
    # does not trigger a false positive.
    import re

    non_comment_body = "\n".join(
        line for line in body_lower.splitlines() if not line.lstrip().startswith("--")
    )
    bare_add_col = re.findall(r"add column(?!\s+if\s+not\s+exists)", non_comment_body)
    assert not bare_add_col, f"Found ADD COLUMN without IF NOT EXISTS in 019: {bare_add_col}"

    # All CREATE INDEX statements must use IF NOT EXISTS
    bare_create_idx = re.findall(r"create index(?!\s+if\s+not\s+exists)", non_comment_body)
    assert not bare_create_idx, (
        f"Found CREATE INDEX without IF NOT EXISTS in 019: {bare_create_idx}"
    )


# ---------------------------------------------------------------------------
# Migration 031 header correctness
# ---------------------------------------------------------------------------


def test_migration_031_header_says_031():
    """The 031 file's header comment must identify itself as Migration 031,
    not 029 (the original copy-paste error)."""
    migrations_dir = Path(__file__).parent.parent / "migrations"
    migration = migrations_dir / "031_annotations_extensions.sql"
    assert migration.exists()

    # Read only the first 5 lines — the header is always at the top.
    lines = migration.read_text().splitlines()
    header_block = "\n".join(lines[:5]).lower()
    assert "migration 031" in header_block, (
        f"031_annotations_extensions.sql header must say 'Migration 031', got: {header_block!r}"
    )
    assert "migration 029" not in header_block, (
        "031_annotations_extensions.sql header still says 'Migration 029' — "
        "the stale header has not been fixed."
    )


def test_migration_031_rollback_comment_says_031():
    """The ROLLBACK comment inside 031 must reference '031_annotations_extensions',
    not any other stem."""
    migrations_dir = Path(__file__).parent.parent / "migrations"
    body = (migrations_dir / "031_annotations_extensions.sql").read_text()
    assert "031_annotations_extensions" in body, (
        "ROLLBACK DELETE statement must reference '031_annotations_extensions'"
    )


# ---------------------------------------------------------------------------
# Migration 041 — missing indexes
# ---------------------------------------------------------------------------


def test_migration_041_exists():
    migrations_dir = Path(__file__).parent.parent / "migrations"
    assert (migrations_dir / "041_missing_indexes.sql").exists()


def test_migration_041_is_idempotent():
    """Every CREATE INDEX in 041 must use IF NOT EXISTS.

    Comment lines are excluded because the HNSW rebuild example in the header
    shows a bare CREATE INDEX CONCURRENTLY — that is intentional documentation,
    not a schema statement.
    """
    migrations_dir = Path(__file__).parent.parent / "migrations"
    raw = (migrations_dir / "041_missing_indexes.sql").read_text().lower()
    # Strip SQL single-line comment lines before scanning for bare CREATE INDEX.
    body = "\n".join(line for line in raw.splitlines() if not line.lstrip().startswith("--"))

    import re

    bare = re.findall(r"create index(?!\s+if\s+not\s+exists)", body)
    assert not bare, f"Found non-idempotent CREATE INDEX in 041: {bare}"


def test_migration_041_indexes_present():
    """All four required indexes must be declared in 041."""
    migrations_dir = Path(__file__).parent.parent / "migrations"
    body = (migrations_dir / "041_missing_indexes.sql").read_text().lower()

    assert "idx_draft_versions_created_by" in body
    assert "idx_pending_chat_seed_org" in body
    assert "idx_pending_chat_seed_draft" in body
    assert "idx_llm_usage_org_created" in body


def test_migration_041_llm_usage_composite_index_justified():
    """The llm_usage index must be composite (org_id, created_at) as per
    the cost-dashboard query analysis in the migration header."""
    migrations_dir = Path(__file__).parent.parent / "migrations"
    body = (migrations_dir / "041_missing_indexes.sql").read_text().lower()

    # Find the llm_usage index definition and verify it includes both columns.
    import re

    # Match the ON clause for the llm_usage index
    m = re.search(r"on llm_usage\s*\(([^)]+)\)", body)
    assert m, "Could not find ON llm_usage(...) in 041"
    cols = m.group(1)
    assert "org_id" in cols, f"Expected org_id in composite index, got: {cols}"
    assert "created_at" in cols, f"Expected created_at in composite index, got: {cols}"


def test_migration_041_pending_chat_seed_draft_is_partial():
    """The pending_chat_seed(draft_id) index must be partial (WHERE draft_id
    IS NOT NULL) because ad-hoc seeds without a draft are never joined."""
    migrations_dir = Path(__file__).parent.parent / "migrations"
    body = (migrations_dir / "041_missing_indexes.sql").read_text().lower()

    import re

    # Find the draft partial-index block
    m = re.search(
        r"idx_pending_chat_seed_draft.*?;",
        body,
        re.DOTALL,
    )
    assert m, "idx_pending_chat_seed_draft not found"
    snippet = m.group(0)
    assert "where draft_id is not null" in snippet, (
        "pending_chat_seed draft index must be partial (WHERE draft_id IS NOT NULL)"
    )


# ---------------------------------------------------------------------------
# migrate_chat_encryption.py — unconditional STORAGE_ENCRYPTION_KEY check
# ---------------------------------------------------------------------------


def test_migrate_chat_encryption_requires_key_unconditionally(monkeypatch):
    """migrate_chat_encryption.migrate() must return non-zero (or raise) when
    STORAGE_ENCRYPTION_KEY is absent, regardless of APP_ENV."""
    import importlib

    monkeypatch.delenv("STORAGE_ENCRYPTION_KEY", raising=False)
    monkeypatch.delenv("APP_ENV", raising=False)

    import scripts.migrate_chat_encryption as _mod

    # Reload to pick up monkeypatched env
    importlib.reload(_mod)

    result = _mod.migrate()
    assert result != 0, (
        "migrate_chat_encryption.migrate() must return non-zero exit code "
        "when STORAGE_ENCRYPTION_KEY is absent"
    )


# ---------------------------------------------------------------------------
# Existing regression tests (preserved from before #859)
# ---------------------------------------------------------------------------


def test_migration_032_impact_reports_version_fk_present():
    """Migration 032 (#618 PR-B) adds the ``draft_version_id`` FK on
    ``impact_reports`` so per-version diff/timeline (PR-C) can join.
    The migration must:

    * Add the column with ``IF NOT EXISTS`` (idempotent re-run).
    * Reference ``draft_versions(id) ON DELETE CASCADE``.
    * Backfill existing rows by linking to the LATEST version of their
      parent draft.
    """
    migrations_dir = Path(__file__).parent.parent / "migrations"
    migration = migrations_dir / "032_impact_reports_version_fk.sql"
    assert migration.exists(), "migration 032 must exist for #618 PR-B"

    body = migration.read_text()
    body_lower = body.lower()

    # Schema mutation (idempotent)
    assert "alter table impact_reports" in body_lower
    assert "add column if not exists draft_version_id" in body_lower
    assert "references draft_versions(id) on delete cascade" in body_lower

    # Index for the per-version lookup
    assert "create index if not exists idx_impact_reports_draft_version" in body_lower

    # Backfill: link existing reports to the latest version of their draft
    assert "update impact_reports" in body_lower
    assert "select id from draft_versions" in body_lower
    assert "order by version_number desc" in body_lower
    assert "where ir.draft_version_id is null" in body_lower


def test_migration_035_draft_reviews_present():
    """Migration 035 (#817) introduces ``draft_reviews`` with a nullable
    ``reviewer_id`` (ON DELETE SET NULL) so review records survive a
    reviewer deletion.

    The migration must:

    * Create the table with ``IF NOT EXISTS`` (idempotent re-run).
    * Cascade-delete with the parent ``drafts`` row.
    * Use ``ON DELETE SET NULL`` for the reviewer FK and keep a name
      snapshot column for UI display after deletion.
    * CHECK-constrain ``outcome`` to the three legal values.
    * Add indexes for the draft-level history query and the reviewer
      anti-join used by the dashboard widget.
    """
    migrations_dir = Path(__file__).parent.parent / "migrations"
    migration = migrations_dir / "035_draft_reviews.sql"
    assert migration.exists(), "migration 035 must exist for #817"

    body = migration.read_text()
    body_lower = body.lower()

    # Schema mutation (idempotent).
    assert "create table if not exists draft_reviews" in body_lower
    # Draft FK cascades.
    assert "references drafts(id) on delete cascade" in body_lower
    # Reviewer FK is SET NULL — preserves the row across user deletion.
    assert "references users(id) on delete set null" in body_lower
    # Snapshot column for deleted-user UI rendering.
    assert "reviewer_name_snapshot" in body_lower
    # Outcome CHECK constraint values.
    assert "check (outcome in" in body_lower
    assert "no_issue" in body_lower
    assert "issue_found" in body_lower
    assert "needs_discussion" in body_lower
    # Indexes are idempotent.
    assert "create index if not exists idx_draft_reviews_draft_id" in body_lower
    assert "create index if not exists idx_draft_reviews_reviewer" in body_lower
    # Partial index on non-null reviewer.
    assert "where reviewer_id is not null" in body_lower
