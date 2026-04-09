"""SPARQL query templates for the Impact Analysis Engine.

Each template is a format string with ``{graph_uri}`` placeholders
that :class:`app.docs.impact.analyzer.ImpactAnalyzer` fills in at run
time. Queries use the ``GRAPH`` keyword to scope the draft subject
lookup to the draft's named graph while letting the body clauses
traverse the default graph (the enacted ontology) freely.

All queries are capped with ``LIMIT`` — the ontology has ~1M triples
and a runaway traversal could return tens of thousands of rows. The
limits are deliberately conservative so the impact report stays
responsive; callers that need more can paginate in the analyzer.

Predicates used (cross-checked against the loaded ontology via the
explorer queries in ``app/explorer/routes.py`` and
``app/ontology/queries.py``):

    estleg:references          draft-side link from DraftLegislation to any entity
    estleg:interpretsProvision CourtDecision -> LegalProvision
    estleg:transposesDirective LegalProvision -> EULegislation
    estleg:implementsEU        alias used in the design doc
    estleg:hasTopic            LegalProvision -> TopicCluster
    estleg:topicCluster        alias surfaced on some older data
    estleg:amendsProvision     Amendment -> LegalProvision
    estleg:definesConcept      LegalProvision -> LegalConcept
    rdfs:label                 human label
    rdf:type                   RDF type
"""

from __future__ import annotations

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
#   hasTopic / topicCluster     — LegalProvision -> TopicCluster
#   amendsProvision             — Amendment -> LegalProvision
#   definesConcept              — LegalProvision -> LegalConcept
#   interpretsProvision         — CourtDecision -> LegalProvision
#   transposesDirective         — LegalProvision -> EULegislation
#   implementsEU                — alias
#
# The UNION pattern keeps the query flat (property-path expressions
# across incoming+outgoing edges make Jena's planner unhappy on 1M
# triples) and the DISTINCT projection drops duplicates from the
# cartesian product.

AFFECTED_ENTITIES = (
    PREFIXES
    + """
SELECT DISTINCT ?entity ?label ?type WHERE {{
  GRAPH <{graph_uri}> {{ ?draft estleg:references ?ref . }}
  {{
    BIND(?ref AS ?entity)
  }} UNION {{
    ?ref estleg:hasTopic ?entity .
  }} UNION {{
    ?ref estleg:topicCluster ?entity .
  }} UNION {{
    ?ref estleg:definesConcept ?entity .
  }} UNION {{
    ?ref estleg:transposesDirective ?entity .
  }} UNION {{
    ?ref estleg:implementsEU ?entity .
  }} UNION {{
    ?entity estleg:interpretsProvision ?ref .
  }} UNION {{
    ?entity estleg:amendsProvision ?ref .
  }}
  OPTIONAL {{ ?entity rdfs:label ?label }}
  OPTIONAL {{ ?entity rdf:type ?type }}
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
# The FILTER excludes the current draft graph from the "other drafts"
# side so we don't self-report. We materialise the graph URI as a
# literal in the FILTER with ``str(?otherGraph) != ...`` — rdflib's
# URIRef comparison is strict about named-graph identity.

CONFLICTS = (
    PREFIXES
    + """
SELECT DISTINCT ?draftRef ?conflictEntity ?conflictLabel ?reason WHERE {{
  GRAPH <{graph_uri}> {{ ?draft estleg:references ?draftRef . }}
  {{
    # Another draft references the same entity.
    GRAPH ?otherGraph {{
      ?otherDraft a estleg:DraftLegislation ;
                  estleg:references ?draftRef .
    }}
    FILTER(str(?otherGraph) != "{graph_uri}")
    BIND(?otherDraft AS ?conflictEntity)
    OPTIONAL {{ ?conflictEntity rdfs:label ?conflictLabel }}
    BIND("Another draft already references this provision" AS ?reason)
  }} UNION {{
    # A Supreme Court decision interprets this provision.
    ?conflictEntity estleg:interpretsProvision ?draftRef .
    OPTIONAL {{ ?conflictEntity rdfs:label ?conflictLabel }}
    BIND("Court decision interprets this provision" AS ?reason)
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
# the provisions' hasTopic edge), count how many sibling provisions
# exist in that cluster and how many the draft actually references.
# A cluster is flagged as a "gap" when the draft references less
# than 20% of its provisions — strongly suggesting the drafter is
# modifying one corner of a topic without considering the rest.
#
# We use a HAVING clause rather than a post-filter so the engine can
# prune early. The 20% threshold is a heuristic — Phase 3 may tighten
# it with topic-specific weights.

GAPS = (
    PREFIXES
    + """
SELECT ?cluster ?clusterLabel ?totalProvisions ?referencedProvisions WHERE {{
  {{
    SELECT ?cluster (COUNT(DISTINCT ?p) AS ?totalProvisions) WHERE {{
      ?p estleg:hasTopic ?cluster .
    }}
    GROUP BY ?cluster
  }}
  {{
    SELECT ?cluster (COUNT(DISTINCT ?p) AS ?referencedProvisions) WHERE {{
      GRAPH <{graph_uri}> {{ ?draft estleg:references ?p . }}
      ?p estleg:hasTopic ?cluster .
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
# We include both ``transposesDirective`` (the canonical predicate
# from the ontology data model) and ``implementsEU`` (an alias used
# in the Phase 1 sync output) so the query works regardless of which
# predicate the loaded data uses. The UNION is flat so Jena's planner
# can push the bindings through quickly.

EU_COMPLIANCE = (
    PREFIXES
    + """
SELECT DISTINCT ?euAct ?euLabel ?estonianProvision ?provisionLabel WHERE {{
  GRAPH <{graph_uri}> {{ ?draft estleg:references ?estonianProvision . }}
  {{
    ?estonianProvision estleg:transposesDirective ?euAct .
  }} UNION {{
    ?estonianProvision estleg:implementsEU ?euAct .
  }}
  ?euAct a estleg:EULegislation .
  OPTIONAL {{ ?euAct rdfs:label ?euLabel }}
  OPTIONAL {{ ?estonianProvision rdfs:label ?provisionLabel }}
}}
LIMIT 200
"""
)
