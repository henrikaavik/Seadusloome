"""Tests for FormField, validators, and live validation endpoint."""

from starlette.testclient import TestClient

from app.main import app
from app.ui.forms.form_field import FormField, FormSelectField, FormTextareaField
from app.ui.forms.validators import (
    get_validator,
    register_validator,
    validate_email,
    validate_max_length,
    validate_min_length,
    validate_password_strength,
    validate_required,
    validate_url,
)

# ---------------------------------------------------------------------------
# Validators
# ---------------------------------------------------------------------------


def test_validate_required_empty():
    assert validate_required("") is not None
    assert validate_required("   ") is not None


def test_validate_required_ok():
    assert validate_required("hello") is None


def test_validate_email_ok():
    assert validate_email("user@example.ee") is None
    assert validate_email("") is None  # empty handled by required


def test_validate_email_bad():
    assert validate_email("not-an-email") is not None
    assert validate_email("missing@tld") is not None
    assert validate_email("@nouser.ee") is not None


def test_validate_email_too_long():
    long = "a" * 250 + "@b.ee"
    assert validate_email(long) is not None


def test_validate_url_ok():
    assert validate_url("https://example.ee") is None
    assert validate_url("http://example.ee/path") is None
    assert validate_url("") is None


def test_validate_url_bad():
    assert validate_url("ftp://example.ee") is not None
    assert validate_url("not a url") is not None


def test_validate_password_too_short():
    assert validate_password_strength("Short1") is not None


def test_validate_password_no_upper():
    assert validate_password_strength("lowercase1") is not None


def test_validate_password_no_digit():
    assert validate_password_strength("NoDigitsHere") is not None


def test_validate_password_ok():
    assert validate_password_strength("GoodPass1") is None


def test_validate_min_length():
    v = validate_min_length(5)
    assert v("ab") is not None
    assert v("abcde") is None
    assert v("") is None  # empty handled by required


def test_validate_max_length():
    v = validate_max_length(3)
    assert v("abcd") is not None
    assert v("abc") is None


def test_registry_lookup():
    assert get_validator("email") is validate_email
    assert get_validator("password") is validate_password_strength
    assert get_validator("nonexistent") is None


def test_registry_register():
    def custom(v):
        return "nope" if v == "bad" else None

    register_validator("custom", custom)
    assert get_validator("custom") is custom


# ---------------------------------------------------------------------------
# FormField component
# ---------------------------------------------------------------------------


def test_form_field_basic():
    field = FormField(name="email", label="E-post", type="email")
    html = str(field)
    assert 'name="email"' in html
    assert "E-post" in html
    assert 'type="email"' in html
    assert 'for="field-email"' in html
    assert 'id="field-email"' in html


def test_form_field_required_marker():
    field = FormField(name="pw", label="Parool", type="password", required=True)
    html = str(field)
    assert "form-field-required" in html
    assert "required" in html


def test_form_field_error_state():
    field = FormField(name="email", label="E-post", error="Vale formaat")
    html = str(field)
    assert "Vale formaat" in html
    assert "form-field--error" in html
    assert 'role="alert"' in html


def test_form_field_help_text():
    field = FormField(name="email", label="E-post", help="Teie tööpost")
    html = str(field)
    assert "Teie tööpost" in html
    assert 'id="email-help"' in html


def test_form_field_htmx_validator():
    field = FormField(name="email", label="E-post", validator="email")
    html = str(field)
    assert "/api/validate/email" in html
    assert 'hx-target="#email-error"' in html


def test_form_field_empty_error_is_hidden_from_a11y():
    """No error and no validator -> the placeholder div must be hidden (#421)."""
    field = FormField(name="email", label="E-post")
    html = str(field)
    # The placeholder div is still in the DOM, but hidden from AT.
    assert 'id="email-error"' in html
    error_div_chunk = html.split('id="email-error"', 1)[0].rsplit("<div", 1)[1]
    assert "hidden" in error_div_chunk
    # And the input must NOT advertise the error id via aria-describedby
    # at all when there is no error and no validator wired.
    assert "aria-describedby" not in html


def test_form_field_with_validator_keeps_error_visible_to_a11y():
    """A live validator means HTMX may stream an error in -- keep visible."""
    field = FormField(name="email", label="E-post", validator="email")
    html = str(field)
    # The placeholder div is in the DOM and NOT hidden so HTMX swaps work.
    assert 'id="email-error"' in html
    error_div = html.split('id="email-error"', 1)[1].split(">", 1)[0]
    assert "hidden" not in error_div


def test_form_textarea_field():
    field = FormTextareaField(name="bio", label="Bio", rows=5)
    html = str(field)
    assert "<textarea" in html
    assert "Bio" in html


def test_form_select_field():
    field = FormSelectField(
        name="role",
        label="Roll",
        options=[("drafter", "Koostaja"), ("admin", "Admin")],
    )
    html = str(field)
    assert "<select" in html
    assert "Koostaja" in html
    assert "Admin" in html


# ---------------------------------------------------------------------------
# Live validation endpoint
# ---------------------------------------------------------------------------


def test_live_validation_email_valid():
    from bs4 import BeautifulSoup
    from bs4.element import Tag

    client = TestClient(app)
    resp = client.post("/api/validate/email", data={"email": "user@example.ee"})
    assert resp.status_code == 200
    soup = BeautifulSoup(resp.text, "html.parser")
    error_div = soup.find(id="email-error")
    assert isinstance(error_div, Tag)
    classes = error_div.get("class") or []
    assert "form-field-error" in classes
    # Empty error placeholder for the success branch.
    assert error_div.get_text(strip=True) == ""
    assert error_div.get("role") is None


def test_live_validation_email_invalid():
    from bs4 import BeautifulSoup
    from bs4.element import Tag

    client = TestClient(app)
    resp = client.post("/api/validate/email", data={"email": "bad"})
    assert resp.status_code == 200
    soup = BeautifulSoup(resp.text, "html.parser")
    error_div = soup.find(id="email-error")
    assert isinstance(error_div, Tag)
    assert error_div.get("role") == "alert"
    assert "kehtiv" in error_div.get_text()


def test_live_validation_password_short():
    from bs4 import BeautifulSoup
    from bs4.element import Tag

    client = TestClient(app)
    resp = client.post("/api/validate/password", data={"password": "short"})
    assert resp.status_code == 200
    soup = BeautifulSoup(resp.text, "html.parser")
    error_div = soup.find(id="password-error")
    assert isinstance(error_div, Tag)
    assert "8 tähemärki" in error_div.get_text()


def test_live_validation_unknown_validator():
    client = TestClient(app)
    resp = client.post("/api/validate/unknown", data={"unknown": "x"})
    # Unknown validator -> 404 so form typos surface immediately.
    assert resp.status_code == 404
    assert "Unknown validator" in resp.text
