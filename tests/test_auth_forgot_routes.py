"""Forgot-password route tests."""

import os
import uuid

import bcrypt
import psycopg
import pytest
from starlette.testclient import TestClient

from app.email.service import _reset_provider_for_tests


def _connect() -> psycopg.Connection:
    return psycopg.connect(os.environ.get("DATABASE_URL", ""))


@pytest.fixture(autouse=True)
def _stub_email(monkeypatch):
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.delenv("POSTMARK_API_TOKEN", raising=False)
    _reset_provider_for_tests()
    yield
    _reset_provider_for_tests()


@pytest.fixture
def real_user():
    user_id = uuid.uuid4()
    email = f"forgot-{user_id}@example.com"
    pw_hash = bcrypt.hashpw(b"Initial1A", bcrypt.gensalt()).decode()
    with _connect() as conn:
        conn.execute(
            "INSERT INTO users (id, email, password_hash, full_name, role) "
            "VALUES (%s, %s, %s, %s, 'drafter')",
            (user_id, email, pw_hash, "Forgot User"),
        )
        conn.commit()
    yield {"id": str(user_id), "email": email}
    with _connect() as conn:
        conn.execute("DELETE FROM users WHERE id = %s", (user_id,))
        conn.execute(
            "DELETE FROM password_reset_attempts WHERE email_hash IN ("
            "  SELECT encode(digest(lower(%s), 'sha256'), 'hex')"
            ")",
            (email,),
        )
        conn.commit()


@pytest.fixture
def client():
    from app.main import app

    return TestClient(app)


def test_get_forgot_page_renders(client):
    resp = client.get("/auth/forgot")
    assert resp.status_code == 200
    assert "Parooli lähtestamine" in resp.text


def test_post_forgot_unknown_email_renders_generic_success(client):
    resp = client.post("/auth/forgot", data={"email": "nobody@example.com"})
    assert resp.status_code == 200
    assert "Kui see e-post on registreeritud" in resp.text


def test_post_forgot_known_email_creates_token_and_logs_email(client, real_user, caplog):
    import logging

    with caplog.at_level(logging.INFO, logger="app.email.stub_provider"):
        resp = client.post("/auth/forgot", data={"email": real_user["email"]})
    assert resp.status_code == 200
    assert any("/auth/reset/" in r.message for r in caplog.records)
    with _connect() as conn:
        n = conn.execute(
            "SELECT COUNT(*) FROM password_reset_tokens WHERE user_id = %s",
            (real_user["id"],),
        ).fetchone()[0]
    assert n == 1


def test_post_forgot_records_attempt_for_unknown_email(client):
    """Unknown emails still record an attempt row — used by rate limiter."""
    resp = client.post("/auth/forgot", data={"email": "rate-limit@example.com"})
    assert resp.status_code == 200
    with _connect() as conn:
        n = conn.execute(
            "SELECT COUNT(*) FROM password_reset_attempts WHERE email_hash = "
            "encode(digest(lower(%s), 'sha256'), 'hex')",
            ("rate-limit@example.com",),
        ).fetchone()[0]
    assert n == 1
