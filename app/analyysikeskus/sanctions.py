"""SPARQL helpers for the Sanktsioonide indeks workflow (A1, plan section 5).

The `Sanktsioonide indeks` (Sanctions index) workflow surfaces all
sanctions attached to a chosen provision or act and lists comparable
sanctions in other acts. The ontology models a sanction as a reified
``estleg:Sanction`` node hanging off a provision via
``estleg:hasSanction`` with structured penalty data
(``min/maxPenaltyAmount`` × ``min/maxPenaltyUnit`` × ``min/maxPenaltyCurrency``),
the ``estleg:sanctionType`` discriminator, ``estleg:enforcedAtLevel``
(act / minister / parliament), and ``estleg:isStatutoryDefault`` for
default rules. See ``docs/2026-05-15-ontology-six-use-cases-plan.md``
section 2.5 row A1 — the predicates were confirmed by audit and the
pipeline runs corpus-wide.

The three public functions are written so that:

* :func:`list_sanctions_for_provision` projects every Sanction attached
  to a single provision.
* :func:`list_sanctions_for_act` joins via ``estleg:partOf`` (the act
  → provision membership relation) and aggregates Sanction rows from
  every member provision.
* :func:`find_similar_sanctions` returns Sanction rows from *other*
  acts whose sanctionType matches and whose penalty range overlaps the
  given seed row's range, capped at *limit*. The comparison is simple
  range overlap on ``min/maxPenaltyAmount``; we do **not** use ML
  similarity here — the design note explicitly calls for "simple range
  overlap, not ML similarity".

Every call is guarded — a dead Jena yields an empty list, never a
500. The result rows are :class:`SanctionRow` dataclass instances so
the route layer can render them deterministically without sprinkling
``dict.get`` defaults everywhere.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from app.ontology.queries import PREFIXES
from app.ontology.sparql_client import SparqlClient

logger = logging.getLogger(__name__)

# Cap how many comparison rows we surface in the "Sarnaste aktide
# sanktsioonid" section — the table stays scannable and a 1M-triple
# corpus could otherwise return thousands of overlapping rows for a
# generic fine sanction.
_MAX_SIMILAR_SANCTIONS = 50

# Cap how many Sanction rows we project for a single act — a real
# corpus act (e.g. KarS) can have hundreds; the UI keeps the list
# scannable and signals truncation in the summary line.
_MAX_SANCTIONS_PER_ACT = 200

# ---------------------------------------------------------------------------
# SanctionRow — the structured row the UI renders
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SanctionRow:
    """A single Sanction node projected out of the ontology.

    Mirrors the predicates from
    ``docs/2026-05-15-ontology-six-use-cases-plan.md`` section 2.5 row
    A1. Most fields are ``Optional``-shaped because the ontology
    populates them best-effort — e.g. a sanction may have only a
    ``max`` bound, or no currency on imprisonment-shaped penalties.

    Attributes:
        sanction_uri: The reified ``estleg:Sanction`` node's URI, or
            empty string when the SPARQL serialiser yields a blank
            node. The route uses this for the Tõendid row link target
            and falls back to ``provision_uri`` when empty.
        provision_uri: The owning ``LegalProvision`` URI — always set
            because we walked the graph through ``hasSanction``.
        provision_label: ``rdfs:label`` on the provision; the route
            uses it as the row's primary label.
        act_uri: The Act URI the provision is ``partOf`` — may be
            empty for sandbox / unattached provisions.
        act_label: ``rdfs:label`` on the act.
        sanction_type: The ontology's coarse type string (e.g.
            ``"imprisonment"``, ``"fine"``). Translated to an Estonian
            display label by the route via :data:`SANCTION_TYPE_LABELS_ET`.
        min_amount / max_amount: Numeric penalty bounds. ``None`` when
            the predicate is absent. ``float`` because the ontology
            uses ``xsd:decimal`` for some currency-bearing rows and
            ``xsd:integer`` for time-bearing rows; we coerce to
            ``float`` so a single comparator works on both.
        min_unit / max_unit: The ontology's unit string (e.g.
            ``"years"``, ``"days"``, ``"daily_rates"``, ``"monetary"``).
            Translated by :data:`SANCTION_UNIT_LABELS_ET`.
        min_currency / max_currency: ISO currency code (e.g.
            ``"EUR"``) when the unit is monetary; ``None`` otherwise.
        enforced_at_level: ``"act"``, ``"minister"``, ``"parliament"``
            etc. — surfaced verbatim in the row's metadata.
        is_statutory_default: Whether this sanction is the statutory
            default rule (``estleg:isStatutoryDefault`` = ``true``).
            ``None`` when the predicate is absent.
    """

    sanction_uri: str = ""
    provision_uri: str = ""
    provision_label: str = ""
    act_uri: str = ""
    act_label: str = ""
    sanction_type: str = ""
    min_amount: float | None = None
    max_amount: float | None = None
    min_unit: str = ""
    max_unit: str = ""
    min_currency: str | None = None
    max_currency: str | None = None
    enforced_at_level: str = ""
    is_statutory_default: bool | None = None
    # Free-form bag for any extra predicate values the route wants to
    # surface (e.g. a SHACL-side `rdfs:comment`); not populated by the
    # current query templates but the dataclass keeps the door open.
    extras: dict[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Estonian display labels — sanction types + units
# ---------------------------------------------------------------------------
#
# Ontology string values → human-readable Estonian. Used by both the
# Tulemused summary line ("X sanktsiooni; Y rahatrahv, Z vangistus")
# and the per-row table cells. Keys mirror the ontology's coarse
# vocabulary; new values fall back to the raw string verbatim via
# :func:`sanction_type_label`.

SANCTION_TYPE_LABELS_ET: dict[str, str] = {
    "imprisonment": "Vangistus",
    "fine": "Rahatrahv",
    "pecuniary_punishment": "Rahaline karistus",
    "arrest": "Arest",
    "coercive_payment": "Sunniraha",
}

SANCTION_UNIT_LABELS_ET: dict[str, str] = {
    "years": "aastat",
    "months": "kuud",
    "days": "päeva",
    "daily_rates": "päevamäära",
    "fine_units": "trahvi-ühikut",
    # ``"monetary"`` is special — the display becomes the currency code
    # (e.g. "EUR") because "monetary EUR" reads worse than just "EUR".
    "monetary": "",
}


def sanction_type_label(sanction_type: str) -> str:
    """Estonian display label for *sanction_type*, falling back to the raw string."""
    key = (sanction_type or "").strip()
    if not key:
        return "Sanktsioon"
    return SANCTION_TYPE_LABELS_ET.get(key, key)


def sanction_unit_label(unit: str, currency: str | None = None) -> str:
    """Estonian display label for *unit*; ``"monetary"`` falls back to *currency*.

    The ontology uses ``unit="monetary"`` for amount-with-currency
    sanctions; the human reading is just the currency code ("EUR")
    rather than "monetary EUR". Unknown units fall back to the raw
    string so a future ontology extension shows up legibly.
    """
    key = (unit or "").strip()
    if not key:
        return (currency or "").strip()
    if key == "monetary":
        return (currency or "").strip() or "EUR"
    return SANCTION_UNIT_LABELS_ET.get(key, key)


# ---------------------------------------------------------------------------
# SPARQL templates
# ---------------------------------------------------------------------------
#
# All three templates project the same shape of columns so the
# :class:`SanctionRow` builder works uniformly. We use OPTIONAL on
# every field except ``provision`` and ``sanction`` because the
# corpus' completeness varies — e.g. some sanctions have only a max
# bound, some lack currency entirely.

_PROVISION_SANCTIONS_QUERY = (
    PREFIXES
    + """
