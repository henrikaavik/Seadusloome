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
from dataclasses import dataclass
from typing import overload

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
    return env_bool("CHAT_AUTO_TITLE_ENABLED")


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


# ---------------------------------------------------------------------------
# Typed settings registry (#897)
# ---------------------------------------------------------------------------
#
# Every environment variable the application reads is declared here with its
# type, documented default, and a one-line description. ``app/`` modules
# outside this file MUST NOT read ``os.environ`` directly (pinned by
# ``tests/test_settings_registry.py``); they call the typed getters instead:
#
#     from app import config
#
#     pool_max = config.env_int("DB_POOL_MAX")
#     tika_url = config.env_str("TIKA_URL")
#
# Design rules:
#
# - **Call-time reads.** Getters read ``os.environ`` on every call (no
#   import-time snapshot), so ``monkeypatch.setenv`` in tests and Coolify
#   env edits behave exactly as before. Modules that intentionally read a
#   value once at import time (``app.db`` pool sizing,
#   ``app.auth.jwt_provider.SECRET_KEY``, ``app.sync.webhook.WEBHOOK_SECRET``)
#   keep that timing — they just source the string from here.
# - **No hidden normalization.** ``env_str`` returns the value exactly like
#   ``os.environ.get`` — no ``strip()``/``lower()`` — because several values
#   (SECRET_KEY, webhook and token secrets) are key material in which
#   whitespace is significant. Call sites keep their own ``strip()`` /
#   ``rstrip()`` behaviour.
# - **Domain logic stays in the owning module.** Stub fallbacks,
#   raise-in-production gates, fallback chains (DOWNLOAD_TOKEN_SECRET →
#   SECRET_KEY), and bespoke clamps live next to the feature they protect;
#   this module owns *declaration + typed parsing* only.
# - ``parse="site"`` entries are declared for inventory/docs purposes but
#   keep a module-local parser whose semantics are too quirky to generalize
#   (e.g. ``METRICS_RETENTION_DAYS`` falls back to 0 — retention *disabled*
#   — on garbage, not to its default). Those sites read the raw string via
#   ``env_str`` and parse locally; ``env_int``/``env_float`` refuse such
#   names so the quirk cannot be flattened by accident.


@dataclass(frozen=True)
class EnvSetting:
    """Declaration of one environment variable consumed by the app.

    ``default`` is the *documented* default. For ``int``/``float``/``bool``
    entries it is also enforced by the typed getters; for ``str`` entries
    the call site supplies it (several string vars carry different defaults
    at different call sites — e.g. ``APP_BASE_URL``), and for
    ``parse="site"`` entries the owning module applies it locally.
    """

    name: str
    kind: str  # "str" | "int" | "float" | "bool"
    default: str | int | float | bool | None
    description: str
    secret: bool = False
    section: str = "App"
    # int/float parsing: "strict" (ValueError propagates — config typos
    # crash at first read, matching the historical int()/float() call
    # sites), "lenient" (warn + default), or "site" (module-local parser).
    parse: str = "strict"
    # Lenient parses only: clamp floor applied to *valid* values.
    minimum: int | float | None = None
    # bool parsing: "truthy"       — {"1","true","yes","on"}, default when unset
    #               "true-literal" — value.lower() == "true", default when unset
    #               "one-literal"  — value == "1" exactly (unset → False)
    #               "off-set"      — NOT in {"off","0","false","no","disabled"}
    bool_style: str = "truthy"


_MIB = 1024 * 1024

