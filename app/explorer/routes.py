"""Explorer API routes for browsing the Estonian Legal Ontology."""

from __future__ import annotations

import json
import logging
import re
import uuid
from typing import Any
from urllib.parse import unquote

from starlette.requests import Request
from starlette.responses import JSONResponse

from app.db import get_connection as _connect
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
from app.ontology.relations import legal_phrase as _legal_phrase
from app.ontology.sparql_client import SparqlClient

logger = logging.getLogger(__name__)

_DEFAULT_PAGE_SIZE = 20
_MAX_PAGE_SIZE = 100
_MAX_SEARCH_LIMIT = 50


# #757: evidence-card detail panel (epic #762, design doc
# ``docs/2026-05-12-oiguskaart-evidence-map.md`` workstream D).
#
# The node detail panel turns into an "evidence card": source / date-version /
# relation-type-in-legal-language / a deterministic "why it matters" line / four
# actions.
#
# C0 (2026-05-15): the inline ``_RELATION_LEGAL_PHRASES`` dict was migrated
# verbatim into ``app.ontology.relations`` so the predicate-vocabulary lives in
# one place (impact queries, chat tools, explorer, drafter all import from
# there). The ``relation_legal_phrase`` wrapper below preserves the original
# signature and fallback behaviour. The ``_WHY_IT_MATTERS`` rule table stays
# here because it's explorer-specific UX copy, not vocabulary.


#: The "why it matters" rule table — a deterministic one-line plain-language
#: note keyed on ``(relation_local_name_lowercased, impact_band_or_None)``. When
#: a band is known (a draft-overlay / impact-report context) the high-stakes
#: bands ("high" / "critical") get a punchier line; otherwise the band-agnostic
#: variant is used. The phrasing mirrors the Analüüsikeskus ``Tõendid`` rows
#: ("Tunnistab kehtetuks — eelnõu kaotaks selle sätte."). Anything not in the
#: table falls through to :func:`why_it_matters`'s generic line.
_WHY_IT_MATTERS: dict[str, str] = {
    # repeals — the strongest signal: the provision disappears.
    "repeals": "Tunnistab kehtetuks — eelnõu kaotaks selle sätte.",
    "repealsprovision": "Tunnistab kehtetuks — eelnõu kaotaks selle sätte.",
    "repealedby": "On tunnistatud kehtetuks — see säte ei kehti enam.",
    # amends — the provision changes.
    "amends": "Muudab — säte saaks uue redaktsiooni.",
    "amendsprovision": "Muudab — säte saaks uue redaktsiooni.",
    "amendedby": "On muudetud — kontrolli kehtivat redaktsiooni.",
    "replaces": "Asendab — varasem säte kaotaks kehtivuse.",
    "replacedby": "On asendatud — kontrolli, milline säte praegu kehtib.",
    # EU transposition.
    "transposesdirective": "Võtab üle direktiivi — muudatus mõjutab EL nõuete täitmist.",
    "transposes": "Võtab üle EL õigust — muudatus mõjutab EL nõuete täitmist.",
    "transposedby": "On üle võetud — muudatus võib mõjutada EL nõuete täitmist.",
    "implementseu": "Rakendab EL õigust — muudatus mõjutab EL nõuete täitmist.",
    "implementseulaw": "Rakendab EL õigust — muudatus mõjutab EL nõuete täitmist.",
    "harmonisedwith": "On harmoneeritud EL õigusaktiga — kontrolli kooskõla.",
    "harmonizedwith": "On harmoneeritud EL õigusaktiga — kontrolli kooskõla.",
    # citations / references.
    "references": "Viitab — eelnõu tugineb sellele sättele.",
    "cites": "Viitab — eelnõu tugineb sellele sättele.",
    "citedby": "Viidatakse — teised aktid tuginevad sellele sättele.",
    "relatedto": "On seotud — muudatus võib mõjutada ka seda üksust.",
    "basedon": "Tugineb — kontrolli, kas alus jääb kehtima.",
    # court decisions.
    "applies": "Kohaldab — kohtupraktika tugineb sellele sättele.",
    "appliesprovision": "Kohaldab — kohtupraktika tugineb sellele sättele.",
    "interprets": "Tõlgendab — kohtupraktika sisustab selle sätte tähenduse.",
    "interpretsprovision": "Tõlgendab — kohtupraktika sisustab selle sätte tähenduse.",
}