SELECT ?sanction ?provision ?provisionLabel
       ?act ?actLabel
       ?sanctionType
       ?minAmount ?maxAmount
       ?minUnit ?maxUnit
       ?minCurrency ?maxCurrency
       ?enforcedAtLevel
       ?isStatutoryDefault
WHERE {
  ?provision estleg:hasSanction ?sanction .
  OPTIONAL { ?provision rdfs:label ?provisionLabel }
  OPTIONAL { ?provision estleg:partOf ?act .
             OPTIONAL { ?act rdfs:label ?actLabel } }
  OPTIONAL { ?sanction estleg:sanctionType ?sanctionType }
  OPTIONAL { ?sanction estleg:minPenaltyAmount ?minAmount }
  OPTIONAL { ?sanction estleg:maxPenaltyAmount ?maxAmount }
  OPTIONAL { ?sanction estleg:minPenaltyUnit ?minUnit }
  OPTIONAL { ?sanction estleg:maxPenaltyUnit ?maxUnit }
  OPTIONAL { ?sanction estleg:minPenaltyCurrency ?minCurrency }
  OPTIONAL { ?sanction estleg:maxPenaltyCurrency ?maxCurrency }
  OPTIONAL { ?sanction estleg:enforcedAtLevel ?enforcedAtLevel }
  OPTIONAL { ?sanction estleg:isStatutoryDefault ?isStatutoryDefault }
}
ORDER BY ?provision
LIMIT """
    + str(_MAX_SANCTIONS_PER_ACT)
    + "\n"
)

_ACT_SANCTIONS_QUERY = (
    PREFIXES
    + """
