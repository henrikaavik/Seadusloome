"""Load RDF data into Apache Jena Fuseki via Graph Store Protocol."""

import logging
import os

import httpx

logger = logging.getLogger(__name__)

JENA_URL = os.environ.get("JENA_URL", "http://localhost:3030")
JENA_DATASET = os.environ.get("JENA_DATASET", "ontology")
JENA_ADMIN_USER = "admin"
JENA_ADMIN_PASSWORD = os.environ.get("FUSEKI_ADMIN_PASSWORD", "localdev")


def get_sparql_endpoint() -> str:
    return f"{JENA_URL}/{JENA_DATASET}/sparql"


def get_graph_store_endpoint() -> str:
    return f"{JENA_URL}/{JENA_DATASET}/data"


def upload_turtle(turtle_data: str, graph_uri: str | None = None) -> bool:
    """Upload Turtle data to Jena Fuseki via Graph Store Protocol.

    Args:
        turtle_data: RDF data serialized as Turtle.
        graph_uri: Named graph URI, or None for default graph.

    Returns:
        True if upload succeeded.
    """
    endpoint = get_graph_store_endpoint()
    params = {}
    if graph_uri:
        params["graph"] = graph_uri
    else:
        params["default"] = ""

    try:
        response = httpx.put(
            endpoint,
            content=turtle_data.encode("utf-8"),
            headers={"Content-Type": "text/turtle; charset=utf-8"},
            params=params,
            auth=(JENA_ADMIN_USER, JENA_ADMIN_PASSWORD),
            timeout=120.0,
        )
        response.raise_for_status()
        logger.info("Uploaded to Jena Fuseki (status %d)", response.status_code)
        return True
    except httpx.HTTPError:
        logger.exception("Failed to upload to Jena Fuseki")
        return False


def clear_default_graph() -> bool:
    """Clear the default graph in Jena Fuseki."""
    endpoint = get_graph_store_endpoint()
    try:
        response = httpx.delete(
            endpoint,
            params={"default": ""},
            auth=(JENA_ADMIN_USER, JENA_ADMIN_PASSWORD),
            timeout=30.0,
        )
        response.raise_for_status()
        logger.info("Cleared default graph")
        return True
    except httpx.HTTPError:
        logger.exception("Failed to clear default graph")
        return False


def sparql_query(query: str) -> dict:  # type: ignore[type-arg]
    """Execute a SPARQL SELECT query and return results as dict."""
    endpoint = get_sparql_endpoint()
    try:
        response = httpx.post(
            endpoint,
            data={"query": query},
            headers={"Accept": "application/sparql-results+json"},
            timeout=30.0,
        )
        response.raise_for_status()
        return response.json()  # type: ignore[no-any-return]
    except httpx.HTTPError:
        logger.exception("SPARQL query failed")
        return {"results": {"bindings": []}}


def get_triple_count() -> int:
    """Get the number of triples in the default graph."""
    result = sparql_query("SELECT (COUNT(*) AS ?count) WHERE { ?s ?p ?o }")
    bindings = result.get("results", {}).get("bindings", [])
    if bindings:
        return int(bindings[0]["count"]["value"])
    return 0


def check_health() -> bool:
    """Check if Jena Fuseki is reachable."""
    try:
        response = httpx.get(f"{JENA_URL}/$/ping", timeout=5.0)
        return response.status_code == 200
    except httpx.HTTPError:
        return False
