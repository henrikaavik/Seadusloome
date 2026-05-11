"""Free-text → EU legal act lookup for the EL ülevõtt workflow (#723).

The EL ülevõtt workflow accepts a CELEX number *or* an EU act title /
policy area. CELEX inputs resolve cleanly through
:class:`app.docs.reference_resolver.ReferenceResolver` (regex →
``estleg:celexNumber`` SPARQL lookup). A free-text title has no such
path — the resolver does **not** resolve EU acts by label — so this
module does a small case-insensitive substring search on every
``estleg:EULegislation`` node's ``rdfs:label`` and hands the route a
list of candidates to disambiguate (it never silently picks one).

The search term goes into a SPARQL ``FILTER(CONTAINS(LCASE(?label),
LCASE(?q)))`` *string-literal* context, so it is bound via a
``VALUES ?q { "…" }`` clause (``SparqlClient.query(..., bindings=…)``)
— properly escaped, never string-interpolated.

A dead Jena (or any SPARQL error) ⇒ ``[]`` so the route degrades to
the "ei tuvastanud EL õigusakti" warning rather than 500.
"""

from __future__ import annotations

import logging
from typing import Any

from app.docs.impact.queries import PREFIXES
from app.ontology.sparql_client import SparqlClient

logger = logging.getLogger(__name__)

# Cap the candidate list — the disambiguation card stays scannable, and
# a 2-char query like "EL" shouldn't dump hundreds of rows.
_MAX_EU_CANDIDATES = 10

# Don't even hit Jena for a query this short — too noisy to be useful.
_MIN_QUERY_LEN = 2

_EU_LABEL_SEARCH = (
    PREFIXES
    + """
SELECT DISTINCT ?euAct ?label ?celex
WHERE {
  ?euAct a estleg:EULegislation .
  ?euAct rdfs:label ?label .
  OPTIONAL { ?euAct estleg:celexNumber ?celex }
  FILTER(CONTAINS(LCASE(STR(?label)), LCASE(?q)))
}
ORDER BY ?label
LIMIT """
    + str(_MAX_EU_CANDIDATES)
    + "\n"
)


def search_eu_acts_by_label(
    text: str,
    *,
    sparql_client: SparqlClient | None = None,
) -> list[dict[str, Any]]:
    """Return ``estleg:EULegislation`` candidates whose label contains *text*.

    Args:
        text: The free-text title / policy-area string from the search
            box. Stripped; values shorter than two characters return
            ``[]`` (too broad to be useful).
        sparql_client: Optional :class:`SparqlClient` override (tests
            inject one whose ``.query`` is mocked).

    Returns:
        A list (≤ :data:`_MAX_EU_CANDIDATES`) of dicts shaped
        ``{"uri": str, "label": str, "celex": str|None}``, newest-label
        order. ``[]`` when nothing matches, the query is too short, or
        any SPARQL error occurs (the route then shows the "ei tuvastanud
        EL õigusakti" warning).
    """
    q = (text or "").strip()
    if len(q) < _MIN_QUERY_LEN:
        return []

    client = sparql_client if sparql_client is not None else SparqlClient()
    try:
        rows = client.query(_EU_LABEL_SEARCH, bindings={"q": q})
    except Exception:
        logger.warning("search_eu_acts_by_label: SPARQL query failed for q=%r", q, exc_info=True)
        return []

    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows or []:
        uri = str(row.get("euAct") or "").strip()
        if not uri or uri in seen:
            continue
        seen.add(uri)
        label = str(row.get("label") or "").strip() or uri
        celex = str(row.get("celex") or "").strip() or None
        out.append({"uri": uri, "label": label, "celex": celex})
        if len(out) >= _MAX_EU_CANDIDATES:
            break
    return out
