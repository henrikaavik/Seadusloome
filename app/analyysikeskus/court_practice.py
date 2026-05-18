"""SPARQL helpers for the Kohtupraktika sätte kohta workflow (C3, plan section 5).

The `Kohtupraktika sätte kohta` (Court practice for a provision) workflow surfaces
every ``CourtDecision`` / ``EUCourtDecision`` that interprets or applies a chosen
provision or act, grouped by court (Riigikohus, Euroopa Kohus, ringkonnakohus),
with citation counts and a year-bucket time trend.

The ontology models a court → provision interpretation via the symmetric pair
``estleg:interpretsLaw`` (CourtDecision → Provision) and ``estleg:interpretedBy``
(Provision → CourtDecision). Both directions are present in the data — the
queries below ``UNION`` them so a one-side serialisation never drops rows.
Predicates come from :mod:`app.ontology.relations` (C0) — never hardcoded — so
a future rename only happens in one place.

Public functions:

* :func:`list_decisions_for_provision` — every decision interpreting a single
  provision.
* :func:`list_decisions_for_act` — every decision interpreting *any* provision
  of an act. Joined via the literal ``estleg:sourceAct`` title in prod —
  ``estleg:partOf`` carries zero rows in the deployed corpus (verified by
  the 2026-05-18 ontology probe), so the historical UNION arm was dropped.
  See ``docs/2026-05-18-bugfix-plan.md`` Wave 2 Step 5.
* :func:`group_by_court` — pure helper: groups a list of
  :class:`CourtDecisionRow` rows by an Estonian court bucket
  (Riigikohus / Euroopa Kohus / ringkonnakohus / muu), counts citations, and
  computes a year-bucket trend.

Every SPARQL call is guarded — a dead Jena yields ``[]``, never a 500.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Literal

from app.ontology.queries import PREFIXES
from app.ontology.relations import PREDICATES
from app.ontology.sparql_client import SparqlClient

logger = logging.getLogger(__name__)

# Cap how many decision rows we project for a single provision/act.  The
# Riigikohus alone has 12k+ decisions in the corpus; a high-traffic
# provision can collect 50+ interpretive decisions over the years.  100
# keeps the page scannable while ensuring the rare "very heavily cited"
# provision still renders meaningfully.
_MAX_DECISIONS_PER_QUERY = 100


# ---------------------------------------------------------------------------
# Court buckets — Estonian taxonomy
# ---------------------------------------------------------------------------

CourtBucket = Literal["riigikohus", "euroopa_kohus", "ringkonnakohus", "muu"]

# Estonian display labels for the four buckets.  The bucket key is the
# stable identifier used in the grouping output; the label is for UI.
COURT_BUCKET_LABELS_ET: dict[CourtBucket, str] = {
    "riigikohus": "Riigikohus",
    "euroopa_kohus": "Euroopa Kohus",
    "ringkonnakohus": "Ringkonnakohus",
    "muu": "Muu kohus",
}

# Render order for the buckets on the result page — Riigikohus first
# (most authoritative for Estonian law), then EU, then ringkonnakohus,
# then everything else.
COURT_BUCKET_ORDER: list[CourtBucket] = [
    "riigikohus",
    "euroopa_kohus",
    "ringkonnakohus",
    "muu",
]


# Estonian Supreme Court case numbers follow the ``N-N-N-...`` shape
# (``3-1-1-63-15``, ``5-19-1-2`` …). EU court case numbers carry the
# ``C-`` / ``T-`` / ``F-`` letter prefix (``C-131/12``, ``T-99/04``).
# These patterns are used by :func:`classify_court` when neither the
# rdf:type localname nor the label gives a clean signal — e.g. data
# rows that only carry a case number literal.
_EE_CASE_RE = re.compile(r"^\d+-\d+-\d+(?:-\d+)*$")
_EU_CASE_RE = re.compile(r"^[CTF]-\d+/\d+$", re.IGNORECASE)


def classify_court(
    type_uri: str = "",
    court_label: str = "",
    case_number: str = "",
) -> CourtBucket:
    """Classify a decision into one of the four court buckets.

    The signal sources in priority order:

    1. ``type_uri`` — the RDF type's local name. ``EUCourtDecision`` →
       ``euroopa_kohus``; ``CourtDecision`` falls through to the
       label/case-number heuristics (most Estonian cases use the bare
       ``CourtDecision`` type with court info in the label).
    2. ``court_label`` — explicit court name on the decision
       (``rdfs:label`` of the ``court`` field or directly on the decision).
       "Riigikohus" → ``riigikohus``; "Euroopa Kohus" / "EL Kohus" /
       "CJEU" / "ECJ" → ``euroopa_kohus``; "Ringkonnakohus" →
       ``ringkonnakohus``; otherwise → ``muu``.
    3. ``case_number`` — Estonian ``N-N-N-…`` numbers → ``riigikohus``
       (the supreme-court case-number format); ``C-N/N`` / ``T-N/N`` →
       ``euroopa_kohus`` (the CJEU case-number format).

    Returns ``"muu"`` when no signal matches — keeps the bucket complete
    so the grouper never drops a row.
    """
    type_local = _local_name(type_uri).lower()
    if type_local == "eucourtdecision":
        return "euroopa_kohus"

    label_lc = (court_label or "").strip().lower()
    if label_lc:
        if "riigikohus" in label_lc:
            return "riigikohus"
        if (
            "euroopa kohus" in label_lc
            or "el kohus" in label_lc
            or "euroopa liidu kohus" in label_lc
            or "court of justice" in label_lc
            or label_lc == "cjeu"
            or label_lc == "ecj"
        ):
            return "euroopa_kohus"
        if "ringkonnakohus" in label_lc:
            return "ringkonnakohus"

    case = (case_number or "").strip()
    if case:
        if _EU_CASE_RE.fullmatch(case):
            return "euroopa_kohus"
        if _EE_CASE_RE.fullmatch(case):
            # Estonian dashed case number — most commonly Riigikohus in
            # the published corpus.
            return "riigikohus"

    return "muu"


def _local_name(uri: str) -> str:
    """Return the local name of a URI (after ``#`` or last ``/``)."""
    if not uri:
        return ""
    s = str(uri).strip()
    if "#" in s:
        return s.rsplit("#", 1)[-1]
    if "/" in s:
        return s.rsplit("/", 1)[-1]
    return s


# ---------------------------------------------------------------------------
# CourtDecisionRow — the structured row the UI renders
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CourtDecisionRow:
    """A single CourtDecision projected from the ontology.

    Most fields are ``Optional``-shaped because the corpus' completeness
    varies — a decision row may lack an explicit court label or a year.

    Attributes:
        decision_uri: The decision URI (the ``estleg:interpretsLaw``
            subject when projected via the canonical direction). Always
            set when the SPARQL result row resolves.
        decision_label: ``rdfs:label`` on the decision. Falls back to the
            case number / URI tail in the route.
        case_number: ``estleg:caseNumber`` / ``estleg:kohtuasi`` literal,
            or empty string when absent. Used both to display ("Kohtuasi
            nr …") and to classify EU vs Estonian case shapes.
        decision_date: ``xsd:date`` literal as a string (``"YYYY-MM-DD"``).
            Empty when absent; the year-bucket trend uses :func:`year_of`
            to extract the year defensively.
        court_uri: When the ontology models the court as a separate
            entity (``estleg:court``), its URI; empty string otherwise.
        court_label: ``rdfs:label`` of the linked court (or, when the
            court is inlined as a literal, the literal itself).
        type_uri: The decision's ``rdf:type`` URI. Used by
            :func:`classify_court` to distinguish ``EUCourtDecision`` from
            ``CourtDecision``.
        provision_uri: The Provision URI the decision interprets — kept
            on the row so a "back to source" link works even when the
            same decision interprets several provisions.
        provision_label: ``rdfs:label`` on the provision.
    """

    decision_uri: str = ""
    decision_label: str = ""
    case_number: str = ""
    decision_date: str = ""
    court_uri: str = ""
    court_label: str = ""
    type_uri: str = ""
    provision_uri: str = ""
    provision_label: str = ""
    extras: dict[str, str] = field(default_factory=dict)

    @property
    def bucket(self) -> CourtBucket:
        """Convenience: the row's court bucket (Estonian taxonomy)."""
        return classify_court(self.type_uri, self.court_label, self.case_number)

    @property
    def year(self) -> int | None:
        """Convenience: the integer year of the decision date, or ``None``."""
        return year_of(self.decision_date)


def year_of(date_literal: str) -> int | None:
    """Extract the year from an ``xsd:date`` (or ``xsd:dateTime``) literal.

    Defensive: a malformed literal yields ``None`` rather than raising,
    so a single bad row doesn't poison the year-bucket trend. Accepts
    both ``"YYYY-MM-DD"`` and ``"YYYY-MM-DDTHH:MM:SSZ"``-ish shapes.
    """
    if not date_literal:
        return None
    text = str(date_literal).strip()
    if len(text) < 4:
        return None
    head = text[:4]
    if not head.isdigit():
        return None
    try:
        year = int(head)
    except ValueError:
        return None
    # Sanity bounds — anything outside 1900..2100 is almost certainly a
    # parse glitch (CELEX year tails get misclassified as decision dates
    # by some older exports).  Drop them silently.
    if not (1900 <= year <= 2100):
        return None
    return year


# ---------------------------------------------------------------------------
# CourtPracticeGroup — the grouped output the UI renders
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CourtPracticeGroup:
    """One court bucket's rows + citation count + year-bucket trend.

    Attributes:
        bucket: The bucket key — one of :data:`COURT_BUCKET_ORDER`.
        label_et: The Estonian display label
            (:data:`COURT_BUCKET_LABELS_ET`).
        rows: The decisions in this bucket, ordered newest-first by
            decision date when the date is present (rows without a
            date sink to the bottom).
        citation_count: ``len(rows)``. Surfaced explicitly so the UI
            doesn't need to recount.
        year_trend: ``{year: count}`` — how many decisions fell in each
            year, sorted ascending by year. Years with no decisions are
            omitted (sparse). Decisions without a parseable year are
            silently excluded from the trend.
    """

    bucket: CourtBucket
    label_et: str
    rows: list[CourtDecisionRow]
    citation_count: int
    year_trend: dict[int, int]


# ---------------------------------------------------------------------------
# SPARQL templates
# ---------------------------------------------------------------------------
#
# We project the same shape of columns in both queries so the row builder
# works uniformly.  Both directions of the interpretation edge are
# UNION-ed so a one-side serialisation never drops rows.

# Both queries use the canonical predicate URIs from C0 — never bare
# "estleg:interpretsLaw" strings — by interpolating
# ``PREDICATES.INTERPRETS_LAW`` / ``PREDICATES.INTERPRETED_BY`` as
# ``<...>`` URI terms at build time.  The interpolation is safe because
# the URI constants are module-level :data:`~typing.Final` strings —
# never user input.

_PROVISION_DECISIONS_QUERY = (
    PREFIXES
    + f"""
SELECT DISTINCT ?decision ?decisionLabel ?caseNumber ?decisionDate
                ?court ?courtLabel ?type
                ?provision ?provisionLabel
WHERE {{
  {{
    ?decision <{PREDICATES.INTERPRETS_LAW}> ?provision .
  }} UNION {{
    ?provision <{PREDICATES.INTERPRETED_BY}> ?decision .
  }}
  OPTIONAL {{ ?decision rdfs:label ?decisionLabel }}
  OPTIONAL {{ ?decision estleg:caseNumber ?caseNumber }}
  OPTIONAL {{ ?decision estleg:decisionDate ?decisionDate }}
  OPTIONAL {{ ?decision estleg:court ?court .
             OPTIONAL {{ ?court rdfs:label ?courtLabel }} }}
  OPTIONAL {{ ?decision a ?type .
             FILTER(STRSTARTS(STR(?type), "https://data.riik.ee/ontology/estleg#")) }}
  OPTIONAL {{ ?provision rdfs:label ?provisionLabel }}
}}
ORDER BY DESC(?decisionDate)
LIMIT {_MAX_DECISIONS_PER_QUERY}
"""
)

# The act-level query joins provisions to the act via the literal
# ``estleg:sourceAct`` title (the only join path that carries rows in
# prod — see the 2026-05-18 ontology probe in
# ``docs/2026-05-18-bugfix-plan.md``: 24,221 ``sourceAct`` triples, all
# ``xsd:string``; zero ``partOf`` / ``partOfAct`` triples).
#
# The act-binding is therefore a string-literal — ``?actLit`` — not a
# URI, and the caller injects it via :meth:`SparqlClient._inject_bindings`
# (the string-literal variant of ``_inject_uri_bindings``).
_ACT_DECISIONS_QUERY = (
    PREFIXES
    + f"""
SELECT DISTINCT ?decision ?decisionLabel ?caseNumber ?decisionDate
                ?court ?courtLabel ?type
                ?provision ?provisionLabel
WHERE {{
  ?provision estleg:sourceAct ?actLit .
  {{
    ?decision <{PREDICATES.INTERPRETS_LAW}> ?provision .
  }} UNION {{
    ?provision <{PREDICATES.INTERPRETED_BY}> ?decision .
  }}
  OPTIONAL {{ ?decision rdfs:label ?decisionLabel }}
  OPTIONAL {{ ?decision estleg:caseNumber ?caseNumber }}
  OPTIONAL {{ ?decision estleg:decisionDate ?decisionDate }}
  OPTIONAL {{ ?decision estleg:court ?court .
             OPTIONAL {{ ?court rdfs:label ?courtLabel }} }}
  OPTIONAL {{ ?decision a ?type .
             FILTER(STRSTARTS(STR(?type), "https://data.riik.ee/ontology/estleg#")) }}
  OPTIONAL {{ ?provision rdfs:label ?provisionLabel }}
}}
ORDER BY DESC(?decisionDate)
LIMIT {_MAX_DECISIONS_PER_QUERY}
"""
)


# Lightweight URI → literal-title reverse lookup. The corpus has no
# atomic Act URIs but a caller may still pass a URI during the
# resolver-rewrite transition (Wave 2 Step 2). When we get a URI, peek
# its ``rdfs:label``; if a label exists, use that as ``?actLit``. If no
# label is found (the common prod case for any caller still on the URI
# shape) the caller sees an empty result rather than a silent SPARQL
# error.
_ACT_LABEL_FOR_URI_QUERY = (
    PREFIXES
    + """
SELECT ?label
WHERE {
  ?act rdfs:label ?label .
}
LIMIT 1
"""
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def list_decisions_for_provision(
    provision_uri: str,
    *,
    sparql_client: SparqlClient | None = None,
) -> list[CourtDecisionRow]:
    """Return every CourtDecision interpreting *provision_uri*.

    Args:
        provision_uri: A ``LegalProvision`` URI (the object of
            ``estleg:interpretsLaw`` / subject of
            ``estleg:interpretedBy``). Empty / whitespace input yields
            ``[]`` without hitting Jena.
        sparql_client: Optional :class:`SparqlClient` override (tests
            inject one whose ``.query`` is mocked).

    Returns:
        A list of :class:`CourtDecisionRow` — one per matching decision.
        Deduped by ``decision_uri`` so a decision that carries both edge
        directions doesn't double-count. ``[]`` when no decisions match
        or any SPARQL error occurs.
    """
    uri = (provision_uri or "").strip()
    if not uri:
        return []

    client = sparql_client if sparql_client is not None else SparqlClient()
    try:
        rows = client.query(
            _PROVISION_DECISIONS_QUERY,
            uri_bindings={"provision": uri},
        )
    except Exception:
        logger.warning(
            "list_decisions_for_provision: SPARQL query failed for %r",
            uri,
            exc_info=True,
        )
        return []

    return _rows_to_decisions(rows)


def list_decisions_for_act(
    act_identifier: str,
    *,
    sparql_client: SparqlClient | None = None,
) -> list[CourtDecisionRow]:
    """Return every CourtDecision interpreting any provision of an act.

    Walks the graph ``?provision estleg:sourceAct ?actLit`` then
    ``?decision interpretsLaw ?provision`` (or the inverse) and
    aggregates. ``?actLit`` is bound as a *string literal* — the
    deployed corpus stores ``estleg:sourceAct`` as a literal title
    (24,221 triples, all ``xsd:string``; zero ``partOf`` triples), per
    the 2026-05-18 ontology probe.

    Args:
        act_identifier: The act title literal (e.g.
            ``"Avaliku teabe seadus"``). Accepts a URI during the
            resolver-rewrite transition (Wave 2 Step 2): when the
            input starts with ``http://`` / ``https://`` the helper
            does a one-shot ``rdfs:label`` reverse-lookup and uses
            that label as the literal. Empty / whitespace yields
            ``[]``.
        sparql_client: Optional :class:`SparqlClient` override.

    Returns:
        A list of :class:`CourtDecisionRow` — deduped by
        ``decision_uri``. ``[]`` on no matches / SPARQL error /
        URI-with-no-label.
    """
    identifier = (act_identifier or "").strip()
    if not identifier:
        return []

    client = sparql_client if sparql_client is not None else SparqlClient()
    act_literal = _resolve_act_literal(identifier, client=client)
    if not act_literal:
        return []

    try:
        rows = client.query(
            _ACT_DECISIONS_QUERY,
            bindings={"actLit": act_literal},
        )
    except Exception:
        logger.warning(
            "list_decisions_for_act: SPARQL query failed for %r",
            identifier,
            exc_info=True,
        )
        return []

    return _rows_to_decisions(rows)


def _resolve_act_literal(identifier: str, *, client: SparqlClient) -> str:
    """Return the act title literal for *identifier*.

    Pass-through when *identifier* is already a literal title (does not
    start with ``http://`` / ``https://``). When it's a URI, look up
    ``?act rdfs:label`` and return the first label; on miss or error,
    returns ``""`` so the caller sees an empty result rather than a
    spurious match.
    """
    text = identifier.strip()
    if not text:
        return ""
    if not (text.startswith("http://") or text.startswith("https://")):
        # Already a literal title.
        return text
    # URI path — peek the label.
    try:
        rows = client.query(
            _ACT_LABEL_FOR_URI_QUERY,
            uri_bindings={"act": text},
        )
    except Exception:
        logger.warning(
            "_resolve_act_literal: rdfs:label lookup failed for %r",
            text,
            exc_info=True,
        )
        return ""
    for row in rows or []:
        label = (row.get("label") or "").strip()
        if label:
            return label
    return ""


def group_by_court(
    rows: list[CourtDecisionRow],
) -> list[CourtPracticeGroup]:
    """Group *rows* by court bucket; compute citation counts + year trends.

    Empty *rows* ⇒ ``[]`` (no empty groups returned). Buckets are
    returned in :data:`COURT_BUCKET_ORDER` for stable rendering; an
    empty bucket is omitted.

    Within each group, rows are sorted by ``decision_date`` descending
    (newest first); rows without a date sink to the bottom in the order
    they arrived. The ``year_trend`` is sparse — years with zero
    decisions are not present — and sorted ascending by year so a UI
    line chart / bar chart consumes it directly.
    """
    if not rows:
        return []

    buckets: dict[CourtBucket, list[CourtDecisionRow]] = {b: [] for b in COURT_BUCKET_ORDER}
    # Dedupe on (decision_uri, provision_uri) — the same decision URI
    # can appear once per interpreted provision, but for grouping we
    # consider each decision once per bucket.  Keep the first occurrence.
    seen: set[str] = set()
    for r in rows:
        key = r.decision_uri or f"_blank_{id(r)}"
        if key in seen:
            continue
        seen.add(key)
        buckets[r.bucket].append(r)

    out: list[CourtPracticeGroup] = []
    for bucket in COURT_BUCKET_ORDER:
        group_rows = buckets[bucket]
        if not group_rows:
            continue
        # Sort newest first; missing dates ("") sort last because empty
        # string compares less than any non-empty string — invert with a
        # presence flag.
        sorted_rows = sorted(
            group_rows,
            key=lambda r: (r.decision_date == "", _reverse_date_key(r.decision_date)),
        )
        year_counter: Counter[int] = Counter()
        for r in sorted_rows:
            y = r.year
            if y is not None:
                year_counter[y] += 1
        # Sort year_trend ascending so the UI gets oldest → newest.
        year_trend = dict(sorted(year_counter.items()))
        out.append(
            CourtPracticeGroup(
                bucket=bucket,
                label_et=COURT_BUCKET_LABELS_ET[bucket],
                rows=sorted_rows,
                citation_count=len(sorted_rows),
                year_trend=year_trend,
            )
        )
    return out


def _reverse_date_key(date_literal: str) -> str:
    """Sort key that orders ``YYYY-MM-DD`` strings *descending* lexically.

    Empty strings come back as empty (the caller's presence flag handles
    those); non-empty strings get a per-character inversion so Python's
    ascending sort produces descending dates without needing
    ``reverse=True`` (which would invert the presence-flag tier).
    """
    if not date_literal:
        return ""
    # Invert ASCII by subtracting from a high codepoint — produces a key
    # whose lexical order is the reverse of the input's.  Works for any
    # date literal because the digits 0-9 and the "-" separator stay in
    # the printable range.
    return "".join(chr(0x10FFFF - ord(c)) for c in date_literal)


# ---------------------------------------------------------------------------
# Internal — row → CourtDecisionRow
# ---------------------------------------------------------------------------


def _rows_to_decisions(rows: list[dict[str, str]]) -> list[CourtDecisionRow]:
    """Convert SPARQL JSON binding rows into :class:`CourtDecisionRow`.

    Dedupes on ``(decision_uri, provision_uri)`` — the UNION of the two
    edge directions can produce two rows for the same edge if both
    serialisations are present.
    """
    out: list[CourtDecisionRow] = []
    seen: set[tuple[str, str]] = set()
    for row in rows or []:
        decision_uri = (row.get("decision") or "").strip()
        provision_uri = (row.get("provision") or "").strip()
        key = (decision_uri, provision_uri)
        if key in seen:
            continue
        seen.add(key)
        out.append(
            CourtDecisionRow(
                decision_uri=decision_uri,
                decision_label=(row.get("decisionLabel") or "").strip(),
                case_number=(row.get("caseNumber") or "").strip(),
                decision_date=_normalise_date(row.get("decisionDate")),
                court_uri=(row.get("court") or "").strip(),
                court_label=(row.get("courtLabel") or "").strip(),
                type_uri=(row.get("type") or "").strip(),
                provision_uri=provision_uri,
                provision_label=(row.get("provisionLabel") or "").strip(),
            )
        )
    return out


def _normalise_date(raw: Any) -> str:
    """Normalise a SPARQL date literal to ``"YYYY-MM-DD"`` (or pass through).

    Strips a trailing timezone designator (``"Z"`` / ``"+02:00"``) and a
    time suffix so :func:`year_of` and the date-string sort key both
    behave predictably. A non-date literal passes through unchanged so
    the UI can still render it.
    """
    if raw is None:
        return ""
    text = str(raw).strip()
    if not text:
        return ""
    # Drop a "T..." time suffix.
    if "T" in text:
        text = text.split("T", 1)[0]
    # Drop a trailing timezone if present (``YYYY-MM-DDZ`` / ``…+02:00``).
    if len(text) > 10 and text[10] in {"Z", "+", "-"}:
        text = text[:10]
    return text
