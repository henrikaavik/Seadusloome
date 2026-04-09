"""Centralised database connection helper.

Every module that needs a PostgreSQL connection should import from here
rather than duplicating the ``DATABASE_URL`` lookup and ``psycopg.connect`` call.
"""

import os

import psycopg

# Dev-only fallback. In any non-development environment a missing
# DATABASE_URL is a hard failure so we don't silently talk to a local DB.
_DEV_DATABASE_URL = "postgresql://seadusloome:localdev@localhost:5432/seadusloome"


def _load_database_url() -> str:
    """Return the DATABASE_URL, enforcing an explicit value off-dev."""
    value = os.environ.get("DATABASE_URL")
    if value:
        return value
    if os.environ.get("APP_ENV", "development") == "development":
        return _DEV_DATABASE_URL
    raise RuntimeError("DATABASE_URL must be set in non-development environments")


DATABASE_URL = _load_database_url()


def get_connection() -> psycopg.Connection:  # type: ignore[type-arg]
    """Return a new ``psycopg`` connection using the shared ``DATABASE_URL``."""
    return psycopg.connect(DATABASE_URL)
