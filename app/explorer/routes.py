"""Explorer API routes for browsing the Estonian Legal Ontology."""

from __future__ import annotations

import logging
import re
from urllib.parse import unquote

from starlette.requests import Request
from starlette.responses import JSONResponse

from app.ontology.queries import (
    CATEGORY_OVERVIEW,
    ENTITIES_AT_DATE,
    ENTITIES_AT_DATE_COUNT,
    ENTITIES_BY_CATEGORY,
    ENTITIES_BY_CATEGORY_COUNT,
    ENTITY_DETAIL_INCOMING,
    ENTITY_DETAIL_OUTGOING,
    ENTITY_METADATA,
    SEARCH_ENTITIES,
)
from app.ontology.sparql_client import SparqlClient

logger = logging.getLogger(__name__)

_DEFAULT_PAGE_SIZE = 20
_MAX_PAGE_SIZE = 100
_MAX_SEARCH_LIMIT = 50

# Shared client instance (created lazily so env vars are read at first use)
_client: SparqlClient | None = None


def _get_client() -> SparqlClient:
    global _client  # noqa: PLW0603
    if _client is None:
        _client = SparqlClient()
    return _client


def _parse_page_params(req: Request) -> tuple[int, int, int]:
    """Extract page and size from query parameters.

    Returns (page, size, offset).
    """
    try:
        page = max(1, int(req.query_params.get("page", "1")))
    except (ValueError, TypeError):
        page = 1
    try:
        raw = int(req.query_params.get("size", str(_DEFAULT_PAGE_SIZE)))
        size = min(_MAX_PAGE_SIZE, max(1, raw))
    except (ValueError, TypeError):
        size = _DEFAULT_PAGE_SIZE
    offset = (page - 1) * size
    return page, size, offset


def _sanitize_regex(pattern: str) -> str:
    """Escape special regex characters in user input for SPARQL REGEX filter.

    This prevents regex injection while still allowing basic substring search.
    """
    return re.escape(pattern)


_SAFE_URI_RE = re.compile(r"^https?://[A-Za-z0-9./:_#\-]{1,512}$")


def _validate_uri(uri: str) -> bool:
    """Validate that a string is a safe URI (no SPARQL injection vectors).

    Rejects URIs containing closing angle brackets, braces, quotes, or
    other characters that could break out of a ``<uri>`` context in SPARQL.
    """
    return bool(_SAFE_URI_RE.fullmatch(uri))


def _validate_date(date_str: str) -> bool:
    """Validate that a string is a valid ISO date (YYYY-MM-DD)."""
    return bool(re.match(r"^\d{4}-\d{2}-\d{2}$", date_str))


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------


def explorer_overview(req: Request) -> JSONResponse:
    """GET /api/explorer/overview -- category overview with entity counts."""
    client = _get_client()
    rows = client.query(CATEGORY_OVERVIEW)

    categories = []
    for row in rows:
        type_uri = row.get("type", "")
        # Extract short name from URI
        if "#" in type_uri:
            short_name = type_uri.rsplit("#", 1)[-1]
        else:
            short_name = type_uri.rsplit("/", 1)[-1]
        categories.append(
            {
                "uri": type_uri,
                "name": short_name,
                "count": int(row.get("count", 0)),
            }
        )

    return JSONResponse(
        {
            "data": categories,
            "meta": {"total": len(categories)},
        }
    )


def explorer_category(req: Request, name: str) -> JSONResponse:
    """GET /api/explorer/category/{name} -- paginated entities by type."""
    page, size, offset = _parse_page_params(req)

    # The name is the URI-encoded category URI
    category_uri = unquote(name)
    if not _validate_uri(category_uri):
        return JSONResponse(
            {"error": "Invalid category URI", "data": [], "meta": {}},
            status_code=400,
        )

    client = _get_client()

    # Get total count — use VALUES URI binding to prevent SPARQL injection
    total = client.count(
        client._inject_uri_bindings(ENTITIES_BY_CATEGORY_COUNT, {"categoryType": category_uri})
    )

    # Get paginated entities — use VALUES URI binding + append LIMIT/OFFSET
    entities_sparql = ENTITIES_BY_CATEGORY + f"\nLIMIT {size}\nOFFSET {offset}\n"
    rows = client.query(
        entities_sparql,
        uri_bindings={"categoryType": category_uri},
    )

    entities = []
    for row in rows:
        entities.append(
            {
                "uri": row.get("entity", ""),
                "label": row.get("label", ""),
                "type": row.get("type", ""),
            }
        )

    return JSONResponse(
        {
            "data": entities,
            "meta": {"page": page, "size": size, "total": total},
        }
    )


