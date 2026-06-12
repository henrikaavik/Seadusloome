"""Guard the framework-free contract of the ``app.dashboard.service`` data layer (#860).

The Töölaud page was relocated out of the cross-cutting ``app/templates/``
aggregator into the ``app/dashboard`` feature module with a clean layering
split:

    - ``app.dashboard.service`` — the data layer: every ``_connect()`` /
      SPARQL widget query + bookmark CRUD as ``inputs → rows`` functions.
    - ``app.dashboard.pages``   — the FastHTML rendering + route layer that
      consumes the service functions.

The service layer must stay **framework-free** so the Phase-5 public API +
MCP server (CLAUDE.md "Internal service functions ... wrappable as both REST
endpoints and MCP tools") can import it without dragging in the web stack.
This test pins that invariant the same way ``tests/test_ui_import_direction``
pins the ui layer's: an **AST scan** of ``app/dashboard/service.py`` (walking
*every* import node — nested + relative) asserts the module's own source
imports no ``fasthtml`` / ``starlette``. The page layer
(``app.dashboard.pages``) is deliberately NOT scanned: it is the presentation
layer and is *expected* to import the framework.

The contract is the **source import surface**, not the full runtime transitive
closure. A runtime ``sys.modules`` check (like
``tests/test_impact_import_direction``'s) is intentionally *not* used here:
``app.dashboard.service`` depends on the neutral data helper
``app.analyysikeskus.eu_transposition``, but importing any
``app.analyysikeskus`` submodule executes that package's ``__init__`` — which
eagerly imports its FastHTML UI siblings (``result_shell`` / ``routes``). That
package-init coupling is an ``app.analyysikeskus`` concern, out of scope for
this relocation; it does not make ``service.py`` itself framework-coupled. The
AST guard is the meaningful, enforceable boundary: ``service.py`` never *writes*
a framework import, so the Phase-5 wrapper can call its functions without the
service code reaching into ``fasthtml`` / ``starlette`` directly.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

# tests/ -> repo root -> app/dashboard/service.py
_REPO_ROOT = Path(__file__).resolve().parent.parent
_SERVICE_FILE = _REPO_ROOT / "app" / "dashboard" / "service.py"

# Web-framework roots the data layer must never import. ``app.dashboard.service``
# may depend on the neutral lower layers (``app.db``, ``app.analyysikeskus``'s
# SPARQL helper, …) but nothing from the presentation stack.
_FORBIDDEN_FRAMEWORK_ROOTS: frozenset[str] = frozenset(
    {
        "fasthtml",
        "starlette",
    }
)


def _module_dotted_name(path: Path) -> str:
    """Dotted module name for a repo ``.py`` *path* (anchors relative imports)."""
    rel = path.resolve().relative_to(_REPO_ROOT).with_suffix("")
    parts = list(rel.parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _resolve_relative(module_dotted: str, level: int, sub_module: str | None) -> str | None:
    """Resolve a relative ``from`` import to its absolute dotted path.

    Mirrors CPython's package-relative resolution: ``level`` counts leading
    dots (``level == 1`` anchors at the importing module's package; each extra
    dot strips one more trailing component), then ``sub_module`` (the text
    after the dots) is appended. Returns ``None`` when the reference walks
    above the repo root.
    """
    package_parts = module_dotted.split(".")[:-1]
    ascend = level - 1
    if ascend > len(package_parts):
        return None
    base_parts = package_parts[: len(package_parts) - ascend] if ascend else package_parts
    if sub_module:
        base_parts = base_parts + sub_module.split(".")
    return ".".join(base_parts)


def _forbidden_root_for(dotted: str | None) -> str | None:
    """Return the framework root *dotted* falls under, else ``None``.

    A target matches a root if it equals the root or is a submodule of it
    (prefix + ``.``), so ``starlette_extras`` does NOT match ``starlette``.
    """
    if not dotted:
        return None
    for root in _FORBIDDEN_FRAMEWORK_ROOTS:
        if dotted == root or dotted.startswith(root + "."):
            return root
    return None


def _forbidden_imports_in(path: Path) -> list[str]:
    """Return forbidden framework import targets found in *path* (AST scan).

    Walks every ``import a.b`` and ``from a.b import c`` node — including
    function-local ones and relative imports — and flags any whose resolved
    absolute dotted path lands on a forbidden framework root.
    """
    module_dotted = _module_dotted_name(path)
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _forbidden_root_for(alias.name):
                    offenders.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                resolved = _resolve_relative(module_dotted, node.level, node.module)
                if _forbidden_root_for(resolved):
                    offenders.append(f"{'.' * node.level}{node.module or ''} -> {resolved}")
            elif _forbidden_root_for(node.module):
                offenders.append(node.module or "")
    return offenders


def test_service_file_exists() -> None:
    """Sanity guard so the AST scan below isn't vacuously green on a missing file."""
    assert _SERVICE_FILE.exists(), (
        f"{_SERVICE_FILE} not found — the dashboard data layer must live there (#860)."
    )


