"""Profile + change-password integration tests."""

import os
import uuid

import bcrypt
import psycopg
import pytest
from starlette.testclient import TestClient


def _connect() -> psycopg.Connection:
    return psycopg.connect(os.environ.get("DATABASE_URL", ""))


@pytest.fixture
def logged_in_user_client():
    user_id = uuid.uuid4()
    email = f"prof-{user_id}@example.com"
    pw_hash = bcrypt.hashpw(b"Initial1A", bcrypt.gensalt()).decode()
    with _connect() as conn:
        conn.execute(
            "INSERT INTO users (id, email, password_hash, full_name, role) "
            "VALUES (%s, %s, %s, %s, 'drafter')",
            (user_id, email, pw_hash, "Prof User"),
        )
        conn.commit()

    from app.auth.jwt_provider import JWTAuthProvider
    from app.main import app

    provider = JWTAuthProvider()
    user = provider.authenticate(email, "Initial1A")
    assert user is not None
    access, refresh = provider.create_tokens(user)

    client = TestClient(app)
    client.cookies.set("access_token", access)
    client.cookies.set("refresh_token", refresh)

    yield {"client": client, "id": str(user_id), "email": email}

    with _connect() as conn:
        conn.execute("DELETE FROM users WHERE id = %s", (user_id,))
        conn.commit()


def test_get_profile_lists_change_password(logged_in_user_client):
    resp = logged_in_user_client["client"].get("/profile")
    assert resp.status_code == 200
    assert "Profiil" in resp.text
    assert "Vaheta parool" in resp.text


def test_get_profile_password_form(logged_in_user_client):
    resp = logged_in_user_client["client"].get("/profile/password")
    assert resp.status_code == 200
    assert "Praegune parool" in resp.text


def test_post_profile_password_wrong_current(logged_in_user_client):
    resp = logged_in_user_client["client"].post(
        "/profile/password",
        data={
            "current_password": "wrong",
            "new_password": "Brandnew1Z",
            "new_password_confirm": "Brandnew1Z",
        },
    )
    assert resp.status_code == 200
    assert "Praegune parool on vale" in resp.text


def test_post_profile_password_success(logged_in_user_client):
    resp = logged_in_user_client["client"].post(
        "/profile/password",
        data={
            "current_password": "Initial1A",
            "new_password": "Brandnew1Z",
            "new_password_confirm": "Brandnew1Z",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/auth/login"
    set_cookies = resp.headers.get_list("set-cookie")
    assert any("access_token" in sc and "Max-Age=0" in sc for sc in set_cookies)
    assert any("refresh_token" in sc and "Max-Age=0" in sc for sc in set_cookies)


def test_post_profile_password_mismatch(logged_in_user_client):
    resp = logged_in_user_client["client"].post(
        "/profile/password",
        data={
            "current_password": "Initial1A",
            "new_password": "Brandnew1Z",
            "new_password_confirm": "Different1Z",
        },
    )
    assert resp.status_code == 200
    assert "Paroolid ei kattu" in resp.text
