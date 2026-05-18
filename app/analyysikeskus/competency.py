"""SPARQL helpers for the Pädevuste kaardistus workflow (A3 v1).

Task A3 from ``docs/2026-05-15-ontology-six-use-cases-plan.md`` (section 5,
Direction A, lines 300-307).

**Scope of v1.** Institution-level grouping only. We answer two questions
for a chosen Estonian state institution (e.g. ``Andmekaitse Inspektsioon``):

1. **Which powers (volitused) does this institution hold today, grouped by
   act?** A power, in ontology terms, is a ``LegalProvision`` whose
   ``estleg:competentAuthority`` points at the institution — every such
   provision is one power vested in that body. We bucket them by the
   literal ``estleg:sourceAct`` title (the Wave 2 spike in
   ``docs/2026-05-18-bugfix-plan.md`` confirmed both ``estleg:partOf``
   and ``estleg:partOfAct`` carry zero triples in prod and the only
   working provision-to-act join is the literal ``sourceAct`` edge) so
   the result reads as a per-act list of competence rows ("In
   *Karistusseadustik* this institution has the following 12 powers …").
2. **Where does this institution's competence overlap with another body?**
   An overlap is a provision that has at least *two distinct* institutions
   on the ``competentAuthority`` predicate — i.e. the same legal power is
   nominally vested in more than one body. The query projects the
   provision + every other institution on it; the route renders one row
   per overlap.

**What v1 cannot answer yet.** The ontology audit (plan section 5.5)
confirmed that the ``Competence`` class is reified
(``estleg:Competence`` nodes carrying ``estleg:institution``,
``estleg:competenceType``, ``estleg:appliesToProvision``,
``estleg:appliesToProvisionCount``) but two critical fields for v2 are
**not** populated corpus-wide and the SHACL shape that would constrain
them (``CompetenceShape``) has not been merged in the source ontology:

* ``estleg:competenceArea`` — would let us group powers by *area*
  (data-protection, taxation, traffic, ...) instead of by act.
* ``estleg:grantedBy`` — would let us answer "which act granted this
  competence" for the v2 evidence trail.

Until the ontology issue (#215 in
``henrikaavik/estonian-legal-ontology``) merges and the data is
re-published, **gap analysis** — competence areas with no assigned
institution — is also impossible: we don't yet know the canonical set
of areas. We therefore explicitly **defer** the "by area" grouping and
the gap-analysis section to v2 and surface a clear note on the result
page so a ministry lawyer does not mistake the institution-level view
for the full picture.

The single source of truth for the predicate URIs used here is
:class:`app.ontology.relations.PREDICATES` (``competentAuthority``).
This module imports the prefix block + helpers and never spells out
``estleg:competentAuthority`` as a hard-coded string in user-visible
logic.

A dead Jena ⇒ ``[]`` everywhere — the route then renders the friendly
"pädevusi ei leitud" branch instead of 500-ing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from app.ontology.queries import PREFIXES
from app.ontology.sparql_client import SparqlClient

logger = logging.getLogger(__name__)


# Cap the institution-name lookup so a 2-char query like "RT" doesn't
# dump every state body on the page.
_MAX_INSTITUTION_CANDIDATES = 15

# Don't even hit Jena for a query this short — too noisy to be useful.
_MIN_QUERY_LEN = 2

# Cap how many competence provisions we project for one institution. A
# heavyweight body (Maksu- ja Tolliamet) holds hundreds of powers across
# many acts; the v1 page truncates the list with a note rather than
# rendering a 500-row table.
_MAX_COMPETENCES_PER_INSTITUTION = 300

# Cap how many overlap rows we project. Overlaps are by definition rarer
# than direct competence rows but the cap keeps the worst case bounded.
_MAX_OVERLAP_ROWS = 200


# ---------------------------------------------------------------------------
# Dataclasses — structured rows the route renders
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InstitutionCandidate:
    """One ``estleg:Institution`` candidate from a free-text label search.

    The free-text input branch surfaces these as clickable disambiguation
    rows — the user names a body, we never silently pick.

    Attributes:
        uri: The Institution's URI (canonical ``estleg:Institution_*`` /
            ``estleg:Issuer_*`` shape; both are state-body Institutions
            per the audit).
        label: ``rdfs:label`` on the Institution.
    """

    uri: str
    label: str


@dataclass(frozen=True)
class CompetenceRow:
    """One competence row: a single ``LegalProvision`` vested in the institution.

    The provision is the "power" — its ``rdfs:label`` is what a lawyer
    reads as the description of the competence. The act bucket comes
    from the provision's literal ``estleg:sourceAct`` edge so the route
    can group powers by act without re-querying.

    Attributes:
        provision_uri: The ``LegalProvision`` URI carrying the
            ``competentAuthority`` edge.
        provision_label: ``rdfs:label`` on the provision — the readable
            description of the power.
        act_uri: Always empty in prod — the corpus carries no act URI
            edge on provisions (see Wave 2 spike). Kept on the
            dataclass so the route's "render as a link only when a URI
            is set" guard short-circuits to a label-only heading.
        act_label: The literal ``estleg:sourceAct`` title (e.g.
            ``"Karistusseadustik"``). May be empty for orphan
            provisions; the route then shows the row under a "Muud"
            bucket.
    """

    provision_uri: str
    provision_label: str
    act_uri: str = ""
    act_label: str = ""


@dataclass(frozen=True)
class OverlapRow:
    """One overlap row: a provision that vests powers in more than one body.

    The route renders one row per (provision, other-institution) pair.
    The seed institution itself is filtered out — overlaps are *with
    other* bodies, never with the seed itself.

    Attributes:
        provision_uri: The provision URI carrying multiple
            ``competentAuthority`` edges.
        provision_label: ``rdfs:label`` on the provision.
        act_uri: The owning Act URI (best-effort).
        act_label: ``rdfs:label`` on the act.
        other_institution_uri: The *other* Institution URI on the
            provision (one row per other institution; the route may
            render several rows for a 3-way overlap).
        other_institution_label: ``rdfs:label`` on the other Institution.
    """

    provision_uri: str
    provision_label: str
    act_uri: str = ""
    act_label: str = ""
    other_institution_uri: str = ""
    other_institution_label: str = ""


@dataclass(frozen=True)
class InstitutionCompetences:
    """Aggregated competence view for one institution — what A3 v1 produces.

    The route renders this directly: ``by_act`` becomes the per-act
    accordion / sub-section list; ``overlaps`` becomes the
    "Kattuvad pädevused" table; ``total_count`` feeds the summary line
    ("Kokku N pädevust X aktis"); ``truncated`` flips a warning when the
    list was capped.

    Attributes:
        institution_uri: The seed Institution URI (carried through so
            the route can build "Ava õiguskaardil" links).
        institution_label: The seed Institution's label.
        by_act: ``act_title → list[CompetenceRow]`` — provisions
            grouped by the literal ``estleg:sourceAct`` title (the
            prod corpus carries no act URIs on provisions, so we
            bucket on the literal title string instead). Orphan
            provisions with no ``sourceAct`` edge bucket under the
            empty string ``""`` and the route labels them "Muud".
            Iteration order matches insertion order from the SPARQL
            ``ORDER BY ?actLit ?provision`` so consecutive rows in the
            same act stay together.
        overlaps: List of :class:`OverlapRow` — provisions where the
            seed institution shares competence with another body.
        total_count: Total number of competence provisions (sum of
            ``by_act`` list lengths).
        truncated: ``True`` when the SPARQL LIMIT capped the result
            list at :data:`_MAX_COMPETENCES_PER_INSTITUTION`. The route
            shows a "Tulemus on kärbitud" note when this is set.
    """

    institution_uri: str
    institution_label: str
    by_act: dict[str, list[CompetenceRow]] = field(default_factory=dict)
    overlaps: list[OverlapRow] = field(default_factory=list)
    total_count: int = 0
    truncated: bool = False


# ---------------------------------------------------------------------------
# SPARQL templates
# ---------------------------------------------------------------------------
#
# All queries pin to ``estleg:Institution`` as the rdf:type so we don't
# accidentally project, say, an unrelated reified Competence node onto a
# rdfs:label search. The competence relation is always
# ``estleg:competentAuthority`` (Provision → Institution, per the audit
# at plan section 2.5 row A3).

_INSTITUTION_LABEL_SEARCH = (
    PREFIXES
    + """
