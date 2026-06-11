"""Migration 040 creates the login_attempts throttling table (#851 D1)."""

from __future__ import annotations

import os

import pytest

from app.db import get_connection


def test_login_attempts_table_exists():
    if not os.getenv("DATABASE_URL"):
        pytest.skip("integration test — DATABASE_URL not set")
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'login_attempts' ORDER BY column_name"
        )
        cols = {r[0] for r in cur.fetchall()}
    assert {"id", "email_hash", "ip", "attempted_at"} <= cols


def test_login_attempts_indexes_exist():
    if not os.getenv("DATABASE_URL"):
        pytest.skip("integration test — DATABASE_URL not set")
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT indexname FROM pg_indexes WHERE tablename = 'login_attempts'")
        indexes = {r[0] for r in cur.fetchall()}
    assert "idx_login_attempts_email_hash_time" in indexes
    assert "idx_login_attempts_ip_time" in indexes
