"""Pin the post-#860 patch-path contract for the Analüüsikeskus routes package.

After #860 split ``app/analyysikeskus/routes.py`` into a ``routes/`` package
(``_common`` + one submodule per workflow), ``app.analyysikeskus.routes``
re-exports the moved symbols for direct-import convenience — but a
``patch("app.analyysikeskus.routes.X")`` only rebinds the package-level alias,
NOT the bindings inside submodules that imported the symbol at module load
time. This is the standard Python "patch where used, not where defined" rule,
the same contract pinned for ``app/docs/routes/`` in
``tests/test_docs_routes_patch_paths.py``.

These tests pin both halves of the contract:

1. Direct imports from ``app.analyysikeskus.routes`` keep returning the moved
   symbol (back-compat for callers that import, not patch).
2. Patching ``app.analyysikeskus.routes.<symbol>`` does NOT affect a
   submodule's internal call site — submodules use their own module globals.
3. Patching ``app.analyysikeskus.routes.<submodule>.<symbol>`` DOES intercept
   that submodule's call (the canonical "patch where used" path the project's
   test suite now uses for every Analüüsikeskus workflow).

Covered:
* ``_common._rag_candidates`` re-bound into ``_sanktsioonid`` (a
  cross-workflow helper shared by every reference-driven workflow).
* ``_common._Scope`` (a class shared by all eleven workflows).
* An engine dependency imported straight into a workflow submodule —
  ``_sanktsioonid.list_sanctions_for_provision`` — to pin the contract for the
  "imported dependency" category of patch target.
"""

from __future__ import annotations

from unittest.mock import patch

from app.analyysikeskus.routes import _common, _sanktsioonid
from app.analyysikeskus.routes._sanktsioonid import (
    list_sanctions_for_provision as _sanctions_dep_imported_at_load,
)

# ---------------------------------------------------------------------------
# Shared-helper contract: _common._rag_candidates via _sanktsioonid
# ---------------------------------------------------------------------------


def test_direct_import_from_package_returns_moved_symbol() -> None:
    """``from app.analyysikeskus.routes import _rag_candidates`` still works."""
    from app.analyysikeskus.routes import _rag_candidates

    assert _rag_candidates is _common._rag_candidates


def test_sanktsioonid_owns_its_local_rag_binding() -> None:
    """``_sanktsioonid`` imported ``_rag_candidates`` at load time; its function
    bodies resolve the name through the submodule-local global."""
    assert _sanktsioonid._rag_candidates is _common._rag_candidates


def test_package_level_patch_does_not_reach_submodule_globals() -> None:
    """``patch("app.analyysikeskus.routes._rag_candidates")`` only rebinds the
    package alias; the submodule's binding stays original.

    This is the failure mode behind the 42 test breakages during the #860
    split — workflow tests patched the package alias, which no longer reaches
    the per-workflow handlers. Pinned so future extractions don't regress.
    """

    def stub(*_args, **_kwargs):  # pragma: no cover — only invoked on failure
        return [{"label": "X", "ref": "X"}]

    with patch("app.analyysikeskus.routes._rag_candidates", stub):
        from app.analyysikeskus.routes import _rag_candidates as patched_pkg
        from app.analyysikeskus.routes._sanktsioonid import (
            _rag_candidates as patched_in_submodule,
        )

        assert patched_pkg is stub, "package-level binding should be the stub"
        assert patched_in_submodule is _common._rag_candidates, (
            "submodule binding must NOT see the package-level patch — "
            "this is the documented post-#860 contract"
        )


def test_submodule_patch_intercepts_submodule_callers() -> None:
    """Patching where the symbol is USED (inside ``_sanktsioonid``) DOES
    intercept the handler's internal call. Canonical "patch where used"."""

    def stub(*_args, **_kwargs):  # pragma: no cover — only invoked on failure
        return [{"label": "X", "ref": "X"}]

    with patch("app.analyysikeskus.routes._sanktsioonid._rag_candidates", stub):
        from app.analyysikeskus.routes._sanktsioonid import (
            _rag_candidates as patched_in_submodule,
        )

        assert patched_in_submodule is stub, (
            "submodule-local binding must reflect the submodule-targeted patch"
        )


# ---------------------------------------------------------------------------
# _Scope is re-exported and identical across the package + submodules
# ---------------------------------------------------------------------------


