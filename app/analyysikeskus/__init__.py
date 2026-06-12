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
"""

from app.analyysikeskus.result_shell import analysis_result_shell
from app.analyysikeskus.routes import register_analyysikeskus_routes

__all__ = ["analysis_result_shell", "register_analyysikeskus_routes"]