#: Estonian labels for impact bands used by :func:`why_it_matters` — kept here
#: (rather than imported from :mod:`app.docs.impact.scoring`) so the explorer
#: API has no dependency on the document-analysis layer for this small rule.
_BAND_LABELS_ET: dict[str, str] = {
    "low": "madal mõju",
    "medium": "keskmine mõju",
    "high": "kõrge risk",
    "critical": "kriitiline mõju",
}


def _relation_key(name_or_uri: str) -> str:
    """Reduce a relation IRI / prefixed name / bare name to its lookup key.

    Strips a namespace (``…#local`` / ``…/local`` / ``prefix:local``) and
    lower-cases the result so the rule tables can be keyed once.
    """
    if not name_or_uri:
        return ""
    s = str(name_or_uri).strip()
    if "#" in s:
        s = s.rsplit("#", 1)[-1]
    elif "/" in s:
        s = s.rsplit("/", 1)[-1]
    if ":" in s:
        s = s.rsplit(":", 1)[-1]
    return s.lower()


def relation_legal_phrase(name_or_uri: str) -> str:
    """Return the Estonian legal phrase for a relation, or its short name.

    ``relation_legal_phrase("estleg:amendsProvision")`` → ``"muudab"``,
    ``relation_legal_phrase("repeals")`` → ``"tunnistab kehtetuks"``. Unknown
    relations fall back to the bare local name so the panel never shows an
    empty "Seose liik" slot.

    C0 (2026-05-15): delegates to ``app.ontology.relations.legal_phrase`` so
    every surface (impact queries, chat tools, explorer, drafter) shares one
    vocabulary. Kept here as a thin wrapper to preserve the existing import
    path used by callers in this module and by tests in
    ``tests/test_explorer_routes.py``.
    """
    return _legal_phrase(name_or_uri)


def why_it_matters(name_or_uri: str, band: str | None = None) -> str:
    """Return a one-line plain-language "miks see oluline on" note.

    Deterministic — a lookup in :data:`_WHY_IT_MATTERS` keyed on the relation's
    local name, with the optional :data:`~app.docs.impact.scoring.ImpactBand`
    appended for context when it's a high-stakes band. There is no LLM call.

    Examples::

        why_it_matters("repeals", "critical")
        # → "Tunnistab kehtetuks — eelnõu kaotaks selle sätte. (kriitiline mõju)"
        why_it_matters("amendsProvision")
        # → "Muudab — säte saaks uue redaktsiooni."

    Unknown relations get a generic line built from
    :func:`relation_legal_phrase`.
    """
    key = _relation_key(name_or_uri)
    base = _WHY_IT_MATTERS.get(key)
    if not base:
        phrase = relation_legal_phrase(name_or_uri)
        if phrase:
            base = f"Seos: {phrase}. Muudatus võib mõjutada seda üksust."
        else:
            base = "Muudatus võib mõjutada seda üksust."
    band_key = (band or "").strip().lower()
    if band_key in ("high", "critical"):
        return f"{base} ({_BAND_LABELS_ET.get(band_key, band_key)})"
    return base


# Metadata predicate local-name → Estonian "kuupäev / versioon" label. Used to
# pull a date / version line out of an entity's literal metadata for the
# evidence card's "Kuupäev / versioon" slot. Matched case-insensitively.
_DATE_METADATA_LABELS_ET: dict[str, str] = {
    "validfrom": "Jõustunud",
    "validuntil": "Kehtetu alates",
    "dateadopted": "Vastu võetud",
    "vastuvõtmiskuupäev": "Vastu võetud",
    "datepublished": "Avaldatud",
    "avaldamiskuupäev": "Avaldatud",
    "dateenacted": "Jõustunud",
    "jõustumiskuupäev": "Jõustunud",
    "decisiondate": "Otsuse kuupäev",
    "lahendikuupäev": "Otsuse kuupäev",
    "version": "Redaktsioon",
    "versioon": "Redaktsioon",
    "redaktsioon": "Redaktsioon",
    "casenumber": "Kohtuasja number",
    "celexnumber": "CELEX-number",
}

