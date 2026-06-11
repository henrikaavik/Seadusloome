"""Import-safety harness.

Two unrelated invariants live here because both are about what happens at
*module import time*:

1. ``Button`` must not get shadowed by ``from fasthtml.common import *``.
   FastHTML re-exports an HTML ``Button`` builder under the same name as our
   design-system primitive; any page module that does the wildcard import
   *after* importing ``app.ui.primitives.button.Button`` silently overwrites
   the symbol with the unstyled stdlib version.

2. Lazy-init contract for SDK singletons. ``ClaudeProvider`` and
   ``VoyageProvider`` are documented in CLAUDE.md as lazily-initialised: the
   ``anthropic`` and ``voyageai`` packages must NOT be required to merely
   import ``app.main`` in stub mode. This guards the four Sprint-1 metrics
   collectors (#195/#196/#197/#323) against accidentally triggering SDK
   construction at module load (e.g. via a top-level decorator that
   eagerly resolves a provider).
"""

from __future__ import annotations

import importlib
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import app.ui.primitives.button as button_module

# The two import lines that, in combination, make a module a Button-shadow
# risk: the wildcard re-exports FastHTML's HTML ``Button`` builder; the
# design-system import must come *after* it to win. A module that binds
# only one (e.g. ``app.admin._shared`` has the wildcard but no Button) is
# not at risk and is correctly skipped.
_WILDCARD_IMPORT = "from fasthtml.common import *"
_BUTTON_IMPORT = "from app.ui.primitives.button import Button"

# Admin modules that MUST be discovered. ``app/admin/routes.py`` wires
# handlers from every one of these into the FastHTML router, and each uses
# the wildcard + design-system Button pattern the guard exists to police.
# Asserting this floor means a new admin page module is guarded
# automatically, while a refactor that breaks discovery (e.g. dropping the
# wildcard, renaming the Button import) fails loudly instead of silently
# shrinking coverage. This list replaces the funnelling the retired
# ``app.templates.admin_dashboard`` shim used to provide (#860).
_KNOWN_ADMIN_BUTTON_MODULES = frozenset(
    {
        "app.admin.dashboard",
        "app.admin.audit",
        "app.admin.analytics",
        "app.admin.cost_dashboard",
        "app.admin.health",
        "app.admin.job_monitor",
        "app.admin.performance",
        "app.admin.sentry_panel",
        "app.admin.sync",
    }
)


def _discover_admin_button_modules() -> list[str]:
    """Scan ``app/admin/*.py`` for the Button-shadow pattern.

    A module qualifies when its source contains BOTH the ``fasthtml.common``
    wildcard import and the design-system ``Button`` import — i.e. exactly
    the modules where the shadow can occur. Returns dotted module paths,
    sorted for a stable parametrize order.
    """
    # button.py lives at app/ui/primitives/button.py; parents[2] is the
    # ``app`` package root, so the admin package is a direct sibling.
    admin_dir = Path(button_module.__file__).resolve().parents[2] / "admin"
    discovered: list[str] = []
    for path in sorted(admin_dir.glob("*.py")):
        if path.name == "__init__.py":
            continue
        source = path.read_text(encoding="utf-8")
        if _WILDCARD_IMPORT in source and _BUTTON_IMPORT in source:
            discovered.append(f"app.admin.{path.stem}")
    return discovered


_ADMIN_BUTTON_MODULES = _discover_admin_button_modules()

PAGE_MODULES = [
    "app.main",
    "app.auth.routes",
    "app.auth.organizations",
    "app.auth.users",
    "app.templates.dashboard",
    # Auto-discovered admin page modules (the wildcard + design-system
    # Button pattern). Discovery replaces the manual list the retired
    # ``app.templates.admin_dashboard`` shim used to funnel (#860).
    *_ADMIN_BUTTON_MODULES,
    "app.explorer.pages",
]


def test_admin_button_discovery_covers_known_modules():
    """Discovery must never silently go empty or shrink below the known set.

    Every module in ``_KNOWN_ADMIN_BUTTON_MODULES`` has its handlers wired
    into ``app/admin/routes.py`` and carries the Button-shadow pattern, so
    each must be picked up by ``_discover_admin_button_modules``. A new admin
    page is then guarded automatically; a refactor that breaks the detection
    heuristic fails here instead of quietly dropping coverage.
    """
    discovered = set(_ADMIN_BUTTON_MODULES)
    missing = _KNOWN_ADMIN_BUTTON_MODULES - discovered
    assert not missing, (
        "Button-shadow discovery did not pick up known admin modules "
        f"{sorted(missing)} — check that they still contain both "
        f"{_WILDCARD_IMPORT!r} and {_BUTTON_IMPORT!r}, or update "
        "_KNOWN_ADMIN_BUTTON_MODULES if a module was intentionally removed. "
        f"Discovered: {sorted(discovered)}."
    )


@pytest.mark.parametrize("module_path", PAGE_MODULES)
def test_button_symbol_not_shadowed(module_path: str):
    module = importlib.import_module(module_path)
    if not hasattr(module, "Button"):
        pytest.skip(f"{module_path} does not bind Button at all")
    assert module.Button is button_module.Button, (
        f"{module_path}.Button has been shadowed by another import — "
        f"got {module.Button!r}, expected {button_module.Button!r}"
    )


def test_app_main_imports_without_constructing_sdk_singletons():
    """``import app.main`` must not eagerly construct ``ClaudeProvider`` or
    ``VoyageProvider`` — even when ``anthropic`` and ``voyageai`` are
    unavailable. Run in a subprocess so blocked imports don't leak into the
    rest of the test session via ``sys.modules`` caching.
    """
    script = textwrap.dedent(
        """
        import builtins, sys

        _real_import = builtins.__import__
        _blocked = {"anthropic", "voyageai"}

        def _blocking_import(name, *args, **kwargs):
            if name.split(".")[0] in _blocked:
                raise ImportError(f"{name} blocked for lazy-init smoke test")
            return _real_import(name, *args, **kwargs)

        builtins.__import__ = _blocking_import

        import app.main  # noqa: F401  -- must not require anthropic/voyageai
        import app.llm.claude as c
        import app.rag.embedding as e

        assert c._default_provider is None, (
            f"ClaudeProvider was eagerly constructed at import time: {c._default_provider!r}"
        )
        assert e._default_provider is None, (
            f"VoyageProvider was eagerly constructed at import time: {e._default_provider!r}"
        )
        print("OK")
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        env={"APP_ENV": "development", "DISABLE_BACKGROUND_WORKER": "1", "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, (
        f"Stub-mode import failed.\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "OK" in result.stdout
