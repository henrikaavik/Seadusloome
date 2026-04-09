"""Simple numbered-file migration runner for PostgreSQL."""

import os
import sys
from pathlib import Path

import psycopg


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

    database_url = get_database_url()
    print("Connecting to database...")

    with psycopg.connect(database_url) as conn:
        ensure_migrations_table(conn)
        applied = get_applied_migrations(conn)
        files = get_migration_files(migrations_dir)

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
