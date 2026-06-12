"""Typed settings registry contract (#897).

Three pins:

1. ``app/`` modules outside ``app/config.py`` perform NO direct
   environment reads — every variable goes through the typed getters
   (``env_str`` / ``env_int`` / ``env_float`` / ``env_bool``) or the
   semantic helpers (``get_app_env`` etc.) in :mod:`app.config`.
2. ``.env.example`` and ``ENV_REGISTRY`` stay in sync, both directions
   (modulo an explicit allowlist of compose-only / future vars).
3. The typed getters honour each entry's declared parse semantics
   (strict vs lenient ints, the four bool styles, raw str passthrough).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app import config

REPO_ROOT = Path(__file__).resolve().parent.parent
APP_DIR = REPO_ROOT / "app"
ENV_EXAMPLE = REPO_ROOT / ".env.example"

# ---------------------------------------------------------------------------
# 1. No inline env reads outside app/config.py
# ---------------------------------------------------------------------------

# Direct environment *reads*. A bare ``**os.environ`` spread (subprocess
# env passthrough in app/docs/docx_export.py) is deliberately NOT matched:
# it forwards the environment wholesale rather than reading config from it.
_ENV_READ = re.compile(
    r"os\.environ\.get\s*\(|os\.environ\s*\[|os\.getenv\s*\(|"
    r"from\s+os\s+import\s+[^\n]*\b(?:environ|getenv)\b"
)


def test_no_inline_env_reads_outside_config() -> None:
    offenders: list[str] = []
    for path in sorted(APP_DIR.rglob("*.py")):
        if path == APP_DIR / "config.py":
            continue
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if _ENV_READ.search(line):
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno}: {stripped}")
    assert not offenders, (
        "Direct os.environ reads are not allowed outside app/config.py (#897). "
        "Declare the variable in config.ENV_REGISTRY and read it via "
        "config.env_str/env_int/env_float/env_bool instead:\n  " + "\n  ".join(offenders)
    )


# ---------------------------------------------------------------------------
# 2. .env.example <-> registry sync
# ---------------------------------------------------------------------------

# Variables allowed in .env.example without a registry entry: consumed by
# docker-compose / the postgres container, or documented Phase-5 stubs that
# nothing reads yet (app/auth/tara_provider.py docstring).
ALLOWED_EXTRA_IN_ENV_EXAMPLE = frozenset(
    {
        "POSTGRES_USER",
        "POSTGRES_PASSWORD",
        "POSTGRES_DB",
        "ONTOLOGY_REPO_URL",
        "AUTH_PROVIDER",
        "TARA_CLIENT_ID",
        "TARA_CLIENT_SECRET",
        "TARA_REDIRECT_URI",
        "TARA_ISSUER_URL",
    }
)

# Matches both active entries (``NAME=...``) and documented-but-commented
# entries (``# NAME=``), e.g. DISABLE_BACKGROUND_WORKER.
_ENV_EXAMPLE_VAR = re.compile(r"^#?\s*([A-Z][A-Z0-9_]*)=", re.MULTILINE)


def _env_example_names() -> set[str]:
    return set(_ENV_EXAMPLE_VAR.findall(ENV_EXAMPLE.read_text(encoding="utf-8")))


def test_env_example_documents_every_registry_var() -> None:
    documented = _env_example_names()
    missing = sorted(s.name for s in config.ENV_REGISTRY if s.name not in documented)
    assert not missing, (
        ".env.example is missing registry-declared variables (#897); "
        f"document them (commented-out is fine): {missing}"
    )


def test_env_example_has_no_undeclared_vars() -> None:
    declared = {s.name for s in config.ENV_REGISTRY} | ALLOWED_EXTRA_IN_ENV_EXAMPLE
    unknown = sorted(_env_example_names() - declared)
    assert not unknown, (
        ".env.example documents variables that neither ENV_REGISTRY nor the "
        f"compose/future allowlist declares — stale docs or a missed entry: {unknown}"
    )


# ---------------------------------------------------------------------------
# 3. Getter semantics
# ---------------------------------------------------------------------------


def test_registry_is_valid_and_unique() -> None:
    names = [s.name for s in config.ENV_REGISTRY]
    assert len(names) == len(set(names))
    # _validate_registry() ran at import; re-run for explicitness.
    config._validate_registry()


def test_env_str_mirrors_environ_get(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TIKA_URL", raising=False)
    assert config.env_str("TIKA_URL") is None
    assert config.env_str("TIKA_URL", "http://x:9998") == "http://x:9998"
    monkeypatch.setenv("TIKA_URL", "")
    assert config.env_str("TIKA_URL") == ""  # set-but-empty is NOT defaulted
    monkeypatch.setenv("TIKA_URL", "  spaced  ")
    assert config.env_str("TIKA_URL") == "  spaced  "  # never normalized


def test_env_str_rejects_undeclared_and_wrong_kind() -> None:
    with pytest.raises(KeyError, match="ENV_REGISTRY"):
        config.env_str("NOT_A_DECLARED_VAR")
    with pytest.raises(TypeError, match="kind='int'"):
        config.env_str("DB_POOL_MIN")
    with pytest.raises(TypeError, match="kind='str'"):
        config.env_int("TIKA_URL")


def test_env_int_strict_propagates_garbage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DB_POOL_MIN", raising=False)
    assert config.env_int("DB_POOL_MIN") == 1
    monkeypatch.setenv("DB_POOL_MIN", "7")
    assert config.env_int("DB_POOL_MIN") == 7
    monkeypatch.setenv("DB_POOL_MIN", "seven")
    with pytest.raises(ValueError):
        config.env_int("DB_POOL_MIN")


def test_env_int_lenient_warns_and_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TIKA_MAX_TEXT_BYTES", "garbage")
    assert config.env_int("TIKA_MAX_TEXT_BYTES") == 20 * 1024 * 1024
    monkeypatch.setenv("TIKA_MAX_TEXT_BYTES", "0")
    assert config.env_int("TIKA_MAX_TEXT_BYTES") == 1  # clamped to minimum
    monkeypatch.setenv("TIKA_MAX_TEXT_BYTES", "1024")
    assert config.env_int("TIKA_MAX_TEXT_BYTES") == 1024


def test_env_float_lenient(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TIKA_TIMEOUT_SECONDS", raising=False)
    assert config.env_float("TIKA_TIMEOUT_SECONDS") == 60.0
    monkeypatch.setenv("TIKA_TIMEOUT_SECONDS", "2.5")
    assert config.env_float("TIKA_TIMEOUT_SECONDS") == 2.5
    monkeypatch.setenv("TIKA_TIMEOUT_SECONDS", "soon")
    assert config.env_float("TIKA_TIMEOUT_SECONDS") == 60.0


def test_env_int_refuses_site_parsed_vars() -> None:
    with pytest.raises(TypeError, match="site"):
        config.env_int("METRICS_RETENTION_DAYS")


def test_env_str_allows_site_parsed_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    """parse="site" vars are readable as raw strings whatever their kind.

    This is the documented escape hatch for module-local parsers
    (app/metrics.py retention quirk, app/jobs/worker.py positive-seconds
    parse) — without it those sites would be forced back onto os.environ.
    """
    monkeypatch.setenv("METRICS_RETENTION_DAYS", "7")
    assert config.env_str("METRICS_RETENTION_DAYS", "30") == "7"
    monkeypatch.delenv("METRICS_RETENTION_DAYS", raising=False)
    assert config.env_str("METRICS_RETENTION_DAYS", "30") == "30"
    monkeypatch.delenv("JOB_REAPER_INTERVAL_S", raising=False)
    assert config.env_str("JOB_REAPER_INTERVAL_S") is None


def test_env_bool_truthy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CHAT_FOLLOW_UPS_ENABLED", raising=False)
    assert config.env_bool("CHAT_FOLLOW_UPS_ENABLED") is True
    for value in ("1", "true", "YES", " on "):
        monkeypatch.setenv("CHAT_FOLLOW_UPS_ENABLED", value)
        assert config.env_bool("CHAT_FOLLOW_UPS_ENABLED") is True
    monkeypatch.setenv("CHAT_FOLLOW_UPS_ENABLED", "0")
    assert config.env_bool("CHAT_FOLLOW_UPS_ENABLED") is False


def test_env_bool_true_literal_cookie_secure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("COOKIE_SECURE", raising=False)
    assert config.env_bool("COOKIE_SECURE") is True
    monkeypatch.setenv("COOKIE_SECURE", "True")
    assert config.env_bool("COOKIE_SECURE") is True
    # Historical quirk, deliberately preserved: only the literal "true"
    # counts — "1" disables the secure flag.
    monkeypatch.setenv("COOKIE_SECURE", "1")
    assert config.env_bool("COOKIE_SECURE") is False


def test_env_bool_one_literal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DISABLE_BACKGROUND_WORKER", raising=False)
    assert config.env_bool("DISABLE_BACKGROUND_WORKER") is False
    monkeypatch.setenv("DISABLE_BACKGROUND_WORKER", "true")
    assert config.env_bool("DISABLE_BACKGROUND_WORKER") is False
    monkeypatch.setenv("DISABLE_BACKGROUND_WORKER", "1")
    assert config.env_bool("DISABLE_BACKGROUND_WORKER") is True


def test_env_bool_off_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CSRF_ORIGIN_CHECK", raising=False)
    assert config.env_bool("CSRF_ORIGIN_CHECK") is True
    for value in ("off", "0", "FALSE", "no", " disabled "):
        monkeypatch.setenv("CSRF_ORIGIN_CHECK", value)
        assert config.env_bool("CSRF_ORIGIN_CHECK") is False
    monkeypatch.setenv("CSRF_ORIGIN_CHECK", "anything-else")
    assert config.env_bool("CSRF_ORIGIN_CHECK") is True
