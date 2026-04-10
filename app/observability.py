"""Sentry integration and PII scrubbing for error tracking.

Initializes the Sentry SDK with the ``SENTRY_DSN`` environment variable.
When the DSN is unset (local dev), initialization is skipped silently.

Call ``init_sentry()`` once at application startup — before ``fast_app()``
creates the ASGI app — so that the Starlette integration can wrap the
app and capture unhandled exceptions automatically.
"""

from __future__ import annotations

import logging
import os
import subprocess
from typing import Any

logger = logging.getLogger(__name__)


def _get_git_sha() -> str:
    """Return a short Git commit hash for Sentry release tagging.

    Reads ``GIT_SHA`` from the environment first (set by CI/CD or
    Dockerfile ``ARG``).  Falls back to ``git rev-parse --short HEAD``
    for local development.  Returns ``"unknown"`` when neither works.
    """
    sha = os.environ.get("GIT_SHA")
    if sha:
        return sha
    try:
        return (
            subprocess.check_output(  # noqa: S603, S607
                ["git", "rev-parse", "--short", "HEAD"],
                stderr=subprocess.DEVNULL,
            )
            .decode()
            .strip()
        )
    except Exception:
        return "unknown"


def _scrub_pii(event: dict[str, Any], hint: dict[str, Any]) -> dict[str, Any] | None:
    """Remove PII (emails, names) from Sentry events before transmission.

    Strips ``user`` context entirely and redacts any ``email`` or
    ``full_name`` values found in breadcrumb data and exception frame
    local variables.
    """
    # Remove top-level user context so emails/names never reach Sentry.
    event.pop("user", None)

    # Scrub breadcrumb data values.
    for breadcrumb in event.get("breadcrumbs", {}).get("values", []):
        data = breadcrumb.get("data")
        if isinstance(data, dict):
            for key in list(data.keys()):
                if key in ("email", "full_name", "password", "token"):
                    data[key] = "[Redacted]"

    # Scrub exception frame local variables.
    exception_info = event.get("exception")
    if exception_info:
        for exc_value in exception_info.get("values", []):
            stacktrace = exc_value.get("stacktrace")
            if not stacktrace:
                continue
            for frame in stacktrace.get("frames", []):
                local_vars = frame.get("vars")
                if isinstance(local_vars, dict):
                    for key in list(local_vars.keys()):
                        if key in ("email", "full_name", "password", "token"):
                            local_vars[key] = "[Redacted]"

    return event


def init_sentry() -> None:
    """Initialize Sentry SDK if ``SENTRY_DSN`` is set.

    Safe to call unconditionally — when the DSN is empty or missing,
    the function returns immediately without importing ``sentry_sdk``.
    """
    dsn = os.environ.get("SENTRY_DSN")
    if not dsn:
        logger.debug("SENTRY_DSN not set — Sentry disabled")
        return

    import sentry_sdk

    sentry_sdk.init(
        dsn=dsn,
        traces_sample_rate=0.1,
        release=_get_git_sha(),
        environment=os.environ.get("APP_ENV", "development"),
        before_send=_scrub_pii,  # type: ignore[arg-type]  # Sentry stubs define Event as TypedDict
    )
    logger.info("Sentry initialized (release=%s)", _get_git_sha())