ENV_REGISTRY: tuple[EnvSetting, ...] = (
    # --- PostgreSQL --------------------------------------------------------
    EnvSetting(
        "DATABASE_URL",
        "str",
        None,
        "Postgres DSN. Unset: app.db falls back to the localhost dev DSN in "
        "development and raises in any other environment.",
        secret=True,
        section="PostgreSQL",
    ),
    EnvSetting(
        "DB_POOL_MIN",
        "int",
        1,
        "Connection-pool floor (app.db clamps to >= 0 at import).",
        section="PostgreSQL",
    ),
    EnvSetting(
        "DB_POOL_MAX",
        "int",
        10,
        "Connection-pool ceiling (app.db clamps to >= 1 at import).",
        section="PostgreSQL",
    ),
    EnvSetting(
        "DB_POOL_TIMEOUT",
        "float",
        2.0,
        "Seconds to wait for a free pooled connection before failing.",
        section="PostgreSQL",
    ),
    EnvSetting(
        "DB_CONNECT_TIMEOUT",
        "int",
        5,
        "TCP connect timeout (seconds) for new Postgres connections.",
        section="PostgreSQL",
    ),
    EnvSetting(
        "DB_BREAKER_COOLDOWN",
        "float",
        2.0,
        "Circuit-breaker cooldown (seconds) after pool exhaustion.",
        section="PostgreSQL",
    ),
    # --- Jena Fuseki --------------------------------------------------------
    EnvSetting(
        "JENA_URL",
        "str",
        "http://localhost:3030",
        "Base URL of the Apache Jena Fuseki triplestore.",
        section="Jena Fuseki",
    ),
    EnvSetting(
        "JENA_DATASET",
        "str",
        "ontology",
        "Fuseki dataset name queried by the app and loaded by the sync.",
        section="Jena Fuseki",
    ),
    EnvSetting(
        "FUSEKI_ADMIN_PASSWORD",
        "str",
        None,
        "Fuseki admin credential for sync graph management. Unset: dev-only "
        "fallback inside app.sync (fails closed outside development).",
        secret=True,
        section="Jena Fuseki",
    ),
    # --- Ontology sync ------------------------------------------------------
    EnvSetting(
        "GITHUB_WEBHOOK_SECRET",
        "str",
        "",
        "HMAC secret for GitHub push-webhook signature verification. "
        "Empty: signature verification is skipped (dev only).",
        secret=True,
        section="Ontology sync",
    ),
    EnvSetting(
        "SYNC_MIN_TRIPLES",
        "int",
        1_000_000,
        "Absolute minimum triple count for post-sync verification; "
        "negative values clamp to 0, garbage falls back to the default.",
        section="Ontology sync",
        parse="lenient",
        minimum=0,
    ),
    # --- App ----------------------------------------------------------------
    EnvSetting(
        "APP_ENV",
        "str",
        "development",
        "Deployment environment (development | test | ci | staging | "
        "production). Read via config.get_app_env(); drives the fail-closed "
        "stub gate config.is_stub_allowed().",
        section="App",
        parse="site",
    ),
    EnvSetting(
        "WORKER_MODE",
        "str",
        "inproc",
        "Background-job worker mode (inproc | standalone). Read via "
        "config.get_worker_mode(); unknown values raise at startup.",
        section="App",
        parse="site",
    ),
    EnvSetting(
        "DISABLE_BACKGROUND_WORKER",
        "bool",
        False,
        'TEST-ONLY: literal "1" suppresses the lifespan background worker. '
        "Never set in production or uploads stay status='uploaded' forever.",
        section="App",
        bool_style="one-literal",
    ),
    EnvSetting(
        "APP_BASE_URL",
        "str",
        None,
        "Public base URL of the deployment. Used for password-reset links "
        "(default http://localhost:8000 there) and the CSRF origin "
        "allowlist (no default there — own-origin only).",
        section="App",
    ),
    # --- Auth & cookies -----------------------------------------------------
    EnvSetting(
        "SECRET_KEY",
        "str",
        None,
        "JWT signing secret (HS256, >= 32 UTF-8 bytes). Unset: dev fallback "
        "in development, refused elsewhere. Whitespace is significant — "
        "never normalized.",
        secret=True,
        section="Auth & cookies",
    ),
    EnvSetting(
        "COOKIE_SECURE",
        "bool",
        True,
        'Secure flag on auth cookies. Only the literal "true" '
        "(case-insensitive) enables it when set; defaults to true when "
        "unset. NB: COOKIE_SECURE=1 disables it.",
        section="Auth & cookies",
        bool_style="true-literal",
    ),
    EnvSetting(
        "TRUSTED_PROXY_HOSTS",
        "str",
        "",
        "Comma-separated proxy hosts trusted for X-Forwarded-* parsing. "
        "Empty: built-in Traefik/Coolify defaults. Wildcards are refused "
        "(app.auth.perimeter).",
        section="Auth & cookies",
    ),
    EnvSetting(
        "CSRF_ORIGIN_CHECK",
        "bool",
        True,
        "HTTP Origin/Referer CSRF check. On unless set to one of "
        "off/0/false/no/disabled (emergency escape hatch only).",
        section="Auth & cookies",
        bool_style="off-set",
    ),
    # --- Observability ------------------------------------------------------
    EnvSetting(
        "SENTRY_DSN",
        "str",
        None,
        "Sentry project DSN. Unset/empty: Sentry stays disabled.",
        secret=True,
        section="Observability",
    ),
    EnvSetting(
        "GIT_SHA",
        "str",
        None,
        "Short commit hash for Sentry release tagging (set by CI/CD); "
        "falls back to `git rev-parse --short HEAD`, then 'unknown'.",
        section="Observability",
    ),
    EnvSetting(
        "SENTRY_API_TOKEN",
        "str",
        None,
        "Sentry API token for the admin issue panel (read-only scope).",
        secret=True,
        section="Observability",
    ),
    EnvSetting(
        "SENTRY_ORG_SLUG",
        "str",
        None,
        "Sentry organisation slug for the admin issue panel.",
        section="Observability",
    ),
    EnvSetting(
        "SENTRY_PROJECT_SLUG",
        "str",
        None,
        "Sentry project slug for the admin issue panel.",
        section="Observability",
    ),
    EnvSetting(
        "METRICS_RETENTION_DAYS",
        "int",
        30,
        "Days of app-metrics history to keep; 0 disables retention. "
        "Quirk preserved from #861: garbage values disable retention (0) "
        "rather than falling back to the default.",
        section="Observability",
        parse="site",
    ),
    # --- Documents & storage ------------------------------------------------
    EnvSetting(
        "TIKA_URL",
        "str",
        None,
        "Apache Tika REST endpoint for .docx/.pdf parsing. Unset: stub "
        "mode in dev environments, error in production.",
        section="Documents & storage",
    ),
    EnvSetting(
        "TIKA_TIMEOUT_SECONDS",
        "float",
        60.0,
        "Tika request timeout in seconds.",
        section="Documents & storage",
        parse="lenient",
    ),
    EnvSetting(
        "TIKA_MAX_TEXT_BYTES",
        "int",
        20 * _MIB,
        "Ceiling for extracted plaintext accepted from Tika (bytes); values below 1 clamp to 1.",
        section="Documents & storage",
        parse="lenient",
        minimum=1,
    ),
    EnvSetting(
        "MAX_UPLOAD_SIZE_MB",
        "int",
        50,
        "Maximum draft upload size in megabytes; values below 1 clamp to 1.",
        section="Documents & storage",
        parse="lenient",
        minimum=1,
    ),
    EnvSetting(
        "SEADUSLOOME_SIMILARITY_THRESHOLD",
        "float",
        0.15,
        "Jaccard similarity threshold for the similar-drafts signal; "
        "valid values are clamped to [0, 1] at the call site.",
        section="Documents & storage",
        parse="lenient",
    ),
    EnvSetting(
        "RESOLVER_REF_HASH_SECRET",
        "str",
        None,
        "HMAC key for reference-resolver miss-log identifiers. Required in "
        "production; dev sentinel otherwise (app.docs.reference_resolver).",
        secret=True,
        section="Documents & storage",
    ),
    EnvSetting(
        "DOWNLOAD_TOKEN_SECRET",
        "str",
        None,
        "HMAC key for signed expiring report-download URLs. Unset: falls "
        "back to SECRET_KEY so one rotation rotates both.",
        secret=True,
        section="Documents & storage",
    ),
    EnvSetting(
        "EXPORT_DIR",
        "str",
        None,
        "Directory for generated .docx/.pdf exports. Unset: dev-friendly "
        "./storage/exports outside production (app.docs.docx_export).",
        section="Documents & storage",
    ),
    EnvSetting(
        "STORAGE_ENCRYPTION_KEY",
        "str",
        None,
        "Fernet key(s) for encrypted draft storage, comma-separated for "
        "MultiFernet rotation (first encrypts, all decrypt). Required in "
        "production.",
        secret=True,
        section="Documents & storage",
    ),
    EnvSetting(
        "STORAGE_DIR",
        "str",
        None,
        "Directory for encrypted draft files. Unset: ./storage/drafts in "
        "dev environments, /var/seadusloome/drafts in production.",
        section="Documents & storage",
    ),
    # --- LLM & embeddings ---------------------------------------------------
    EnvSetting(
        "ANTHROPIC_API_KEY",
        "str",
        None,
        "Anthropic API key for Claude. Unset: ClaudeProvider serves stub "
        "responses in dev environments, raises in production.",
        secret=True,
        section="LLM & embeddings",
    ),
    EnvSetting(
        "CLAUDE_MODEL",
        "str",
        "claude-sonnet-4-6",
        "Model id used by ALL LLM-backed features (extraction, impact, "
        "chat, drafter). Default lives in app.llm.claude.DEFAULT_MODEL.",
        section="LLM & embeddings",
    ),
    EnvSetting(
        "VOYAGE_API_KEY",
        "str",
        None,
        "Voyage AI key for embeddings. Unset: deterministic stub vectors "
        "in dev environments, raises in production.",
        secret=True,
        section="LLM & embeddings",
    ),
    EnvSetting(
        "VOYAGE_MODEL",
        "str",
        "voyage-multilingual-2",
        "Voyage AI embedding model id. Default lives in app.rag.embedding.DEFAULT_MODEL.",
        section="LLM & embeddings",
    ),
    EnvSetting(
        "VOYAGE_DIMENSIONS",
        "int",
        1024,
        "Embedding dimensionality; must match the model output and the pgvector index.",
        section="LLM & embeddings",
    ),
    # --- Chat & cost limits -------------------------------------------------
    EnvSetting(
        "CHAT_MAX_MESSAGES_PER_HOUR",
        "int",
        100,
        "Per-user chat message rate limit (messages per hour).",
        section="Chat & cost limits",
    ),
    EnvSetting(
        "ORG_MAX_MONTHLY_COST_USD",
        "float",
        50.0,
        "Per-organisation monthly LLM cost budget in USD.",
        section="Chat & cost limits",
    ),
    EnvSetting(
        "CHAT_AUTO_TITLE_ENABLED",
        "bool",
        True,
        "Auto-generate conversation titles after the first exchange.",
        section="Chat & cost limits",
    ),
    EnvSetting(
        "CHAT_FOLLOW_UPS_ENABLED",
        "bool",
        True,
        "Suggest follow-up questions after assistant replies.",
        section="Chat & cost limits",
    ),
    # --- Background jobs ----------------------------------------------------
    EnvSetting(
        "JOB_REAPER_INTERVAL_S",
        "float",
        60.0,
        "Seconds between orphan-job recovery passes. Non-positive or "
        "garbage values fall back to the default (app.jobs.worker).",
        section="Background jobs",
        parse="site",
    ),
    EnvSetting(
        "JOB_REAPER_CLAIMED_TIMEOUT_S",
        "float",
        None,
        "Age after which a 'claimed' job is returned to the queue. "
        "Default: app.jobs.worker.DEFAULT_REAPER_CLAIMED_TIMEOUT_S.",
        section="Background jobs",
        parse="site",
    ),
    EnvSetting(
        "JOB_REAPER_RUNNING_TIMEOUT_S",
        "float",
        None,
        "Age after which a 'running' job is presumed orphaned and retried. "
        "Default: app.jobs.worker.DEFAULT_REAPER_RUNNING_TIMEOUT_S.",
        section="Background jobs",
        parse="site",
    ),
    # --- Email --------------------------------------------------------------
    EnvSetting(
        "POSTMARK_API_TOKEN",
        "str",
        None,
        "Postmark server token. Unset: stub provider in dev environments, raises in production.",
        secret=True,
        section="Email",
    ),
    EnvSetting(
        "EMAIL_FROM",
        "str",
        None,
        'Sender address for outgoing mail. Default: "Seadusloome '
        '<noreply@sixtyfour.ee>" (app.email.service).',
        section="Email",
    ),
)

