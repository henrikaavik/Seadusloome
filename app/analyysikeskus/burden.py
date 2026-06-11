"""Re-export shim — the Halduskoormus SPARQL data layer now lives in
:mod:`app.impact.burden`.

The burden data/query/label primitives were promoted into the neutral
``app.impact`` analysis layer (#860) so the impact analyzer can consume
them with a normal top-level import instead of the old function-local
cycle-breaking import (the impact analyzer ←→
``app.analyysikeskus.burden``). The Halduskoormus *workflow* (routes,
result-shell rendering, Estonian copy) still lives under
:mod:`app.analyysikeskus`, which now depends **on** ``app.impact`` —
the one-directional rule the refactor enforces (``analyysikeskus →
impact``, never the reverse).

This module re-exports the full public + test-referenced surface of
:mod:`app.impact.burden` so existing importers and ``unittest.mock``
patch paths (``app.analyysikeskus.burden.X``) keep working unchanged.
New code should import from :mod:`app.impact.burden` directly.
"""

from __future__ import annotations

from app.impact.burden import (
    BURDEN_DESCRIPTIONS_ET,
    BURDEN_LABELS_ET,
    BurdenDelta,
    BurdenKey,
    BurdenRow,
    BurdenSummary,
    _build_act_burden_query,
    _build_draft_affected_provisions_graph_query,
    _build_draft_affected_provisions_query,
    _build_provision_burden_query,
    _build_provisions_burden_values_query,
    bucket_burden_rows,
    burden_delta_for_draft,
    burden_description,
    burden_key_order,
    burden_label,
    list_burden_for_act,
    list_burden_for_provision,
    top_duty_holders,
)

__all__ = [
    "BURDEN_DESCRIPTIONS_ET",
    "BURDEN_LABELS_ET",
    "BurdenDelta",
    "BurdenKey",
    "BurdenRow",
    "BurdenSummary",
    "bucket_burden_rows",
    "burden_delta_for_draft",
    "burden_description",
    "burden_key_order",
    "burden_label",
    "list_burden_for_act",
    "list_burden_for_provision",
    "top_duty_holders",
    "_build_act_burden_query",
    "_build_draft_affected_provisions_graph_query",
    "_build_draft_affected_provisions_query",
    "_build_provision_burden_query",
    "_build_provisions_burden_values_query",
]
