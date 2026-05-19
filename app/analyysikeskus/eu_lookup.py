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
import re
from typing import Any

from app.docs.impact.queries import PREFIXES
from app.ontology.sparql_client import SparqlClient

logger = logging.getLogger(__name__)

# Canonical CELEX shape: 1-digit sector (1-9) + 4-digit year + single
# uppercase form letter (R/L/D/H/…) + 4-digit running number.
# Examples: ``32016R0679`` (GDPR), ``32019L1152`` (Working Conditions).
# Counter-examples: ``12abc34``, ``GDPR``, ``directive 95/46``,
# ``32016X0679`` (invalid form letter — see below).
#
# Form-letter whitelist: the documented binding-instrument letters that
# a Seadusloome user is realistically going to look up. ``X`` and other
# rarely-used letters are deliberately excluded so the warning message
# stays a high-signal "this looks like a real CELEX missing from data"
# rather than firing on every alphanumeric near-CELEX shape. Tightening
# this set would require a follow-up change with user data on which
# letters get typed in practice.
#
# Note this is intentionally STRICTER than the matchers in
# :mod:`app.analyysikeskus.input_parser` (``\\d{5}[A-Z]\\d{1,4}``).
# Those have to accept sloppy CELEX shapes embedded in pasted phrases
# so the resolver gets a chance to look them up. This helper is the
# user-facing "is this a CELEX we should report as canonical-but-missing
# from the ontology" check — when it matches, we tell the user "this
# looks like a real CELEX number, it's just not in our data yet";
# when it doesn't, we keep the generic "ei tuvastatud" message.
_CANONICAL_CELEX_RE = re.compile(r"^[1-9]\d{4}[RLDHSFGABCEJMOPQTUK]\d{4}$")


def is_canonical_celex_shape(s: str) -> bool:
    """True if *s* looks like a real CELEX number.

    Case-insensitive: ``32016R0679`` and ``32016r0679`` both match.
    The resolver itself uppercases lowercase form-letters before
    SPARQL lookup (see :func:`app.docs.reference_resolver._normalize_celex`),
    so the user-facing classifier must do the same — otherwise lowercase
    CELEX input that the resolver *would* recognise still fell through
    to the generic "ei tuvastatud" message.

    Used by the EL ülevõtt route (#805) and the impact-report renderer
    (#815) to distinguish "user typed a canonical-shaped CELEX that
    happens to be missing from our ontology" from "user typed prose /
    garbage that doesn't look like an EU reference at all". When True,
    the UI surfaces a "kontrollige käsitsi" warning naming the CELEX;
    when False, it shows the generic "Ei tuvastanud EL õigusakti" hint.

    The regex matches the canonical CELEX shape:

    * Sector: a single non-zero digit (``1`` – ``9``) — sector ``0``
      doesn't exist in EurLex.
    * Year: four digits (``1957`` – present).
    * Form letter: one uppercase ASCII letter drawn from the binding-
      instrument whitelist (``R`` regulation, ``L`` directive,
      ``D`` decision, ``H`` recommendation, …). The rarely-used ``X``
      ("other") is deliberately excluded — see the regex comment
      above for the rationale.
    * Running number: four digits (zero-padded).

    Args:
        s: The candidate string. Leading/trailing whitespace is
            stripped before matching; an empty/blank input returns
            ``False``.

    Returns:
        ``True`` when *s* (stripped) matches the canonical CELEX shape;
        ``False`` otherwise. Never raises.

    Examples:
        >>> is_canonical_celex_shape("32016R0679")  # GDPR
        True
        >>> is_canonical_celex_shape("32016r0679")  # lowercase form letter
        True
        >>> is_canonical_celex_shape("32019L1152")  # Working Conditions
        True
        >>> is_canonical_celex_shape("12abc34")
        False
        >>> is_canonical_celex_shape("GDPR")
        False
        >>> is_canonical_celex_shape("")
        False
    """
    stripped = (s or "").strip()
    if not stripped:
        return False
    # Uppercase the form letter (and any incidental letter casing) before
    # the strict whitelist match. The resolver itself uppercases CELEX
    # form letters before SPARQL lookup, so this helper must agree.
    return bool(_CANONICAL_CELEX_RE.match(stripped.upper()))


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