_SETTINGS_BY_NAME: dict[str, EnvSetting] = {s.name: s for s in ENV_REGISTRY}

_TRUTHY_VALUES = frozenset({"1", "true", "yes", "on"})
_OFF_VALUES = frozenset({"off", "0", "false", "no", "disabled"})

_VALID_KINDS = frozenset({"str", "int", "float", "bool"})
_VALID_PARSES = frozenset({"strict", "lenient", "site"})
_VALID_BOOL_STYLES = frozenset({"truthy", "true-literal", "one-literal", "off-set"})


def _validate_registry() -> None:
    """Fail fast at import on malformed registry entries (incl. dupes)."""
    if len(_SETTINGS_BY_NAME) != len(ENV_REGISTRY):
        raise RuntimeError("ENV_REGISTRY contains duplicate variable names")
    for s in ENV_REGISTRY:
        if s.kind not in _VALID_KINDS:
            raise RuntimeError(f"{s.name}: unknown kind {s.kind!r}")
        if s.parse not in _VALID_PARSES:
            raise RuntimeError(f"{s.name}: unknown parse {s.parse!r}")
        if s.bool_style not in _VALID_BOOL_STYLES:
            raise RuntimeError(f"{s.name}: unknown bool_style {s.bool_style!r}")
        if s.kind == "int" and s.parse != "site" and not isinstance(s.default, int):
            raise RuntimeError(f"{s.name}: int entries need an int default")
        if s.kind == "float" and s.parse != "site" and not isinstance(s.default, (int, float)):
            raise RuntimeError(f"{s.name}: float entries need a numeric default")
        if s.kind == "bool" and not isinstance(s.default, bool):
            raise RuntimeError(f"{s.name}: bool entries need a bool default")
        if s.minimum is not None and s.parse != "lenient":
            raise RuntimeError(f"{s.name}: minimum is only valid with parse='lenient'")


