"""Analüüsikeskus — the legal-analysis workflow hub (#714).

Exposes the workflow directory (#720), the reusable analysis-result shell
(#721), and the eleven wired workflows registered by
:func:`register_analyysikeskus_routes`: ``Normi mõjuahel`` (#722),
``EL ülevõtt ja harmoneerimine`` (#723), ``Sanktsioonide indeks``,
``Kohtupraktika``, ``Halduskoormus``, ``Pädevused``, ``Ajalugu``,
``Sarnasus``, and the policy-intent ``Mõju poliitikamõttest`` flow (#814,
two POST handlers).

The route handlers live in the ``routes`` *package* (split per-workflow in
#860, mirroring ``app/docs/routes/``); cross-workflow helpers live in
``routes/_common.py``. Framework-free service functions for the Phase-5
REST/MCP surface live in ``services/`` (see
``docs/2026-06-12-service-layer-convention.md``).

Both public exports — :func:`analysis_result_shell` and
:func:`register_analyysikeskus_routes` — are resolved *lazily* (PEP 562
module ``__getattr__``) rather than imported at module scope. Both live in
the FastHTML UI layer (``result_shell`` / ``routes``), so eagerly importing
them would pull ``fasthtml`` / ``starlette`` into ``sys.modules`` the moment
anyone imports a *neutral* sibling — the framework-free ``services/``
subpackage, or the data-only ``app.analyysikeskus.eu_transposition`` helper
that ``app.dashboard.service`` consumes — defeating the Phase-5 service-layer
guarantee. The lazy export keeps ``from app.analyysikeskus import
register_analyysikeskus_routes`` (used by ``app/main.py``) working while only
loading the framework layer when the export is actually requested; pinned by
``tests/test_analyysikeskus_services_no_framework.py``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.analyysikeskus.result_shell import analysis_result_shell
    from app.analyysikeskus.routes import register_analyysikeskus_routes

__all__ = ["analysis_result_shell", "register_analyysikeskus_routes"]


def __getattr__(name: str) -> Any:
    """Lazily resolve the public exports from the UI layer (PEP 562)."""
    if name == "register_analyysikeskus_routes":
        from app.analyysikeskus.routes import register_analyysikeskus_routes

        return register_analyysikeskus_routes
    if name == "analysis_result_shell":
        from app.analyysikeskus.result_shell import analysis_result_shell

        return analysis_result_shell
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
