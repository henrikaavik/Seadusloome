"""Expose the running app's version, build time, and git sha.

The Docker build writes ``/app/VERSION.json`` with the values supplied
via ``ARG GIT_SHA`` / ``ARG BUILD_TIME`` (or derived from a copied
``.git`` directory). At runtime we read that file once at import time
and cache the result. When the file is missing — for example during
``uv run pytest`` in a dev checkout — we fall back to invoking
``git rev-parse HEAD`` so the admin footer still shows something useful.
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

_VERSION_PATH = Path("/app/VERSION.json")
_FALLBACK_APP_VERSION = "0.1.0"

_cached_version: dict[str, str] | None = None


def _load_from_file(path: Path) -> dict[str, str] | None:
    """Read and validate ``VERSION.json`` at *path*, or return None."""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError:
        logger.exception("Failed to read %s", path)
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.exception("VERSION.json is not valid JSON")
        return None
    if not isinstance(data, dict):
        return None
    return {
        "app": str(data.get("app") or _FALLBACK_APP_VERSION),
        "sha": str(data.get("sha") or "unknown"),
        "built_at": str(data.get("built_at") or "unknown"),
    }


def _git_sha_fallback() -> str:
    """Return a short git sha prefixed ``dev-`` or ``"dev"`` on failure."""
    try:
        sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return "dev"
    return f"dev-{sha[:7]}" if sha else "dev"


def _compute_version(path: Path | None = None) -> dict[str, str]:
    """Compute the version dict without touching the module cache.

    ``path`` defaults to the module-level ``_VERSION_PATH`` resolved at
    call time — so tests patching ``_VERSION_PATH`` via
    ``patch.object(version_module, "_VERSION_PATH", ...)`` see their
    override take effect.
    """
    target = path if path is not None else _VERSION_PATH
    from_file = _load_from_file(target)
    if from_file is not None:
        return from_file
    return {
        "app": _FALLBACK_APP_VERSION,
        "sha": _git_sha_fallback(),
        "built_at": "unknown",
    }


def read_version() -> dict[str, str]:
    """Return the cached version dict with keys ``app``, ``sha``, ``built_at``."""
    global _cached_version
    if _cached_version is None:
        _cached_version = _compute_version()
    return dict(_cached_version)


def _reset_cache_for_tests() -> None:
    """Test-only helper: clear the module-level cache."""
    global _cached_version
    _cached_version = None


# Prime the cache at import time so the fallback subprocess call doesn't
# happen inside a request handler.
read_version()
