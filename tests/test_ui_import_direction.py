"""Architectural guard: the ``app.ui`` layer must not import feature modules.

``app.ui`` is the shared design-system / presentation layer. Features
(``app.docs``, ``app.chat``, ``app.drafter``, …) compose ``app.ui``
primitives — the dependency arrow points feature→ui. The reverse edge
(ui→feature) couples the design system to one feature's domain logic and
creates import cycles the moment a feature imports a ui primitive that
imports back into that feature.

#860 removed the concrete ``app.ui.primitives.annotation_button`` /
``app.ui.surfaces.annotation_popover`` → ``app.annotations.row_keys`` edge
by relocating ``target_dom_id`` into ``app.ui.dom_ids``. This test pins the
*whole* ui layer so the edge cannot reappear (here or anywhere else under
``app/ui``).

The scan is AST-based and walks *every* import node — including imports
nested inside functions (the lazy-import idiom used in
``app.ui.components.search_routes``) — so a deferred import cannot dodge the
guard.

Relative imports are resolved to their absolute dotted path *before* the
forbidden-roots check, using the importing module's own package as the
anchor (the same algorithm CPython's importmachinery uses). This matters:
a relative import can escape ``app.ui`` entirely — e.g.
``from ...explorer.routes import x`` written inside
``app.ui.components.search_routes`` resolves to ``app.explorer.routes`` —
so skipping ``level > 0`` nodes (the original bug, #890 review) would let a
forbidden edge slip through disguised as a "local" import. Every import,
absolute or relative, is normalised to an absolute path and checked the
same way, and the allowlist is keyed on that resolved path.
"""

from __future__ import annotations

import ast
import textwrap
from pathlib import Path

import pytest

# Feature packages the ui layer must never depend on. Deliberately excludes
# genuinely-shared lower layers (``app.ontology``, ``app.auth``, ``app.db``,
# ``app.config``, ``app.storage``, ``app.llm`` …): those sit *below* ui in the
# dependency stack and are fair game.
FORBIDDEN_ROOTS: frozenset[str] = frozenset(
    {
        "app.annotations",
        "app.docs",
        "app.chat",
        "app.drafter",
        "app.analyysikeskus",
        "app.admin",
        "app.explorer",
        "app.impact",
    }
)

# Pre-existing ui→feature edges that #860 is NOT in scope to remove. Each is a
# ``(module_dotted_path, forbidden_root)`` pair. Documented here with a TODO so
# the guard stays green while making the remaining debt explicit and grep-able.
#
# TODO(#860): app.ui.components.search_routes lazily imports app.explorer.routes
# (``_get_client`` / ``_sanitize_regex``) and app.ontology.queries to back the
# global-search dropdown. ``app.ontology`` is a shared lower layer (not in
# FORBIDDEN_ROOTS), but the explorer.routes dependency is a real ui→feature edge
# that needs its own extraction (move the shared SPARQL client/sanitiser helpers
# out of app.explorer.routes into a neutral module). Out of scope for #860's
# target_dom_id move; allowlisted until then.
ALLOWLIST: frozenset[tuple[str, str]] = frozenset(
    {
        ("app.ui.components.search_routes", "app.explorer"),
    }
)

_UI_ROOT = Path(__file__).resolve().parent.parent / "app" / "ui"


def _module_and_package(py_file: Path) -> tuple[str, str]:
    """Map a file to its ``(module, package)`` dotted paths.

    * ``…/app/ui/primitives/badge.py`` ->
      (``app.ui.primitives.badge``, ``app.ui.primitives``) — a regular module
      anchors relative imports against its parent package.
    * ``…/app/ui/components/__init__.py`` ->
      (``app.ui.components``, ``app.ui.components``) — an ``__init__`` *is* its
      package, so relative imports anchor against itself, not its parent
      (matching CPython).
    """
    repo_root = _UI_ROOT.parent.parent
    rel = py_file.relative_to(repo_root).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        package_parts = parts[:-1]
        module = ".".join(package_parts)
        return module, module
    module = ".".join(parts)
    return module, _package_of(module)