def test_scope_class_is_shared_singleton_across_namespaces() -> None:
    """``_Scope`` is defined once in ``_common`` and re-exported unchanged;
    direct imports from the package and from a submodule are the same object."""
    from app.analyysikeskus.routes import _Scope as scope_from_pkg
    from app.analyysikeskus.routes._sanktsioonid import _Scope as scope_from_submodule

    assert scope_from_pkg is _common._Scope
    assert scope_from_submodule is _common._Scope


# ---------------------------------------------------------------------------
# Imported-dependency contract: _sanktsioonid.list_sanctions_for_provision
# ---------------------------------------------------------------------------


def test_sanctions_dep_owns_its_local_binding() -> None:
    """``_sanktsioonid`` imports ``list_sanctions_for_provision`` from the
    ``sanctions`` engine at load time; the submodule-local global is what its
    handler calls."""
    from app.analyysikeskus.sanctions import list_sanctions_for_provision as canonical

    assert _sanctions_dep_imported_at_load is canonical
    assert _sanktsioonid.list_sanctions_for_provision is canonical


def test_package_alias_does_not_expose_engine_dep() -> None:
    """The package alias does NOT re-export engine dependencies.

    The pre-#860 monolith imported ``list_sanctions_for_provision`` at module
    top level, so ``patch("app.analyysikeskus.routes.list_sanctions_for_provision")``
    used to resolve (and silently rebind only the alias). The split package
    re-exports each submodule's *defined* public names but not the engine
    symbols a workflow merely *imports*, so the old package-level patch path
    now raises ``AttributeError`` — a hard, loud signal to switch to the
    submodule path rather than a silent no-op.
    """
    import app.analyysikeskus.routes as pkg

    assert not hasattr(pkg, "list_sanctions_for_provision"), (
        "engine deps must not be re-exported at the package level — patch the "
        "owning submodule (app.analyysikeskus.routes._sanktsioonid.…) instead"
    )


def test_sanctions_dep_submodule_patch_intercepts() -> None:
    """Patching the submodule path DOES intercept the handler's call —
    the path every ``test_analyysikeskus_sanctions.py`` test now uses."""

    def stub(*_args, **_kwargs):  # pragma: no cover — only invoked on failure
        return []

    with patch("app.analyysikeskus.routes._sanktsioonid.list_sanctions_for_provision", stub):
        from app.analyysikeskus.routes._sanktsioonid import (
            list_sanctions_for_provision as patched_in_submodule,
        )

        assert patched_in_submodule is stub


# ---------------------------------------------------------------------------
# Service-layer contract (#860 DoD item 3): orchestration deps moved into the
# framework-free services, so the "patch where used" target moves with them.
# ---------------------------------------------------------------------------


def test_normi_orchestration_dep_lives_in_the_service_not_the_route() -> None:
    """``run_adhoc_impact_analysis`` is called by the Normi *service*, not the
    route — so the route submodule no longer binds it, and the service module
    does. Tests intercept it at the canonical engine home or the service path.
    """
    from app.analyysikeskus.adhoc_analysis import run_adhoc_impact_analysis as canonical
    from app.analyysikeskus.routes import _normi
    from app.analyysikeskus.services import normi_mojuahel as svc

    assert svc.run_adhoc_impact_analysis is canonical
    # The route no longer imports the orchestration dependency — it delegates
    # to the service and only renders the typed result.
    assert not hasattr(_normi, "run_adhoc_impact_analysis")


def test_el_orchestration_deps_live_in_the_service_not_the_route() -> None:
    """``run_eu_transposition`` / ``search_eu_acts_by_label`` are called by the
    EL *service*; the route submodule no longer binds them."""
    from app.analyysikeskus.eu_lookup import search_eu_acts_by_label as canonical_search
    from app.analyysikeskus.routes import _el_ulevott
    from app.analyysikeskus.services import el_ulevott as svc
    from app.impact.eu_transposition import run_eu_transposition as canonical_transp

    assert svc.run_eu_transposition is canonical_transp
    assert svc.search_eu_acts_by_label is canonical_search
    assert not hasattr(_el_ulevott, "run_eu_transposition")
    assert not hasattr(_el_ulevott, "search_eu_acts_by_label")


def test_service_submodule_patch_intercepts_service_callers() -> None:
    """Patching the service module's binding intercepts the service's call —
    the path ``test_analyysikeskus_routes.py`` now uses for the EL workflow."""

    def stub(*_args, **_kwargs):  # pragma: no cover — only invoked on failure
        return []

    with patch("app.analyysikeskus.services.el_ulevott.search_eu_acts_by_label", stub):
        from app.analyysikeskus.services.el_ulevott import (
            search_eu_acts_by_label as patched_in_service,
        )

        assert patched_in_service is stub
