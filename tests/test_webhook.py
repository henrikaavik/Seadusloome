"""Tests for GitHub webhook handler."""

import hashlib
import hmac

from app.sync.webhook import verify_signature


def test_verify_signature_valid():
    secret = "test-secret"
    payload = b'{"ref": "refs/heads/main"}'
    sig = "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    assert verify_signature(payload, sig, secret) is True


def test_verify_signature_invalid():
    secret = "test-secret"
    payload = b'{"ref": "refs/heads/main"}'
    assert verify_signature(payload, "sha256=invalid", secret) is False


def test_verify_signature_empty_secret():
    payload = b'{"ref": "refs/heads/main"}'
    assert verify_signature(payload, "sha256=something", "") is False


def test_verify_signature_empty_signature():
    payload = b'{"ref": "refs/heads/main"}'
    assert verify_signature(payload, "", "secret") is False


def test_verify_signature_non_ascii_returns_false():
    """#853 / comment item 2: a non-ASCII byte in the attacker-controlled
    X-Hub-Signature-256 header must yield False, not raise TypeError
    (which previously escaped verify_signature as an unhandled 500)."""
    secret = "test-secret"
    payload = b'{"ref": "refs/heads/main"}'
    assert verify_signature(payload, "sha256=" + "ÿ" * 64, secret) is False


def test_verify_signature_high_codepoint_returns_false():
    """A code point above latin-1 (e.g. an emoji) must also fail cleanly."""
    assert verify_signature(b"{}", "sha256=\U0001f4a9", "secret") is False
