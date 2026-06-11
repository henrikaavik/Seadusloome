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
"""

from __future__ import annotations

import ast
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


def _module_dotted_path(py_file: Path) -> str:
    """``…/app/ui/primitives/badge.py`` -> ``app.ui.primitives.badge``."""
    repo_root = _UI_ROOT.parent.parent
    rel = py_file.relative_to(repo_root).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


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


def _iter_imported_names(tree: ast.Module) -> list[str]:
    """Every dotted module name imported anywhere in *tree* (nested included)."""
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            # Skip relative imports (level > 0): they resolve within app.ui by
            # construction and cannot point at a sibling feature package.
            if node.level == 0 and node.module:
                names.append(node.module)
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
    module = _module_dotted_path(py_file)
    tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))

    violations: list[str] = []
    for imported in _iter_imported_names(tree):
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
        tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
        if not any(_forbidden_root_for(name) == root for name in _iter_imported_names(tree)):
            stale.append((module, root))

    assert not stale, (
        "ALLOWLIST contains entries whose ui->feature import no longer exists "
        "— remove them (the underlying edge is gone):\n  "
        + "\n  ".join(f"{m} -> {r}" for m, r in stale)
    )
