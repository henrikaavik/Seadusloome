"""Simple numbered-file migration runner for PostgreSQL.

Pre-run validation (run before any DB connection is made):

- Duplicate numeric prefix detection: two files with the same leading number
  are an error UNLESS they are whitelisted as historical anomalies.  Any NEW
  duplicate (prefix > 040) causes an immediate abort.
- Contiguity check: a missing number in the sequence is warned, not aborted,
  because migration 029 was intentionally skipped (031 landed before 029 was
  ever written; 029 is a permanent gap in history).
- The known duplicate pair 036_draft_shared_notification_type /
  036_message_tool_use_tracking is whitelisted as a historical artefact.
  Both files are applied in alphabetical (stem) order on first run; the runner
  records BOTH stems in schema_migrations.  Renaming them would break already-
  applied environments where both stems are already in the table.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import psycopg

# ---------------------------------------------------------------------------
# Historical whitelist — gaps and duplicates that existed before migration 041
# and must NOT be flagged as errors.
# ---------------------------------------------------------------------------

# Numeric prefixes that are intentionally absent from the sequence.
# 029 was never written; 031 landed first and the number was skipped.
_WHITELISTED_GAPS: frozenset[int] = frozenset({29})

# Numeric prefixes where exactly two files share the same number.
# The pair 036_draft_shared_notification_type + 036_message_tool_use_tracking
# was merged simultaneously before this runner gained duplicate detection.
# Both have been applied to production; renaming either would cause attempted
# re-application on any environment that already has both stems recorded.
_WHITELISTED_DUPLICATE_PREFIXES: frozenset[int] = frozenset({36})


def _extract_prefix(stem: str) -> int | None:
    """Return the leading integer prefix from a migration stem, or None."""
    m = re.match(r"^(\d+)_", stem)
    if m:
        return int(m.group(1))
    return None


def validate_migration_files(files: list[Path]) -> None:
    """Validate numeric-prefix uniqueness and sequence contiguity.

    Raises ``SystemExit`` on any NEW (non-whitelisted) duplicate prefix.
    Emits warnings for known-whitelisted issues and non-whitelisted gaps.
    """
    prefix_map: dict[int, list[str]] = {}
    for f in files:
        p = _extract_prefix(f.stem)
        if p is None:
            continue
        prefix_map.setdefault(p, []).append(f.stem)

    # --- Duplicate detection ---
    for prefix, stems in sorted(prefix_map.items()):
        if len(stems) <= 1:
            continue
        if prefix in _WHITELISTED_DUPLICATE_PREFIXES:
            print(
                f"  WARNING: duplicate prefix {prefix:03d} — "
                f"{', '.join(sorted(stems))} — "
                "whitelisted historical artefact; both stems will be applied "
                "in alphabetical order."
            )
        else:
            print(
                f"  ERROR: duplicate numeric prefix {prefix:03d} — "
                f"{', '.join(sorted(stems))} — "
                "rename one file before running migrations."
            )
            sys.exit(1)

    # --- Contiguity check (warn only) ---
    if not prefix_map:
        return
    min_prefix = min(prefix_map)
    max_prefix = max(prefix_map)
    for n in range(min_prefix, max_prefix + 1):
        if n in prefix_map:
            continue
        if n in _WHITELISTED_GAPS:
            print(
                f"  WARNING: gap at prefix {n:03d} — "
                "whitelisted: 029 was intentionally skipped in history."
            )
        else:
            print(
                f"  WARNING: gap at prefix {n:03d} — "
                "no migration file for this number. "
                "Verify this is intentional."
            )


def get_database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        url = "postgresql://seadusloome:localdev@localhost:5432/seadusloome"
    return url


def ensure_migrations_table(conn: psycopg.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version TEXT PRIMARY KEY,
            applied_at TIMESTAMPTZ DEFAULT now()
        )
    """)
    conn.commit()


def get_applied_migrations(conn: psycopg.Connection) -> set[str]:
    rows = conn.execute("SELECT version FROM schema_migrations").fetchall()
    return {row[0] for row in rows}


def get_migration_files(migrations_dir: Path) -> list[Path]:
    files = sorted(migrations_dir.glob("*.sql"))
    return files


def apply_migration(conn: psycopg.Connection, path: Path) -> None:
    version = path.stem
    sql = path.read_text()
    print(f"  Applying {version}...")
    conn.execute(sql)  # type: ignore[arg-type]
    conn.execute(
        "INSERT INTO schema_migrations (version) VALUES (%s)",
        (version,),  # type: ignore[arg-type]
    )
    conn.commit()
    print(f"  Applied {version}")


def migrate() -> None:
    migrations_dir = Path(__file__).parent.parent / "migrations"
    if not migrations_dir.exists():
        print("No migrations directory found.")
        sys.exit(1)

    files = get_migration_files(migrations_dir)

    print("Validating migration file sequence...")
    validate_migration_files(files)
    print("Validation passed.")

    database_url = get_database_url()
    print("Connecting to database...")

    with psycopg.connect(database_url) as conn:
        ensure_migrations_table(conn)
        applied = get_applied_migrations(conn)

        pending = [f for f in files if f.stem not in applied]

        if not pending:
            print("All migrations already applied.")
            return

        print(f"Found {len(pending)} pending migration(s):")
        for path in pending:
            apply_migration(conn, path)

        print("All migrations applied successfully.")


if __name__ == "__main__":
    migrate()
