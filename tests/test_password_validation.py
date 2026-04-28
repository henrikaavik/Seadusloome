"""validate_password tests — existing rules + email-substring extension."""

from app.auth.password import validate_password


def test_existing_rules_still_pass():
    assert validate_password("Abcdef12") is None


def test_short_password_rejected():
    assert "8 tähemärki" in (validate_password("Ab1") or "")


def test_no_uppercase_rejected():
    assert "suurtähte" in (validate_password("abcdef12") or "")


def test_no_digit_rejected():
    assert "numbrit" in (validate_password("Abcdefgh") or "")


def test_email_substring_rejected():
    assert "e-posti" in (validate_password("Henrik123", email="henrik@example.com") or "")


def test_email_substring_check_is_case_insensitive():
    assert "e-posti" in (validate_password("HENRIK123", email="henrik@example.com") or "")


def test_email_substring_optional():
    # No email arg → no substring check.
    assert validate_password("Henrik123") is None


def test_email_with_no_at_treated_as_full_localpart():
    # Defensive: callers should always pass a real email, but if they don't,
    # use the full string as the local-part.
    assert "e-posti" in (validate_password("Henrik123", email="henrik") or "")
