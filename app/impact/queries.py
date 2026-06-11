"""SPARQL query templates for the Impact Analysis Engine.

Each template is a format string with ``{graph_uri}`` placeholders
that :class:`app.impact.analyzer.ImpactAnalyzer` fills in at run
time. Queries use the ``GRAPH`` keyword to scope the draft subject
lookup to the draft's named graph while letting the body clauses
traverse the default graph (the enacted ontology) freely.

All queries are capped with ``LIMIT`` — the ontology has ~1M triples
and a runaway traversal could return tens of thousands of rows. The
limits are deliberately conservative so the impact report stays
responsive; callers that need more can paginate in the analyzer.

Predicates used (canonical names, verified against the
``henrikaavik/estonian-legal-ontology`` source on 2026-05-15 — see
``docs/2026-05-15-ontology-six-use-cases-plan.md`` section 2.5):

    estleg:references          draft-side link from DraftLegislation to any entity
    estleg:interpretsLaw       CourtDecision -> LegalProvision
    estleg:interpretedBy       LegalProvision -> CourtDecision (inverse)
    estleg:transposesDirective Estonian Act -> EULegislation
    estleg:transposedBy        EULegislation -> Estonian Act (inverse)
    estleg:harmonisedWith      LegalProvision -> EU act
    estleg:requestedCluster    LegalProvision -> TopicCluster (populated)
    estleg:topicCluster        SHACL-defined alias (unused in current data)
    estleg:amends              AmendmentEvent -> LegalProvision
    estleg:amendedBy           LegalProvision -> Act / Draft (inverse)
    estleg:definesConcept      LegalProvision -> LegalConcept
    rdfs:label                 human label
    rdf:type                   RDF type

#C0 (2026-05-15): the legacy predicates ``interpretsProvision``,
``amendsProvision``, ``hasTopic``, and ``implementsEU`` were verified
not to exist in the source ontology. Every UNION branch using those
names returned zero rows, so impact reports under-reported conflicts
and amendments. The queries now project a ``?relation`` variable on
each row so downstream renderers can show the relation type in legal
language (C5). The canonical predicate URIs are defined in
:mod:`app.ontology.relations`.

Security: every ``{graph_uri}`` interpolation goes through
:func:`_validate_graph_uri` (#465). The draft graph URI is generated
server-side from a UUID, so user input never reaches a query, but the
validator is a defence-in-depth guard against future code paths that
might assemble a graph URI from user-supplied data.
"""

from __future__ import annotations

# #480: the canonical definition of ``_SAFE_GRAPH_URI`` /
# ``_validate_graph_uri`` lives in ``app.sync.jena_loader`` because
# that's where the Graph Store Protocol transport enforces the
# named-graph contract. We re-export both names here so the queries
# module and the analyzer keep their existing import paths — there is
# exactly one regex definition in the codebase so the SPARQL layer and
# the GSP layer cannot drift.
#
# #465/#476: every ``graph_uri`` value interpolated into a SPARQL
# template must match the allowlist. The format mirrors the URIs we
# generate server-side: ``https://data.riik.ee/ontology/estleg/drafts/
# <uuid>`` (and, since #722, the ephemeral ``…/estleg/adhoc/<uuid>``
# graphs the Analüüsikeskus "Normi mõjuahel" workflow mints + deletes
# per request). #476 tightened this from a generic ``https?://...``
# shape to the exact production host + path so any future code path
# handing us a user-supplied URI is rejected loudly rather than
# slipping through on a lexical match.
#
# #479: the regex is deliberately strict about the characters we allow
# in the path. We do NOT permit ``#`` (fragment) or ``?`` (query)
# because every URI we generate uses neither. A future feature that
# needs fragments or query params must extend the regex AND the SPARQL
# template, not loosen this allowlist in isolation.
from app.ontology.scoping import ADHOC_GRAPH_PREFIX, draft_graph_prefix_for
from app.sync.jena_loader import _SAFE_GRAPH_URI, _validate_graph_uri

__all__ = [
    "_SAFE_GRAPH_URI",
    "_validate_graph_uri",
    "PREFIXES",
    "AFFECTED_ENTITIES",
    "CONFLICTS",
    "GAPS",
    "EU_COMPLIANCE",
    "build_affected_entities_query",
    "build_conflicts_query",
    "build_gaps_query",
    "build_eu_compliance_query",
]


PREFIXES = """
PREFIX rdf:    <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
PREFIX rdfs:   <http://www.w3.org/2000/01/rdf-schema#>
PREFIX xsd:    <http://www.w3.org/2001/XMLSchema#>
PREFIX estleg: <https://data.riik.ee/ontology/estleg#>
"""


