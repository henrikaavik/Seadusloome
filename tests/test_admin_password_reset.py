"""Admin reset password tests — system and org variants."""

import base64
import json
import os
import uuid

import bcrypt
import psycopg
import pytest
from starlette.testclient import TestClient


def _decode_session_cookie(client: TestClient) -> dict:
    """Decode the Starlette session cookie payload (base64 JSON, signed).

    The cookie value is ``b64(json).timestamp.signature`` — the payload is
    readable by ANYONE holding the cookie (it is signed, not encrypted),
    which is exactly why #857 forbids credentials inside it.
    """
    raw = client.cookies.get("session_")
    if not raw:
        return {}
    payload = raw.split(".")[0]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))


def _connect() -> psycopg.Connection:
    return psycopg.connect(os.environ.get("DATABASE_URL", ""))


@pytest.fixture
def org_with_users():
    if not os.getenv("DATABASE_URL"):
        pytest.skip("integration test — DATABASE_URL not set")
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
        row = conn.execute(
            "SELECT COUNT(*) FROM password_reset_tokens WHERE user_id = %s AND created_by = %s",
            (target, sa),
        ).fetchone()
        assert row is not None
        n = row[0]
    assert n == 1


def test_org_admin_can_reset_own_org_drafter(org_with_users):
    oa = org_with_users["orgadmin_id"]
    oa_email = f"oa-{oa}@example.com"
    target = org_with_users["drafter_id"]
    c = _client_as(oa, oa_email)
    resp = c.post(f"/org/users/{target}/reset_email", follow_redirects=False)
    assert resp.status_code == 303
    with _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM password_reset_tokens WHERE user_id = %s AND created_by = %s",
            (target, oa),
        ).fetchone()
        assert row is not None
        n = row[0]
    assert n == 1


def test_org_admin_cannot_reset_another_org_admin(org_with_users):
    oa = org_with_users["orgadmin_id"]
    oa_email = f"oa-{oa}@example.com"
    target = org_with_users["other_org_admin_id"]
    c = _client_as(oa, oa_email)
    resp = c.post(f"/org/users/{target}/reset_email", follow_redirects=False)
    assert resp.status_code in (200, 303, 403)
    with _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM password_reset_tokens WHERE user_id = %s",
            (target,),
        ).fetchone()
        assert row is not None
        n = row[0]
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
    # #740: the live temp credential must NOT travel in the redirect URL
    # (history / proxy logs / referrers). It is handed to the reveal page
    # via the signed session cookie instead.
    assert "Tempnew1Z" not in resp.headers["location"]
    assert "temp=" not in resp.headers["location"]

    with _connect() as conn:
        row = conn.execute(
            "SELECT must_change_password, password_hash FROM users WHERE id = %s",
            (target,),
        ).fetchone()
    assert row is not None
    must_change, pw_hash = row
    assert must_change is True
    assert bcrypt.checkpw(b"Tempnew1Z", pw_hash.encode())


def test_temp_password_revealed_exactly_once_on_reset_page(org_with_users):
    """#740 — after setting a temp password the admin sees it once on the
    reset page (sourced from the session, not the URL), and a second view
    of the same page no longer shows it. The reveal response is also
    marked ``Cache-Control: no-store``.
    """
    sa = org_with_users["sysadmin_id"]
    sa_email = f"sa-{sa}@example.com"
    target = org_with_users["drafter_id"]
    c = _client_as(sa, sa_email)

    # 1. Set the temp password — the session now holds the one-time reveal.
    set_resp = c.post(
        f"/admin/users/{target}/reset_temp",
        data={"new_password": "Tempnew1Z"},
        follow_redirects=False,
    )
    assert set_resp.status_code == 303
    reveal_path = set_resp.headers["location"]
    assert reveal_path.endswith(f"/admin/users/{target}/reset")

    # 2. First load of the reset page reveals the credential exactly once.
    first = c.get(reveal_path)
    assert first.status_code == 200
    assert "Tempnew1Z" in first.text
    assert first.headers.get("cache-control") == "no-store"

    # 3. Second load no longer reveals it (session value was consumed).
    second = c.get(reveal_path)
    assert second.status_code == 200
    assert "Tempnew1Z" not in second.text


