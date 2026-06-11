"""SECRET_KEY strength enforcement tests (#857).

``_load_secret_key`` must refuse any explicitly configured signing
secret under 32 UTF-8 bytes (RFC 7518 §3.2 floor for HS256) in EVERY
environment — startup fails closed with a clear error instead of
issuing brute-forceable JWTs. The function reads the environment at
call time, so no module reloads are needed here; ``app.main`` import
behaviour under production env is covered by the reload-based tests in
``tests/test_prod_middleware.py`` / ``tests/test_security_headers.py``.
"""

from __future__ import annotations

import pytest

from app.auth.jwt_provider import (
    _DEV_SECRET_KEY,
    MIN_SECRET_KEY_BYTES,
    _load_secret_key,
)


class TestMinimumLength:
    def test_floor_is_32_bytes(self):
        """The RFC 7518 §3.2 number itself is load-bearing — pin it."""
        assert MIN_SECRET_KEY_BYTES == 32

    @pytest.mark.parametrize("env", ["development", "production", "staging"])
    def test_short_key_refused_in_every_env(self, monkeypatch: pytest.MonkeyPatch, env: str):
        monkeypatch.setenv("APP_ENV", env)
        monkeypatch.setenv("SECRET_KEY", "short-key")
        with pytest.raises(RuntimeError, match="too short"):
            _load_secret_key()

    def test_31_bytes_refused(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("SECRET_KEY", "x" * 31)
        with pytest.raises(RuntimeError, match="31 bytes"):
            _load_secret_key()

    def test_32_ascii_bytes_accepted(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("SECRET_KEY", "x" * 32)
        assert _load_secret_key() == "x" * 32

    def test_length_is_measured_in_utf8_bytes_not_chars(self, monkeypatch: pytest.MonkeyPatch):
        """16 Estonian 'õ' characters are 32 UTF-8 bytes — accepted."""
        value = "õ" * 16
        assert len(value) == 16 and len(value.encode("utf-8")) == 32
        monkeypatch.setenv("SECRET_KEY", value)
        assert _load_secret_key() == value

    def test_error_message_points_at_a_generator(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("SECRET_KEY", "tiny")
        with pytest.raises(RuntimeError, match="token_urlsafe"):
            _load_secret_key()


class TestUnsetFallbacks:
    """Pre-#857 behaviour for the UNSET case is unchanged."""

    def test_unset_in_dev_returns_dev_fallback(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("SECRET_KEY", raising=False)
        monkeypatch.setenv("APP_ENV", "development")
        assert _load_secret_key() == _DEV_SECRET_KEY
        # The fallback itself must satisfy the floor it enforces on others.
        assert len(_DEV_SECRET_KEY.encode("utf-8")) >= MIN_SECRET_KEY_BYTES

    def test_unset_in_production_refuses_startup(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("SECRET_KEY", raising=False)
        monkeypatch.setenv("APP_ENV", "production")
        with pytest.raises(RuntimeError, match="must be set"):
            _load_secret_key()
