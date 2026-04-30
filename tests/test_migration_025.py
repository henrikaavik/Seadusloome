"""Migration 025 creates password_reset_tokens + password_reset_attempts tables."""

from __future__ import annotations

import os

import pytest

from app.db import get_connection


@pytest.mark.integration
def test_password_reset_tokens_table_exists():
    if not os.getenv("DATABASE_URL"):
        pytest.skip("integration test — DATABASE_URL not set")
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'password_reset_tokens' ORDER BY column_name"
        )
        cols = {r[0] for r in cur.fetchall()}
    assert {
        "id",
        "user_id",
        "token_hash",
        "expires_at",
        "used_at",
        "created_by",
        "created_at",
    } <= cols


@pytest.mark.integration
def test_password_reset_attempts_table_exists():
    if not os.getenv("DATABASE_URL"):
        pytest.skip("integration test — DATABASE_URL not set")
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'password_reset_attempts' ORDER BY column_name"
        )
        cols = {r[0] for r in cur.fetchall()}
    assert {"id", "email_hash", "ip", "attempted_at"} <= cols
