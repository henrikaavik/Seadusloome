"""Pure Python form field validators. Return error message or None.

Validators are registered in a global registry by name. The live validation
endpoint (`/api/validate/{field_name}`) looks up validators from this
registry so any form can use HTMX-driven on-blur validation.

Error messages are in Estonian.
"""

from __future__ import annotations

import re
from collections.abc import Callable


class ValidationError(Exception):
    """Raised by validators for exceptional cases (not normal errors)."""


Validator = Callable[[str], str | None]

# ---------------------------------------------------------------------------
# Built-in validators
# ---------------------------------------------------------------------------

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_URL_RE = re.compile(r"^https?://[^\s]+$")


def validate_required(value: str) -> str | None:
    """Require a non-empty value."""
    if not value or not value.strip():
        return "See väli on kohustuslik"
    return None


def validate_email(value: str) -> str | None:
    """Validate a minimally-reasonable email address."""
    if not value:
        return None  # use validate_required if required
    if not _EMAIL_RE.match(value):
        return "Sisestage kehtiv e-posti aadress"
    if len(value) > 254:
        return "E-posti aadress on liiga pikk"
    return None


def validate_url(value: str) -> str | None:
    """Validate an http(s) URL."""
    if not value:
        return None
    if not _URL_RE.match(value):
        return "Sisestage kehtiv URL (http:// või https://)"
    return None


def validate_password_strength(value: str) -> str | None:
    """Minimum 8 chars, one uppercase, one digit."""
    if not value:
        return None
    if len(value) < 8:
        return "Parool peab olema vähemalt 8 tähemärki"
    if not any(c.isupper() for c in value):
        return "Parool peab sisaldama vähemalt ühte suurtähte"
    if not any(c.isdigit() for c in value):
        return "Parool peab sisaldama vähemalt ühte numbrit"
    return None


def validate_min_length(min_length: int) -> Validator:
    """Factory: return a validator enforcing a minimum length."""

    def _validator(value: str) -> str | None:
        if value and len(value) < min_length:
            return f"Peab olema vähemalt {min_length} tähemärki"
        return None

    return _validator


def validate_max_length(max_length: int) -> Validator:
    """Factory: return a validator enforcing a maximum length."""

    def _validator(value: str) -> str | None:
        if value and len(value) > max_length:
            return f"Ei tohi olla pikem kui {max_length} tähemärki"
        return None

    return _validator


# ---------------------------------------------------------------------------
# Registry for the live validation endpoint
# ---------------------------------------------------------------------------

_REGISTRY: dict[str, Validator] = {
    "email": validate_email,
    "url": validate_url,
    "password": validate_password_strength,
    "required": validate_required,
}


def register_validator(name: str, validator: Validator) -> None:
    """Register a validator under a name so routes can use it via HTMX."""
    _REGISTRY[name] = validator


def get_validator(name: str) -> Validator | None:
    """Look up a registered validator by name."""
    return _REGISTRY.get(name)