_validate_registry()


def _setting(name: str) -> EnvSetting:
    try:
        return _SETTINGS_BY_NAME[name]
    except KeyError:
        raise KeyError(
            f"{name!r} is not declared in app.config.ENV_REGISTRY; "
            "declare it there before reading it."
        ) from None


@overload
def env_str(name: str) -> str | None: ...
@overload
def env_str(name: str, default: str) -> str: ...


def env_str(name: str, default: str | None = None) -> str | None:
    """``os.environ.get(name, default)`` with registry validation.

    Returns the value verbatim — no strip/normalization (secrets are key
    material). The *default* is supplied by the call site so string vars
    keep their historical per-site defaults exactly.
    """
    setting = _setting(name)
    if setting.kind != "str":
        raise TypeError(f"{name} is declared kind={setting.kind!r}; use env_{setting.kind}()")
    return os.environ.get(name, default)


def env_int(name: str) -> int:
    """Registry-typed int read; default and parse mode come from the registry.

    ``parse="strict"`` lets ValueError propagate (matching historical
    ``int(os.environ.get(...))`` call sites); ``parse="lenient"`` warns and
    returns the default, clamping valid values to ``minimum`` when declared.
    """
    setting = _setting(name)
    if setting.kind != "int":
        raise TypeError(f"{name} is declared kind={setting.kind!r}; use env_{setting.kind}()")
    if setting.parse == "site":
        raise TypeError(
            f"{name} is parsed by its owning module (parse='site'); read the raw "
            "string via env_str() there instead."
        )
    default = setting.default
    assert isinstance(default, int)
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        if setting.parse == "lenient":
            logger.warning("Invalid %s=%r; using default %s", name, raw, default)
            return default
        raise
    if setting.minimum is not None and value < setting.minimum:
        return int(setting.minimum)
    return value


