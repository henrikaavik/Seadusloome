"""Enforce the framework-free contract for ``app/analyysikeskus/services/``.

The service layer (#860) is the Phase-5 REST/MCP reference pattern: plain
``input → typed result`` functions with **zero** web-framework coupling, so
the same function can be wrapped by a FastHTML route, a REST endpoint, or an
MCP tool without dragging Starlette/FastHTML into the call.

This test AST-scans every module in the services package and fails if any of
them import ``fasthtml`` / ``starlette`` (or a submodule thereof). It also
asserts the two reference services return their typed dataclasses (never an
HTTP response / FT node) for the unresolved branch, which needs no Jena.

Fresh-subprocess runtime tests then pin what the AST scan cannot see: the
package ``__init__`` resolves its UI exports lazily (PEP 562), so importing
``app.analyysikeskus`` — or only the ``services/`` subpackage — never loads
the package's own ``routes`` / ``result_shell`` FastHTML layer **and** loads
zero ``fasthtml`` / ``starlette`` modules at all. The residual transitive
framework floor that once leaked under the services (via ``app.docs``'s eager
``__init__`` → ``app.auth`` and ``app.metrics``'s starlette middleware imports)
was removed in #895 — the runtime tests below now assert zero framework, not a
floor.
"""

from __future__ import annotations

import ast
import pkgutil
import subprocess
import sys
from pathlib import Path

import app.analyysikeskus.services as services_pkg

_FORBIDDEN_ROOTS = {"fasthtml", "starlette"}

# tests/ -> repo root (anchors the fresh-subprocess imports below).
_REPO_ROOT = Path(__file__).resolve().parent.parent


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


# ---------------------------------------------------------------------------
# Runtime import-direction tests (subprocess) — the AST scan above only proves
# the service *source files* carry no direct ``fasthtml`` / ``starlette``
# import. These pin the *runtime* invariant: importing the package — or only
# the framework-free ``services/`` subpackage — loads zero ``fasthtml`` /
# ``starlette`` modules. That rests on two fixes: #860/#894 made the package
# ``__init__`` resolve its public exports lazily (PEP 562) so it never drags
# its own FastHTML UI layer (``routes`` / ``result_shell``) into
# ``sys.modules``, and #895 sealed the transitive leaks the services reached
# through their neutral dependencies (metrics middleware split out of
# ``app.metrics``; lazy ``app.auth`` init; ``BadgeVariant`` type-only in
# ``app.docs.status``). The UI-layer named-module checks are kept alongside the
# bare zero-framework count because they localize a regression faster. Run in
# fresh subprocesses so the assertions see a pristine ``sys.modules`` (the
# parent test process has long since imported fasthtml + the UI layer
# transitively, which would mask any regression).
# ---------------------------------------------------------------------------


