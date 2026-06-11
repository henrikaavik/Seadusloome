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
* :func:`list_sanctions_for_act` joins on the literal ``estleg:sourceAct``
  title (the act → provision membership relation; the Wave 2 diagnostic
  spike in ``docs/2026-05-18-bugfix-plan.md`` confirmed both
  ``estleg:partOf`` and ``estleg:partOfAct`` carry zero triples in prod
  and ``sourceAct`` is always a string literal, never a URI) and
  aggregates Sanction rows from every member provision.
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
from app.ontology.temporal_scope import (
    DEFAULT_SCOPE,
    TemporalScope,
    temporal_scope_clause,
)

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
        act_uri: Empty string in this corpus — the prod ontology does
            not carry a provision → act URI edge (see the Wave 2 spike
            in ``docs/2026-05-18-bugfix-plan.md``). Kept on the
            dataclass so the route's ``if not sr.act_uri:`` guard
            keeps surfacing the label-only heading rather than a
            broken link.
        act_label: The literal ``estleg:sourceAct`` title (e.g.
            ``"Karistusseadustik"``). May be empty when the provision
            has no ``sourceAct`` edge.
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
#
# Temporal scope (#850): every template binds ``?provision`` (a
# ``LegalProvision``) so the current-law filter from
# :mod:`app.ontology.temporal_scope` drops sanctions attached to
# provisions of positively-repealed acts. The default is current law;
# :attr:`TemporalScope.ALL` keeps repealed-act sanctions. The templates
# are now builder *functions* (rather than module constants) so the
# scope clause can be spliced at call time.


def _build_provision_sanctions_query(scope: TemporalScope = DEFAULT_SCOPE) -> str:
    """Sanctions attached to a single provision, scoped by *scope* (#850)."""
    return (
        PREFIXES
        + f"""
SELECT ?sanction ?provision ?provisionLabel
       ?actLit
       ?sanctionType
       ?minAmount ?maxAmount
       ?minUnit ?maxUnit
       ?minCurrency ?maxCurrency
       ?enforcedAtLevel
       ?isStatutoryDefault
WHERE {{
  ?provision estleg:hasSanction ?sanction .
  OPTIONAL {{ ?provision rdfs:label ?provisionLabel }}
  OPTIONAL {{ ?provision estleg:sourceAct ?actLit }}
  OPTIONAL {{ ?sanction estleg:sanctionType ?sanctionType }}
  OPTIONAL {{ ?sanction estleg:minPenaltyAmount ?minAmount }}
  OPTIONAL {{ ?sanction estleg:maxPenaltyAmount ?maxAmount }}
  OPTIONAL {{ ?sanction estleg:minPenaltyUnit ?minUnit }}
  OPTIONAL {{ ?sanction estleg:maxPenaltyUnit ?maxUnit }}
  OPTIONAL {{ ?sanction estleg:minPenaltyCurrency ?minCurrency }}
  OPTIONAL {{ ?sanction estleg:maxPenaltyCurrency ?maxCurrency }}
  OPTIONAL {{ ?sanction estleg:enforcedAtLevel ?enforcedAtLevel }}
  OPTIONAL {{ ?sanction estleg:isStatutoryDefault ?isStatutoryDefault }}
{temporal_scope_clause(scope, "provision")}
}}
ORDER BY ?provision
LIMIT {_MAX_SANCTIONS_PER_ACT}
"""
    )


def _build_act_sanctions_query(scope: TemporalScope = DEFAULT_SCOPE) -> str:
    """Sanctions across every provision of an act, scoped by *scope* (#850)."""
    return (
        PREFIXES
        + f"""
SELECT ?sanction ?provision ?provisionLabel
       ?actLit
       ?sanctionType
       ?minAmount ?maxAmount
       ?minUnit ?maxUnit
       ?minCurrency ?maxCurrency
       ?enforcedAtLevel
       ?isStatutoryDefault
WHERE {{
  ?provision estleg:sourceAct ?actLit .
  ?provision estleg:hasSanction ?sanction .
  OPTIONAL {{ ?provision rdfs:label ?provisionLabel }}
  OPTIONAL {{ ?sanction estleg:sanctionType ?sanctionType }}
  OPTIONAL {{ ?sanction estleg:minPenaltyAmount ?minAmount }}
  OPTIONAL {{ ?sanction estleg:maxPenaltyAmount ?maxAmount }}
  OPTIONAL {{ ?sanction estleg:minPenaltyUnit ?minUnit }}
  OPTIONAL {{ ?sanction estleg:maxPenaltyUnit ?maxUnit }}
  OPTIONAL {{ ?sanction estleg:minPenaltyCurrency ?minCurrency }}
  OPTIONAL {{ ?sanction estleg:maxPenaltyCurrency ?maxCurrency }}
  OPTIONAL {{ ?sanction estleg:enforcedAtLevel ?enforcedAtLevel }}
  OPTIONAL {{ ?sanction estleg:isStatutoryDefault ?isStatutoryDefault }}
{temporal_scope_clause(scope, "provision")}
}}
ORDER BY ?provision
LIMIT {_MAX_SANCTIONS_PER_ACT}
"""
    )


