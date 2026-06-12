"""Enforce the framework-free contract for ``app/analyysikeskus/services/``.

The service layer (#860) is the Phase-5 REST/MCP reference pattern: plain
``input → typed result`` functions with **zero** web-framework coupling, so
the same function can be wrapped by a FastHTML route, a REST endpoint, or an
MCP tool without dragging Starlette/FastHTML into the call.

This test AST-scans every module in the services package and fails if any of
them import ``fasthtml`` / ``starlette`` (or a submodule thereof). It also
asserts the two reference services return their typed dataclasses (never an
HTTP response / FT node) for the unresolved branch, which needs no Jena.
"""

from __future__ import annotations

import ast
import pkgutil
from pathlib import Path

import app.analyysikeskus.services as services_pkg

_FORBIDDEN_ROOTS = {"fasthtml", "starlette"}


def _service_module_paths() -> list[Path]:
    pkg_dir = Path(services_pkg.__file__).parent
    return sorted(p for p in pkg_dir.glob("*.py"))


def _imported_roots(tree: ast.AST) -> set[str]:
    """Return the top-level package of every ``import`` / ``from`` in *tree*."""
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            # Only absolute imports carry a forbidden root; relative
            # (``from . import x``) have module=None / level>0.
            if node.module and node.level == 0:
                roots.add(node.module.split(".")[0])
    return roots


def test_services_package_has_modules() -> None:
    """Guard: the scan actually found the service modules (not an empty glob)."""
    names = {p.stem for p in _service_module_paths()}
    assert {"normi_mojuahel", "el_ulevott"} <= names, names


def test_no_service_module_imports_a_web_framework() -> None:
    """No module under ``services/`` imports fasthtml or starlette."""
    offenders: dict[str, set[str]] = {}
    for path in _service_module_paths():
        tree = ast.parse(path.read_text(), filename=str(path))
        forbidden = _imported_roots(tree) & _FORBIDDEN_ROOTS
        if forbidden:
            offenders[path.name] = forbidden
    assert not offenders, (
        f"service modules must be framework-free, but found web-framework imports: {offenders}"
    )


def test_services_package_importable_without_web_framework_names() -> None:
    """Importing the package exposes the two reference service functions."""
    import app.analyysikeskus.services as svc

    assert hasattr(svc, "analyse_normi_mojuahel")
    assert hasattr(svc, "analyse_el_ulevott")


def test_every_submodule_is_a_clean_import() -> None:
    """Every ``services`` submodule imports cleanly (catches syntax/name bugs)."""
    found = [name for _f, name, _p in pkgutil.iter_modules(services_pkg.__path__)]
    assert "normi_mojuahel" in found
    assert "el_ulevott" in found
    for name in found:
        __import__(f"app.analyysikeskus.services.{name}")


def test_el_ulevott_unresolved_returns_typed_result_not_response() -> None:
    """A garbage input resolves to a typed ``ElUlevottUnresolved`` dataclass.

    This branch needs no Jena (parse → no eu_act ref → label search returns
    ``[]`` on a dead/empty store → unresolved), so it is safe to call in CI.
    The point is that the service returns a *dataclass*, never an HTTP
    response or an FT node.
    """
    from unittest.mock import patch

    from app.analyysikeskus.services.el_ulevott import (
        ElUlevottUnresolved,
        analyse_el_ulevott,
    )

    with patch(
        "app.analyysikeskus.services.el_ulevott.search_eu_acts_by_label",
        return_value=[],
    ):
        result = analyse_el_ulevott("see ei ole õigusakt üldse")

    assert isinstance(result, ElUlevottUnresolved)
    assert result.kind == "unresolved"
    # A well-formed-CELEX-shaped garbage string flips the flag; this prose
    # does not.
    assert result.canonical_celex_shape is False


def test_normi_unresolved_returns_typed_result_not_response() -> None:
    """An unrecognised free-text input resolves to ``NormiUnresolved``.

    ``org_id=None`` skips the draft path; an empty resolver result (patched so
    no Jena is needed) yields the unresolved branch — a dataclass, not a
    response.
    """
    from unittest.mock import patch

    from app.analyysikeskus.services.normi_mojuahel import (
        NormiUnresolved,
        analyse_normi_mojuahel,
    )

    with patch(
        "app.docs.reference_resolver.ReferenceResolver.resolve",
        return_value=[],
    ):
        result = analyse_normi_mojuahel("mingi suvaline jutt", org_id=None)

    assert isinstance(result, NormiUnresolved)
    assert result.kind == "unresolved"
