"""HMAC-signed expiring URLs for impact-report downloads (issue #307).

Plain session-auth gating ties a download link to the user's browser
session: the moment the user copies the URL into another tab, shares it
with a colleague, or hands it to a curl call from a build script, the
link becomes either useless (no session cookie) or a backdoor that lives
as long as the session does. Short-lived signed URLs flip that picture:
the URL itself encodes the authorisation, so it can be safely shared
inside a 1-hour window and is dead after.

The token format is intentionally JWT-flavoured but home-grown so we
don't pull in the `jwt` dependency at the download layer (the auth
module already does, but keeping the report module dependency-free
keeps the surface tight):

    <base64url(payload_json)>.<base64url(hmac_sha256)>

Payload fields:

    draft_id : str  — the impact-report draft this token authorises
    user_id  : str  — who minted the token (audit trail)
    exp      : int  — unix timestamp; rejected when expired
    nonce    : str  — 16 random bytes hex; prevents accidental dedupe

Validation is strict: payload must round-trip cleanly through JSON, the
HMAC must be byte-identical (constant-time compare), the token must not
be expired, and the embedded ``draft_id`` must match the expected one
the route handler is serving. Any mismatch returns ``None`` so handlers
can collapse every failure mode into a single 403 response without
leaking which check failed.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from typing import Any

from app.config import is_stub_allowed

logger = logging.getLogger(__name__)


# Env var name carrying the HMAC key. Operators set this alongside the
# other secrets (SECRET_KEY, STORAGE_ENCRYPTION_KEY) in production.
_DOWNLOAD_TOKEN_SECRET_ENV = "DOWNLOAD_TOKEN_SECRET"

# Dev sentinel — see ``_load_signing_key`` for the production fallback
# rules. Never used in production: the loader raises before reaching it.
_DEV_SIGNING_KEY = b"dev-only-download-token-secret-do-not-use-in-production"

# Default lifetime per the issue spec — 1 hour. Callers may override
# with a shorter TTL for one-shot links if that ever becomes useful.
DEFAULT_TOKEN_TTL_SECONDS = 3600


def _load_signing_key() -> bytes:
    """Return the HMAC signing key, enforcing an explicit value off-dev.

    Resolution order:

    1. ``DOWNLOAD_TOKEN_SECRET`` env var if set — always preferred.
    2. ``SECRET_KEY`` env var if set — re-use the JWT signing key so a
       single rotation rotates both. This is a deliberate convenience
       for operators who don't want to manage one more secret in their
       Coolify env editor.
    3. Dev sentinel — only outside production (mirrors the
       :mod:`app.docs.reference_resolver` pattern). Raises in production
       so a misconfigured deploy can't silently sign tokens with a
       well-known value.
    """
    explicit = os.environ.get(_DOWNLOAD_TOKEN_SECRET_ENV)
    if explicit:
        return explicit.encode("utf-8")
    jwt_secret = os.environ.get("SECRET_KEY")
    if jwt_secret:
        return jwt_secret.encode("utf-8")
    if not is_stub_allowed():  # production
        raise RuntimeError(
            f"{_DOWNLOAD_TOKEN_SECRET_ENV} (or SECRET_KEY) must be set in production "
            "to sign report-download URLs."
        )
    return _DEV_SIGNING_KEY


def _b64url_encode(data: bytes) -> str:
    """Encode *data* as base64url WITHOUT padding (=, /, + are URL-hostile).

    The trailing ``=`` padding chars are stripped because some web
    frameworks/browsers treat them as significant inside query strings;
    the decoder re-pads at parse time so the round-trip is exact.
    """
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    """Decode a base64url payload that may be missing padding.

    Raises ``ValueError`` (or anything ``binascii`` raises wrapped as
    such) on malformed input — callers must treat that as an invalid
    token. Pad length is computed from the input length mod 4.
    """
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def make_download_token(
    draft_id: str,
    user_id: str,
    ttl_seconds: int = DEFAULT_TOKEN_TTL_SECONDS,
) -> str:
    """Mint a short-lived HMAC-signed download token for *draft_id*.

    Parameters
    ----------
    draft_id:
        The impact-report draft id the token authorises. Must match
        exactly when the download endpoint validates.
    user_id:
        The UUID of the user minting the token. Carried in the payload
        so the audit log records who originally requested the link
        (even if the URL is later shared and used by someone else).
    ttl_seconds:
        Token lifetime in seconds. Defaults to 1 hour per the issue.

    Returns the token as ``<payload_b64>.<sig_b64>``. Callers embed it
    in the download URL as ``?token=...``.
    """
    payload: dict[str, Any] = {
        "draft_id": str(draft_id),
        "user_id": str(user_id),
        "exp": int(time.time()) + int(ttl_seconds),
        # 16 random bytes → 32 hex chars; nonce makes two tokens minted
        # in the same second distinct (handy for invalidating one
        # leaked link without rotating the whole secret).
        "nonce": secrets.token_hex(16),
    }
    payload_bytes = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    sig = hmac.new(_load_signing_key(), payload_bytes, hashlib.sha256).digest()
    return f"{_b64url_encode(payload_bytes)}.{_b64url_encode(sig)}"


def validate_download_token(token: str, expected_draft_id: str) -> dict[str, Any] | None:
    """Verify *token* and return its payload, or ``None`` on any failure.

    Failure modes (all collapse to ``None``):

    * Malformed shape (missing ``.`` separator, non-base64url chars).
    * Tampered HMAC (constant-time compare via
      :func:`hmac.compare_digest`).
    * Expired ``exp`` claim.
    * Payload ``draft_id`` does not match ``expected_draft_id``.
    * Payload missing required fields.
    * JSON decode failure.

    Returning ``None`` for every error case lets handlers collapse all
    of them into a single 403 response, which means an attacker cannot
    distinguish "expired" from "wrong draft" from "tampered" via the
    response — every invalid token looks the same from outside.
    """
    if not token or not isinstance(token, str):
        return None
    parts = token.split(".")
    if len(parts) != 2:
        return None
    payload_b64, sig_b64 = parts
    try:
        payload_bytes = _b64url_decode(payload_b64)
        sig_bytes = _b64url_decode(sig_b64)
    except (ValueError, TypeError):
        return None
    if not payload_bytes or not sig_bytes:
        return None

    # Constant-time HMAC compare — never short-circuit on a mismatched
    # prefix because that leaks the matching-byte count to a timing
    # attacker (CWE-208).
    expected_sig = hmac.new(_load_signing_key(), payload_bytes, hashlib.sha256).digest()
    if not hmac.compare_digest(sig_bytes, expected_sig):
        return None

    try:
        payload = json.loads(payload_bytes.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None

    # Required claim check — missing fields are treated as tampered.
    draft_id = payload.get("draft_id")
    user_id = payload.get("user_id")
    exp = payload.get("exp")
    if not isinstance(draft_id, str) or not isinstance(user_id, str):
        return None
    if not isinstance(exp, int):
        return None

    if str(draft_id) != str(expected_draft_id):
        return None
    if int(time.time()) >= exp:
        return None
    return payload
