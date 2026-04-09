"""Centralised database connection helper.

Every module that needs a PostgreSQL connection should import from here
rather than duplicating the ``DATABASE_URL`` lookup and ``psycopg.connect`` call.
"""

import os

import psycopg

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://seadusloome:localdev@localhost:5432/seadusloome",
)


def get_connection() -> psycopg.Connection:  # type: ignore[type-arg]
    """Return a new ``psycopg`` connection using the shared ``DATABASE_URL``."""
    return psycopg.connect(DATABASE_URL)