SELECT DISTINCT ?institution ?label
WHERE {
  ?institution a estleg:Institution .
  ?institution rdfs:label ?label .
  FILTER(CONTAINS(LCASE(STR(?label)), LCASE(?q)))
}
ORDER BY ?label
LIMIT """
    + str(_MAX_INSTITUTION_CANDIDATES)
    + "\n"
)

# Competences for one institution — every provision whose
# ``competentAuthority`` points at the institution, with the owning act
# title joined in optionally. ``estleg:sourceAct`` is the literal-title
# membership edge in prod (the Wave 2 spike confirmed both
# ``estleg:partOf`` and ``estleg:partOfAct`` carry zero triples in this
# corpus). OPTIONAL so an orphan provision still surfaces.
_INSTITUTION_COMPETENCES_QUERY = (
    PREFIXES
    + """
SELECT ?provision ?provisionLabel ?actLit
WHERE {
  ?provision estleg:competentAuthority ?institution .
  OPTIONAL { ?provision rdfs:label ?provisionLabel }
  OPTIONAL { ?provision estleg:sourceAct ?actLit }
}
ORDER BY ?actLit ?provision
LIMIT """
    + str(_MAX_COMPETENCES_PER_INSTITUTION + 1)  # +1 sentinel so we can detect truncation
    + "\n"
)

# Overlap rows — every provision where the seed institution and at
# least one *other* institution both appear on ``competentAuthority``.
# We don't aggregate the list of other institutions here: the route
# renders one row per (provision, other) pair which keeps the table
# scannable and matches the Sanctions / EL ülevõtt row-oriented shape.
#
# ``FILTER(?other != ?institution)`` excludes self-pairs; the route
# also runs a defence-in-depth Python filter.
_INSTITUTION_OVERLAPS_QUERY = (
    PREFIXES
    + """