SELECT ?sanction ?provision ?provisionLabel
       ?act ?actLabel
       ?sanctionType
       ?minAmount ?maxAmount
       ?minUnit ?maxUnit
       ?minCurrency ?maxCurrency
       ?enforcedAtLevel
       ?isStatutoryDefault
WHERE {
  ?provision estleg:partOf ?act .
  ?provision estleg:hasSanction ?sanction .
  OPTIONAL { ?provision rdfs:label ?provisionLabel }
  OPTIONAL { ?act rdfs:label ?actLabel }
  OPTIONAL { ?sanction estleg:sanctionType ?sanctionType }
  OPTIONAL { ?sanction estleg:minPenaltyAmount ?minAmount }
  OPTIONAL { ?sanction estleg:maxPenaltyAmount ?maxAmount }
  OPTIONAL { ?sanction estleg:minPenaltyUnit ?minUnit }
  OPTIONAL { ?sanction estleg:maxPenaltyUnit ?maxUnit }
  OPTIONAL { ?sanction estleg:minPenaltyCurrency ?minCurrency }
  OPTIONAL { ?sanction estleg:maxPenaltyCurrency ?maxCurrency }
  OPTIONAL { ?sanction estleg:enforcedAtLevel ?enforcedAtLevel }
  OPTIONAL { ?sanction estleg:isStatutoryDefault ?isStatutoryDefault }
}
ORDER BY ?provision
LIMIT """
    + str(_MAX_SANCTIONS_PER_ACT)
    + "\n"
)

# Similar sanctions — same sanctionType, other acts only, with a
# range-overlap filter on the amount bounds. We bind ``?type`` /
# ``?seedMin`` / ``?seedMax`` / ``?seedAct`` via
# :meth:`SparqlClient._inject_bindings`. The injector emits VALUES
# with **string literals** (it has to — the same injector is used for
# URI strings, language tags, etc.), so the numeric comparisons in the
# FILTER cast explicitly through ``xsd:decimal(?seedMin)`` and
# ``xsd:decimal(?seedMax)``. Without the cast SPARQL compares lex order
# string-to-decimal, which silently returns no overlapping rows (F2,
# 2026-05-15 review repro: rdflib confirmed 0 rows for string vs 1 row
# for typed decimal).
#
# The range-overlap maths: two ranges [a, b] and [c, d] overlap iff
# a <= d AND c <= b, i.e. ``?seedMin <= ?maxAmount`` AND
# ``?minAmount <= ?seedMax``. Either bound missing on the candidate
# row passes the filter (treated as open-ended), so we don't lose
# rows that have only one numeric bound.
_SIMILAR_SANCTIONS_QUERY = (
    PREFIXES
    + """
SELECT ?sanction ?provision ?provisionLabel
       ?act ?actLabel
       ?sanctionType
       ?minAmount ?maxAmount
       ?minUnit ?maxUnit
       ?minCurrency ?maxCurrency
       ?enforcedAtLevel
       ?isStatutoryDefault
