"""URL-surface assertion for the /drafts routes (#623 routes split).

Pinned BEFORE the routes.py → routes/ package refactor so each move
step can be verified by re-running this test. If a route accidentally
gets dropped during the move, the test fails immediately and points
at the missing path.

The full set covers both ``app/docs/routes.py`` (refactored by #623
into the routes/ package) and ``app/docs/report_routes.py`` (OUT of
scope for this PR — included here so we'd notice if a stray edit
disturbed it). The expected set was captured from
``app.main.app.routes`` introspection on the pre-refactor commit.
"""

from __future__ import annotations


def test_draft_route_surface_is_stable() -> None:
    from app.main import app

    paths: set[tuple[str, tuple[str, ...]]] = set()
    for route in app.routes:
        path = getattr(route, "path", None)
        if path is None or not path.startswith("/drafts"):
            continue
        methods_raw = getattr(route, "methods", None) or ()
        paths.add((path, tuple(sorted(methods_raw))))

    expected: set[tuple[str, tuple[str, ...]]] = {
        # routes.py — refactored by #623
        ("/drafts", ("GET", "HEAD")),
        ("/drafts", ("POST",)),
        ("/drafts/new", ("GET", "HEAD")),
        ("/drafts/{draft_id}", ("GET", "HEAD")),
        ("/drafts/{draft_id}/status", ("GET", "HEAD")),
        ("/drafts/{draft_id}/actions", ("GET", "HEAD")),
        ("/drafts/{draft_id}/keep", ("POST",)),
        ("/drafts/{draft_id}/delete", ("POST",)),
        ("/drafts/{draft_id}/link-vtk", ("POST",)),
        ("/drafts/{draft_id}/retry", ("POST",)),
        # #618 PR-C: side-by-side diff route (versioning UI).
        ("/drafts/{draft_id}/diff", ("GET", "HEAD")),
        # report_routes.py — out of scope for #623, included as a tripwire
        ("/drafts/{draft_id}/report", ("GET", "HEAD")),
        ("/drafts/{draft_id}/report/reanalyze", ("POST",)),
        ("/drafts/{draft_id}/report/section/{section}", ("GET", "HEAD")),
        # C6 (#791): executive summary printout (1-2 page .docx).
        ("/drafts/{draft_id}/report/summary.docx", ("GET", "HEAD")),
        ("/drafts/{draft_id}/export", ("POST",)),
        ("/drafts/{draft_id}/export-status/{job_id}", ("GET", "HEAD")),
        ("/drafts/{draft_id}/export/{job_id}/download", ("GET", "HEAD")),
    }

    missing = expected - paths
    extra = paths - expected
    assert not missing, f"routes dropped during refactor: {sorted(missing)}"
    assert not extra, f"unexpected routes appeared: {sorted(extra)}"
