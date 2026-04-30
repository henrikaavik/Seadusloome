"""Tests for ``app.auth.password.validate_password``.

Covers the existing length / uppercase / digit rules plus the new
email-substring rejection (spec §4.5). All error strings are asserted
in Estonian to catch any accidental English-only fallback.
"""

from __future__ import annotations

import pytest

from app.auth.password import validate_password


class TestLengthRule:
    def test_too_short_rejected(self):
        assert validate_password("Aa1") == "Parool peab olema vähemalt 8 tähemärki pikk"

    def test_seven_chars_rejected(self):
        assert validate_password("Aaaaaa1") is not None

    def test_eight_chars_accepted(self):
        # Eight chars, has upper + digit -> OK.
        assert validate_password("Aaaaaaa1") is None


class TestUppercaseRule:
    def test_no_uppercase_rejected(self):
        assert validate_password("aaaaaaa1") == "Parool peab sisaldama vähemalt ühte suurtähte"

    def test_one_uppercase_accepted(self):
        assert validate_password("aaaaaaaA1") is None


class TestDigitRule:
    def test_no_digit_rejected(self):
        assert validate_password("Aaaaaaaa") == "Parool peab sisaldama vähemalt ühte numbrit"

    def test_one_digit_accepted(self):
        assert validate_password("Aaaaaaa1") is None


class TestEmailSubstringRule:
    """The new rule introduced by the password-management spec §4.5."""

    def test_local_part_in_password_rejected(self):
        # ``henrik`` is the local part of ``henrik@example.ee``; case
        # insensitive substring match must reject this.
        assert (
            validate_password("Henrik2024", email="henrik@example.ee")
            == "Parool ei tohi sisaldada teie e-posti aadressi"
        )

    def test_local_part_case_insensitive(self):
        # Mixed case in both the password and the local part must
        # still trip the rule.
        assert (
            validate_password("HENRIKpw1A", email="Henrik@example.ee")
            == "Parool ei tohi sisaldada teie e-posti aadressi"
        )

    def test_password_without_email_substring_accepted(self):
        # Strong password unrelated to the email — accepted.
        assert validate_password("StrongPass9", email="henrik@example.ee") is None

    def test_email_kwarg_optional(self):
        # When the caller does not supply ``email`` the rule is
        # skipped — preserves backward compatibility for callers that
        # don't know the user's email yet (e.g. live validator).
        assert validate_password("Henrik2024") is None

    def test_admin_seed_credential_rejected(self):
        # Concrete regression check for the seeded admin: the plain
        # word ``admin`` from the seed migration must not be reusable
        # as a password for the same account.
        assert (
            validate_password("Admin999", email="admin@seadusloome.ee")
            == "Parool ei tohi sisaldada teie e-posti aadressi"
        )


@pytest.mark.parametrize(
    "password",
    [
        "ValidPass1",
        "Õige2parool",  # Estonian diacritics in the password are fine
        "Mõnus1ParoolA",
    ],
)
def test_acceptable_passwords(password: str):
    assert validate_password(password) is None
