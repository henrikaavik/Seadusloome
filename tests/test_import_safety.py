"""Guard against ``Button`` getting shadowed by ``from fasthtml.common import *``.

FastHTML re-exports an HTML ``Button`` builder under the same name as our
design-system ``Button`` primitive. Any module that does
``from fasthtml.common import *`` *after* importing
``app.ui.primitives.button.Button`` will silently overwrite the symbol
with the unstyled stdlib version, which is hard to spot in code review.

This test imports every page module that ships a UI and asserts that the
``Button`` name resolves to our primitive afterwards.
"""

from __future__ import annotations

import importlib

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