SELECT DISTINCT ?provision ?provisionLabel ?actLit ?other ?otherLabel
WHERE {
  ?provision estleg:competentAuthority ?institution .
  ?provision estleg:competentAuthority ?other .
  ?other a estleg:Institution .
  OPTIONAL { ?provision rdfs:label ?provisionLabel }
  OPTIONAL { ?provision estleg:sourceAct ?actLit }
  OPTIONAL { ?other rdfs:label ?otherLabel }
  FILTER(?other != ?institution)
}
ORDER BY ?actLit ?provision ?other
LIMIT """
    + str(_MAX_OVERLAP_ROWS)
    + "\n"
)

# Lookup a single Institution's rdfs:label by URI — used by
# :func:`get_institution_label` when the route only has a URI in hand
# (e.g. deep-linked from the explorer evidence card).
_INSTITUTION_LABEL_QUERY = (
    PREFIXES
    + """
SELECT ?label
WHERE {
  ?institution rdfs:label ?label .
}
LIMIT 1
"""
)


# ---------------------------------------------------------------------------
# Public API — institution lookup
# ---------------------------------------------------------------------------


def search_institutions_by_label(
    text: str,
    *,
    sparql_client: SparqlClient | None = None,
) -> list[InstitutionCandidate]:
    """Return ``estleg:Institution`` candidates whose label contains *text*.

    Args:
        text: The free-text institution-name string from the search box.
            Stripped; values shorter than two characters return ``[]``
            (too broad to be useful).
        sparql_client: Optional :class:`SparqlClient` override (tests
            inject one whose ``.query`` is mocked).

    Returns:
        A list (≤ :data:`_MAX_INSTITUTION_CANDIDATES`) of
        :class:`InstitutionCandidate` ordered by label. ``[]`` when
        nothing matches, the query is too short, or any SPARQL error
        occurs (the route then shows the "ei tuvastanud asutust"
        warning).
    """
    q = (text or "").strip()
    if len(q) < _MIN_QUERY_LEN:
        return []

    client = sparql_client if sparql_client is not None else SparqlClient()
    try:
        rows = client.query(_INSTITUTION_LABEL_SEARCH, bindings={"q": q})
    except Exception:
        logger.warning(
            "search_institutions_by_label: SPARQL query failed for q=%r",
            q,
            exc_info=True,
        )
        return []

    out: list[InstitutionCandidate] = []
    seen: set[str] = set()
    for row in rows or []:
        uri = str(row.get("institution") or "").strip()
        if not uri or uri in seen:
            continue
        seen.add(uri)
        label = str(row.get("label") or "").strip() or uri
        out.append(InstitutionCandidate(uri=uri, label=label))
        if len(out) >= _MAX_INSTITUTION_CANDIDATES:
            break
    return out


def get_institution_label(
    institution_uri: str,
    *,
    sparql_client: SparqlClient | None = None,
) -> str:
    """Return the ``rdfs:label`` for *institution_uri* (empty string on miss).

    Used when the route is invoked with a URI directly (a deep-link from
    the explorer) and we need a readable label for the page heading.
    """
    uri = (institution_uri or "").strip()
    if not uri:
        return ""

    client = sparql_client if sparql_client is not None else SparqlClient()
    try:
        rows = client.query(
            _INSTITUTION_LABEL_QUERY,
            uri_bindings={"institution": uri},
        )
    except Exception:
        logger.warning(
            "get_institution_label: SPARQL query failed for %r",
            uri,
            exc_info=True,
        )
        return ""
    if not rows:
        return ""
    return str(rows[0].get("label") or "").strip()


# ---------------------------------------------------------------------------
# Public API — competence + overlap aggregation
# ---------------------------------------------------------------------------


def list_competences_for_institution(
    institution_uri: str,
    *,
    sparql_client: SparqlClient | None = None,
) -> list[CompetenceRow]:
    """Return every competence provision vested in *institution_uri*.

    Walks ``?provision estleg:competentAuthority <institution_uri>`` and
    projects each row through :class:`CompetenceRow` with its owning act
    joined in (optional — orphan provisions surface with empty act URIs).

    Args:
        institution_uri: The ``estleg:Institution`` URI. Empty input
            returns ``[]`` without hitting Jena.
        sparql_client: Optional :class:`SparqlClient` override.

    Returns:
        Up to :data:`_MAX_COMPETENCES_PER_INSTITUTION` rows, ordered by
        ``(act, provision)`` so consecutive same-act rows cluster. The
        list may be capped — the higher-level
        :func:`gather_institution_competences` detects and surfaces this.
        ``[]`` on SPARQL error.
    """
    uri = (institution_uri or "").strip()
    if not uri:
        return []

    client = sparql_client if sparql_client is not None else SparqlClient()
    try:
        rows = client.query(
            _INSTITUTION_COMPETENCES_QUERY,
            uri_bindings={"institution": uri},
        )
    except Exception:
        logger.warning(
            "list_competences_for_institution: SPARQL query failed for %r",
            uri,
            exc_info=True,
        )
        return []

    return _rows_to_competences(rows)


def list_competence_overlaps(
    institution_uri: str,
    *,
    sparql_client: SparqlClient | None = None,
) -> list[OverlapRow]:
    """Return overlap rows — provisions where the institution shares competence.

    An overlap is a provision that has at least two distinct
    Institutions on the ``estleg:competentAuthority`` predicate. The
    query projects one row per (provision, other-institution) pair so
    a 3-way overlap surfaces as two rows.

    Args:
        institution_uri: The seed ``estleg:Institution`` URI. Empty
            input returns ``[]`` without hitting Jena.
        sparql_client: Optional :class:`SparqlClient` override.

    Returns:
        Up to :data:`_MAX_OVERLAP_ROWS` overlap rows. ``[]`` when there
        are no overlaps or on any SPARQL error.
    """
    uri = (institution_uri or "").strip()
    if not uri:
        return []

    client = sparql_client if sparql_client is not None else SparqlClient()
    try:
        rows = client.query(
            _INSTITUTION_OVERLAPS_QUERY,
            uri_bindings={"institution": uri},
        )
    except Exception:
        logger.warning(
            "list_competence_overlaps: SPARQL query failed for %r",
            uri,
            exc_info=True,
        )
        return []

    out: list[OverlapRow] = []
    for r in rows or []:
        other_uri = str(r.get("other") or "").strip()
        # Defence in depth — also drop self-pairs on the Python side
        # in case a stray reasoner round-trip lets the SPARQL FILTER
        # short-circuit.
        if not other_uri or other_uri == uri:
            continue
        # ``act_label`` carries the literal ``estleg:sourceAct`` title;
        # ``act_uri`` is always empty in prod (no act URIs on
        # provisions). See Wave 2 spike.
        out.append(
            OverlapRow(
                provision_uri=str(r.get("provision") or "").strip(),
                provision_label=str(r.get("provisionLabel") or "").strip(),
                act_uri="",
                act_label=str(r.get("actLit") or "").strip(),
                other_institution_uri=other_uri,
                other_institution_label=str(r.get("otherLabel") or "").strip(),
            )
        )
    return out


def gather_institution_competences(
    institution_uri: str,
    *,
    institution_label: str | None = None,
    sparql_client: SparqlClient | None = None,
) -> InstitutionCompetences:
    """Aggregate the full A3 v1 view for one institution.

    Combines :func:`list_competences_for_institution` and
    :func:`list_competence_overlaps` into a single
    :class:`InstitutionCompetences` ready for the route's renderers. Acts
    are bucketed in insertion order (matching the SPARQL
    ``ORDER BY ?act ?provision``) so the per-act sub-sections render
    deterministically.

    Args:
        institution_uri: The seed Institution URI.
        institution_label: Optional pre-fetched label. When ``None`` and
            the URI is non-empty, this function calls
            :func:`get_institution_label` so the result always carries a
            label (or the URI tail as a last resort).
        sparql_client: Optional :class:`SparqlClient` override — passed
            through to all three SPARQL calls so tests inject one mock.

    Returns:
        An :class:`InstitutionCompetences` with ``by_act`` /
        ``overlaps`` populated. ``total_count`` reflects the number of
        competence rows actually surfaced (after the truncation cap);
        ``truncated`` is set when the SPARQL LIMIT clipped the list.
    """
    uri = (institution_uri or "").strip()
    if not uri:
        return InstitutionCompetences(institution_uri="", institution_label="")

    label = (institution_label or "").strip()
    if not label:
        label = get_institution_label(uri, sparql_client=sparql_client)
    if not label:
        # Fall back to a URI tail so the page heading never reads blank.
        tail = uri.rsplit("#", 1)[-1].rsplit("/", 1)[-1]
        label = tail or uri

    rows = list_competences_for_institution(uri, sparql_client=sparql_client)
    # Detect truncation: the SPARQL LIMIT is _MAX + 1 so we can see one
    # past the cap; we then trim back to _MAX for the actual view.
    truncated = len(rows) > _MAX_COMPETENCES_PER_INSTITUTION
    if truncated:
        rows = rows[:_MAX_COMPETENCES_PER_INSTITUTION]

    by_act: dict[str, list[CompetenceRow]] = {}
    for row in rows:
        # Bucket on the literal ``estleg:sourceAct`` title (carried in
        # ``row.act_label``) — the prod corpus has no act URIs to key
        # on, so the title literal IS the bucket identity. The
        # empty-string bucket holds orphan provisions with no sourceAct
        # edge; the route labels it "Muud" rather than blank.
        by_act.setdefault(row.act_label, []).append(row)

    overlaps = list_competence_overlaps(uri, sparql_client=sparql_client)

    return InstitutionCompetences(
        institution_uri=uri,
        institution_label=label,
        by_act=by_act,
        overlaps=overlaps,
        total_count=len(rows),
        truncated=truncated,
    )


# ---------------------------------------------------------------------------
# Internal — row → CompetenceRow
# ---------------------------------------------------------------------------


def _rows_to_competences(rows: list[dict[str, Any]]) -> list[CompetenceRow]:
    """Convert SPARQL JSON binding rows into :class:`CompetenceRow` instances.

    A row without a ``provision`` URI is dropped (it shouldn't happen —
    the query binds ``?provision`` non-optionally — but defensive
    parsing keeps the route from crashing on a stray bind error).

    The ``act_label`` carries the literal ``estleg:sourceAct`` title and
    ``act_uri`` is always empty in this corpus (see Wave 2 spike).
    """
    out: list[CompetenceRow] = []
    for row in rows or []:
        provision_uri = str(row.get("provision") or "").strip()
        if not provision_uri:
            continue
        out.append(
            CompetenceRow(
                provision_uri=provision_uri,
                provision_label=str(row.get("provisionLabel") or "").strip(),
                act_uri="",
                act_label=str(row.get("actLit") or "").strip(),
            )
        )
    return out


__all__ = [
    "InstitutionCandidate",
    "CompetenceRow",
    "OverlapRow",
    "InstitutionCompetences",
    "search_institutions_by_label",
    "get_institution_label",
    "list_competences_for_institution",
    "list_competence_overlaps",
    "gather_institution_competences",
]