def _build_similar_sanctions_query(scope: TemporalScope = DEFAULT_SCOPE) -> str:
    """Comparable sanctions in *other* acts, scoped by *scope* (#850).

    Same sanctionType, other acts only, with a range-overlap filter on
    the amount bounds. We bind ``?type`` / ``?seedMin`` / ``?seedMax`` /
    ``?seedActLit`` via :meth:`SparqlClient._inject_bindings`. The
    injector emits VALUES with **string literals** (it has to — the same
    injector is used for URI strings, language tags, etc.), so the
    numeric comparisons in the FILTER cast explicitly through
    ``xsd:decimal(?seedMin)`` and ``xsd:decimal(?seedMax)``. Without the
    cast SPARQL compares lex order string-to-decimal, which silently
    returns no overlapping rows (F2, 2026-05-15 review repro: rdflib
    confirmed 0 rows for string vs 1 row for typed decimal).

    The range-overlap maths: two ranges [a, b] and [c, d] overlap iff
    a <= d AND c <= b, i.e. ``?seedMin <= ?maxAmount`` AND
    ``?minAmount <= ?seedMax``. Either bound missing on the candidate
    row passes the filter (treated as open-ended), so we don't lose
    rows that have only one numeric bound.

    Temporal scope (#850): the comparison list should not surface
    sanctions from repealed acts as live alternatives, so the default
    current-law filter is applied here too.
    """
    return (
        PREFIXES
        + f"""
SELECT ?sanction ?provision ?provisionLabel
       ?actLit
       ?sanctionType
       ?minAmount ?maxAmount
       ?minUnit ?maxUnit
       ?minCurrency ?maxCurrency
       ?enforcedAtLevel
       ?isStatutoryDefault
WHERE {{
  ?provision estleg:hasSanction ?sanction .
  ?sanction estleg:sanctionType ?sanctionType .
  OPTIONAL {{ ?provision rdfs:label ?provisionLabel }}
  OPTIONAL {{ ?provision estleg:sourceAct ?actLit }}
  OPTIONAL {{ ?sanction estleg:minPenaltyAmount ?minAmount }}
  OPTIONAL {{ ?sanction estleg:maxPenaltyAmount ?maxAmount }}
  OPTIONAL {{ ?sanction estleg:minPenaltyUnit ?minUnit }}
  OPTIONAL {{ ?sanction estleg:maxPenaltyUnit ?maxUnit }}
  OPTIONAL {{ ?sanction estleg:minPenaltyCurrency ?minCurrency }}
  OPTIONAL {{ ?sanction estleg:maxPenaltyCurrency ?maxCurrency }}
  OPTIONAL {{ ?sanction estleg:enforcedAtLevel ?enforcedAtLevel }}
  OPTIONAL {{ ?sanction estleg:isStatutoryDefault ?isStatutoryDefault }}
  FILTER(STR(?sanctionType) = ?type)
  FILTER(!BOUND(?actLit) || STR(?actLit) != ?seedActLit)
  FILTER(!BOUND(?maxAmount) || xsd:decimal(?seedMin) <= ?maxAmount)
  FILTER(!BOUND(?minAmount) || ?minAmount <= xsd:decimal(?seedMax))
{temporal_scope_clause(scope, "provision")}
}}
ORDER BY ?actLit ?provision
LIMIT {_MAX_SIMILAR_SANCTIONS}
"""
    )