# Metadata predicate local-name → "Allikas" label, for relations that point at
# the parent act / law / court the entity belongs to. The companion
# ``_SOURCE_RELATIONS`` set drives which *outgoing* relations are treated as
# "this entity's source".
_SOURCE_RELATIONS_ET: dict[str, str] = {
    "sourceact": "Õigusakt",
    "partof": "Kuulub",
    "decidedby": "Kohus",
    "court": "Kohus",
    "fromcourt": "Kohus",
    "topiccluster": "Teemavaldkond",
    "hastopic": "Teemavaldkond",
}

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


def _build_date_info(metadata: dict[str, str]) -> list[dict[str, str]]:
    """#757: extract the evidence card's "Kuupäev / versioon" rows from metadata.

    Returns a list of ``{"label": <Estonian label>, "value": <literal>}`` for
    every metadata predicate whose local name maps in
    :data:`_DATE_METADATA_LABELS_ET` and that has a non-empty value. Order
    follows :data:`_DATE_METADATA_LABELS_ET` (jõustunud/vastu võetud/… first).
    """
    date_info: list[dict[str, str]] = []
    # Lower-case keys → original values for the case-insensitive lookup.
    lc_meta = {k.lower(): v for k, v in metadata.items() if v}
    seen_labels: set[str] = set()
    for key, label in _DATE_METADATA_LABELS_ET.items():
        val = lc_meta.get(key)
        if val and label not in seen_labels:
            date_info.append({"label": label, "value": str(val)})
            seen_labels.add(label)
    return date_info


def _build_source(outgoing: list[dict[str, str]]) -> dict[str, str] | None:
    """#757: derive the evidence card's "Allikas" (parent act / law / court).

    Scans the entity's outgoing relations for the first one whose local name is
    in :data:`_SOURCE_RELATIONS_ET` (``sourceAct``, ``partOf``, ``decidedBy``,
    …) and returns ``{"uri", "label", "relationLabel", "kindLabel"}``. Returns
    ``None`` when the entity has no such relation (e.g. a top-level act).
    """
    for rel in outgoing:
        key = _relation_key(rel.get("predicateName") or rel.get("predicate") or "")
        kind_label = _SOURCE_RELATIONS_ET.get(key)
        if kind_label:
            obj = rel.get("object", "")
            if not obj:
                continue
            label = rel.get("objectLabel") or ""
            if not label:
                # Fall back to the URI fragment so the card always has text.
                label = obj.rsplit("#", 1)[-1] if "#" in obj else obj.rsplit("/", 1)[-1]
            return {
                "uri": obj,
                "label": label,
                "relationLabel": relation_legal_phrase(key),
                "kindLabel": kind_label,
            }
    return None


