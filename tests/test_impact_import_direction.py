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


def _forbidden_imports_in(path: Path) -> list[str]:
    """Return ``app.<forbidden>`` import targets found in *path* (AST scan).

    Walks both ``import a.b.c`` and ``from a.b import c`` statements,
    including function-local ones (we ``ast.walk`` the whole tree, not just
    the module body), and flags any whose dotted path starts with
    ``app.<forbidden>``.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                _record_if_forbidden(alias.name, offenders)
        elif isinstance(node, ast.ImportFrom):
            # ``from . import x`` (level > 0, module is None/relative) stays
            # inside the package and is never a cross-layer dependency.
            if node.level and not node.module:
                continue
            if node.level:
                # A relative import never reaches a sibling top-level
                # package like ``app.docs`` — it resolves within ``app.impact``.
                continue
            if node.module:
                _record_if_forbidden(node.module, offenders)
    return offenders


def _record_if_forbidden(dotted: str, offenders: list[str]) -> None:
    parts = dotted.split(".")
    if len(parts) >= 2 and parts[0] == "app" and parts[1] in _FORBIDDEN_TOP_LEVEL:
        offenders.append(dotted)


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