# Default-scope (current-law) snapshots of the templates. Kept as
# module constants for backwards compatibility with callers / tests that
# referenced the pre-#850 ``_*_QUERY`` strings directly; new code should
# call the builder functions with an explicit scope.
_PROVISION_SANCTIONS_QUERY = _build_provision_sanctions_query()
_ACT_SANCTIONS_QUERY = _build_act_sanctions_query()
_SIMILAR_SANCTIONS_QUERY = _build_similar_sanctions_query()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def list_sanctions_for_provision(
    provision_uri: str,
    *,
    scope: TemporalScope = DEFAULT_SCOPE,
    sparql_client: SparqlClient | None = None,
) -> list[SanctionRow]:
    """Return every Sanction attached to *provision_uri*.

    Args:
        provision_uri: A ``LegalProvision`` URI (the
            ``estleg:hasSanction`` subject). Empty / whitespace
            input yields ``[]`` without hitting Jena.
        scope: Temporal scope (#850). Default current law — a sanction
            on a positively-repealed provision / act is dropped;
            :attr:`TemporalScope.ALL` keeps it.
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
            _build_provision_sanctions_query(scope),
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
    act_title: str,
    *,
    scope: TemporalScope = DEFAULT_SCOPE,
    sparql_client: SparqlClient | None = None,
) -> list[SanctionRow]:
    """Return every Sanction attached to any provision of *act_title*.

    Walks the graph ``?provision estleg:sourceAct "<act_title>"`` then
    ``?provision estleg:hasSanction ?sanction`` and aggregates.

    The act join is on the **literal title string** (e.g.
    ``"Karistusseadustik"``) because the prod ontology stores
    ``estleg:sourceAct`` as a string literal rather than a URI — the
    Wave 2 spike in ``docs/2026-05-18-bugfix-plan.md`` confirmed that
    ``estleg:partOf`` / ``estleg:partOfAct`` carry zero triples and
    ``sourceAct`` is the only working provision → act join in the
    corpus. The parameter used to be an act URI; callers were updated
    in the same patch to pass the title string instead.

    Args:
        act_title: The Act's title as a string literal (matches the
            ``estleg:sourceAct`` literal value on provisions). Empty /
            whitespace input yields ``[]`` without hitting Jena. If a
            caller passes a URI here by mistake the query simply returns
            no rows (no triples have a URI on the right-hand side of
            ``sourceAct``) — degrades gracefully rather than 500-ing.
        scope: Temporal scope (#850). Default current law excludes
            sanctions on provisions of a positively-repealed act;
            :attr:`TemporalScope.ALL` includes them.
        sparql_client: Optional :class:`SparqlClient` override.

    Returns:
        A list of :class:`SanctionRow` for every Sanction attached to
        a member provision of the act. ``[]`` on no matches / SPARQL
        error.
    """
    title = (act_title or "").strip()
    if not title:
        return []

    client = sparql_client if sparql_client is not None else SparqlClient()
    try:
        rows = client.query(
            _build_act_sanctions_query(scope),
            bindings={"actLit": title},
        )
    except Exception:
        logger.warning(
            "list_sanctions_for_act: SPARQL query failed for %r",
            title,
            exc_info=True,
        )
        return []

    return _rows_to_sanctions(rows)


def find_similar_sanctions(
    sanction_row: SanctionRow,
    *,
    limit: int = 10,
    scope: TemporalScope = DEFAULT_SCOPE,
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
            empty type yields ``[]``. The seed's ``act_label`` (the
            literal ``sourceAct`` title) is excluded from results so
            the comparison list only shows sanctions in *other* acts.
        limit: Cap the result list (default 10). Hard-capped to
            :data:`_MAX_SIMILAR_SANCTIONS` so the SPARQL query stays
            quick on a 1M-triple corpus.
        scope: Temporal scope (#850). Default current law — comparable
            sanctions are not drawn from positively-repealed acts;
            :attr:`TemporalScope.ALL` widens to the full corpus.
        sparql_client: Optional :class:`SparqlClient` override.

    Returns:
        A list of :class:`SanctionRow` — comparable sanctions from
        other acts, sorted by the literal act title. ``[]`` when no
        sanction_type is set, when no overlaps exist, or on SPARQL
        error.
    """
    sanction_type = (sanction_row.sanction_type or "").strip()
    if not sanction_type:
        return []

    # Treat missing seed bounds as fully open so they overlap any
    # range. Using huge sentinel-like values keeps the FILTER simple
    # (no nested BOUND checks on the seed side).
    #
    # F7 (2026-05-15 review): the upper-bound sentinel must be an
    # **integer**, not a float. ``str(1.0e18)`` emits ``"1e+18"`` and
    # ``xsd:decimal`` does not accept exponential notation in its
    # lexical form (the grammar is ``[+-]?[0-9]+(\\.[0-9]+)?``) — Apache
    # Jena's ``xsd:decimal(?seedMax)`` cast then returns an empty
    # binding and the min-only-seed row count goes to zero. An ``int``
    # large enough to dominate any real penalty range serialises as a
    # plain run of digits and stays a valid xsd:decimal lexical form.
    seed_min = sanction_row.min_amount if sanction_row.min_amount is not None else 0.0
    seed_max: float | int = (
        sanction_row.max_amount if sanction_row.max_amount is not None else 10**18
    )

    # The "other acts" filter joins on the literal ``estleg:sourceAct``
    # title (carried on :class:`SanctionRow` in ``act_label`` — the
    # row-builder always assigns the literal there, see
    # :func:`_rows_to_sanctions`). The prod ontology has no act URIs on
    # provisions, so an empty seed title legitimately means "skip the
    # other-act filter" rather than "no comparable rows".
    seed_act_lit = sanction_row.act_label or ""

    client = sparql_client if sparql_client is not None else SparqlClient()
    try:
        rows = client.query(
            _build_similar_sanctions_query(scope),
            bindings={
                "type": sanction_type,
                "seedMin": _xsd_decimal_literal(seed_min),
                "seedMax": _xsd_decimal_literal(seed_max),
                "seedActLit": seed_act_lit,
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
    # SPARQL ``FILTER(STR(?actLit) != ?seedActLit)`` lets an unbound
    # actLit through (BOUND(?actLit) is false ⇒ the FILTER short-circuits
    # true). The comparison is on the literal title we just bound.
    if seed_act_lit:
        parsed = [r for r in parsed if r.act_label != seed_act_lit]
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

    The ``act_label`` carries the literal ``estleg:sourceAct`` title and
    ``act_uri`` is always empty — the Wave 2 prod spike showed
    ``sourceAct`` is a string literal in this corpus, never a URI.
    Downstream renderers that gated link-rendering on ``act_uri`` (e.g.
    :func:`app.analyysikeskus.routes._padevused_act_heading`) degrade
    gracefully to a label-only heading.
    """
    out: list[SanctionRow] = []
    for row in rows or []:
        out.append(
            SanctionRow(
                sanction_uri=(row.get("sanction") or "").strip(),
                provision_uri=(row.get("provision") or "").strip(),
                provision_label=(row.get("provisionLabel") or "").strip(),
                act_uri="",
                act_label=(row.get("actLit") or "").strip(),
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


def _xsd_decimal_literal(value: float | int) -> str:
    """Format ``value`` as a valid ``xsd:decimal`` lexical form.

    ``xsd:decimal``'s grammar is ``[+-]?[0-9]+(\\.[0-9]+)?`` — no
    exponent allowed. Python's ``str()`` emits exponential notation
    for large floats (``str(1.0e18) == "1e+18"``), which Apache Jena
    rejects when used with the ``xsd:decimal(?seedMax)`` constructor
    inside the similar-sanctions FILTER and silently drops the row
    (F7, 2026-05-15 review).

    This helper:

    * Returns ``"0"`` for ``0`` (any flavour) — the shortest valid form.
    * Returns ``str(int(v))`` when ``v`` is integral — no trailing
      ``.0``, never an exponent.
    * Otherwise renders the float in non-exponential fixed-point form
      and trims trailing zeros / lone decimal points.

    Examples:
        >>> _xsd_decimal_literal(0)
        '0'
        >>> _xsd_decimal_literal(0.0)
        '0'
        >>> _xsd_decimal_literal(100.0)
        '100'
        >>> _xsd_decimal_literal(100.5)
        '100.5'
        >>> _xsd_decimal_literal(10**18)
        '1000000000000000000'
        >>> _xsd_decimal_literal(1.0e18)
        '1000000000000000000'
    """
    if value == 0:
        return "0"
    if isinstance(value, int) or value == int(value):
        return str(int(value))
    # repr() keeps the original precision for small/normal floats; for
    # extremes (1e20) it switches to exponential, so we force fixed-point
    # and prune the trailing zero/period.
    s = repr(value)
    if "e" in s or "E" in s:
        s = f"{value:.10f}"
    return s.rstrip("0").rstrip(".") or "0"


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
