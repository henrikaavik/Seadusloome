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