# ---------------------------------------------------------------------------
# 1. AFFECTED_ENTITIES — 2-hop BFS from draft references
# ---------------------------------------------------------------------------
#
# The draft's named graph stores ``draft-self estleg:references ?ref``
# edges. We pivot from each referenced entity out one more hop via
# the common relational predicates the ontology uses:
#
#   requestedCluster / topicCluster  — LegalProvision -> TopicCluster
#   amends / amendedBy               — AmendmentEvent -> Provision (and inverse)
#   definesConcept                   — LegalProvision -> LegalConcept
#   interpretsLaw / interpretedBy    — CourtDecision -> Provision (and inverse)
#   transposesDirective              — Estonian Act -> EULegislation
#   harmonisedWith                   — Provision -> EU act
#
# The UNION pattern keeps the query flat (property-path expressions
# across incoming+outgoing edges make Jena's planner unhappy on 1M
# triples) and the DISTINCT projection drops duplicates from the
# cartesian product. Each branch BINDs a ``?relation`` literal so the
# analyzer / renderer can show the relation type in legal language
# (epic C5, ``app.ontology.relations.legal_phrase``).

AFFECTED_ENTITIES = (
    PREFIXES
    + """
SELECT DISTINCT ?entity ?label ?type ?relation WHERE {{
  {{
    GRAPH <{graph_uri}> {{ ?draft estleg:references ?ref . }}
    {{
      BIND(?ref AS ?entity)
      BIND(estleg:references AS ?relation)
    }} UNION {{
      ?ref estleg:requestedCluster ?entity .
      BIND(estleg:requestedCluster AS ?relation)
    }} UNION {{
      ?ref estleg:topicCluster ?entity .
      BIND(estleg:topicCluster AS ?relation)
    }} UNION {{
      ?ref estleg:definesConcept ?entity .
      BIND(estleg:definesConcept AS ?relation)
    }} UNION {{
      ?ref estleg:transposesDirective ?entity .
      BIND(estleg:transposesDirective AS ?relation)
    }} UNION {{
      ?ref estleg:harmonisedWith ?entity .
      BIND(estleg:harmonisedWith AS ?relation)
    }} UNION {{
      ?ref estleg:interpretedBy ?entity .
      BIND(estleg:interpretedBy AS ?relation)
    }} UNION {{
      ?entity estleg:interpretsLaw ?ref .
      BIND(estleg:interpretsLaw AS ?relation)
    }} UNION {{
      ?ref estleg:amendedBy ?entity .
      BIND(estleg:amendedBy AS ?relation)
    }} UNION {{
      ?entity estleg:amends ?ref .
      BIND(estleg:amends AS ?relation)
    }}
    OPTIONAL {{ ?entity rdfs:label ?label }}
    OPTIONAL {{ ?entity rdf:type ?type }}
    # Step 5A live-deploy follow-up: in this corpus ``estleg:Law``
    # instances are *topic-map clusters* (``ALKS_Map_2026``,
    # ``RKIOMPU1974_Map_2026``, …) — thematic groupings indexed by
    # ``estleg:requestedCluster`` from each provision. A single resolved
    # provision (e.g. ``ATMOSF_Par_143``) traversed via that arm yields
    # 500+ unrelated topic-map "Laws" (1974 maritime safety convention
    # etc.). Filter them out: the referenced entity itself never satisfies
    # ``?entity a estleg:Law`` because it is a Section/LegalProvision_
    # subclass, so this exclusion only removes the cluster fan-out. If
    # the upstream ontology ever types real atomic acts as ``estleg:Law``
    # (today they are not), reconsider this filter. The FILTER lives on
    # the URI-shaped branches only — the act-level literal branch below
    # has no URI to type, so it is naturally unaffected.
    FILTER NOT EXISTS {{ ?entity a estleg:Law }}
  }} UNION {{
    # Wave 2 Step 5A partial-match surfacing
    # (docs/2026-05-18-bugfix-plan.md). The resolver writes a distinct
    # ``estleg:referencesAct "<title>"`` LITERAL edge whenever the act
    # half of a reference resolved but the § could not be pinned (e.g.
    # ``riigieelarve seaduse § 20 lõike 5`` → act_title="Riigieelarve
    # seadus", section="20"). The literal edge is explicitly NOT
    # traversable — there is no URI to fan out from — so it's a natural
    # BFS dead-end. Surface it in the affected-entities list so the
    # user sees "this draft touches Riigieelarve seadus" alongside the
    # full URI matches; without this branch the partial_match row is
    # persisted (PG + Jena) but invisible to ministry users reading the
    # impact report.
    #
    # The ``?entity`` projection is polymorphic: existing branches bind
    # it to a URI; this branch binds it to a literal string. The
    # downstream analyzer/renderer reads ``?entity`` as ``str(row.get(
    # "entity"))`` already so the polymorphism is transparent — the
    # ``OPTIONAL`` ``rdfs:label`` / ``rdf:type`` clauses on the
    # URI-shaped branches above simply don't fire on a literal, and the
    # renderer falls back to the literal itself for the display.
    # ``estleg:Law`` exclusion does not apply to literals (literals
    # can't have rdf:type), so the FILTER NOT EXISTS above doesn't
    # touch this branch either.
    GRAPH <{graph_uri}> {{ ?draft estleg:referencesAct ?entity . }}
    BIND(estleg:referencesAct AS ?relation)
  }}
}}
LIMIT 500
"""
)