def env_float(name: str) -> float:
    """Registry-typed float read; same strict/lenient contract as env_int."""
    setting = _setting(name)
    if setting.kind != "float":
        raise TypeError(f"{name} is declared kind={setting.kind!r}; use env_{setting.kind}()")
    if setting.parse == "site":
        raise TypeError(
            f"{name} is parsed by its owning module (parse='site'); read the raw "
            "string via env_str() there instead."
        )
    default = setting.default
    assert isinstance(default, (int, float))
    raw = os.environ.get(name)
    if raw is None:
        return float(default)
    try:
        value = float(raw)
    except ValueError:
        if setting.parse == "lenient":
            logger.warning("Invalid %s=%r; using default %s", name, raw, default)
            return float(default)
        raise
    if setting.minimum is not None and value < setting.minimum:
        return float(setting.minimum)
    return value


def env_bool(name: str) -> bool:
    """Registry-typed bool read; the parse style comes from the registry.

    Styles (declared per-entry, preserving each site's historical
    semantics): ``truthy`` — member of {1,true,yes,on} case-insensitively,
    registry default when unset; ``true-literal`` — exactly "true"
    case-insensitively (NB ``COOKIE_SECURE=1`` is *false*); ``one-literal``
    — exactly "1" (unset → False); ``off-set`` — true unless a member of
    {off,0,false,no,disabled}.
    """
    setting = _setting(name)
    if setting.kind != "bool":
        raise TypeError(f"{name} is declared kind={setting.kind!r}; use env_{setting.kind}()")
    raw = os.environ.get(name)
    style = setting.bool_style
    if style == "truthy":
        if raw is None:
            return bool(setting.default)
        return raw.strip().lower() in _TRUTHY_VALUES
    if style == "true-literal":
        if raw is None:
            return bool(setting.default)
        return raw.lower() == "true"
    if style == "one-literal":
        return raw == "1"
    # "off-set" — validated at import, nothing else can reach here.
    return (raw or "").strip().lower() not in _OFF_VALUES
