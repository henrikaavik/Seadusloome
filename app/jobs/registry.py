"""Shared handler wiring for inproc and standalone worker modes (#348).

Background job handlers live in :mod:`app.docs` and :mod:`app.drafter`
and register themselves via the ``@register_handler`` decorator as a
side effect of import. The FastHTML app currently triggers those
imports transitively through its route-registration imports
(``from app.docs.routes import ...`` etc), which is fine for the
in-process worker mode but leaves the standalone worker
(:mod:`scripts.run_worker`) without an obvious place to do the same
wiring — and we explicitly do NOT want the standalone worker pulling
in FastHTML / Starlette to get the side effects.

This module is the single, framework-free entry point both modes
call to ensure the handler registry is populated. It must NOT import
``app.main`` or any FastHTML/Starlette module.

Adding a new handler family
---------------------------
1. Write the handler module (e.g. ``app/foo/bar_handler.py``) and add
   ``register_handler("bar_job")(bar)`` at module bottom.
2. Add the import to :func:`register_all_handlers` below. The import
   alone is what triggers registration — no further wiring needed.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def register_all_handlers() -> None:
    """Import every handler module so its ``@register_handler`` runs.

    Idempotent: re-importing a module re-runs the decorator, which
    overwrites the existing registry entry with the same function
    object. Safe to call from both the FastHTML lifespan hook (inproc
    mode) and :mod:`scripts.run_worker` (standalone mode).

    The function returns nothing; callers can introspect the result
    via :data:`app.jobs.worker._HANDLERS` if they need to assert
    coverage in tests.
    """
    # Document upload pipeline handlers: parse_draft, extract_entities,
    # analyze_impact, export_report, draft_cleanup. Each handler module
    # has a ``register_handler(...)`` call at the bottom; importing the
    # package re-exports those side effects.
    from app.docs import (  # noqa: F401  -- side-effect import
        analyze_handler,
        cleanup_handler,
        export_handler,
        extract_handler,
        parse_handler,
    )

    # AI Law Drafter handlers: drafter_clarify, drafter_research,
    # drafter_structure, drafter_draft, drafter_regenerate_clause.
    from app.drafter import handlers  # noqa: F401  -- side-effect import

    logger.debug("register_all_handlers: imported app.docs handlers and app.drafter handlers")