def _forbidden_root_for(name: str | None) -> str | None:
    """Return the forbidden root *name* falls under, else ``None``.

    ``name`` is a dotted import target (``app.explorer.routes`` or, for a
    bare ``import app.explorer``, ``app.explorer``). A target matches a root
    if it equals the root or is a submodule of it (prefix + ``.``), so
    ``app.adminpanel`` does NOT match ``app.admin``.
    """
    if not name:
        return None
    for root in FORBIDDEN_ROOTS:
        if name == root or name.startswith(root + "."):
            return root
    return None


def _package_of(module: str) -> str:
    """Return the package a *module* lives in.

    A module's "package" is its parent dotted path:
    ``app.ui.components.search_routes`` -> ``app.ui.components``. For a
    package's own ``__init__`` (which we represent by the package's dotted
    path, e.g. ``app.ui.components``) the package *is* itself — Python
    anchors relative imports in an ``__init__`` against the package it
    defines, not its parent. We can't distinguish the two from the dotted
    name alone, so the caller passes the package directly when scanning an
    ``__init__`` (see ``_module_and_package`` / ``_iter_resolved_imports``).
    """
    return module.rpartition(".")[0]


def _resolve_relative_import(
    package: str,
    level: int,
    module: str | None,
) -> str | None:
    """Resolve a relative ``ImportFrom`` to its absolute dotted path.

    Mirrors CPython's ``importlib._bootstrap._resolve_name``: starting from
    the importing module's *package*, ``level`` dots walk up one package per
    dot beyond the first (``level=1`` = current package, ``level=2`` = parent,
    …), then ``module`` (the text after the dots, if any) is appended.

    Args:
        package: The package the importing module lives in
            (``app.ui.components`` for ``app.ui.components.search_routes``;
            for an ``__init__`` the package it defines).
        level: ``ast.ImportFrom.level`` — the number of leading dots.
        module: ``ast.ImportFrom.module`` — the dotted text after the dots,
            or ``None`` for ``from . import x`` / ``from .. import x``.

    Returns:
        The absolute dotted path the import targets, or ``None`` if the
        relative import walks above the top-level package (malformed — Python
        would raise ``ImportError`` at import time, and it cannot name a
        feature package anyway).
    """
    if level <= 0:
        # Absolute import: ``module`` is already the absolute path.
        return module
    # ``level`` dots strip ``level - 1`` trailing components off the package.
    bits = package.split(".") if package else []
    if level - 1 > len(bits):
        # Escapes above the project root — not resolvable / not a feature.
        return None
    base = bits[: len(bits) - (level - 1)]
    if module:
        base = base + module.split(".")
    return ".".join(base) if base else None


def _iter_resolved_imports(tree: ast.Module, module: str, package: str) -> list[str]:
    """Every imported module name in *tree*, resolved to an absolute path.

    Walks *every* import node (nested/lazy included). ``ast.Import`` targets
    are always absolute. ``ast.ImportFrom`` targets are resolved against
    *package* via :func:`_resolve_relative_import` so a relative import that
    escapes ``app.ui`` is checked exactly like the equivalent absolute import.

    Args:
        tree: The parsed module AST.
        module: The importing module's own dotted path (for diagnostics).
        package: The package to anchor relative imports against.
    """
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            resolved = _resolve_relative_import(package, node.level, node.module)
            if resolved:
                names.append(resolved)
    return names


_UI_PY_FILES = sorted(_UI_ROOT.rglob("*.py"))


def test_ui_root_has_python_files() -> None:
    """Sanity: the glob actually found ui modules (guards a silent no-op)."""
    assert _UI_PY_FILES, f"no .py files discovered under {_UI_ROOT}"


