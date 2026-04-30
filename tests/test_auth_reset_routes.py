"""Reset-password route tests."""

import os
import uuid

import bcrypt
import psycopg
import pytest
from starlette.testclient import TestClient

from app.auth.password import issue_reset_token


def _connect() -> psycopg.Connection:
    return psycopg.connect(os.environ.get("DATABASE_URL", ""))


@pytest.fixture
def real_user_with_token():
    user_id = uuid.uuid4()
    email = f"reset-{user_id}@example.com"
    pw_hash = bcrypt.hashpw(b"Initial1A", bcrypt.gensalt()).decode()
    with _connect() as conn:
        conn.execute(
            "INSERT INTO users (id, email, password_hash, full_name, role) "
            "VALUES (%s, %s, %s, %s, 'drafter')",
            (user_id, email, pw_hash, "Reset User"),
        )
        conn.commit()
        token = issue_reset_token(user_id=user_id, created_by=None, conn=conn)
        conn.commit()
    yield {"id": str(user_id), "email": email, "token": token}
    with _connect() as conn:
        conn.execute("DELETE FROM users WHERE id = %s", (user_id,))
        conn.commit()


@pytest.fixture
def client():
    from app.main import app

    return TestClient(app)


def test_get_reset_page_renders_for_valid_token(client, real_user_with_token):
    resp = client.get(f"/auth/reset/{real_user_with_token['token']}")
    assert resp.status_code == 200
    assert "Määra uus parool" in resp.text


def test_get_reset_page_invalid_token(client):
    resp = client.get("/auth/reset/notarealtoken")
    assert resp.status_code == 200
    assert "aegunud või vigane" in resp.text


def test_post_reset_success_clears_cookies_and_changes_password(client, real_user_with_token):
    resp = client.post(
        f"/auth/reset/{real_user_with_token['token']}",
        data={"new_password": "Brandnew1Z", "new_password_confirm": "Brandnew1Z"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/auth/login"
    set_cookies = resp.headers.get_list("set-cookie")
    assert any("access_token" in sc and "Max-Age=0" in sc for sc in set_cookies)
    assert any("refresh_token" in sc and "Max-Age=0" in sc for sc in set_cookies)
    with _connect() as conn:
        new_hash = conn.execute(
            "SELECT password_hash FROM users WHERE id = %s",
            (real_user_with_token["id"],),
        ).fetchone()[0]
    assert bcrypt.checkpw(b"Brandnew1Z", new_hash.encode())


def test_post_reset_password_mismatch(client, real_user_with_token):
    resp = client.post(
        f"/auth/reset/{real_user_with_token['token']}",
        data={"new_password": "Brandnew1Z", "new_password_confirm": "Different1Z"},
    )
    assert resp.status_code == 200
    assert "Paroolid ei kattu" in resp.text


def test_post_reset_password_validation_error(client, real_user_with_token):
    resp = client.post(
        f"/auth/reset/{real_user_with_token['token']}",
        data={"new_password": "short", "new_password_confirm": "short"},
    )
    assert resp.status_code == 200
    assert "8 tähemärki" in resp.text


def test_post_reset_used_token_rejected(client, real_user_with_token):
    client.post(
        f"/auth/reset/{real_user_with_token['token']}",
        data={"new_password": "Brandnew1Z", "new_password_confirm": "Brandnew1Z"},
    )
    resp = client.post(
        f"/auth/reset/{real_user_with_token['token']}",
        data={"new_password": "Anothernw1Z", "new_password_confirm": "Anothernw1Z"},
    )
    assert resp.status_code == 200
    assert "aegunud või vigane" in resp.text
