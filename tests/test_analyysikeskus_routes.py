"""Tests for the Analüüsikeskus routes (#714 — #720 directory, #721 result shell).

Follows the auth-mocking pattern from ``tests/test_chat_routes.py``: the
``app.main.app`` is exercised end-to-end via ``TestClient`` so the
FastHTML wiring + the ``auth_before`` Beforeware are validated; the
DB-touching ``_get_recent_analyses`` helper is patched out.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch


def _authed_user() -> dict[str, Any]:
    return {
        "id": "33333333-3333-3333-3333-333333333333",
        "email": "kasutaja@seadusloome.ee",
        "full_name": "Test Kasutaja",
        "role": "drafter",
        "org_id": "11111111-1111-1111-1111-111111111111",
    }


def _stub_provider() -> MagicMock:
    provider = MagicMock()
    provider.get_current_user.return_value = _authed_user()
    return provider


def _authed_client():
    from starlette.testclient import TestClient

    client = TestClient(
        __import__("app.main", fromlist=["app"]).app,
        follow_redirects=False,
    )
    client.cookies.set("access_token", "stub-token")
    return client


# ---------------------------------------------------------------------------
# Unauthenticated requests redirect to login
# ---------------------------------------------------------------------------


def test_analyysikeskus_redirects_unauthenticated():
    from starlette.testclient import TestClient

    from app.main import app

    client = TestClient(app, follow_redirects=False)
    resp = client.get("/analyysikeskus")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/auth/login"


def test_workflow_routes_redirect_unauthenticated():
    from starlette.testclient import TestClient

    from app.main import app

    client = TestClient(app, follow_redirects=False)
    for path in ("/analyysikeskus/normi-mojuahel", "/analyysikeskus/el-ulevott"):
        resp = client.get(path)
        assert resp.status_code == 303, path
        assert resp.headers["location"] == "/auth/login", path


# ---------------------------------------------------------------------------
# #720 — directory page
# ---------------------------------------------------------------------------


@patch("app.analyysikeskus.routes._get_recent_analyses", return_value=[])
@patch("app.auth.middleware._get_provider")
def test_analyysikeskus_directory_renders(mock_provider: MagicMock, mock_recent: MagicMock):
    mock_provider.return_value = _stub_provider()
    client = _authed_client()
    resp = client.get("/analyysikeskus")
    assert resp.status_code == 200
    body = resp.text
    assert "Analüüsikeskus" in body
    # Both in-scope workflow titles.
    assert "Normi mõjuahel" in body
    assert "EL ülevõtt" in body
    # The primary action button on each workflow card.
    assert "Alusta analüüsi" in body
    # The recent-analyses section + its empty state.
    assert "Hiljutised analüüsid" in body
    assert "Veel pole analüüse." in body
    # Sidebar marks the new nav item active.
    assert 'aria-current="page"' in body


# ---------------------------------------------------------------------------
# #721 / #722 — Normi mõjuahel stub result shell
# ---------------------------------------------------------------------------


@patch("app.analyysikeskus.routes._get_recent_analyses", return_value=[])
@patch("app.auth.middleware._get_provider")
def test_normi_mojuahel_stub_renders_result_shell(
    mock_provider: MagicMock, mock_recent: MagicMock
):
    mock_provider.return_value = _stub_provider()
    client = _authed_client()
    # "AvTS § 35" url-encoded.
    resp = client.get("/analyysikeskus/normi-mojuahel?sisend=AvTS+%C2%A7+35")
    assert resp.status_code == 200
    body = resp.text
    assert "Normi mõjuahel" in body
    # All five Core-UI-Pattern block headings, in order.
    for heading in ("Sisend", "Ulatus", "Tulemused", "Tõendid", "Soovitatud tegevused"):
        assert heading in body, heading
    # The echoed input.
    assert "AvTS § 35" in body
    # The "Tulemused" block carries the koostamisel/tulekul placeholder.
    assert "koostamisel" in body or "tulekul" in body
    # The static action set.
    assert "Küsi nõustajalt" in body
    assert "/chat/new" in body
    # Back-link near the top.
    assert "← Analüüsikeskus" in body


def test_normi_mojuahel_blank_input_redirects():
    with patch("app.auth.middleware._get_provider") as mock_provider:
        mock_provider.return_value = _stub_provider()
        client = _authed_client()
        resp = client.get("/analyysikeskus/normi-mojuahel")
        assert resp.status_code == 303
        assert resp.headers["location"] == "/analyysikeskus"


# ---------------------------------------------------------------------------
# #721 / #723 — EL ülevõtt stub result shell
# ---------------------------------------------------------------------------


@patch("app.analyysikeskus.routes._get_recent_analyses", return_value=[])
@patch("app.auth.middleware._get_provider")
def test_el_ulevott_stub_renders(mock_provider: MagicMock, mock_recent: MagicMock):
    mock_provider.return_value = _stub_provider()
    client = _authed_client()
    resp = client.get("/analyysikeskus/el-ulevott?sisend=32016R0679")
    assert resp.status_code == 200
    body = resp.text
    assert "EL ülevõtt" in body
    for heading in ("Sisend", "Ulatus", "Tulemused", "Tõendid", "Soovitatud tegevused"):
        assert heading in body, heading
    assert "32016R0679" in body


# ---------------------------------------------------------------------------
# Result-shell _block_body: empty list → fallback, non-empty list → wrapped
# ---------------------------------------------------------------------------


def test_result_shell_empty_list_block_renders_fallback():
    from fasthtml.common import P, to_xml

    from app.analyysikeskus.result_shell import analysis_result_shell

    page = analysis_result_shell(
        workflow_title="Normi mõjuahel",
        input_summary=P("Sisestasite: «AvTS § 35»"),
        results_block=[],  # an empty findings list must NOT render as "[]"
        evidence_block=[],
        actions=[{"label": "Tagasi", "href": "/analyysikeskus"}],
        user={"id": "u-1", "email": "u@x.ee", "full_name": "U", "role": "drafter", "org_id": None},
    )
    html = to_xml(page)
    assert "Tulemusi ei leitud." in html
    assert "Tõendeid ei leitud." in html
    assert "[]" not in html


def test_result_shell_nonempty_list_block_renders_all_items():
    from fasthtml.common import P, to_xml

    from app.analyysikeskus.result_shell import analysis_result_shell

    page = analysis_result_shell(
        workflow_title="Normi mõjuahel",
        input_summary=P("Sisestasite: «AvTS § 35»"),
        results_block=[P("Leid üks"), P("Leid kaks")],
        evidence_block=P("Tõend"),
        actions=[{"label": "Tagasi", "href": "/analyysikeskus"}],
        user={"id": "u-1", "email": "u@x.ee", "full_name": "U", "role": "drafter", "org_id": None},
    )
    html = to_xml(page)
    assert "Leid üks" in html
    assert "Leid kaks" in html
    assert "Tulemusi ei leitud." not in html
