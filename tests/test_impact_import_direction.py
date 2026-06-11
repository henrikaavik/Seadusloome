"""Guard the one-directional dependency of the neutral ``app.impact`` layer (#860).

The impact engine was promoted from ``app/docs/impact`` to top-level
``app/impact`` and the burden/sanctions SPARQL data layers were moved in
alongside it so the analyzer's C6 helpers can use **normal top-level
imports** instead of the old function-local cycle-breaking imports
(``analyysikeskus`` ←→ the impact engine). This test pins the invariants
that keep the cycle from regrowing:

(a) the old ``app/docs/impact`` package path is gone — there must be no
    re-introduced shim there for the cycle to route through;
(b) no module under ``app/impact/`` imports any of the *consumer* layers
    (``app.analyysikeskus`` / ``app.docs`` / ``app.chat`` / ``app.explorer``
    / ``app.drafter`` / ``app.admin``) — ``app.impact`` depends only on the
    neutral foundations (``app.ontology`` / ``app.sync`` / stdlib);
(c) importing ``app.impact.analyzer`` in a *fresh* interpreter succeeds
    without ``app.analyysikeskus`` ever entering ``sys.modules`` — i.e. the
    runtime import cycle is truly gone, not merely papered over by an
    import ordering trick.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

# tests/ -> repo root -> app/impact
_REPO_ROOT = Path(__file__).resolve().parent.parent
_IMPACT_DIR = _REPO_ROOT / "app" / "impact"

# The consumer layers ``app.impact`` must never import from. ``app.impact``
# is the neutral analysis layer: it may depend on ``app.ontology`` /
# ``app.sync`` (foundations) but nothing that sits *above* it in the
# dependency graph, or the cycle this refactor broke would regrow.
_FORBIDDEN_TOP_LEVEL = frozenset(
    {
        "analyysikeskus",
        "docs",
        "chat",
        "explorer",
        "drafter",
        "admin",
    }
)


def _impact_module_paths() -> list[Path]:
    """Every ``.py`` file in the ``app/impact`` package."""
    return sorted(_IMPACT_DIR.rglob("*.py"))


def _module_dotted_name(path: Path) -> str:
    """Dotted module name for an ``app/impact`` ``.py`` *path*.

    ``app/impact/analyzer.py`` → ``app.impact.analyzer``;
    ``app/impact/__init__.py`` → ``app.impact``;
    ``app/impact/sub/mod.py``  → ``app.impact.sub.mod``.

    Used to resolve relative imports (``from ..x import y``) against the
    importing module's own location — the same way CPython does.
    """
    rel = path.resolve().relative_to(_REPO_ROOT).with_suffix("")
    parts = list(rel.parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _resolve_relative(module_dotted: str, level: int, sub_module: str | None) -> str | None:
    """Resolve a relative ``from`` import to its absolute dotted path.

    Mirrors CPython's package-relative resolution:

    * ``level`` counts the leading dots. ``level == 1`` anchors at the
      importing module's **package** (for a module ``a.b.c`` that is
      ``a.b``); each extra dot strips one more trailing package component.
    * ``sub_module`` is the text after the dots (``node.module``), or
      ``None`` for a bare ``from . import x`` / ``from .. import x``.

    Returns the absolute dotted path the import lands on (e.g.
    ``app.docs`` for ``from ..docs import x`` inside ``app.impact.foo``),
    or ``None`` when the relative reference walks above the package root
    (an over-deep ``from ...... import x`` that Python itself would reject)
    — those are reported separately by the caller as escapes.

    Anchoring rule: the importing module's package is *all parts except
    the last* (``app.impact.foo`` → package ``app.impact``). ``level``
    dots beyond the first each drop one more trailing component, so for
    ``app.impact.foo``: level 1 → ``app.impact``, level 2 → ``app``,
    level 3 → ``""`` (above root → ``None``).
    """
    package_parts = module_dotted.split(".")[:-1]
    # The first dot anchors at the package itself; each additional dot
    # ascends one more level.
    ascend = level - 1
    if ascend > len(package_parts):
        return None  # escapes above the top-level root
    base_parts = package_parts[: len(package_parts) - ascend] if ascend else package_parts
    if sub_module:
        base_parts = base_parts + sub_module.split(".")
    return ".".join(base_parts)


def _forbidden_imports_in(path: Path) -> list[str]:
    """Return forbidden import targets found in *path* (AST scan).

    Walks both ``import a.b.c`` and ``from a.b import c`` statements —
    including function-local ones (we ``ast.walk`` the whole tree, not
    just the module body) and **relative** imports — and flags any whose
    *resolved* absolute dotted path lands on a forbidden consumer layer.

    Relative imports are resolved against this module's own dotted name
    (the way CPython resolves them at runtime) before the forbidden-root
    check, so ``from ..docs import x`` inside ``app/impact/analyzer.py``
    is caught exactly like the absolute ``import app.docs`` would be. A
    relative import that escapes ``app.impact`` entirely (resolves to a
    sibling top-level package, or above the repo root) is itself reported
    — the neutral layer has no business reaching outside its own package
    by a relative path.
    """
    module_dotted = _module_dotted_name(path)
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                _record_if_forbidden(alias.name, offenders)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                resolved = _resolve_relative(module_dotted, node.level, node.module)
                _record_relative(module_dotted, node, resolved, offenders)
            elif node.module:
                _record_if_forbidden(node.module, offenders)
    return offenders


def _record_if_forbidden(dotted: str, offenders: list[str]) -> None:
    parts = dotted.split(".")
    if len(parts) >= 2 and parts[0] == "app" and parts[1] in _FORBIDDEN_TOP_LEVEL:
        offenders.append(dotted)


def _record_relative(
    module_dotted: str,
    node: ast.ImportFrom,
    resolved: str | None,
    offenders: list[str],
) -> None:
    """Flag a resolved relative import if it hits a consumer layer or escapes.

    A relative import inside ``app.impact`` is fine only while it stays
    *within* ``app.impact``. If it resolves onto a forbidden consumer
    root it is recorded with the same rule as an absolute import; if it
    resolves anywhere else outside ``app.impact`` (a sibling top-level
    package, or above the repo root) it is recorded as an escape — the
    stricter rule, since the neutral layer should never reach a sibling
    by a relative path.
    """
    dots = "." * node.level
    raw = f"{dots}{node.module or ''}"
    if resolved is None:
        offenders.append(f"{raw} (escapes above package root)")
        return
    if resolved == "app.impact" or resolved.startswith("app.impact."):
        return  # stays inside the neutral layer — allowed
    parts = resolved.split(".")
    if len(parts) >= 2 and parts[0] == "app" and parts[1] in _FORBIDDEN_TOP_LEVEL:
        offenders.append(f"{raw} -> {resolved}")
    else:
        offenders.append(f"{raw} -> {resolved} (relative import escapes app.impact)")


def test_docs_impact_package_is_gone() -> None:
    """The old ``app/docs/impact`` path must not exist (no cycle re-entry point)."""
    legacy = _REPO_ROOT / "app" / "docs" / "impact"
    assert not legacy.exists(), (
        f"{legacy} still exists — the impact engine was promoted to app/impact and "
        "the old path must die so the analyysikeskus import cycle cannot regrow."
    )


def test_impact_package_has_modules() -> None:
    """Sanity guard so the AST scan below isn't vacuously green on an empty dir."""
    paths = _impact_module_paths()
    names = {p.name for p in paths}
    # The promoted engine + the moved data layers.
    for expected in ("analyzer.py", "scoring.py", "queries.py", "burden.py", "sanctions.py"):
        assert expected in names, f"expected app/impact/{expected} to exist; got {sorted(names)}"