# ---------------------------------------------------------------------------
# 2. CONFLICTS — draft references that another draft already references
# ---------------------------------------------------------------------------
#
# A conflict is reported whenever another persistent draft graph
# references the same ontology entity as the current draft. This
# catches "two ministries drafting amendments to the same section"
# which is the most common hard conflict in the Estonian legislative
# process.
#
# Secondary pattern: a Supreme Court decision that interprets the
# same provision. The analyzer surfaces those as advisory conflicts
# (severity medium) so the drafter reads the relevant case law before
# proposing a change.
#
# Three exclusions guard the cross-draft arm (#844):
#
#   A5 (REQUIRED, #849 depends on it): a draft must never self-conflict
#   against its OWN prior-version graphs. Versions share the
#   ``…/drafts/<uuid>`` prefix (v1 = ``…/drafts/<uuid>``, v2+ =
#   ``…/drafts/<uuid>/v<n>`` per upload.py §9.5), so an exact ``!=``
#   exclusion of just the current graph (the old behaviour) left v1/v2
#   visible to a v3 analyze run as phantom "another draft". We exclude
#   the whole ``{draft_prefix}`` namespace with ``!STRSTARTS`` instead.
#
#   A3(c): ephemeral Normi-mõjuahel probe graphs (``…/estleg/adhoc/…``)
#   are typed ``estleg:DraftLegislation`` (adhoc_analysis.py) so a
#   concurrent probe would otherwise be persisted into a real impact
#   report as a phantom conflict. Exclude the adhoc namespace too.
#
#   A3(b) — org scoping of FOREIGN drafts — is NOT expressible in SPARQL
#   here (Jena has no notion of Postgres org ownership). It is enforced
#   downstream in ``ImpactAnalyzer._detect_conflicts`` by post-filtering
#   the ``?conflictEntity`` draft URIs against the org's owned graphs and
#   masking the rest (mirrors ``similarity.list_similar_drafts_for_view``).
#
# ``{draft_prefix}`` is the version-agnostic ``…/drafts/<uuid>`` prefix
# derived from ``{graph_uri}`` by the builder.

CONFLICTS = (
    PREFIXES
    + """
SELECT DISTINCT ?draftRef ?conflictEntity ?conflictLabel ?reason ?relation ?otherGraph WHERE {{
  GRAPH <{graph_uri}> {{ ?draft estleg:references ?draftRef . }}
  {{
    # Another draft references the same entity.
    #
    # #855: the other draft's ``rdfs:label`` triple lives INSIDE its own
    # named graph (the draft graph builder writes ``<g>#self rdfs:label
    # "<title>"`` into ``<g>``). On Fuseki there is no ``unionDefaultGraph``
    # so the label OPTIONAL must sit INSIDE ``GRAPH ?otherGraph`` — when it
    # was outside, ``?conflictEntity rdfs:label ?conflictLabel`` matched
    # only the default graph, never bound, and the UI fell back to raw
    # draft URIs. Keeping it inside the GRAPH block binds the title.
    GRAPH ?otherGraph {{
      ?otherDraft a estleg:DraftLegislation ;
                  estleg:references ?draftRef .
      BIND(?otherDraft AS ?conflictEntity)
      OPTIONAL {{ ?conflictEntity rdfs:label ?conflictLabel }}
    }}
    # A5: exclude this draft's own (current + prior-version) graphs.
    FILTER(!STRSTARTS(str(?otherGraph), "{draft_prefix}"))
    # A3(c): exclude ephemeral adhoc probe graphs.
    FILTER(!STRSTARTS(str(?otherGraph), "{adhoc_prefix}"))
    BIND("Teine eelnõu viitab juba sellele sättele" AS ?reason)
    BIND(estleg:references AS ?relation)
  }} UNION {{
    # A Supreme Court decision interprets this provision —
    # CourtDecision estleg:interpretsLaw Provision (forward) OR
    # Provision estleg:interpretedBy CourtDecision (inverse).
    ?conflictEntity estleg:interpretsLaw ?draftRef .
    OPTIONAL {{ ?conflictEntity rdfs:label ?conflictLabel }}
    BIND("Kohtulahend tõlgendab seda sätet" AS ?reason)
    BIND(estleg:interpretsLaw AS ?relation)
  }} UNION {{
    ?draftRef estleg:interpretedBy ?conflictEntity .
    OPTIONAL {{ ?conflictEntity rdfs:label ?conflictLabel }}
    BIND("Kohtulahend tõlgendab seda sätet" AS ?reason)
    BIND(estleg:interpretedBy AS ?relation)
  }}
}}
LIMIT 200
"""
)


