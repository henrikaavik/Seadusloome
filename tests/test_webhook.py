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