@pytest.mark.parametrize("module_path", _impact_module_paths(), ids=lambda p: p.name)
def test_impact_module_has_no_consumer_layer_imports(module_path: Path) -> None:
    """No module under app/impact imports a consumer layer (one-directional)."""
    offenders = _forbidden_imports_in(module_path)
    assert not offenders, (
        f"{module_path.relative_to(_REPO_ROOT)} imports forbidden consumer layer(s): "
        f"{offenders}. app.impact is the neutral layer — consumers (analyysikeskus, "
        "docs, chat, explorer, drafter, admin) import FROM it, never the reverse."
    )


# ---------------------------------------------------------------------------
# Resolver unit tests — prove the relative-import resolution actually catches
# escapes (the P2 the original guard missed: ``if node.level: continue``).
# ---------------------------------------------------------------------------


def test_resolve_relative_one_dot_anchors_at_package() -> None:
    """``from . import x`` inside app.impact.foo resolves to app.impact.x."""
    assert _resolve_relative("app.impact.analyzer", 1, "queries") == "app.impact.queries"
    # Bare ``from . import x`` (module is None) resolves to the package itself.
    assert _resolve_relative("app.impact.analyzer", 1, None) == "app.impact"


def test_resolve_relative_two_dots_escapes_to_sibling() -> None:
    """``from ..docs import x`` inside app.impact.foo resolves to app.docs."""
    assert _resolve_relative("app.impact.analyzer", 2, "docs") == "app.docs"
    assert _resolve_relative("app.impact.analyzer", 2, "chat.tools") == "app.chat.tools"


def test_resolve_relative_nested_module_depth() -> None:
    """Depth accounting holds for a nested module app.impact.sub.mod."""
    # one dot → its own package app.impact.sub
    assert _resolve_relative("app.impact.sub.mod", 1, "sib") == "app.impact.sub.sib"
    # two dots → app.impact
    assert _resolve_relative("app.impact.sub.mod", 2, "queries") == "app.impact.queries"
    # three dots → app, then +docs → app.docs (escape to sibling layer)
    assert _resolve_relative("app.impact.sub.mod", 3, "docs") == "app.docs"


