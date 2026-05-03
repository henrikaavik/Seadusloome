"""Pin the post-#704 patch-path contract.

After the #704 routes/ split started extracting helpers into
``_shared.py`` / ``_status_tracker.py`` / ``_upload.py`` / ``_list.py`` /
``_detail.py`` / ``_detail_modals.py`` / ``_detail_versions.py``,
``app.docs.routes`` re-exports the moved symbols for direct-import
convenience but a ``patch("app.docs.routes.X")`` only rebinds the
package-level alias — NOT the bindings inside submodules that
imported the symbol at module load time. This is the standard Python
"patch where used, not where defined" rule.

These tests pin both halves of the contract for each extracted
submodule:

1. Direct imports from ``app.docs.routes`` keep returning the moved
   symbol (back-compat for callers that aren't patching).
2. Patching ``app.docs.routes.<symbol>`` does NOT affect the
   submodule's internal call site — submodules use their own globals.
3. Patching ``app.docs.routes.<submodule>.<symbol>`` DOES intercept
   the submodule's call (the canonical "patch where used" path).

Reviewer note from PR #710 / #704 PR-B asked for this pin so future
extractions in PR-C / D / E inherit the documented contract. PR-C
adds the matching three-test block for ``_upload._validate_parent_vtk_fk``.
PR-D adds the matching three-test block for
``_list.list_drafts_for_org_filtered``. PR-E adds the matching
three-test block for ``_detail.fetch_draft``.
"""

from __future__ import annotations

from unittest.mock import patch

from app.docs.routes import _detail, _list, _shared, _upload
from app.docs.routes._detail import (
    fetch_draft as _detail_imported_at_module_load,
)
from app.docs.routes._list import (
    list_drafts_for_org_filtered as _list_imported_at_module_load,
)
from app.docs.routes._status_tracker import (
    _poll_interval_seconds as _imported_at_module_load,
)
from app.docs.routes._upload import (
    _validate_parent_vtk_fk as _upload_imported_at_module_load,
)

# ---------------------------------------------------------------------------
# PR-B contract: _shared._poll_interval_seconds via _status_tracker
# ---------------------------------------------------------------------------


def test_direct_import_from_package_returns_moved_symbol() -> None:
    """``from app.docs.routes import _poll_interval_seconds`` still works."""
    from app.docs.routes import _poll_interval_seconds

    assert _poll_interval_seconds is _shared._poll_interval_seconds


def test_status_tracker_module_owns_its_local_binding() -> None:
    """``_status_tracker`` imported the helper at load time; the
    module-local reference is what its function bodies use."""
    assert _imported_at_module_load is _shared._poll_interval_seconds


def test_package_level_patch_does_not_reach_submodule_globals() -> None:
    """``patch("app.docs.routes._poll_interval_seconds")`` only rebinds
    the package alias; the submodule's local binding stays original.

    This is the failure mode the reviewer flagged on PR #710. Pinning
    it as a test so a future extraction in PR-C / D / E doesn't
    accidentally claim package-level patches catch submodule callers.
    """

    def stub(_draft):  # pragma: no cover — only invoked if test fails
        return 999

    with patch("app.docs.routes._poll_interval_seconds", stub):
        from app.docs.routes import _poll_interval_seconds as patched_pkg
        from app.docs.routes._status_tracker import (
            _poll_interval_seconds as patched_in_submodule,
        )

        assert patched_pkg is stub, "package-level binding should be the stub"
        assert patched_in_submodule is _shared._poll_interval_seconds, (
            "submodule binding must NOT see the package-level patch — "
            "this is the documented post-#704 contract"
        )


def test_submodule_patch_intercepts_submodule_callers() -> None:
    """Patching where the symbol is USED (inside ``_status_tracker``)
    DOES intercept the tracker's internal call. This is the canonical
    "patch where used" recipe."""

    def stub(_draft):  # pragma: no cover — only invoked if test fails
        return 999

    with patch("app.docs.routes._status_tracker._poll_interval_seconds", stub):
        from app.docs.routes._status_tracker import (
            _poll_interval_seconds as patched_in_submodule,
        )

        assert patched_in_submodule is stub, (
            "submodule-local binding must reflect the submodule-targeted patch"
        )


# ---------------------------------------------------------------------------
# PR-C contract: _upload._validate_parent_vtk_fk via _upload callers
# ---------------------------------------------------------------------------


def test_upload_module_owns_its_local_binding() -> None:
    """``_upload`` defines ``_validate_parent_vtk_fk`` at module scope;
    its own ``create_draft_handler`` resolves the name through the
    module-local global, not through the package re-export."""
    assert _upload_imported_at_module_load is _upload._validate_parent_vtk_fk


def test_package_level_patch_does_not_reach_upload_globals() -> None:
    """``patch("app.docs.routes._validate_parent_vtk_fk")`` only rebinds
    the package alias; ``_upload``'s local binding stays original.

    Mirrors :func:`test_package_level_patch_does_not_reach_submodule_globals`
    so PR-C extractions inherit the same documented contract: package
    aliases are convenience back-compat re-exports, NOT patch points
    that propagate into submodules.
    """

    def stub(_conn, _parent_vtk_id, _org_id):  # pragma: no cover — only on failure
        return "stubbed"

    with patch("app.docs.routes._validate_parent_vtk_fk", stub):
        from app.docs.routes import _validate_parent_vtk_fk as patched_pkg
        from app.docs.routes._upload import (
            _validate_parent_vtk_fk as patched_in_submodule,
        )

        assert patched_pkg is stub, "package-level binding should be the stub"
        assert patched_in_submodule is _upload._validate_parent_vtk_fk, (
            "submodule binding must NOT see the package-level patch — "
            "this is the documented post-#704 contract for _upload too"
        )


