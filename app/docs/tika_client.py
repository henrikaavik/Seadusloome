"""HTTP client for Apache Tika's REST API.

Apache Tika is deployed as a separate Docker service (``seadusloome-tika``)
on Coolify exposing three endpoints we care about:

    PUT /tika      → plaintext extraction (Accept: text/plain)
    PUT /meta      → metadata extraction (Accept: application/json)
    GET /version   → health probe

The client is intentionally minimal: no async, no retry logic, no
streaming. Draft files are already fully in memory by the time the
parse_draft handler runs, and the job queue's exponential backoff
covers transient failures at a higher level.

Stub mode
---------

Deploying Tika is an ops step that happens *after* the first Phase 2
batches land — we don't want the dev loop to require a running Tika
container before ``/drafts/upload`` can be demoed end-to-end. So when
``TIKA_URL`` is unset AND ``APP_ENV != "production"`` the client runs
in stub mode and returns canned placeholder text from ``extract_text``,
an empty metadata dict from ``extract_metadata``, and ``False`` from
``is_healthy``. The returned text is deterministic and long enough to
flow through the rest of the pipeline (entity extraction, impact
analysis) without tripping an "empty text" guard.

Production enforcement
----------------------

In production, a missing ``TIKA_URL`` is a hard failure — but we raise
at *call time*, not at import time. That way a misconfiguration does
not crash the whole FastHTML app on boot; it only takes down the
parse pipeline until the env var is fixed. The error message points at
the README section that covers Coolify wiring so on-call knows where
to look.

Env vars
--------

    TIKA_URL               base URL (e.g. ``http://seadusloome-tika:9998``).
                           Unset is allowed in dev (stub mode) and a hard
                           error in production.
    TIKA_TIMEOUT_SECONDS   per-request timeout, defaults to 60s. The Tika
                           spec says large PDFs can take tens of seconds,
                           so 60s is the conservative floor.
    APP_ENV                ``development`` (default) or ``production``.
                           Controls stub-mode eligibility.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class TikaError(Exception):
    """Raised when a Tika request fails (timeout, 5xx, connect error).

    The handler catches this and flips the draft to ``failed`` with the
    exception message. Downstream retries come from the job queue's
    exponential backoff — this class is deliberately unchanged by
    transient vs. permanent errors because Tika has no reliable way to
    distinguish them at the protocol level.
    """


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------


_DEFAULT_STUB_NOTE = (
    "[STUB Tika] This is placeholder text returned by the Tika stub client "
    "because TIKA_URL is not configured. Set TIKA_URL in your environment "
    "to point at a running Apache Tika service (see README § Deploying "
    "Apache Tika) to extract real document contents."
)


def _load_url(explicit: str | None) -> str | None:
    """Return the effective Tika URL, or ``None`` to trigger stub/error mode."""
    if explicit:
        return explicit.rstrip("/")
    raw = os.environ.get("TIKA_URL")
    if raw:
        return raw.rstrip("/")
    return None


def _load_timeout(explicit: float | None) -> float:
    """Return the effective request timeout in seconds."""
    if explicit is not None:
        return float(explicit)
    raw = os.environ.get("TIKA_TIMEOUT_SECONDS", "60")
    try:
        return float(raw)
    except ValueError:
        logger.warning("Invalid TIKA_TIMEOUT_SECONDS=%r, falling back to 60", raw)
        return 60.0


def _is_production() -> bool:
    """Return True when the current environment is production."""
    return os.environ.get("APP_ENV", "development") == "production"


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class TikaClient:
    """Thin synchronous HTTP client for Apache Tika.

    Args:
        url: Base URL of the Tika service. If ``None`` (the default), the
            client reads ``TIKA_URL`` from the environment. An unset
            value triggers stub mode in dev and a call-time ``RuntimeError``
            in production.
        timeout: Per-request timeout in seconds. If ``None`` (the default),
            the client reads ``TIKA_TIMEOUT_SECONDS`` from the environment.
    """

    def __init__(self, url: str | None = None, timeout: float | None = None) -> None:
        self.url = _load_url(url)
        self.timeout = _load_timeout(timeout)
        # ``stub_mode`` is a cached boolean so we do not re-check the env
        # on every call. The caller can detect stub mode via ``is_healthy``.
        self._stub_mode = self.url is None and not _is_production()

    # -- stub-mode helpers --------------------------------------------------

    def _require_live_url(self) -> str:
        """Return ``self.url`` or raise if we're in a prod-missing-url state."""
        if self.url is None:
            # In dev we should have been in stub mode — callers must not
            # reach here. In prod we raise with a pointer at the README.
            raise RuntimeError(
                "TIKA_URL is not set. Apache Tika must be deployed as a "
                "Coolify service (see README § Deploying Apache Tika) and "
                "TIKA_URL must point at its internal URL before draft "
                "parsing can run."
            )
        return self.url

    def _stub_extract_text(self, file_bytes: bytes, filename_hint: str = "") -> str:
        """Return canned text for stub mode.

        The text embeds the byte count and optional filename hint so
        tests can still assert useful properties about the result, and
        so that downstream pipelines see input variation from different
        uploads even in stub mode.
        """
        suffix = f" from {filename_hint}" if filename_hint else ""
        return (
            f"{_DEFAULT_STUB_NOTE}\n\n"
            f"[STUB Tika] Extracted {len(file_bytes)} bytes{suffix}. "
            f"Real extraction requires the Tika service."
        )

    # -- public API ---------------------------------------------------------

    def extract_text(self, file_bytes: bytes, content_type: str) -> str:
        """Extract plaintext from *file_bytes* via Tika's ``PUT /tika``.

        Args:
            file_bytes: Raw (decrypted) file contents.
            content_type: MIME type of the file (e.g. ``application/pdf``).

        Returns:
            The plaintext response body. May be empty if Tika could not
            extract anything — the handler should guard against that.

        Raises:
            TikaError: On timeout, non-2xx response, or connection error.
            RuntimeError: In production when ``TIKA_URL`` is unset.
        """
        if self._stub_mode:
            logger.debug("TikaClient running in stub mode (TIKA_URL unset in dev)")
            return self._stub_extract_text(file_bytes)

        url = self._require_live_url()
        endpoint = f"{url}/tika"
        try:
            response = httpx.put(
                endpoint,
                content=file_bytes,
                headers={
                    "Content-Type": content_type or "application/octet-stream",
                    "Accept": "text/plain",
                },
                timeout=self.timeout,
            )
            response.raise_for_status()
        except httpx.TimeoutException as exc:
            raise TikaError(
                f"Tika request timed out after {self.timeout:.1f}s at {endpoint}"
            ) from exc
        except httpx.HTTPStatusError as exc:
            raise TikaError(
                f"Tika returned HTTP {exc.response.status_code} from {endpoint}: "
                f"{exc.response.text[:200]}"
            ) from exc
        except httpx.HTTPError as exc:
            raise TikaError(f"Tika request to {endpoint} failed: {exc}") from exc
        return response.text

    def extract_metadata(self, file_bytes: bytes, content_type: str) -> dict[str, Any]:
        """Extract document metadata via Tika's ``PUT /meta``.

        Returns an empty dict in stub mode (no canned metadata shape is
        stable enough to be useful for tests).
        """
        if self._stub_mode:
            logger.debug("TikaClient.extract_metadata stub mode — returning {}")
            return {}

        url = self._require_live_url()
        endpoint = f"{url}/meta"
        try:
            response = httpx.put(
                endpoint,
                content=file_bytes,
                headers={
                    "Content-Type": content_type or "application/octet-stream",
                    "Accept": "application/json",
                },
                timeout=self.timeout,
            )
            response.raise_for_status()
        except httpx.TimeoutException as exc:
            raise TikaError(
                f"Tika metadata request timed out after {self.timeout:.1f}s at {endpoint}"
            ) from exc
        except httpx.HTTPStatusError as exc:
            raise TikaError(
                f"Tika metadata returned HTTP {exc.response.status_code} from {endpoint}: "
                f"{exc.response.text[:200]}"
            ) from exc
        except httpx.HTTPError as exc:
            raise TikaError(f"Tika metadata request to {endpoint} failed: {exc}") from exc

        try:
            data = response.json()
        except ValueError as exc:
            raise TikaError(f"Tika returned non-JSON metadata from {endpoint}") from exc
        if not isinstance(data, dict):
            raise TikaError(
                f"Tika metadata from {endpoint} was not a JSON object (got {type(data).__name__})"
            )
        return data

    def is_healthy(self) -> bool:
        """Return True when Tika answers ``GET /version`` with a 200 + body.

        Never raises — healthchecks must be safe to run in any context,
        including the admin dashboard that surfaces service status.
        Stub mode always returns False so operators see the client is
        not talking to a real Tika.
        """
        if self.url is None:
            return False
        endpoint = f"{self.url}/version"
        try:
            response = httpx.get(endpoint, timeout=min(self.timeout, 5.0))
        except httpx.HTTPError:
            return False
        if response.status_code != 200:
            return False
        return bool(response.text and response.text.strip())


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------


_default_client: TikaClient | None = None


def get_default_tika_client() -> TikaClient:
    """Return a lazily-initialised module-level :class:`TikaClient`.

    The parse handler uses this instead of constructing a fresh client
    per job so the env var lookup only happens once per process. Tests
    that need to patch the client can either monkeypatch this function
    or reset ``_default_client`` via the ``reset_default_tika_client``
    helper below.
    """
    global _default_client  # noqa: PLW0603
    if _default_client is None:
        _default_client = TikaClient()
    return _default_client


def reset_default_tika_client() -> None:
    """Clear the cached singleton. Intended for test isolation only."""
    global _default_client  # noqa: PLW0603
    _default_client = None