WHERE {
  ?provision estleg:hasSanction ?sanction .
  ?sanction estleg:sanctionType ?sanctionType .
  OPTIONAL { ?provision rdfs:label ?provisionLabel }
  OPTIONAL { ?provision estleg:partOf ?act .
             OPTIONAL { ?act rdfs:label ?actLabel } }
  OPTIONAL { ?sanction estleg:minPenaltyAmount ?minAmount }
  OPTIONAL { ?sanction estleg:maxPenaltyAmount ?maxAmount }
  OPTIONAL { ?sanction estleg:minPenaltyUnit ?minUnit }
  OPTIONAL { ?sanction estleg:maxPenaltyUnit ?maxUnit }
  OPTIONAL { ?sanction estleg:minPenaltyCurrency ?minCurrency }
  OPTIONAL { ?sanction estleg:maxPenaltyCurrency ?maxCurrency }
  OPTIONAL { ?sanction estleg:enforcedAtLevel ?enforcedAtLevel }
  OPTIONAL { ?sanction estleg:isStatutoryDefault ?isStatutoryDefault }
  FILTER(STR(?sanctionType) = ?type)
  FILTER(!BOUND(?act) || STR(?act) != ?seedAct)
  FILTER(!BOUND(?maxAmount) || xsd:decimal(?seedMin) <= ?maxAmount)
  FILTER(!BOUND(?minAmount) || ?minAmount <= xsd:decimal(?seedMax))
}
ORDER BY ?act ?provision
LIMIT """
    + str(_MAX_SIMILAR_SANCTIONS)
    + "\n"
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def list_sanctions_for_provision(
    provision_uri: str,
    *,
    sparql_client: SparqlClient | None = None,
) -> list[SanctionRow]:
    """Return every Sanction attached to *provision_uri*.

    Args:
        provision_uri: A ``LegalProvision`` URI (the
            ``estleg:hasSanction`` subject). Empty / whitespace
            input yields ``[]`` without hitting Jena.
        sparql_client: Optional :class:`SparqlClient` override (tests
            inject one whose ``.query`` is mocked).

    Returns:
        A list of :class:`SanctionRow` — one per matching Sanction
        node. ``[]`` when the provision has no sanctions, or when any
        SPARQL error occurs (the route degrades to "Sanktsioone ei
        leitud").
    """
    uri = (provision_uri or "").strip()
    if not uri:
        return []

    client = sparql_client if sparql_client is not None else SparqlClient()
    try:
        rows = client.query(
            _PROVISION_SANCTIONS_QUERY,
            uri_bindings={"provision": uri},
        )
    except Exception:
        logger.warning(
            "list_sanctions_for_provision: SPARQL query failed for %r",
            uri,
            exc_info=True,
        )
        return []

    return _rows_to_sanctions(rows)


def list_sanctions_for_act(
    act_uri: str,
    *,
    sparql_client: SparqlClient | None = None,
) -> list[SanctionRow]:
    """Return every Sanction attached to any provision of *act_uri*.

    Walks the graph ``?provision estleg:partOf <act_uri>`` then
    ``?provision estleg:hasSanction ?sanction`` and aggregates.

    Args:
        act_uri: The Act URI. Empty / whitespace input yields ``[]``.
        sparql_client: Optional :class:`SparqlClient` override.

    Returns:
        A list of :class:`SanctionRow` for every Sanction attached to
        a member provision of the act. ``[]`` on no matches / SPARQL
        error.
    """
    uri = (act_uri or "").strip()
    if not uri:
        return []

    client = sparql_client if sparql_client is not None else SparqlClient()
    try:
        rows = client.query(
            _ACT_SANCTIONS_QUERY,
            uri_bindings={"act": uri},
        )
    except Exception:
        logger.warning(
            "list_sanctions_for_act: SPARQL query failed for %r",
            uri,
            exc_info=True,
        )
        return []

    return _rows_to_sanctions(rows)


def find_similar_sanctions(
    sanction_row: SanctionRow,
    *,
    limit: int = 10,
    sparql_client: SparqlClient | None = None,
) -> list[SanctionRow]:
    """Find sanctions in *other* acts with matching type and overlapping range.

    Simple range overlap on ``min/maxPenaltyAmount`` — not ML
    similarity (per the plan note: "uses simple range overlap, not ML
    similarity"). Two ranges [a, b] and [c, d] overlap iff
    ``a <= d AND c <= b``; a missing bound on the candidate row is
    treated as open-ended (we don't drop rows that only have one
    bound, since they may legitimately match).

    Args:
        sanction_row: The seed row. Must have ``sanction_type`` set;
            empty type yields ``[]``. The seed's ``act_uri`` is
            excluded from results so the comparison list only shows
            sanctions in *other* acts.
        limit: Cap the result list (default 10). Hard-capped to
            :data:`_MAX_SIMILAR_SANCTIONS` so the SPARQL query stays
            quick on a 1M-triple corpus.
        sparql_client: Optional :class:`SparqlClient` override.

    Returns:
        A list of :class:`SanctionRow` — comparable sanctions from
        other acts, sorted by act URI. ``[]`` when no sanction_type
        is set, when no overlaps exist, or on SPARQL error.
    """
    sanction_type = (sanction_row.sanction_type or "").strip()
    if not sanction_type:
        return []

    # Treat missing seed bounds as fully open so they overlap any
    # range. Using huge sentinel-like floats keeps the FILTER simple
    # (no nested BOUND checks on the seed side).
    seed_min = sanction_row.min_amount if sanction_row.min_amount is not None else 0.0
    seed_max = sanction_row.max_amount if sanction_row.max_amount is not None else 1.0e18

    client = sparql_client if sparql_client is not None else SparqlClient()
    try:
        rows = client.query(
            _SIMILAR_SANCTIONS_QUERY,
            bindings={
                "type": sanction_type,
                "seedMin": str(seed_min),
                "seedMax": str(seed_max),
                "seedAct": sanction_row.act_uri or "",
            },
        )
    except Exception:
        logger.warning(
            "find_similar_sanctions: SPARQL query failed for type=%r",
            sanction_type,
            exc_info=True,
        )
        return []

    parsed = _rows_to_sanctions(rows)
    # Defence in depth — also filter on the Python side in case the
    # SPARQL ``FILTER(STR(?act) != ?seedAct)`` lets a blank-node act
    # through (BOUND(?act) is false ⇒ the FILTER short-circuits true).
    if sanction_row.act_uri:
        parsed = [r for r in parsed if r.act_uri != sanction_row.act_uri]
    # Honour the caller's limit on top of the SPARQL LIMIT.
    capped_limit = max(0, min(limit, _MAX_SIMILAR_SANCTIONS))
    return parsed[:capped_limit]


# ---------------------------------------------------------------------------
# Internal — row → SanctionRow
# ---------------------------------------------------------------------------


def _rows_to_sanctions(rows: list[dict[str, str]]) -> list[SanctionRow]:
    """Convert SPARQL JSON binding rows into :class:`SanctionRow` instances.

    Numeric fields are coerced with :func:`_as_float` (lenient: a
    malformed literal yields ``None``, not a crash). Boolean fields go
    through :func:`_as_bool`. Empty / blank-node URIs are kept as
    empty strings so the caller's ``if`` checks stay simple.
    """
    out: list[SanctionRow] = []
    for row in rows or []:
        out.append(
            SanctionRow(
                sanction_uri=(row.get("sanction") or "").strip(),
                provision_uri=(row.get("provision") or "").strip(),
                provision_label=(row.get("provisionLabel") or "").strip(),
                act_uri=(row.get("act") or "").strip(),
                act_label=(row.get("actLabel") or "").strip(),
                sanction_type=(row.get("sanctionType") or "").strip(),
                min_amount=_as_float(row.get("minAmount")),
                max_amount=_as_float(row.get("maxAmount")),
                min_unit=(row.get("minUnit") or "").strip(),
                max_unit=(row.get("maxUnit") or "").strip(),
                min_currency=_as_optional_str(row.get("minCurrency")),
                max_currency=_as_optional_str(row.get("maxCurrency")),
                enforced_at_level=(row.get("enforcedAtLevel") or "").strip(),
                is_statutory_default=_as_bool(row.get("isStatutoryDefault")),
            )
        )
    return out


def _as_float(value: Any) -> float | None:
    """Coerce a SPARQL string-typed numeric literal to ``float``; ``None`` on error.

    The :class:`SparqlClient` JSON extractor strips RDF types so
    ``"21.5"`` and ``"21"`` both arrive as plain strings; ``float``
    handles both. A blank value, ``None``, or a non-numeric string
    yields ``None`` so the row keeps the missing-bound semantics.
    """
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def _as_bool(value: Any) -> bool | None:
    """Coerce an ``xsd:boolean`` literal value to ``bool``; ``None`` on absence."""
    if value is None:
        return None
    s = str(value).strip().lower()
    if not s:
        return None
    if s in ("true", "1"):
        return True
    if s in ("false", "0"):
        return False
    return None


def _as_optional_str(value: Any) -> str | None:
    """Return *value* trimmed to ``str``, or ``None`` for empty / missing."""
    if value is None:
        return None
    s = str(value).strip()
    return s or None
