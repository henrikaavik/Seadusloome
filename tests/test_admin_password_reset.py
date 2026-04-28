"""Admin reset password tests — system and org variants."""

import os
import uuid

import bcrypt
import psycopg
import pytest
from starlette.testclient import TestClient


def _connect() -> psycopg.Connection:
    return psycopg.connect(os.environ.get("DATABASE_URL", ""))


@pytest.fixture
def org_with_users():
    org_id = uuid.uuid4()
    sysadmin_id = uuid.uuid4()
    orgadmin_id = uuid.uuid4()
    drafter_id = uuid.uuid4()
    other_org_admin_id = uuid.uuid4()

    pw = bcrypt.hashpw(b"Initial1A", bcrypt.gensalt()).decode()

    with _connect() as conn:
        conn.execute(
            "INSERT INTO organizations (id, name, slug) VALUES (%s, %s, %s)",
            (org_id, f"OrgX-{org_id}", f"orgx-{org_id}"),
        )
        conn.execute(
            "INSERT INTO users (id, email, password_hash, full_name, role, org_id) VALUES "
            "(%s, %s, %s, 'SysAdmin', 'admin', NULL)",
            (sysadmin_id, f"sa-{sysadmin_id}@example.com", pw),
        )
        conn.execute(
            "INSERT INTO users (id, email, password_hash, full_name, role, org_id) VALUES "
            "(%s, %s, %s, 'OrgAdmin', 'org_admin', %s)",
            (orgadmin_id, f"oa-{orgadmin_id}@example.com", pw, org_id),
        )
        conn.execute(
            "INSERT INTO users (id, email, password_hash, full_name, role, org_id) VALUES "
            "(%s, %s, %s, 'Drafter', 'drafter', %s)",
            (drafter_id, f"dr-{drafter_id}@example.com", pw, org_id),
        )
        conn.execute(
            "INSERT INTO users (id, email, password_hash, full_name, role, org_id) VALUES "
            "(%s, %s, %s, 'OtherOrgAdmin', 'org_admin', %s)",
            (other_org_admin_id, f"ooa-{other_org_admin_id}@example.com", pw, org_id),
        )
        conn.commit()

    yield {
        "org_id": str(org_id),
        "sysadmin_id": str(sysadmin_id),
        "orgadmin_id": str(orgadmin_id),
        "drafter_id": str(drafter_id),
        "other_org_admin_id": str(other_org_admin_id),
    }

    with _connect() as conn:
        for uid in (sysadmin_id, orgadmin_id, drafter_id, other_org_admin_id):
            conn.execute("DELETE FROM users WHERE id = %s", (uid,))
        conn.execute("DELETE FROM organizations WHERE id = %s", (org_id,))
        conn.commit()


def _client_as(user_id: str, email: str) -> TestClient:
    from app.auth.jwt_provider import JWTAuthProvider
    from app.main import app

    p = JWTAuthProvider()
    user = p.authenticate(email, "Initial1A")
    assert user is not None, f"Failed to auth as {email}"
    access, refresh = p.create_tokens(user)
    c = TestClient(app)
    c.cookies.set("access_token", access)
    c.cookies.set("refresh_token", refresh)
    return c


def test_system_admin_can_reset_drafter(org_with_users):
    sa = org_with_users["sysadmin_id"]
    sa_email = f"sa-{sa}@example.com"
    target = org_with_users["drafter_id"]
    c = _client_as(sa, sa_email)
    resp = c.post(f"/admin/users/{target}/reset_email", follow_redirects=False)
    assert resp.status_code == 303
    with _connect() as conn:
        n = conn.execute(
            "SELECT COUNT(*) FROM password_reset_tokens WHERE user_id = %s AND created_by = %s",
            (target, sa),
        ).fetchone()[0]
    assert n == 1


def test_org_admin_can_reset_own_org_drafter(org_with_users):
    oa = org_with_users["orgadmin_id"]
    oa_email = f"oa-{oa}@example.com"
    target = org_with_users["drafter_id"]
    c = _client_as(oa, oa_email)
    resp = c.post(f"/org/users/{target}/reset_email", follow_redirects=False)
    assert resp.status_code == 303
    with _connect() as conn:
        n = conn.execute(
            "SELECT COUNT(*) FROM password_reset_tokens WHERE user_id = %s AND created_by = %s",
            (target, oa),
        ).fetchone()[0]
    assert n == 1


def test_org_admin_cannot_reset_another_org_admin(org_with_users):
    oa = org_with_users["orgadmin_id"]
    oa_email = f"oa-{oa}@example.com"
    target = org_with_users["other_org_admin_id"]
    c = _client_as(oa, oa_email)
    resp = c.post(f"/org/users/{target}/reset_email", follow_redirects=False)
    assert resp.status_code in (200, 303, 403)
    with _connect() as conn:
        n = conn.execute(
            "SELECT COUNT(*) FROM password_reset_tokens WHERE user_id = %s",
            (target,),
        ).fetchone()[0]
    assert n == 0


def test_system_admin_temp_password_sets_must_change(org_with_users):
    sa = org_with_users["sysadmin_id"]
    sa_email = f"sa-{sa}@example.com"
    target = org_with_users["drafter_id"]
    c = _client_as(sa, sa_email)
    resp = c.post(
        f"/admin/users/{target}/reset_temp",
        data={"new_password": "Tempnew1Z"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "/reset" in resp.headers["location"]
    assert "temp=Tempnew1Z" in resp.headers["location"]

    with _connect() as conn:
        row = conn.execute(
            "SELECT must_change_password, password_hash FROM users WHERE id = %s",
            (target,),
        ).fetchone()
    must_change, pw_hash = row
    assert must_change is True
    assert bcrypt.checkpw(b"Tempnew1Z", pw_hash.encode())


def test_org_admin_cannot_temp_password_org_admin(org_with_users):
    oa = org_with_users["orgadmin_id"]
    oa_email = f"oa-{oa}@example.com"
    target = org_with_users["other_org_admin_id"]
    c = _client_as(oa, oa_email)
    resp = c.post(
        f"/org/users/{target}/reset_temp",
        data={"new_password": "Tempnew1Z"},
        follow_redirects=False,
    )
    assert resp.status_code in (200, 303, 403)
    with _connect() as conn:
        must_change = conn.execute(
            "SELECT must_change_password FROM users WHERE id = %s",
            (target,),
        ).fetchone()[0]
    assert must_change is False
