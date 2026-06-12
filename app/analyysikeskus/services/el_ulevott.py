"""EL ülevõtt ja harmoneerimine — framework-free service (#860, Phase-5 ref).

``analyse_el_ulevott(sisend)`` resolves the user's input to one EU-act URI and
returns a typed result describing one of three outcomes:

* :class:`ElTranspositionResult` — the input resolved to exactly one EU act
  (via a CELEX through :class:`ReferenceResolver`, or a single label-search
  hit); the act/provision-level transposition rows were fetched via
  :func:`app.impact.eu_transposition.run_eu_transposition` (entity-centred, no
  synthetic graph). ``rows`` is empty when Jena is unreachable — the caller
  shows a graceful "ei õnnestunud" line, not a 500.
* :class:`ElUlevottDisambiguation` — a free-text title/policy area matched
  several candidate EU acts; the caller offers them as choices.
* :class:`ElUlevottUnresolved` — nothing resolved. ``canonical_celex_shape``
  flags whether the input *looked* like a well-formed CELEX that simply isn't
  in the ontology yet (so the caller can show the specific "not mapped"
  message vs the generic hint).

There are **no** ``fasthtml`` / ``starlette`` imports here. The route wraps
this by matching on the result type and rendering each branch through
``analysis_result_shell``; a Phase-5 REST endpoint / MCP tool serialises the
dataclass to JSON.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from app.analyysikeskus.eu_lookup import is_canonical_celex_shape, search_eu_acts_by_label
from app.analyysikeskus.input_parser import parse_user_reference
from app.docs.reference_resolver import ReferenceResolver
from app.impact.eu_transposition import run_eu_transposition

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Typed results
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ElCandidate:
    """One EU-act candidate for the disambiguation outcome.

    Mirrors the ``search_eu_acts_by_label`` row shape the route renders:
    ``uri`` (the EU-act URI), ``label`` (its title), ``celex`` (the CELEX
    number, when known).
    """

    uri: str
    label: str
    celex: str | None = None


@dataclass(frozen=True)
class ElTranspositionResult:
    """The input resolved to one EU act; transposition rows were fetched.

    ``rows`` is the raw normalised output of :func:`run_eu_transposition`
    (a list of dicts) — kept as-is so the route renders the table exactly as
    before and a JSON wrapper serialises it verbatim. ``eu_label`` / ``celex``
    are refreshed from the first row when the runner saw a richer value on the
    act itself.
    """

    kind: str = field(default="transposition", init=False)
    eu_act_uri: str = ""
    eu_label: str = "EL õigusakt"
    celex: str | None = None
    rows: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class ElUlevottDisambiguation:
    """A free-text input matched several candidate EU acts."""

    kind: str = field(default="disambiguation", init=False)
    candidates: list[ElCandidate] = field(default_factory=list)


@dataclass(frozen=True)
class ElUlevottUnresolved:
    """Nothing resolved. ``canonical_celex_shape`` picks the message variant."""

    kind: str = field(default="unresolved", init=False)
    canonical_celex_shape: bool = False


ElUlevottResult = ElTranspositionResult | ElUlevottDisambiguation | ElUlevottUnresolved


# ---------------------------------------------------------------------------
# Internal composition helpers (framework-free)
# ---------------------------------------------------------------------------


def _resolve_eu_act_from_celex(refs: list[Any]) -> Any | None:
    """Resolve the first ``eu_act`` ref via :class:`ReferenceResolver`.

    Returns the single resolved ref (with ``entity_uri`` set) when exactly one
    EU act resolves, else ``None``. A dead Jena / resolver crash ⇒ ``None``.
    """
    eu_refs = [r for r in refs if getattr(r, "ref_type", "") == "eu_act"]
    if not eu_refs:
        return None
    try:
        resolved = ReferenceResolver().resolve(eu_refs)
    except Exception:
        logger.warning("EL ülevõtt service: CELEX resolution failed", exc_info=True)
        return None
    with_uri = [
        r for r in resolved if getattr(r, "entity_uri", None) and str(r.entity_uri).strip()
    ]
    if len(with_uri) == 1:
        return with_uri[0]
    return None


def _eu_label_search(sisend: str) -> list[dict[str, Any]]:
    """Free-text → EU-act candidates; an unreachable Jena yields ``[]``."""
    try:
        return search_eu_acts_by_label(sisend)
    except Exception:
        logger.warning("EL ülevõtt service: EU-act label search failed", exc_info=True)
        return []


def _transposition_for(eu_act_uri: str, eu_label: str, celex: str | None) -> ElTranspositionResult:
    """Run the transposition query and build the typed result.

    Refreshes ``eu_label`` / ``celex`` from the first row (the runner may see a
    richer value on the act itself than the resolver / label-search handed us).
    """
    rows = run_eu_transposition(eu_act_uri)
    if rows:
        eu_label = str(rows[0].get("eu_label") or eu_label) or eu_label
        celex = (str(rows[0].get("celex") or "").strip() or celex) or celex
    return ElTranspositionResult(eu_act_uri=eu_act_uri, eu_label=eu_label, celex=celex, rows=rows)


# ---------------------------------------------------------------------------
# Public service function
# ---------------------------------------------------------------------------


def analyse_el_ulevott(sisend: str) -> ElUlevottResult:
    """Resolve *sisend* to an EU act and fetch its transposition overview.

    Args:
        sisend: The user's free-text input — a CELEX number, an EU-act title,
            or a policy area. Must be non-empty and already ``.strip()``-ed.

    Returns:
        One of :data:`ElUlevottResult` — a frozen dataclass discriminated by
        its ``kind`` field. Never raises for a dead Jena: the CELEX/label paths
        degrade to :class:`ElUlevottUnresolved`, and a resolved act with an
        unreachable transposition query yields an empty ``rows`` list.
    """
    sisend = (sisend or "").strip()
    parsed_refs = parse_user_reference(sisend)

    # --- 1. CELEX path -----------------------------------------------------
    resolved_eu = _resolve_eu_act_from_celex(parsed_refs)
    if resolved_eu is not None:
        eu_uri = str(resolved_eu.entity_uri)
        matched_label = str(getattr(resolved_eu, "matched_label", "") or "").strip()
        extracted = getattr(resolved_eu, "extracted", None)
        celex = str(getattr(extracted, "ref_text", "") or "").strip() or None
        return _transposition_for(eu_uri, matched_label or celex or "EL õigusakt", celex)

    # --- 2. label-search path ---------------------------------------------
    candidates = _eu_label_search(sisend)
    if len(candidates) == 1:
        only = candidates[0]
        return _transposition_for(
            str(only.get("uri") or ""),
            str(only.get("label") or "EL õigusakt"),
            str(only.get("celex") or "").strip() or None,
        )
    if len(candidates) > 1:
        return ElUlevottDisambiguation(
            candidates=[
                ElCandidate(
                    uri=str(c.get("uri") or ""),
                    label=str(c.get("label") or "EL õigusakt"),
                    celex=str(c.get("celex") or "").strip() or None,
                )
                for c in candidates
            ]
        )

    # Nothing resolved.
    return ElUlevottUnresolved(canonical_celex_shape=is_canonical_celex_shape(sisend))