def test_upload_submodule_patch_intercepts_upload_callers() -> None:
    """Patching where the symbol is USED (inside ``_upload``) DOES
    intercept the handler's internal call. Canonical "patch where
    used" recipe for the PR-C extraction."""

    def stub(_conn, _parent_vtk_id, _org_id):  # pragma: no cover — only on failure
        return "stubbed"

    with patch("app.docs.routes._upload._validate_parent_vtk_fk", stub):
        from app.docs.routes._upload import (
            _validate_parent_vtk_fk as patched_in_submodule,
        )

        assert patched_in_submodule is stub, (
            "submodule-local binding must reflect the submodule-targeted patch"
        )


# ---------------------------------------------------------------------------
# PR-D contract: _list.list_drafts_for_org_filtered via _list callers
# ---------------------------------------------------------------------------


def test_list_module_owns_its_local_binding() -> None:
    """``_list`` imports ``list_drafts_for_org_filtered`` at module load
    time; its own ``drafts_list_page`` resolves the name through the
    module-local global, not through the package re-export."""
    assert _list_imported_at_module_load is _list.list_drafts_for_org_filtered


def test_package_level_patch_does_not_reach_list_globals() -> None:
    """``patch("app.docs.routes.list_drafts_for_org_filtered")`` only
    rebinds the package alias; ``_list``'s local binding stays original.

    Mirrors :func:`test_package_level_patch_does_not_reach_submodule_globals`
    so PR-D extractions inherit the same documented contract: package
    aliases are convenience back-compat re-exports, NOT patch points
    that propagate into submodules.
    """

    def stub(*_args, **_kwargs):  # pragma: no cover — only invoked on failure
        return ([], 0)

    with patch("app.docs.routes.list_drafts_for_org_filtered", stub):
        from app.docs.routes import (
            list_drafts_for_org_filtered as patched_pkg,
        )
        from app.docs.routes._list import (
            list_drafts_for_org_filtered as patched_in_submodule,
        )

        assert patched_pkg is stub, "package-level binding should be the stub"
        assert patched_in_submodule is _list.list_drafts_for_org_filtered, (
            "submodule binding must NOT see the package-level patch — "
            "this is the documented post-#704 contract for _list too"
        )


def test_list_submodule_patch_intercepts_list_callers() -> None:
    """Patching where the symbol is USED (inside ``_list``) DOES
    intercept the handler's internal call. Canonical "patch where
    used" recipe for the PR-D extraction."""

    def stub(*_args, **_kwargs):  # pragma: no cover — only invoked on failure
        return ([], 0)

    with patch("app.docs.routes._list.list_drafts_for_org_filtered", stub):
        from app.docs.routes._list import (
            list_drafts_for_org_filtered as patched_in_submodule,
        )

        assert patched_in_submodule is stub, (
            "submodule-local binding must reflect the submodule-targeted patch"
        )


# ---------------------------------------------------------------------------
# PR-E contract: _detail.fetch_draft via _detail callers
# ---------------------------------------------------------------------------


def test_detail_module_owns_its_local_binding() -> None:
    """``_detail`` imports ``fetch_draft`` at module load time; its own
    ``draft_detail_page`` resolves the name through the module-local
    global, not through the package re-export."""
    assert _detail_imported_at_module_load is _detail.fetch_draft


def test_package_level_patch_does_not_reach_detail_globals() -> None:
    """A ``_detail``-targeted patch does NOT propagate into sibling
    submodules that imported ``fetch_draft`` independently.

    Mirrors :func:`test_package_level_patch_does_not_reach_submodule_globals`
    for the PR-E extraction. ``_detail.py`` and ``_detail_versions.py``
    each import ``fetch_draft`` directly from
    :mod:`app.docs.draft_model` at module load time, so a patch on one
    submodule's binding must NOT be visible to the other. This is the
    boundary that requires per-handler patch sites in test code rather
    than a single package-level patch.
    """

    def stub(*_args, **_kwargs):  # pragma: no cover — only invoked on failure
        return None

    # Snapshot the canonical implementation BEFORE patching so we can
    # assert the sibling submodule still resolves to it.
    from app.docs.draft_model import fetch_draft as canonical_fetch_draft

    with patch("app.docs.routes._detail.fetch_draft", stub):
        from app.docs.routes._detail import fetch_draft as patched_in_detail
        from app.docs.routes._detail_versions import (
            fetch_draft as patched_in_versions,
        )

        assert patched_in_detail is stub, "_detail-local binding should be the stub"
        assert patched_in_versions is canonical_fetch_draft, (
            "sibling submodule binding must NOT see the _detail-targeted patch — "
            "this is the documented post-#704 contract for _detail too"
        )


def test_detail_submodule_patch_intercepts_detail_callers() -> None:
    """Patching where the symbol is USED (inside ``_detail``) DOES
    intercept the handler's internal call. Canonical "patch where
    used" recipe for the PR-E extraction."""

    def stub(*_args, **_kwargs):  # pragma: no cover — only invoked on failure
        return None

    with patch("app.docs.routes._detail.fetch_draft", stub):
        from app.docs.routes._detail import fetch_draft as patched_in_submodule

        assert patched_in_submodule is stub, (
            "submodule-local binding must reflect the submodule-targeted patch"
        )