@pytest.mark.parametrize("py_file", _UI_PY_FILES, ids=lambda p: p.name)
def test_ui_module_does_not_import_feature_package(py_file: Path) -> None:
    """No module under ``app/ui`` may import a feature package (#860).

    Allowlisted, documented pre-existing edges are tolerated; every new edge
    fails the build.
    """
    module, package = _module_and_package(py_file)
    tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))

    violations: list[str] = []
    for imported in _iter_resolved_imports(tree, module, package):
        root = _forbidden_root_for(imported)
        if root is None:
            continue
        if (module, root) in ALLOWLIST:
            continue
        violations.append(f"{module} imports {imported} (forbidden root {root})")

    assert not violations, (
        "app.ui must not import feature modules (ui->feature is the disallowed "
        "direction; see #860):\n  " + "\n  ".join(violations)
    )


def test_allowlist_entries_are_still_real() -> None:
    """An allowlisted edge that no longer exists is stale debt — flag it.

    Keeps ALLOWLIST honest: once the underlying import is removed (e.g. the
    explorer.routes extraction lands), this test fails and forces the entry
    out, so the allowlist never silently hides a now-clean module.
    """
    repo_root = _UI_ROOT.parent.parent
    stale: list[tuple[str, str]] = []
    for module, root in ALLOWLIST:
        py_file = repo_root / Path(*module.split(".")).with_suffix(".py")
        init_file = repo_root / Path(*module.split(".")) / "__init__.py"
        source_path = py_file if py_file.exists() else init_file
        if not source_path.exists():
            stale.append((module, root))
            continue
        _module, package = _module_and_package(source_path)
        tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
        resolved = _iter_resolved_imports(tree, _module, package)
        if not any(_forbidden_root_for(name) == root for name in resolved):
            stale.append((module, root))

    assert not stale, (
        "ALLOWLIST contains entries whose ui->feature import no longer exists "
        "— remove them (the underlying edge is gone):\n  "
        + "\n  ".join(f"{m} -> {r}" for m, r in stale)
    )


# ---------------------------------------------------------------------------
# Resolver unit tests (#890 review): a relative import that escapes app.ui
# must be resolved to its absolute path and caught like any absolute import.
# ---------------------------------------------------------------------------


class TestResolveRelativeImport:
    """``_resolve_relative_import`` mirrors CPython's relative-name resolution."""

    def test_absolute_import_passes_through(self) -> None:
        # level=0 -> the module text is already absolute.
        assert (
            _resolve_relative_import("app.ui.components", 0, "app.explorer.routes")
            == "app.explorer.routes"
        )

    def test_single_dot_with_module_is_current_package(self) -> None:
        # ``from .sibling import x`` in app.ui.surfaces.* -> app.ui.surfaces.sibling
        assert (
            _resolve_relative_import("app.ui.surfaces", 1, "annotation_popover")
            == "app.ui.surfaces.annotation_popover"
        )

    def test_single_dot_no_module_is_the_package_itself(self) -> None:
        # ``from . import dom_ids`` -> the package; the imported *name* (dom_ids)
        # is a member, not part of the module path AST gives us.
        assert _resolve_relative_import("app.ui.surfaces", 1, None) == "app.ui.surfaces"

    def test_two_dots_no_module_is_parent_package(self) -> None:
        # ``from .. import primitives`` in app.ui.surfaces.* -> app.ui
        assert _resolve_relative_import("app.ui.surfaces", 2, None) == "app.ui"

    def test_two_dots_with_module_is_sibling_subpackage(self) -> None:
        # ``from ..primitives import badge`` in app.ui.surfaces.* -> app.ui.primitives
        assert _resolve_relative_import("app.ui.surfaces", 2, "primitives") == "app.ui.primitives"

    def test_three_dots_escapes_app_ui_into_feature_package(self) -> None:
        # THE BUG (#890 review): ``from ...explorer.routes import x`` written in
        # app.ui.components.search_routes resolves OUT of app.ui to a feature.
        # package = app.ui.components, level=3 strips 2 trailing parts -> app,
        # then append explorer.routes -> app.explorer.routes.
        assert (
            _resolve_relative_import("app.ui.components", 3, "explorer.routes")
            == "app.explorer.routes"
        )

    def test_over_deep_relative_import_returns_none(self) -> None:
        # More dots than the package is deep -> walks above the project root.
        # Python would raise ImportError; we treat it as unresolvable (and it
        # cannot name a feature package anyway).
        assert _resolve_relative_import("app.ui", 5, "x") is None


