"""Tests for the migration runner."""

from pathlib import Path

from scripts.migrate import get_migration_files


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
