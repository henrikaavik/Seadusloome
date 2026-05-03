"""Pin the post-#704 patch-path contract.

After the #704 routes/ split started extracting helpers into
``_shared.py`` and ``_status_tracker.py``, ``app.docs.routes``
re-exports the moved symbols for direct-import convenience but a
``patch("app.docs.routes.X")`` only rebinds the package-level alias —
NOT the bindings inside submodules that imported the symbol at module
load time. This is the standard Python "patch where used, not where
defined" rule.

These tests pin both halves of the contract:

1. Direct imports from ``app.docs.routes`` keep returning the moved
   symbol (back-compat for callers that aren't patching).
2. Patching ``app.docs.routes._poll_interval_seconds`` does NOT
   affect ``_status_tracker``'s internal call site — submodules use
   their own globals.
3. Patching ``app.docs.routes._status_tracker._poll_interval_seconds``
   DOES intercept the tracker's call (the canonical "patch where
   used" path).

Reviewer note from PR #710 / #704 PR-B asked for this pin so future
extractions in PR-C / D / E inherit the documented contract.
"""

from __future__ import annotations

from unittest.mock import patch

from app.docs.routes import _shared
from app.docs.routes._status_tracker import (
    _poll_interval_seconds as _imported_at_module_load,
)


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