# ---------------------------------------------------------------------------
# 3. GAPS — topic clusters touched but underrepresented
# ---------------------------------------------------------------------------
#
# For every topic cluster reachable from the draft's references (via
# the provisions' requestedCluster edge — the canonical populated
# predicate; ``topicCluster`` is its SHACL alias and currently empty
# but queried for forward-compat), count how many sibling provisions
# exist in that cluster and how many the draft actually references.
# A cluster is flagged as a "gap" when the draft references less
# than 20% of its provisions — strongly suggesting the drafter is
# modifying one corner of a topic without considering the rest.
#
# We use a HAVING clause rather than a post-filter so the engine can
# prune early. The 20% threshold is a heuristic — Phase 3 may tighten
# it with topic-specific weights.
#
# C0 (2026-05-15): the previous ``estleg:hasTopic`` predicate does not
# exist in the source ontology, so this query returned zero rows. Now
# the UNION over ``requestedCluster`` / ``topicCluster`` matches the
# canonical vocabulary.

GAPS = (
    PREFIXES
    + """
SELECT ?cluster ?clusterLabel ?totalProvisions ?referencedProvisions WHERE {{
  {{
    SELECT ?cluster (COUNT(DISTINCT ?p) AS ?totalProvisions) WHERE {{
      {{ ?p estleg:requestedCluster ?cluster . }}
      UNION
      {{ ?p estleg:topicCluster ?cluster . }}
    }}
    GROUP BY ?cluster
  }}
  {{
    SELECT ?cluster (COUNT(DISTINCT ?p) AS ?referencedProvisions) WHERE {{
      GRAPH <{graph_uri}> {{ ?draft estleg:references ?p . }}
      {{ ?p estleg:requestedCluster ?cluster . }}
      UNION
      {{ ?p estleg:topicCluster ?cluster . }}
    }}
    GROUP BY ?cluster
  }}
  OPTIONAL {{ ?cluster rdfs:label ?clusterLabel }}
  FILTER(?referencedProvisions * 5 < ?totalProvisions)
}}
ORDER BY DESC(?totalProvisions)
LIMIT 100
"""
)


# ---------------------------------------------------------------------------
# 4. EU_COMPLIANCE — EU legislation transposed by referenced provisions
# ---------------------------------------------------------------------------
#
# If the draft touches a provision that transposes an EU directive or
# regulation, the impact report must flag the EU instrument so the
# drafter knows the change might affect transposition compliance.
# The analyzer does NOT decide "compliant" vs "non-compliant" — it
# simply surfaces the link so a human reviewer can assess the
# transposition impact.
#
# C0 (2026-05-15): the ``estleg:implementsEU`` alias was dropped
# because the predicate does not exist in the source ontology. The
# query now uses ``transposesDirective`` (forward), ``transposedBy``
# (inverse, in case data only carries one direction), and
# ``harmonisedWith`` (provision-level harmonisation).
#
# 2026-05-18 (Wave 2 Step 5 of docs/2026-05-18-bugfix-plan.md): the
# act-level UNION arms that chained through ``estleg:sourceAct`` /
# ``estleg:partOf`` were SILENTLY DEAD in prod:
#
#   * ``estleg:sourceAct`` is a string LITERAL in this corpus (24,221
#     triples, all ``xsd:string`` — see Step 1 spike). Binding
#     ``?_parentAct`` to a literal then joining ``?_parentAct
#     estleg:transposesDirective ?euAct`` cannot match — literals
#     cannot be subjects of those triples → zero rows.
#   * ``estleg:partOf`` and ``estleg:partOfAct`` have ZERO triples in
#     prod — both UNION arms returned zero rows for every draft.
#
# The reverse-lookup option (``?actUri rdfs:label ?actLit`` then chain
# via the URI) was considered but rejected: a SPARQL probe against
# prod showed the title ``"Atmosfääriõhu kaitse seadus"`` resolves
# to a Draft URI (``Draft_KLIM14_1034``), not an atomic act URI with
# transposition edges. Joining on labels would surface draft +
# topic-map false positives.
#
# Fix: drop the act-level chain entirely. The corpus has no reliable
# act URIs to traverse; provision-level transposition + harmonisation
# (SHACL lines 158-163 and 226-230) are the only honest paths. When
# act-level data later lands cleanly, a new UNION arm can be added.
#
# 2026-05-18 (Wave 2 Step 5A, P2 review follow-up): the same
# ``estleg:referencesAct`` literal branch was added to AFFECTED_ENTITIES
# above to surface act-level partial matches in the impact report.
# Deliberately NOT added here. EU compliance is intrinsically
# provision-level: the corpus's transposition + harmonisation edges
# attach to provisions/acts via ``transposesDirective`` /
# ``harmonisedWith``, neither of which has a literal-title join key.
# Adding a ``GRAPH <…> { ?draft estleg:referencesAct ?actTitle }`` arm
# here would produce empty rows — there is no second hop from a string
# literal to an EU directive in this data. If the source data ever
# grows a literal-keyed predicate that ties an act title to an EU
# instrument (e.g. ``estleg:transposedIntoTitle "<lit>"``), a new arm
# becomes meaningful; today it would just be dead code.