def test_org_admin_temp_password_reveal_uses_session_not_url(org_with_users):
    """#740 — the org-admin variant of the reset flow has the same
    no-credential-in-URL guarantee.
    """
    oa = org_with_users["orgadmin_id"]
    oa_email = f"oa-{oa}@example.com"
    target = org_with_users["drafter_id"]
    c = _client_as(oa, oa_email)
    resp = c.post(
        f"/org/users/{target}/reset_temp",
        data={"new_password": "Tempnew1Z"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"].endswith(f"/org/users/{target}/reset")
    assert "Tempnew1Z" not in resp.headers["location"]

    page = c.get(resp.headers["location"])
    assert page.status_code == 200
    assert "Tempnew1Z" in page.text
    assert page.headers.get("cache-control") == "no-store"


def test_temp_password_never_appears_in_session_cookie(org_with_users):
    """#857 — the session cookie is signed but NOT encrypted, so the live
    credential must never enter its payload. The cookie may carry only an
    opaque single-use token referencing the server-side stash.
    """
    sa = org_with_users["sysadmin_id"]
    sa_email = f"sa-{sa}@example.com"
    target = org_with_users["drafter_id"]
    c = _client_as(sa, sa_email)

    set_resp = c.post(
        f"/admin/users/{target}/reset_temp",
        data={"new_password": "Tempnew1Z"},
        follow_redirects=False,
    )
    assert set_resp.status_code == 303

    # Decode the cookie payload exactly like an attacker with cookie access
    # would: the password must not be there, the opaque reference must be.
    session_payload = _decode_session_cookie(c)
    assert "Tempnew1Z" not in json.dumps(session_payload)
    reveal_ref = session_payload.get("pw_reset_reveal")
    assert isinstance(reveal_ref, dict)
    assert reveal_ref.get("user_id") == str(target)
    assert isinstance(reveal_ref.get("token"), str) and reveal_ref["token"]
    assert "password" not in reveal_ref

    # The reveal flow still works end-to-end off that reference…
    page = c.get(set_resp.headers["location"])
    assert "Tempnew1Z" in page.text
    # …and afterwards no trace of the credential remains anywhere client-side.
    assert "Tempnew1Z" not in json.dumps(_decode_session_cookie(c))


def test_admin_created_user_forced_to_change_password_on_first_login(org_with_users):
    """#857 — accounts provisioned via POST /admin/users carry
    ``must_change_password=TRUE``; the first login bounces to
    ``/profile/password`` before any other page is reachable.
    """
    sa = org_with_users["sysadmin_id"]
    sa_email = f"sa-{sa}@example.com"
    c = _client_as(sa, sa_email)

    new_email = f"uus-{uuid.uuid4()}@example.com"
    try:
        created = c.post(
            "/admin/users",
            data={
                "email": new_email,
                "password": "Algne1Parool",
                "full_name": "Uus Ametnik",
                "role": "drafter",
                "org_id": org_with_users["org_id"],
            },
            follow_redirects=False,
        )
        assert created.status_code == 303

        with _connect() as conn:
            row = conn.execute(
                "SELECT must_change_password FROM users WHERE email = %s",
                (new_email,),
            ).fetchone()
        assert row is not None and row[0] is True

        # First login: authenticate works, but navigation is forced to the
        # password-change page by the must-change middleware.
        from app.auth.jwt_provider import JWTAuthProvider
        from app.main import app

        p = JWTAuthProvider()
        user = p.authenticate(new_email, "Algne1Parool")
        assert user is not None
        assert user["must_change_password"] is True

        access, refresh = p.create_tokens(user)
        nc = TestClient(app)
        nc.cookies.set("access_token", access)
        nc.cookies.set("refresh_token", refresh)
        resp = nc.get("/dashboard", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/profile/password"
    finally:
        with _connect() as conn:
            conn.execute("DELETE FROM users WHERE email = %s", (new_email,))
            conn.commit()


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
        row = conn.execute(
            "SELECT must_change_password FROM users WHERE id = %s",
            (target,),
        ).fetchone()
        assert row is not None
        must_change = row[0]
    assert must_change is False