def test_service_layer_imports_no_web_framework() -> None:
    """``app.dashboard.service`` must not import ``fasthtml`` / ``starlette`` (#860).

    The data layer is framework-free so it can be wrapped as a REST/MCP tool
    in Phase 5 without pulling in the web stack. The rendering + route layer
    (``app.dashboard.pages``) is where the framework belongs.
    """
    offenders = _forbidden_imports_in(_SERVICE_FILE)
    assert not offenders, (
        "app.dashboard.service must stay framework-free but imports: "
        f"{offenders}. Move framework-coupled code into app.dashboard.pages "
        "(the presentation layer)."
    )


def test_package_init_does_not_eagerly_import_pages() -> None:
    """``import app.dashboard`` (the package) must not pull in the FastHTML page layer.

    The package ``__init__`` exports ``register_dashboard_routes`` *lazily*
    (PEP 562 ``__getattr__``) rather than ``from app.dashboard.pages import
    register_dashboard_routes`` at module scope. An eager import would load
    :mod:`app.dashboard.pages` — and therefore ``fasthtml`` — the moment anyone
    did ``import app.dashboard.service`` (which first runs the package init),
    silently defeating the service layer's framework-free design.

    Run in a subprocess so the assertion sees a pristine ``sys.modules`` — the
    parent test process has almost certainly already imported ``fasthtml`` and
    ``app.dashboard.pages`` transitively, which would mask a regression here.
    """
    code = (
        "import sys\n"
        "import app.dashboard  # noqa: F401  -- runs the package __init__\n"
        "leaked = [m for m in sys.modules if m == 'app.dashboard.pages']\n"
        "assert not leaked, "
        "'app.dashboard.__init__ eagerly imported app.dashboard.pages "
        "(framework layer) — make the export lazy (PEP 562 __getattr__)'\n"
        "from app.dashboard import register_dashboard_routes\n"
        "assert callable(register_dashboard_routes), 'lazy export must still resolve'\n"
        "print('OK')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        "fresh-subprocess import of app.dashboard eagerly loaded the page layer "
        "or the lazy export failed to resolve.\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "OK" in result.stdout


# ---------------------------------------------------------------------------
# Resolver / classifier unit tests — prove the AST scan actually catches a
# framework import (absolute, relative, and nested) and stays clean otherwise.
# ---------------------------------------------------------------------------


def _scan_source(
    tmp_path: Path, source: str, rel_subpath: str = "app/dashboard/service.py"
) -> list[str]:
    """Write *source* under a synthetic repo root and scan it.

    Monkeypatches the module-level ``_REPO_ROOT`` so the fake tree's
    ``app/dashboard/service.py`` resolves to ``app.dashboard.service`` exactly
    like the real one (needed for correct relative-import anchoring).
    """
    import tests.test_dashboard_import_direction as mod  # self

    target = tmp_path / rel_subpath
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(source, encoding="utf-8")
    original_root = mod._REPO_ROOT
    mod._REPO_ROOT = tmp_path.resolve()
    try:
        return _forbidden_imports_in(target)
    finally:
        mod._REPO_ROOT = original_root


def test_absolute_fasthtml_import_is_caught(tmp_path: Path) -> None:
    assert _scan_source(tmp_path, "from fasthtml.common import *\n")


def test_absolute_starlette_import_is_caught(tmp_path: Path) -> None:
    assert _scan_source(tmp_path, "from starlette.responses import JSONResponse\n")


def test_bare_import_fasthtml_is_caught(tmp_path: Path) -> None:
    assert _scan_source(tmp_path, "import fasthtml\n")


def test_nested_function_local_framework_import_is_caught(tmp_path: Path) -> None:
    source = "def f():\n    from starlette.requests import Request\n    return Request\n"
    assert _scan_source(tmp_path, source)


def test_neutral_imports_pass(tmp_path: Path) -> None:
    for source in (
        "from app.db import get_connection as _connect\n",
        "from app.analyysikeskus.eu_transposition import TranspositionDeadlineRow\n",
        "import logging\n",
        "from concurrent.futures import ThreadPoolExecutor\n",
    ):
        assert _scan_source(tmp_path, source) == [], source


def test_lookalike_module_is_not_a_false_positive(tmp_path: Path) -> None:
    """``starlette_extras`` is a different package — must not match ``starlette``."""
    assert _scan_source(tmp_path, "import starlette_extras\n") == []
