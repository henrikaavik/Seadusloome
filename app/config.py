"""Central environment configuration and stub-mode gating.

Phase 2 introduces three external services that the app degrades to
local stubs for when their environment variables are missing:

    - Apache Tika      (``TIKA_URL``)
    - Anthropic Claude (``ANTHROPIC_API_KEY``)
    - Encrypted file storage (``STORAGE_ENCRYPTION_KEY``)

Each used to ship its own dev/prod gate (``app/storage/encrypted.py``,
``app/llm/claude.py``, ``app/docs/tika_client.py``) with subtly
different rules — one allowed only ``APP_ENV=development``, another
allowed any ``APP_ENV != "production"``, and the third hard-coded a
``"development"`` literal. The result was that the same staging
``APP_ENV=staging`` could leave one service in stub mode and another
crashing on missing credentials, depending on which file you looked
at first (#449).

This module is the single source of truth for that gate. The rule is
deliberately simple: stubs are allowed unless ``APP_ENV=production``.
That means dev, test, ci, and staging all default to stub mode; only
an explicit production deployment forces real credentials. Operators
who want production-like staging just set ``APP_ENV=production`` plus
the matching credentials.
"""

from __future__ import annotations

import os


def is_stub_allowed() -> bool:
    """Return True when Phase 2 service stubs are permitted.

    The rule is: stubs allowed UNLESS ``APP_ENV=production``. The
    default of ``"development"`` means an unset env var also allows
    stubs, which keeps a freshly-cloned repo runnable without any
    setup beyond ``uv sync``.

    Used by :mod:`app.storage.encrypted`, :mod:`app.llm.claude`, and
    :mod:`app.docs.tika_client` to keep their stub-mode gating in
    lock-step with each other.
    """
    return os.environ.get("APP_ENV", "development") != "production"


def is_chat_auto_title_enabled() -> bool:
    """Return True when the chat auto-title feature should run.

    Controlled by ``CHAT_AUTO_TITLE_ENABLED``. Any truthy value
    (``"1"``, ``"true"``, ``"yes"``, ``"on"``, case-insensitive)
    enables the feature. Defaults to True when unset.
    """
    raw = os.environ.get("CHAT_AUTO_TITLE_ENABLED")
    if raw is None:
        return True
    return raw.strip().lower() in {"1", "true", "yes", "on"}


# ---------------------------------------------------------------------------
# Worker mode (#348)
# ---------------------------------------------------------------------------
#
# The background job worker can run in two modes:
#
#   ``inproc`` (default) — workers spawn as a daemon thread inside the
#       FastHTML app process via the ASGI lifespan hook. Simplest for
#       local dev and single-container production deployments.
#
#   ``standalone`` — workers run in a separate process (typically a
#       second Coolify container) launched via ``scripts/run_worker.py``.
#       The web container's lifespan SKIPS its inproc worker. Use when
#       worker load grows beyond what one container can handle, or when
#       you want to scale web and worker capacity independently.
#
# Both modes share the same handler registry (``app.jobs.registry``)
# and read/write the same ``background_jobs`` Postgres table. You can
# mix-and-match (one inproc + N standalone) without any other change.

_VALID_WORKER_MODES = frozenset({"inproc", "standalone"})


def get_worker_mode() -> str:
    """Return the configured worker mode (``"inproc"`` | ``"standalone"``).

    Defaults to ``"inproc"`` to preserve the historical single-container
    behaviour. Unknown values raise ``ValueError`` so a typo in Coolify
    env vars surfaces loudly at startup instead of silently disabling
    the worker.
    """
    raw = os.environ.get("WORKER_MODE", "inproc").strip().lower()
    if raw not in _VALID_WORKER_MODES:
        raise ValueError(
            f"WORKER_MODE={raw!r} is invalid; expected one of {sorted(_VALID_WORKER_MODES)}"
        )
    return raw
