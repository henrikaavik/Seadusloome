"""Re-export shim — the Sanktsioonide indeks SPARQL data layer now lives
in :mod:`app.impact.sanctions`.

The sanction data/query/label primitives were promoted into the neutral
``app.impact`` analysis layer (#860) so the impact analyzer can consume
them with a normal top-level import instead of the old function-local
cycle-breaking import (the impact analyzer ←→
``app.analyysikeskus.sanctions``). The Sanktsioonide indeks *workflow*
(routes, result-shell rendering, Estonian copy) still lives under
:mod:`app.analyysikeskus`, which now depends **on** ``app.impact`` —
the one-directional rule the refactor enforces (``analyysikeskus →
impact``, never the reverse).

This module re-exports the full public + test-referenced surface of
:mod:`app.impact.sanctions` so existing importers and ``unittest.mock``
patch paths (``app.analyysikeskus.sanctions.X``) keep working unchanged.
New code should import from :mod:`app.impact.sanctions` directly.
"""

from __future__ import annotations

from app.impact.sanctions import (
    _ACT_SANCTIONS_QUERY,
    _PROVISION_SANCTIONS_QUERY,
    _SIMILAR_SANCTIONS_QUERY,
    SANCTION_TYPE_LABELS_ET,
    SANCTION_UNIT_LABELS_ET,
    SanctionRow,
    _build_act_sanctions_query,
    _build_provision_sanctions_query,
    _build_similar_sanctions_query,
    _xsd_decimal_literal,
    find_similar_sanctions,
    list_sanctions_for_act,
    list_sanctions_for_provision,
    sanction_type_label,
    sanction_unit_label,
)

__all__ = [
    "SANCTION_TYPE_LABELS_ET",
    "SANCTION_UNIT_LABELS_ET",
    "SanctionRow",
    "find_similar_sanctions",
    "list_sanctions_for_act",
    "list_sanctions_for_provision",
    "sanction_type_label",
    "sanction_unit_label",
    "_ACT_SANCTIONS_QUERY",
    "_PROVISION_SANCTIONS_QUERY",
    "_SIMILAR_SANCTIONS_QUERY",
    "_build_act_sanctions_query",
    "_build_provision_sanctions_query",
    "_build_similar_sanctions_query",
    "_xsd_decimal_literal",
]
