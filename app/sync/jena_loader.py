"""Load RDF data into Apache Jena Fuseki via Graph Store Protocol.

This module owns all HTTP traffic to Fuseki's Graph Store Protocol
endpoint (``{JENA_URL}/{JENA_DATASET}/data``) plus a thin SPARQL helper
for introspection. It is used by two very different callers:

    * The sync pipeline (``app/sync/orchestrator.py``) — pushes the
      enacted-law ontology into the **default** graph on a scheduled
      cadence. This is the "big refresh" flow that clears the default
      graph and re-uploads ~1M triples.
    * The draft pipeline (``app/docs/analyze_handler.py``) — writes
      each draft into its own **named graph** so the impact analyser
      can run SPARQL against the union of the default graph and the
      draft graph without mutating the enacted-law data.

The named-graph helpers (added in Phase 2 Batch 3) share the same
``httpx`` + auth pattern as ``upload_turtle`` but talk to
``?graph=<encoded URI>`` instead of ``?default``. The graph URI is
URL-encoded via ``urllib.parse.quote(..., safe="")`` because draft
graph URIs are full HTTPS URLs with colons and slashes that Fuseki
rejects if left raw in the query string.
"""

import logging
import os
import re
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)

JENA_URL = os.environ.get("JENA_URL", "http://localhost:3030")
JENA_DATASET = os.environ.get("JENA_DATASET", "ontology")
JENA_ADMIN_USER = "admin"
JENA_ADMIN_PASSWORD = os.environ.get("FUSEKI_ADMIN_PASSWORD", "localdev")


# #480: the same allowlist that ``app.docs.impact.queries`` uses for
# SPARQL interpolation applies at the Graph Store Protocol layer too —
# ``put_named_graph`` / ``delete_named_graph`` must reject any URI that
# isn't one of our generated ``drafts/<uuid>`` shapes before the HTTP
# request even goes out. Keeping the canonical definition here (and
# re-exporting from ``app.docs.impact.queries``) means the validator
# lives next to the GSP transport, which is conceptually where the
# named-graph contract is enforced.
_SAFE_GRAPH_URI = re.compile(r"^https://data\.riik\.ee/ontology/estleg/drafts/[0-9a-f-]{36}$")


def _validate_graph_uri(uri: str) -> str:
    """Return *uri* unchanged after asserting it matches the allowlist.

    Raises:
        ValueError: When *uri* doesn't fit the safe pattern. Callers
            should surface this as a handler-level failure rather than
            papering over it — an unsafe URI here is a sign of either
            a bug or an injection attempt, never a transient error.
    """
    if not isinstance(uri, str) or not _SAFE_GRAPH_URI.fullmatch(uri):
        raise ValueError(f"Unsafe graph URI rejected: {uri!r}")
    return uri


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


# ---------------------------------------------------------------------------
# Named graph helpers (Phase 2 Batch 3)
# ---------------------------------------------------------------------------
#
# The draft pipeline writes each uploaded draft into a dedicated named
# graph so the impact analyser can query the union of the default graph
# (enacted laws) and the draft graph without polluting the shared
# ontology. Fuseki's Graph Store Protocol supports PUT (replace),
# DELETE, and GET on named graphs via ``?graph=<encoded URI>``. We wrap
# those three verbs plus two small SPARQL queries for introspection.


def put_named_graph(graph_uri: str, turtle: str) -> bool:
    """Upload Turtle data into the named graph identified by *graph_uri*.

    Uses Fuseki's Graph Store Protocol endpoint
    ``{JENA_URL}/{JENA_DATASET}/data?graph={graph_uri}`` with PUT
    semantics — the named graph is **replaced**, not merged. Callers
    that want merge semantics should read the graph, append triples,
    and PUT the result back (no existing caller does this today).

    Args:
        graph_uri: The named graph URI. Typically a full HTTPS URL
            (``https://data.riik.ee/ontology/estleg/drafts/<uuid>``);
            the caller does **not** need to URL-encode it — we do that
            here so calls with the same URI produce the same request.
        turtle: The Turtle serialisation to upload. UTF-8 encoding is
            enforced via the ``Content-Type`` header so Estonian
            characters survive the round-trip.

    Returns:
        ``True`` on any 2xx response, ``False`` on any non-2xx or a
        transport-level failure. Errors are logged at WARNING level
        with the status code so they can be traced in Fuseki's access
        log without grepping for exceptions.
    """
    # #480: reject unsafe URIs before we hit the network. The Graph
    # Store Protocol layer is the last line of defence; any earlier
    # bug (or a future code path assembling a URI from user input)
    # would otherwise leak through directly into Fuseki.
    _validate_graph_uri(graph_uri)
    endpoint = get_graph_store_endpoint()
    encoded = quote(graph_uri, safe="")
    url = f"{endpoint}?graph={encoded}"
    logger.info("PUT named graph %s (%d bytes)", graph_uri, len(turtle))
    try:
        response = httpx.put(
            url,
            content=turtle.encode("utf-8"),
            headers={"Content-Type": "text/turtle; charset=utf-8"},
            auth=(JENA_ADMIN_USER, JENA_ADMIN_PASSWORD),
            timeout=120.0,
        )
    except httpx.HTTPError:
        logger.exception("put_named_graph transport error for %s", graph_uri)
        return False
    if 200 <= response.status_code < 300:
        logger.info(
            "put_named_graph succeeded for %s (status %d)",
            graph_uri,
            response.status_code,
        )
        return True
    logger.warning(
        "put_named_graph non-2xx for %s: status=%d body=%s",
        graph_uri,
        response.status_code,
        response.text[:200],
    )
    return False