class TestRelativeImportGuard:
    """End-to-end: the AST scan + forbidden-root check on synthetic modules.

    These prove the *whole* pipeline (parse -> resolve -> classify) catches a
    relative escape and stays clean for genuinely-internal relative imports —
    independent of whatever the real files under app/ui happen to contain.
    """

    def _resolved(self, source: str, module: str, package: str) -> list[str]:
        tree = ast.parse(source)
        return _iter_resolved_imports(tree, module, package)

    def _forbidden_hits(self, source: str, module: str, package: str) -> list[str]:
        return [
            name
            for name in self._resolved(source, module, package)
            if _forbidden_root_for(name) is not None
        ]

    def test_relative_escape_into_feature_is_caught(self) -> None:
        # A nested (function-level) relative import that escapes into a feature.
        source = textwrap.dedent(
            """
            def _lazy():
                from ...explorer.routes import _get_client  # escapes app.ui
                return _get_client
            """
        )
        hits = self._forbidden_hits(source, "app.ui.components.search_routes", "app.ui.components")
        assert hits == ["app.explorer.routes"], hits
        assert _forbidden_root_for(hits[0]) == "app.explorer"

    def test_relative_escape_into_annotations_is_caught(self) -> None:
        # The exact edge #860 removed, re-introduced via a relative spelling.
        source = "from ...annotations.row_keys import target_dom_id\n"
        hits = self._forbidden_hits(
            source, "app.ui.primitives.annotation_button", "app.ui.primitives"
        )
        assert hits == ["app.annotations.row_keys"], hits

    def test_internal_single_dot_import_is_clean(self) -> None:
        # ``from . import dom_ids`` stays inside app.ui -> no forbidden hit.
        source = "from . import dom_ids\n"
        assert (
            self._forbidden_hits(source, "app.ui.surfaces.annotation_popover", "app.ui.surfaces")
            == []
        )

    def test_internal_two_dot_import_is_clean(self) -> None:
        # ``from .. import primitives`` -> app.ui.primitives -> not forbidden.
        source = "from .. import primitives\n"
        assert (
            self._forbidden_hits(source, "app.ui.surfaces.annotation_popover", "app.ui.surfaces")
            == []
        )

    def test_internal_two_dot_sibling_subpackage_is_clean(self) -> None:
        # ``from ..primitives.badge import Badge`` -> app.ui.primitives.badge.
        source = "from ..primitives.badge import Badge\n"
        assert (
            self._forbidden_hits(source, "app.ui.surfaces.annotation_popover", "app.ui.surfaces")
            == []
        )

    def test_init_anchors_relative_import_against_itself(self) -> None:
        # In app/ui/components/__init__.py the package IS app.ui.components, so
        # ``from .search_routes import x`` stays internal (anchors on itself,
        # not the parent) — and a 2-dot escape lands one level shallower than
        # the same spelling in a non-__init__ sibling module.
        internal = "from .search_routes import global_search_routes\n"
        assert self._forbidden_hits(internal, "app.ui.components", "app.ui.components") == []
        # ``from ...explorer import routes`` in the __init__ (package
        # app.ui.components): strip 2 -> app, append explorer -> app.explorer.
        escape = "from ...explorer import routes\n"
        hits = self._forbidden_hits(escape, "app.ui.components", "app.ui.components")
        assert hits == ["app.explorer"], hits
