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

import pytest

import app.ui.primitives.button as button_module

PAGE_MODULES = [
    "app.main",
    "app.auth.routes",
    "app.auth.organizations",
    "app.auth.users",
    "app.templates.dashboard",
    "app.templates.admin_dashboard",
    "app.explorer.pages",
]


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
