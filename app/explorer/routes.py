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


# #757: evidence-card detail panel (epic #762, design doc
# ``docs/2026-05-12-oiguskaart-evidence-map.md`` workstream D).
#
# The node detail panel turns into an "evidence card": source / date-version /
# relation-type-in-legal-language / a deterministic "why it matters" line / four
# actions. The relation-phrase map and the "why it matters" rule table below are
# *small static rule tables* — there is deliberately no LLM call here. Both are
# keyed on the ontology relation's local name (the bit after ``#`` / ``/``),
# matched case-insensitively so ``estleg:amendsProvision`` and a bare ``amends``
# both resolve.

#: Ontology relation local-name → Estonian *legal* phrase. The values read as a
#: legal lawyer would name the relationship ("muudab", "tunnistab kehtetuks",
#: "võtab üle direktiivi", …) rather than as a raw predicate name. Keys are
#: lower-cased for the case-insensitive lookup in :func:`relation_legal_phrase`.
_RELATION_LEGAL_PHRASES: dict[str, str] = {
    # Amendments / repeals.
    "amendsprovision": "muudab",
    "amends": "muudab",
    "amendedby": "muudetud õigusaktiga",
    "repealsprovision": "tunnistab kehtetuks",
    "repeals": "tunnistab kehtetuks",
    "repealedby": "tunnistatud kehtetuks õigusaktiga",
    "replaces": "asendab",
    "replacedby": "asendatud õigusaktiga",
    # EU transposition / harmonisation.
    "transposesdirective": "võtab üle direktiivi",
    "transposes": "võtab üle",
    "transposedby": "üle võetud õigusaktiga",
    "implementseu": "rakendab EL õigust",
    "implementseulaw": "rakendab EL õigust",
    "harmonisedwith": "on harmoneeritud õigusaktiga",
    "harmonizedwith": "on harmoneeritud õigusaktiga",
    # Citations / references.
    "references": "viitab",
    "cites": "viitab",
    "citedby": "viidatud õigusaktiga",
    "relatedto": "on seotud",
    "basedon": "tugineb",
    # Court-decision relations.
    "appliesprovision": "kohaldab",
    "applies": "kohaldab",
    "interpretsprovision": "tõlgendab",
    "interprets": "tõlgendab",
    # Structure / membership.
    "sourceact": "kuulub õigusakti",
    "partof": "on osa",
    "hasprovision": "sisaldab sätet",
    "hastopic": "kuulub teemavaldkonda",
    "topiccluster": "kuulub teemavaldkonda",
    "definesconcept": "määratleb mõiste",
}


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
    """
    key = _relation_key(name_or_uri)
    if not key:
        return ""
    phrase = _RELATION_LEGAL_PHRASES.get(key)
    if phrase:
        return phrase
    # Fallback: the short name from the original input, un-mangled by the
    # lower-casing the key needed.
    s = str(name_or_uri).strip()
    if "#" in s:
        s = s.rsplit("#", 1)[-1]
    elif "/" in s:
        s = s.rsplit("/", 1)[-1]
    if ":" in s:
        s = s.rsplit(":", 1)[-1]
    return s


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
# Route registration
# ---------------------------------------------------------------------------


def register_explorer_routes(rt) -> None:  # type: ignore[no-untyped-def]
    """Register explorer API routes on the FastHTML route decorator *rt*."""
    rt("/api/explorer/overview", methods=["GET"])(explorer_overview)
    rt("/api/explorer/category/{name:path}", methods=["GET"])(explorer_category)
    rt("/api/explorer/entity/{entity_id:path}", methods=["GET"])(explorer_entity)
    rt("/api/explorer/search", methods=["GET"])(explorer_search)
    rt("/api/explorer/timeline", methods=["GET"])(explorer_timeline)