EU_COMPLIANCE = (
    PREFIXES
    + """
SELECT DISTINCT ?euAct ?euLabel ?estonianProvision ?provisionLabel ?relation WHERE {{
  GRAPH <{graph_uri}> {{ ?draft estleg:references ?estonianProvision . }}
  {{
    # Provision-level transposition (SHACL lines 158-163 allow this).
    ?estonianProvision estleg:transposesDirective ?euAct .
    BIND(estleg:transposesDirective AS ?relation)
  }} UNION {{
    # Provision-level harmonisation (SHACL 226-230).
    ?estonianProvision estleg:harmonisedWith ?euAct .
    BIND(estleg:harmonisedWith AS ?relation)
  }}
  ?euAct a estleg:EULegislation .
  OPTIONAL {{ ?euAct rdfs:label ?euLabel }}
  OPTIONAL {{ ?estonianProvision rdfs:label ?provisionLabel }}
}}
LIMIT 200
"""
)


# ---------------------------------------------------------------------------
# Builders — single choke point for graph_uri interpolation (#465)
# ---------------------------------------------------------------------------
#
# The analyzer used to call ``TEMPLATE.format(graph_uri=...)`` directly
# from each pass; centralising the interpolation here means
# ``_validate_graph_uri`` runs at exactly one place per query type
# and any future template can plug into the same validator.


def build_affected_entities_query(graph_uri: str) -> str:
    """Return the validated AFFECTED_ENTITIES query."""
    safe = _validate_graph_uri(graph_uri)
    return AFFECTED_ENTITIES.format(graph_uri=safe)


def build_conflicts_query(graph_uri: str) -> str:
    """Return the validated CONFLICTS query.

    Besides the validated ``{graph_uri}``, two namespace prefixes are
    interpolated for the self-conflict (A5) and adhoc (A3c) exclusions:

    * ``{draft_prefix}`` — the version-agnostic ``…/drafts/<uuid>``
      prefix of *graph_uri*, so the query excludes this draft's own
      prior-version graphs (e.g. v3 must not conflict with its own
      v1/v2). Derived from the already-validated URI.
    * ``{adhoc_prefix}`` — the constant ``…/estleg/adhoc/`` namespace, so
      ephemeral Normi-mõjuahel probes never surface as phantom conflicts.

    Both prefixes are server-derived constants / functions of the
    validated URI, so no user input reaches the template.
    """
    safe = _validate_graph_uri(graph_uri)
    return CONFLICTS.format(
        graph_uri=safe,
        draft_prefix=draft_graph_prefix_for(safe),
        adhoc_prefix=ADHOC_GRAPH_PREFIX,
    )


def build_gaps_query(graph_uri: str) -> str:
    """Return the validated GAPS query."""
    safe = _validate_graph_uri(graph_uri)
    return GAPS.format(graph_uri=safe)


def build_eu_compliance_query(graph_uri: str) -> str:
    """Return the validated EU_COMPLIANCE query."""
    safe = _validate_graph_uri(graph_uri)
    return EU_COMPLIANCE.format(graph_uri=safe)
