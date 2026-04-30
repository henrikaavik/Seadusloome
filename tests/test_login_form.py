"""Smoke tests for the login form's authentication-related markup.

The login page must render with the right ``autocomplete`` hints so
browsers can offer to fill saved credentials, and submit-side native
validation must be disabled in favour of the inline Estonian validator
(via ``novalidate`` on the form). These behaviours are easy to break
silently when ``FormField`` kwargs leak to the wrong element, so we
assert them explicitly against the rendered HTML.
"""

from __future__ import annotations

from bs4 import BeautifulSoup
from bs4.element import Tag
from starlette.testclient import TestClient

from app.main import app


def test_login_inputs_have_autocomplete_attributes():
    """Email + password inputs must advertise the right autocomplete tokens."""
    client = TestClient(app, follow_redirects=False)
    resp = client.get("/auth/login")
    assert resp.status_code == 200

    soup = BeautifulSoup(resp.text, "html.parser")

    email_input = soup.find("input", attrs={"name": "email"})
    assert isinstance(email_input, Tag), "email input not found in rendered login form"
    assert email_input.get("autocomplete") == "email"

    password_input = soup.find("input", attrs={"name": "password"})
    assert isinstance(password_input, Tag), "password input not found in rendered login form"
    assert password_input.get("autocomplete") == "current-password"


def test_login_form_disables_native_validation():
    """``novalidate`` lets the inline Estonian validator be the only message
    the user sees on submit, instead of Chrome's English bubble."""
    client = TestClient(app, follow_redirects=False)
    resp = client.get("/auth/login")
    assert resp.status_code == 200

    soup = BeautifulSoup(resp.text, "html.parser")
    form = soup.find("form", attrs={"action": "/auth/login"})
    assert isinstance(form, Tag), "login form not found in rendered page"
    # ``novalidate`` is a boolean HTML attribute; presence is what matters.
    assert form.has_attr("novalidate")


def test_login_html_contains_autocomplete_strings():
    """Belt-and-suspenders raw-string check, in case the parser ever drifts."""
    client = TestClient(app, follow_redirects=False)
    resp = client.get("/auth/login")
    assert resp.status_code == 200
    assert 'autocomplete="email"' in resp.text
    assert 'autocomplete="current-password"' in resp.text