def test_package_init_loads_no_framework() -> None:
    """``import app.analyysikeskus`` must pull in no framework and no UI layer.

    The package ``__init__`` exports ``analysis_result_shell`` and
    ``register_analyysikeskus_routes`` *lazily* (PEP 562 ``__getattr__``)
    rather than eagerly ``from app.analyysikeskus.{routes,result_shell}
    import …`` at module scope. An eager import would load the FastHTML UI
    layer — and therefore ``fasthtml`` / ``starlette`` — the moment anyone
    did ``import app.analyysikeskus`` (e.g. transitively via the neutral
    ``app.analyysikeskus.eu_transposition`` helper that
    ``app.dashboard.service`` consumes), silently defeating the service
    layer's framework-free design.

    The two lazy exports must still resolve and be callable — that is what
    keeps ``from app.analyysikeskus import register_analyysikeskus_routes``
    in ``app/main.py`` working.
    """
    code = (
        "import sys\n"
        "import app.analyysikeskus  # noqa: F401  -- runs the package __init__\n"
        "framework = sorted(m for m in sys.modules "
        "if m.startswith(('fasthtml', 'starlette')))\n"
        "assert not framework, "
        "'import app.analyysikeskus pulled in a web framework: ' + repr(framework) + "
        "' -- the package __init__ must export routes/result_shell lazily "
        "(PEP 562 __getattr__)'\n"
        "ui = [m for m in ('app.analyysikeskus.routes', "
        "'app.analyysikeskus.result_shell') if m in sys.modules]\n"
        "assert not ui, "
        "'import app.analyysikeskus eagerly loaded its FastHTML UI layer: ' + repr(ui)\n"
        "from app.analyysikeskus import register_analyysikeskus_routes\n"
        "from app.analyysikeskus import analysis_result_shell\n"
        "assert callable(register_analyysikeskus_routes), "
        "'lazy export register_analyysikeskus_routes must resolve and be callable'\n"
        "assert callable(analysis_result_shell), "
        "'lazy export analysis_result_shell must resolve and be callable'\n"
        "print('OK')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        "fresh-subprocess import of app.analyysikeskus eagerly loaded a web "
        "framework / its UI layer, or a lazy export failed to resolve.\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "OK" in result.stdout


def test_importing_services_loads_no_framework() -> None:
    """``import app.analyysikeskus.services`` must load zero framework — full guarantee.

    The full framework-free guarantee now holds for the service subpackage: a
    fresh ``import app.analyysikeskus.services`` pulls **no** ``fasthtml`` /
    ``starlette`` module into ``sys.modules``. Two independent fixes make this
    true:

    * #894 — the package ``__init__`` exports its UI siblings (``routes`` /
      ``result_shell``) lazily (PEP 562 ``__getattr__``), so importing the
      service layer never drags in ``app.analyysikeskus``'s *own* FastHTML UI.
    * #895 — the transitive leaks the services reached through their neutral
      dependencies were sealed: the ``MetricsMiddleware`` was split out of
      ``app.metrics`` (no more starlette floor under ``app.ontology.sparql_client``);
      ``app.auth.__init__`` went lazy (PEP 562); and ``BadgeVariant`` is now a
      ``TYPE_CHECKING``-only import in ``app.docs.status`` — so the service
      modules' ``app.docs.reference_resolver`` import no longer loads
      ``fasthtml``.

    The zero-framework assertion is the real contract; the UI-layer assertions
    (``routes`` / ``result_shell`` absent) are kept alongside it because they
    localize a regression faster — if a future change reintroduces an eager UI
    export in the package ``__init__``, the named-module check points straight
    at it, whereas the bare framework count only says "something leaked".

    Run in a fresh subprocess so the assertions see a pristine ``sys.modules``
    — the parent test process has long since imported fasthtml + the UI layer
    transitively, which would mask any regression.
    """
    code = (
        "import sys\n"
        "import app.analyysikeskus.services  # noqa: F401\n"
        "framework = sorted(m for m in sys.modules "
        "if m.startswith(('fasthtml', 'starlette')))\n"
        "assert not framework, "
        "'import app.analyysikeskus.services pulled in a web framework: ' + repr(framework) + "
        "' -- the service layer must stay framework-free (PEP 562 lazy UI exports "
        "in the package __init__, plus the upstream leaks sealed in #895)'\n"
        "ui = [m for m in ('app.analyysikeskus.routes', "
        "'app.analyysikeskus.result_shell') if m in sys.modules]\n"
        "assert not ui, "
        "'import app.analyysikeskus.services loaded the package UI layer: ' + repr(ui) + "
        "' -- the package __init__ must export routes/result_shell lazily "
        "(PEP 562 __getattr__) so the framework-free service layer never drags "
        "in its FastHTML UI siblings'\n"
        "print('OK')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        "fresh-subprocess import of app.analyysikeskus.services loaded a web "
        "framework or the package's own FastHTML UI layer (routes / result_shell).\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "OK" in result.stdout
