"""Central environment configuration and stub-mode gating.

The app degrades to local stubs for several external services when
their environment variables are missing:

    - Apache Tika            (``TIKA_URL``)
    - Anthropic Claude       (``ANTHROPIC_API_KEY``)
    - Voyage AI embeddings   (``VOYAGE_API_KEY``)
    - Encrypted file storage (``STORAGE_ENCRYPTION_KEY``)
    - Postmark email         (``POSTMARK_API_TOKEN``)

Each used to ship its own dev/prod gate (``app/storage/encrypted.py``,
``app/llm/claude.py``, ``app/docs/tika_client.py``) with subtly
different rules — one allowed only ``APP_ENV=development``, another
allowed any ``APP_ENV != "production"``, and the third hard-coded a
``"development"`` literal. The result was that the same staging
``APP_ENV=staging`` could leave one service in stub mode and another
crashing on missing credentials, depending on which file you looked
at first (#449).

This module is the single source of truth for that gate. Since #847
the gate **fails closed**: stubs are only permitted when the
normalized ``APP_ENV`` is on the explicit :data:`STUB_ALLOWED_ENVS`
allowlist (``development``, ``test``, ``ci``, ``staging``). A missing
or empty ``APP_ENV`` defaults to ``development`` so a freshly-cloned
repo stays runnable, but any *unrecognized* non-empty value
(``prod``, ``Production`` typos that survive normalization, etc.) is
treated like production — stubs are disabled and consumers raise a
clear error instead of silently serving canned data.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

# Environments in which external-service stubs (Tika, Claude, Voyage,
# storage encryption, email) are permitted. Anything else — including
# unknown/typo'd values — fails closed (#847).
STUB_ALLOWED_ENVS = frozenset({"development", "test", "ci", "staging"})

# Unknown APP_ENV values we have already warned about, so the
# fail-closed path logs once per distinct value instead of per call.
_warned_unknown_envs: set[str] = set()


def _normalized_env(name: str, default: str) -> str:
    """Return ``os.environ[name]`` normalized with ``.strip().lower()``.

    Missing, empty, and whitespace-only values all return *default*.
    Shared by :func:`get_app_env` and :func:`get_worker_mode` (#847)
    so the two env gates cannot drift apart in how they parse their
    variables again.
    """
    raw = os.environ.get(name, "").strip().lower()
    return raw or default


def get_app_env() -> str:
    """Return the normalized ``APP_ENV`` value.

    Missing/empty/whitespace-only values default to ``"development"``,
    which keeps a freshly-cloned repo (and the test suite, which runs
    with ``APP_ENV`` unset) runnable without any setup beyond
    ``uv sync``.
    """
    return _normalized_env("APP_ENV", "development")


def is_stub_allowed() -> bool:
    """Return True when external service stubs are permitted.

    Fail-closed allowlist (#847): stubs are allowed only when the
    normalized ``APP_ENV`` is in :data:`STUB_ALLOWED_ENVS`. Both
    ``production`` and any unrecognized value disable stubs; unknown
    values additionally log a one-time warning so a typo'd
    ``APP_ENV=prod`` is diagnosable from the logs while the consumers
    (which raise on missing credentials when stubs are disabled)
    refuse to serve canned data.

    Used by :mod:`app.storage.encrypted`, :mod:`app.llm.claude`,
    :mod:`app.rag.embedding`, :mod:`app.docs.tika_client`,
    :mod:`app.docs.signed_urls`, :mod:`app.docs.reference_resolver`,
    and :mod:`app.email.service` to keep their stub-mode gating in
    lock-step with each other.
    """
    env = get_app_env()
    if env in STUB_ALLOWED_ENVS:
        return True
    if env != "production" and env not in _warned_unknown_envs:
        _warned_unknown_envs.add(env)
        logger.warning(
            "APP_ENV=%r is not a recognised environment "
            "(allowed stub envs: %s, or 'production'); failing closed — "
            "service stubs are DISABLED and missing credentials will raise.",
            env,
            sorted(STUB_ALLOWED_ENVS),
        )
    return False


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
    behaviour; empty/whitespace-only values are treated as unset.
    Unknown values raise ``ValueError`` so a typo in Coolify env vars
    surfaces loudly at startup instead of silently disabling the
    worker. Normalization goes through the same :func:`_normalized_env`
    helper as the ``APP_ENV`` stub gate (#847).
    """
    raw = _normalized_env("WORKER_MODE", "inproc")
    if raw not in _VALID_WORKER_MODES:
        raise ValueError(
            f"WORKER_MODE={raw!r} is invalid; expected one of {sorted(_VALID_WORKER_MODES)}"
        )
    return raw
