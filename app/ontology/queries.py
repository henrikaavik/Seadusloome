"""SPARQL query templates for the Estonian Legal Ontology explorer."""

# Ontology namespace
ESTLEG_NS = "https://data.riik.ee/ontology/estleg#"

# Common prefixes used in all queries
PREFIXES = """
PREFIX owl:    <http://www.w3.org/2002/07/owl#>
PREFIX rdf:    <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
PREFIX rdfs:   <http://www.w3.org/2000/01/rdf-schema#>
PREFIX xsd:    <http://www.w3.org/2001/XMLSchema#>
PREFIX estleg: <https://data.riik.ee/ontology/estleg#>
"""

# ---------------------------------------------------------------------------
# CATEGORY_OVERVIEW
# Aggregate entity counts per RDF type.
# ---------------------------------------------------------------------------

CATEGORY_OVERVIEW = (
    PREFIXES
    + """
SELECT ?type (COUNT(?entity) AS ?count)
WHERE {
    ?entity rdf:type ?type .
    FILTER(STRSTARTS(STR(?type), "https://data.riik.ee/ontology/estleg#"))
}
GROUP BY ?type
ORDER BY DESC(?count)
"""
)

# ---------------------------------------------------------------------------
# ENTITIES_BY_CATEGORY
# Paginated list of entities filtered by type, ordered by label.
# Requires bindings: categoryType
# Uses LIMIT/OFFSET injected at call site.
# ---------------------------------------------------------------------------

ENTITIES_BY_CATEGORY = (
    PREFIXES
    + """
SELECT ?entity ?label ?type
WHERE {
    ?entity rdf:type ?categoryType .
    ?entity rdf:type ?type .
    OPTIONAL { ?entity rdfs:label ?label }
}
ORDER BY ?label
"""
)

ENTITIES_BY_CATEGORY_COUNT = (
    PREFIXES
    + """
SELECT (COUNT(DISTINCT ?entity) AS ?count)
WHERE {
    ?entity rdf:type ?categoryType .
}
"""
)

# ---------------------------------------------------------------------------
# ENTITY_DETAIL
# Full metadata for one entity plus 1-hop neighbors (outgoing and incoming).
# Uses VALUES for the entity URI.
# ---------------------------------------------------------------------------

ENTITY_DETAIL_OUTGOING = (
    PREFIXES
    + """
SELECT ?predicate ?object ?objectLabel
WHERE {
    ?entityUri ?predicate ?object .
    OPTIONAL { ?object rdfs:label ?objectLabel }
}
LIMIT 500
"""
)

ENTITY_DETAIL_INCOMING = (
    PREFIXES
    + """
SELECT ?subject ?subjectLabel ?predicate
WHERE {
    ?subject ?predicate ?entityUri .
    OPTIONAL { ?subject rdfs:label ?subjectLabel }
}
LIMIT 100
"""
)

ENTITY_METADATA = (
    PREFIXES
    + """
SELECT ?predicate ?value
WHERE {
    ?entityUri ?predicate ?value .
    FILTER(isLiteral(?value))
}
LIMIT 500
"""
)

# ---------------------------------------------------------------------------
# SEARCH_ENTITIES
# Case-insensitive regex search on rdfs:label.
# The search term is injected via regex FILTER (not string interpolation).
# ---------------------------------------------------------------------------

SEARCH_ENTITIES = (
    PREFIXES
    + """
SELECT DISTINCT ?entity ?label ?type
WHERE {{
    ?entity rdfs:label ?label .
    ?entity rdf:type ?type .
    FILTER(REGEX(?label, "{search_pattern}", "i"))
}}
ORDER BY ?label
LIMIT {limit}
"""
)
# NOTE: SEARCH_ENTITIES uses Python .format() for the regex pattern and
# limit because the search term goes inside a SPARQL FILTER(REGEX(...))
# string literal — not a URI context. The search_pattern is pre-escaped
# via re.escape() + backslash/quote doubling in the route layer. The
# double braces are required because .format() needs them for literal
# SPARQL curly braces.

# ---------------------------------------------------------------------------
# ENTITIES_AT_DATE
# Entities valid at a given date: validFrom <= date AND
# (no validUntil OR validUntil > date).
# ---------------------------------------------------------------------------

ENTITIES_AT_DATE = (
    PREFIXES
    + """
SELECT ?entity ?label ?type ?validFrom ?validUntil
WHERE {{
    ?entity estleg:validFrom ?validFrom .
    OPTIONAL {{ ?entity estleg:validUntil ?validUntil }}
    OPTIONAL {{ ?entity rdfs:label ?label }}
    OPTIONAL {{ ?entity rdf:type ?type }}
    FILTER(?validFrom <= "{date}"^^xsd:date)
    FILTER(!BOUND(?validUntil) || ?validUntil > "{date}"^^xsd:date)
}}
ORDER BY ?label
LIMIT {limit}
OFFSET {offset}
"""
)

ENTITIES_AT_DATE_COUNT = (
    PREFIXES
    + """
SELECT (COUNT(?entity) AS ?count)
WHERE {{
    ?entity estleg:validFrom ?validFrom .
    OPTIONAL {{ ?entity estleg:validUntil ?validUntil }}
    FILTER(?validFrom <= "{date}"^^xsd:date)
    FILTER(!BOUND(?validUntil) || ?validUntil > "{date}"^^xsd:date)
}}
"""
)