def explorer_entity(req: Request, entity_id: str) -> JSONResponse:
    """GET /api/explorer/entity/{entity_id} -- entity detail + neighbors.

    #757: the response also carries the *evidence-card* fields the detail panel
    renders — ``source`` (the parent act / law / court the entity belongs to),
    ``dateInfo`` (the entity's date / version literals, Estonian-labelled), and
    a ``predicateLabel`` ("seose liik" in legal language) + ``whyText`` ("miks
    see oluline on") on every outgoing / incoming relation. These are computed
    from small static rule tables — there is no LLM call.
    """
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
                # #757: the relation type in legal language + the deterministic
                # "why it matters" line, so the detail panel's evidence card
                # need not re-derive them client-side.
                "predicateLabel": relation_legal_phrase(short_pred),
                "whyText": why_it_matters(short_pred),
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
                # #757: same pair on the incoming side (e.g. "this provision is
                # repealed by …").
                "predicateLabel": relation_legal_phrase(short_pred),
                "whyText": why_it_matters(short_pred),
            }
        )

    return JSONResponse(
        {
            "data": {
                "uri": entity_uri,
                "metadata": metadata,
                "outgoing": outgoing,
                "incoming": incoming,
                # #757 — evidence-card extras.
                "source": _build_source(outgoing),
                "dateInfo": _build_date_info(metadata),
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
# #755 — draft impact subgraph (epic #762, workstream B)
#
# ``/explorer?draft=<uuid>`` renders only that draft's impact subgraph — the
# affected / conflicting / gap provisions and the relations between them —
# instead of the full 90k-entity graph. The data comes straight out of the
# draft's latest ``impact_reports`` row (the same JSON the report page and the
# .docx export read), so this is *not* a fresh full-ontology traversal: it is
# the already-computed :class:`~app.docs.impact.analyzer.ImpactFindings`
# reshaped into D3 ``{nodes, links}`` form.
#
# Access control mirrors ``app.explorer.pages._fetch_draft_overlay``:
#   - unauthenticated → 401 (defensive; the middleware redirects first)
#   - malformed / non-UUID id → 404 (graceful, never a 500)
#   - draft owned by another org → 404 (existence not revealed)
#   - no impact report yet → 200 with ``has_report=False`` so the JS can show
#     a "run the analysis" fallback instead of a blank graph.
# ---------------------------------------------------------------------------

# How many of each finding kind we surface as graph nodes. The full findings
# list is still in the impact_reports row + the .docx export contains every
# row — this is purely page-weight control for the D3 canvas (mirrors
# ``app.docs.report_routes._MAX_INLINE_ROWS`` in spirit).
_SUBGRAPH_MAX_PER_KIND = 60


def _category_for_type_uri(type_uri: str) -> str:
    """Map an ontology type URI to one of explorer.js' CATEGORY_COLORS keys.

    A loose mirror of explorer.js ``categoryFromUri`` so the subgraph nodes
    pick up sensible colours. Anything unrecognised falls back to
    ``LegalProvision`` (the affected/conflict entities are overwhelmingly
    provisions); the JS resolver applies its own fuzzy fallback on top.
    """
    if not type_uri:
        return "LegalProvision"
    short = type_uri.rsplit("#", 1)[-1] if "#" in type_uri else type_uri.rsplit("/", 1)[-1]
    lower = short.lower()
    if "draft" in lower:
        return "DraftLegislation"
    if "eucourt" in lower or "eu_court" in lower:
        return "EUCourtDecision"
    if "court" in lower or "decision" in lower:
        return "CourtDecision"
    if "directive" in lower or "regulation" in lower or lower.startswith("eu"):
        return "EULegislation"
    if "enacted" in lower or short in ("Act", "EnactedLaw"):
        return "EnactedLaw"
    if "topiccluster" in lower or "topic_cluster" in lower or "cluster" in lower:
        return "TopicCluster"
    if "concept" in lower:
        return "LegalConcept"
    if "provision" in lower or "section" in lower or short in ("Section", "LegalProvision"):
        return "LegalProvision"
    return "LegalProvision"


def _parse_report_json(raw: Any) -> dict[str, Any]:
    """Normalise the JSONB ``report_data`` value into a dict.

    Mirrors ``app.docs.report_routes._parse_report_data`` (kept local so this
    module doesn't reach into ``app/docs/`` for a one-liner).
    """
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, (bytes, bytearray)):
        try:
            return json.loads(raw.decode())
        except (TypeError, ValueError, UnicodeDecodeError):
            return {}
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return {}
    return {}


def _short_name_from_uri(uri: str) -> str:
    """Best-effort short label from a URI fragment / last path segment."""
    if not uri:
        return ""
    tail = uri.rsplit("#", 1)[-1] if "#" in uri else uri.rsplit("/", 1)[-1]
    return tail or uri


def build_draft_subgraph(
    draft_id: str,
    title: str,
    findings: dict[str, Any],
    *,
    max_per_kind: int = _SUBGRAPH_MAX_PER_KIND,
) -> dict[str, Any]:
    """Reshape a draft's :class:`ImpactFindings` JSON into a D3 ``{nodes, links}``.

    The central node is the draft itself; spokes are the affected / conflicting
    / gap entities, each linked back to the draft with a labelled edge
    ("mõjutab" / "konflikt" / "lünk"). Nodes are de-duplicated by URI so an
    entity that is both affected *and* in a conflict appears once. Pure +
    side-effect-free so it's unit-testable without a DB (Phase 5 readiness:
    same shape can back an MCP tool).

    Args:
        draft_id: The draft's UUID (string) — used as the central node id.
        title: The draft's human title (panel label).
        findings: The parsed ``impact_reports.report_data`` dict.
        max_per_kind: Cap on nodes contributed per finding kind.

    Returns:
        ``{"draft_id", "title", "nodes": [...], "links": [...]}``. ``nodes`` and
        ``links`` use the same field names explorer.js' graph layer expects
        (``id`` / ``label`` / ``category`` / ``uri`` / ``isCategory``;
        ``source`` / ``target`` / ``label``).
    """
    draft_node_id = f"draft:{draft_id}"
    nodes: list[dict[str, Any]] = [
        {
            "id": draft_node_id,
            "uri": "",
            "label": title or "Eelnõu",
            "category": "DraftLegislation",
            "isCategory": False,
            "isDraft": True,
            "kind": "draft",
        }
    ]
    links: list[dict[str, Any]] = []
    seen: set[str] = {draft_node_id}

    def _add_spoke(
        node_id: str,
        label: str,
        category: str,
        kind: str,
        edge_label: str,
        *,
        uri: str = "",
        note: str = "",
    ) -> None:
        if not node_id or node_id in seen:
            # Still draw the edge if the node already exists (e.g. affected ∩
            # conflict) so the relationship isn't lost — but only one edge
            # per (kind) to keep it readable.
            if node_id and node_id in seen and node_id != draft_node_id:
                already = any(
                    (link.get("target") == node_id and link.get("kind") == kind) for link in links
                )
                if not already:
                    links.append(
                        {
                            "source": draft_node_id,
                            "target": node_id,
                            "label": edge_label,
                            "kind": kind,
                        }
                    )
            return
        seen.add(node_id)
        node: dict[str, Any] = {
            "id": node_id,
            "uri": uri,
            "label": label or _short_name_from_uri(uri) or node_id,
            "category": category,
            "isCategory": False,
            "kind": kind,
        }
        if note:
            node["note"] = note
        nodes.append(node)
        links.append(
            {
                "source": draft_node_id,
                "target": node_id,
                "label": edge_label,
                "kind": kind,
            }
        )

    # --- Affected entities (the 2-hop BFS hits) ---
    for row in list(findings.get("affected_entities") or [])[:max_per_kind]:
        if not isinstance(row, dict):
            continue
        uri = str(row.get("uri") or "").strip()
        if not uri:
            continue
        _add_spoke(
            uri,
            str(row.get("label") or ""),
            _category_for_type_uri(str(row.get("type") or "")),
            "affected",
            "mõjutab",
            uri=uri,
        )

    # --- Conflicts (overlaps with other drafts / court decisions) ---
    for row in list(findings.get("conflicts") or [])[:max_per_kind]:
        if not isinstance(row, dict):
            continue
        uri = str(row.get("conflicting_entity") or "").strip()
        label = str(row.get("conflicting_label") or "") or _short_name_from_uri(uri)
        reason = str(row.get("reason") or "")
        if uri:
            _add_spoke(
                uri,
                label,
                _category_for_type_uri(uri),
                "conflict",
                "konflikt",
                uri=uri,
                note=reason,
            )
        else:
            # A conflict with no URI still gets a (synthetic) node so the
            # finding isn't silently dropped from the map.
            draft_ref = str(row.get("draft_ref") or "")
            synth_id = f"conflict:{draft_ref or label or len(nodes)}"
            _add_spoke(
                synth_id,
                label or draft_ref or "Konflikt",
                "DraftLegislation",
                "conflict",
                "konflikt",
                note=reason,
            )

    # --- Gaps (under-referenced topic clusters) ---
    for row in list(findings.get("gaps") or [])[:max_per_kind]:
        if not isinstance(row, dict):
            continue
        uri = str(row.get("topic_cluster") or "").strip()
        label = str(row.get("topic_cluster_label") or "") or _short_name_from_uri(uri)
        desc = str(row.get("description") or "")
        node_id = uri or f"gap:{label or len(nodes)}"
        _add_spoke(
            node_id,
            label or "Teemaklaster",
            "TopicCluster",
            "gap",
            "lünk",
            uri=uri,
            note=desc,
        )

    return {
        "draft_id": draft_id,
        "title": title or "Eelnõu",
        "nodes": nodes,
        "links": links,
    }


def explorer_draft_subgraph(req: Request, draft_id: str) -> JSONResponse:
    """GET /explorer/draft-subgraph/{draft_id} — the draft's impact subgraph.

    Returns a JSON ``{nodes, links}`` built from the draft's latest impact
    report. Org-scoped: a user from another org gets a 404 (the draft's
    existence is not revealed). Malformed ids → 404. No impact report yet →
    200 with ``has_report=False`` and empty ``nodes``/``links`` so explorer.js
    can show a graceful "run the analysis" fallback.
    """
    auth = req.scope.get("auth")
    if not auth or not auth.get("id") or not auth.get("org_id"):
        return JSONResponse({"error": "Authentication required"}, status_code=401)

    try:
        draft_uuid = uuid.UUID(str(draft_id))
    except (TypeError, ValueError):
        return JSONResponse({"error": "Eelnõu ei leitud"}, status_code=404)

    org_id = str(auth.get("org_id"))
    # #844: collected inside the connection block below so the conflict
    # masking can reuse it without opening a second connection.
    owned_draft_ids: set[str] = set()
    try:
        with _connect() as conn:
            draft_row = conn.execute(
                "SELECT org_id, title FROM drafts WHERE id = %s",
                (str(draft_uuid),),
            ).fetchone()
            if draft_row is None or str(draft_row[0]) != org_id:
                # Missing or cross-org → 404 (don't reveal which).
                return JSONResponse({"error": "Eelnõu ei leitud"}, status_code=404)
            title = str(draft_row[1] or "Eelnõu")
            report_row = conn.execute(
                """
                SELECT report_data
                FROM impact_reports
                WHERE draft_id = %s
                ORDER BY generated_at DESC
                LIMIT 1
                """,
                (str(draft_uuid),),
            ).fetchone()
            # #844: the viewer's owned draft UUIDs, fetched on this same
            # connection, drive the cross-org conflict mask applied below.
            from app.docs.impact.masking import fetch_owned_draft_ids

            owned_draft_ids = fetch_owned_draft_ids(conn, org_id)
    except Exception:
        logger.exception("draft subgraph: DB error for draft=%s", draft_id)
        # Degrade to "no report" rather than 500 — the JS shows the fallback.
        return JSONResponse(
            {
                "data": {"draft_id": str(draft_uuid), "title": "Eelnõu", "nodes": [], "links": []},
                "meta": {"has_report": False, "draft_id": str(draft_uuid)},
            }
        )

    if report_row is None:
        return JSONResponse(
            {
                "data": {"draft_id": str(draft_uuid), "title": title, "nodes": [], "links": []},
                "meta": {"has_report": False, "draft_id": str(draft_uuid)},
            }
        )

    findings = _parse_report_json(report_row[0])
    # #844: scrub foreign-org conflict rows (and stale adhoc probes) from a
    # pre-fix persisted report before the subgraph builds nodes from them.
    # Reuses ``owned_draft_ids`` fetched on the connection above — no extra
    # round-trip.
    from app.docs.impact.masking import drop_adhoc_conflict_rows, mask_conflict_rows

    masked_conflicts = mask_conflict_rows(
        drop_adhoc_conflict_rows(list(findings.get("conflicts") or [])),
        owned_draft_ids=owned_draft_ids,
    )
    findings = dict(findings)
    findings["conflicts"] = masked_conflicts
    findings["conflict_count"] = len(masked_conflicts)

    subgraph = build_draft_subgraph(str(draft_uuid), title, findings)
    affected = len(list(findings.get("affected_entities") or []))
    conflicts = len(list(findings.get("conflicts") or []))
    gaps = len(list(findings.get("gaps") or []))
    return JSONResponse(
        {
            "data": subgraph,
            "meta": {
                "has_report": True,
                "draft_id": str(draft_uuid),
                "affected_count": affected,
                "conflict_count": conflicts,
                "gap_count": gaps,
                "node_count": len(subgraph["nodes"]),
                # The report itself is unbounded; the graph is capped per kind.
                "truncated": (
                    affected > _SUBGRAPH_MAX_PER_KIND
                    or conflicts > _SUBGRAPH_MAX_PER_KIND
                    or gaps > _SUBGRAPH_MAX_PER_KIND
                ),
            },
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
    # #755: the draft's impact subgraph (org-scoped; backs ?draft=<id>).
    rt("/explorer/draft-subgraph/{draft_id}", methods=["GET"])(explorer_draft_subgraph)