def delete_named_graph(graph_uri: str) -> bool:
    """Delete the named graph identified by *graph_uri*.

    Sends ``DELETE {JENA_URL}/{JENA_DATASET}/data?graph=<uri>``. The
    call is **idempotent**: a 404 is treated as success because the
    caller's intent ("this graph must not exist") is already satisfied
    — this matters for the delete-draft flow where the analyzer may
    never have loaded the graph in the first place (parse or extract
    failed before the named-graph upload).

    Returns:
        ``True`` on 200/204/404, ``False`` on any other status or a
        transport-level failure.
    """
    # #480: same allowlist as put_named_graph — reject unsafe URIs
    # before the HTTP call. Delete is idempotent, but we'd still
    # rather fail loudly than issue a DELETE against an arbitrary
    # graph (possibly dropping the default graph by accident).
    _validate_graph_uri(graph_uri)
    endpoint = get_graph_store_endpoint()
    encoded = quote(graph_uri, safe="")
    url = f"{endpoint}?graph={encoded}"
    logger.info("DELETE named graph %s", graph_uri)
    try:
        response = httpx.delete(
            url,
            auth=(JENA_ADMIN_USER, JENA_ADMIN_PASSWORD),
            timeout=30.0,
        )
    except httpx.HTTPError:
        logger.exception("delete_named_graph transport error for %s", graph_uri)
        return False
    if response.status_code in (200, 204):
        logger.info(
            "delete_named_graph succeeded for %s (status %d)",
            graph_uri,
            response.status_code,
        )
        return True
    if response.status_code == 404:
        # Idempotent: already gone. Still a success from the caller's POV.
        logger.info("delete_named_graph: %s was already absent (404)", graph_uri)
        return True
    logger.warning(
        "delete_named_graph non-2xx for %s: status=%d body=%s",
        graph_uri,
        response.status_code,
        response.text[:200],
    )
    return False


def named_graph_exists(graph_uri: str) -> bool:
    """Return ``True`` if the named graph has at least one triple.

    Uses a SPARQL ``ASK`` query with an explicit ``GRAPH`` clause. An
    empty named graph (or a non-existent one) returns ``False``.
    Transport errors also return ``False`` — defensively, callers
    should not treat this as "the graph is empty" in critical paths,
    but the impact-report flow can safely re-PUT on ``False``.
    """
    _validate_graph_uri(graph_uri)
    # ASK queries aren't covered by the module-level sparql_query helper
    # (which hardcodes the SELECT results shape), so talk to the SPARQL
    # endpoint directly with a tiny inline helper.
    ask = f"ASK {{ GRAPH <{graph_uri}> {{ ?s ?p ?o }} }}"
    endpoint = get_sparql_endpoint()
    try:
        response = httpx.post(
            endpoint,
            data={"query": ask},
            headers={"Accept": "application/sparql-results+json"},
            timeout=30.0,
        )
        response.raise_for_status()
    except httpx.HTTPError:
        logger.exception("named_graph_exists SPARQL ASK failed for %s", graph_uri)
        return False
    try:
        return bool(response.json().get("boolean", False))
    except ValueError:
        logger.warning("named_graph_exists: could not parse JSON response")
        return False


def get_named_graph_triple_count(graph_uri: str) -> int:
    """Return the number of triples in the given named graph.

    Zero is returned both when the graph is empty and when the SPARQL
    query fails — use :func:`named_graph_exists` first if you need to
    distinguish "empty" from "missing".
    """
    _validate_graph_uri(graph_uri)
    sparql_query_text = (
        f"SELECT (COUNT(*) AS ?count) WHERE {{ GRAPH <{graph_uri}> {{ ?s ?p ?o }} }}"
    )
    result = sparql_query(sparql_query_text)
    bindings = result.get("results", {}).get("bindings", [])
    if not bindings:
        return 0
    try:
        return int(bindings[0]["count"]["value"])
    except (KeyError, ValueError, TypeError):
        return 0