def explorer_entity(req: Request, entity_id: str) -> JSONResponse:
    """GET /api/explorer/entity/{entity_id} -- entity detail + neighbors."""
    entity_uri = unquote(entity_id)
    if not _validate_uri(entity_uri):
        return JSONResponse(
            {"error": "Invalid entity URI", "data": None, "meta": {}},
            status_code=400,
        )

    client = _get_client()

    # Get literal metadata — use VALUES URI binding to prevent SPARQL injection
    meta_rows = client.query(ENTITY_METADATA, uri_bindings={"entityUri": entity_uri})
    metadata: dict[str, str] = {}
    for row in meta_rows:
        pred = row.get("predicate", "")
        short_pred = pred.rsplit("#", 1)[-1] if "#" in pred else pred.rsplit("/", 1)[-1]
        metadata[short_pred] = row.get("value", "")

    # Get outgoing triples (entity -> predicate -> object)
    out_rows = client.query(ENTITY_DETAIL_OUTGOING, uri_bindings={"entityUri": entity_uri})
    outgoing = []
    for row in out_rows:
        pred = row.get("predicate", "")
        short_pred = pred.rsplit("#", 1)[-1] if "#" in pred else pred.rsplit("/", 1)[-1]
        outgoing.append(
            {
                "predicate": pred,
                "predicateName": short_pred,
                "object": row.get("object", ""),
                "objectLabel": row.get("objectLabel", ""),
            }
        )

    # Get incoming triples (subject -> predicate -> entity)
    in_rows = client.query(ENTITY_DETAIL_INCOMING, uri_bindings={"entityUri": entity_uri})
    incoming = []
    for row in in_rows:
        pred = row.get("predicate", "")
        short_pred = pred.rsplit("#", 1)[-1] if "#" in pred else pred.rsplit("/", 1)[-1]
        incoming.append(
            {
                "subject": row.get("subject", ""),
                "subjectLabel": row.get("subjectLabel", ""),
                "predicate": pred,
                "predicateName": short_pred,
            }
        )

    return JSONResponse(
        {
            "data": {
                "uri": entity_uri,
                "metadata": metadata,
                "outgoing": outgoing,
                "incoming": incoming,
            },
            "meta": {},
        }
    )


def explorer_search(req: Request) -> JSONResponse:
    """GET /api/explorer/search -- search entities by label."""
    q = req.query_params.get("q", "").strip()
    if not q:
        return JSONResponse(
            {
                "data": [],
                "meta": {"query": "", "total": 0},
            }
        )

    try:
        limit = min(_MAX_SEARCH_LIMIT, max(1, int(req.query_params.get("limit", "20"))))
    except (ValueError, TypeError):
        limit = 20

    safe_pattern = _sanitize_regex(q)
    safe_pattern = safe_pattern.replace("\\", "\\\\").replace('"', '\\"')
    query = SEARCH_ENTITIES.format(search_pattern=safe_pattern, limit=limit)

    client = _get_client()
    rows = client.query(query)

    results = []
    for row in rows:
        results.append(
            {
                "uri": row.get("entity", ""),
                "label": row.get("label", ""),
                "type": row.get("type", ""),
            }
        )

    return JSONResponse(
        {
            "data": results,
            "meta": {"query": q, "total": len(results)},
        }
    )


def explorer_timeline(req: Request) -> JSONResponse:
    """GET /api/explorer/timeline -- entities valid at a given date."""
    date = req.query_params.get("date", "").strip()
    if not date or not _validate_date(date):
        return JSONResponse(
            {"error": "Parameter 'date' is required in YYYY-MM-DD format", "data": [], "meta": {}},
            status_code=400,
        )

    page, size, offset = _parse_page_params(req)

    client = _get_client()

    # Count
    count_query = ENTITIES_AT_DATE_COUNT.format(date=date)
    total = client.count(count_query)

    # Paginated results
    query = ENTITIES_AT_DATE.format(date=date, limit=size, offset=offset)
    rows = client.query(query)

    entities = []
    for row in rows:
        entities.append(
            {
                "uri": row.get("entity", ""),
                "label": row.get("label", ""),
                "type": row.get("type", ""),
                "validFrom": row.get("validFrom", ""),
                "validUntil": row.get("validUntil", ""),
            }
        )

    return JSONResponse(
        {
            "data": entities,
            "meta": {"date": date, "page": page, "size": size, "total": total},
        }
    )


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------


def register_explorer_routes(rt) -> None:  # type: ignore[no-untyped-def]
    """Register explorer API routes on the FastHTML route decorator *rt*."""
    rt("/api/explorer/overview", methods=["GET"])(explorer_overview)
    rt("/api/explorer/category/{name:path}", methods=["GET"])(explorer_category)
    rt("/api/explorer/entity/{entity_id:path}", methods=["GET"])(explorer_entity)
    rt("/api/explorer/search", methods=["GET"])(explorer_search)
    rt("/api/explorer/timeline", methods=["GET"])(explorer_timeline)