def test_resolve_relative_above_root_is_none() -> None:
    """An over-deep relative import that walks above the repo root → None."""
    # app.impact.analyzer: package app.impact (2 parts). level 4 ascends 3,
    # which exceeds the 2 package parts → None.
    assert _resolve_relative("app.impact.analyzer", 4, "x") is None


def _scan_source(tmp_path: Path, rel_subpath: str, source: str) -> list[str]:
    """Write *source* at ``tmp_path/<rel_subpath>`` and scan it.

    *rel_subpath* is laid out under a synthetic repo root so that
    ``_module_dotted_name`` (which strips ``_REPO_ROOT`` then turns the
    path into a dotted name) computes the right importing-module name.
    We monkeypatch the module-level ``_REPO_ROOT`` so the fake tree's
    ``app/impact/...`` resolves to ``app.impact.*`` exactly like the real
    one.
    """
    import tests.test_impact_import_direction as mod  # self

    target = tmp_path / rel_subpath
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(source, encoding="utf-8")
    original_root = mod._REPO_ROOT
    mod._REPO_ROOT = tmp_path.resolve()
    try:
        return _forbidden_imports_in(target)
    finally:
        mod._REPO_ROOT = original_root


def test_relative_import_to_consumer_layer_is_caught(tmp_path: Path) -> None:
    """``from ..docs import x`` inside app/impact/* is flagged (the missed P2)."""
    offenders = _scan_source(
        tmp_path,
        "app/impact/analyzer.py",
        "from ..docs import analyze_handler\n",
    )
    assert offenders, "relative import to app.docs must be caught"
    assert any("app.docs" in o for o in offenders), offenders


def test_deep_relative_import_to_chat_is_caught(tmp_path: Path) -> None:
    """``from ...chat import y`` from a nested app/impact module is flagged."""
    offenders = _scan_source(
        tmp_path,
        "app/impact/sub/mod.py",
        "from ...chat import tools\n",
    )
    # app.impact.sub.mod, level 3 → app, +chat → app.chat (forbidden).
    assert any("app.chat" in o for o in offenders), offenders


def test_relative_import_to_sibling_app_package_is_flagged_as_escape(tmp_path: Path) -> None:
    """A relative escape to a non-forbidden sibling (e.g. app.ontology) still fails.

    The stricter rule: the neutral layer should never reach a sibling
    top-level package by a *relative* path, even an otherwise-allowed one
    like ``app.ontology`` (which is fine as an absolute import). This
    keeps the guard from quietly allowing ``from ..ontology import x`` as
    a backdoor shaped like an internal import.
    """
    offenders = _scan_source(
        tmp_path,
        "app/impact/analyzer.py",
        "from ..ontology import relations\n",
    )
    assert any("escapes app.impact" in o for o in offenders), offenders


def test_internal_relative_import_passes(tmp_path: Path) -> None:
    """``from . import queries`` / ``from .queries import X`` stays clean."""
    for source in (
        "from . import queries\n",
        "from .queries import PREFIXES\n",
        "from .sub.mod import thing\n",
    ):
        offenders = _scan_source(tmp_path, "app/impact/analyzer.py", source)
        assert offenders == [], (
            f"internal relative import wrongly flagged: {source!r} -> {offenders}"
        )


def test_absolute_imports_still_classified_correctly(tmp_path: Path) -> None:
    """Absolute imports keep their original behaviour after the refactor."""
    # Allowed foundations pass.
    assert (
        _scan_source(tmp_path, "app/impact/analyzer.py", "import app.ontology.relations\n") == []
    )
    assert (
        _scan_source(tmp_path, "app/impact/analyzer.py", "from app.sync import jena_loader\n")
        == []
    )
    # Forbidden consumer absolute import is caught.
    caught = _scan_source(
        tmp_path, "app/impact/analyzer.py", "from app.analyysikeskus import burden\n"
    )
    assert any("app.analyysikeskus" in o for o in caught), caught


def test_importing_analyzer_does_not_pull_in_analyysikeskus() -> None:
    """``import app.impact.analyzer`` in a fresh interpreter must not load analyysikeskus.

    Run in a subprocess so the assertion sees a pristine ``sys.modules`` —
    the parent test process has almost certainly already imported
    ``app.analyysikeskus`` transitively, which would mask a regrown cycle.
    """
    code = (
        "import sys\n"
        "import app.impact.analyzer  # noqa: F401\n"
        "leaked = [m for m in sys.modules if m == 'app.analyysikeskus' "
        "or m.startswith('app.analyysikeskus.')]\n"
        "assert not leaked, "
        "'app.impact.analyzer pulled in analyysikeskus (cycle regrown): %r' % leaked\n"
        "print('OK')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        "fresh-subprocess import of app.impact.analyzer failed or leaked analyysikeskus.\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "OK" in result.stdout
