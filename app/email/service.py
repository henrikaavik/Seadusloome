"""Lazy singleton selecting the active EmailProvider per env config."""

from __future__ import annotations

import os
import threading

from app.config import is_stub_allowed
from app.email.provider import EmailProvider
from app.email.stub_provider import StubProvider

_provider: EmailProvider | None = None
_lock = threading.Lock()

_DEFAULT_FROM = "Seadusloome <noreply@sixtyfour.ee>"


def get_email_provider() -> EmailProvider:
    """Return the active provider per env config.

    Selection rule (mirrors ``app/llm/claude.py``):

    - dev/test/staging (``APP_ENV != production``) without ``POSTMARK_API_TOKEN`` → StubProvider
    - dev/test/staging with ``POSTMARK_API_TOKEN`` → real PostmarkProvider (lets staging
      exercise the wire)
    - production without ``POSTMARK_API_TOKEN`` → ``RuntimeError`` so deployment fails loudly
    - production with ``POSTMARK_API_TOKEN`` → real PostmarkProvider
    """
    global _provider
    if _provider is not None:
        return _provider

    with _lock:
        if _provider is not None:
            return _provider

        token = os.environ.get("POSTMARK_API_TOKEN", "").strip()
        from_addr = os.environ.get("EMAIL_FROM", "").strip() or _DEFAULT_FROM

        if not token:
            if is_stub_allowed():
                _provider = StubProvider()
                return _provider
            raise RuntimeError(
                "POSTMARK_API_TOKEN must be set in production "
                "(APP_ENV=production). Refusing to silently fall back to stub."
            )

        # Imported lazily so envs without postmarker installed can still use the stub.
        from app.email.postmark_provider import PostmarkProvider

        _provider = PostmarkProvider(api_token=token, default_from=from_addr)
        return _provider


def _reset_provider_for_tests() -> None:
    global _provider
    _provider = None
