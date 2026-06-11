"""SPARQL helpers for the Ajaloolise kehtivuse workflow (A4 v1).

Task A4 v1 from ``docs/2026-05-15-ontology-six-use-cases-plan.md`` (lines
309-331). Given a Provision / Act / CourtDecision URI, surfaces the
act-level temporal data that the ontology populates corpus-wide today:

* Act-level timeline — ``estleg:entryIntoForce``, ``estleg:repealDate``,
  ``estleg:lastAmendmentDate``, ``estleg:temporalStatus`` on the owning
  Act.
* AmendmentEvent list — every ``estleg:AmendmentEvent`` whose
  ``estleg:amends`` target is (a) the input provision directly, (b) any
  provision of the input act, or (c) the input act itself. For each
  event we surface its date (``estleg:eventDate``), entry-into-force
  date (``estleg:entryIntoForceDate``), RT citation
  (``estleg:rtReference``) and the set of affected provisions
  (``estleg:amends``).
* Court decisions — every ``estleg:CourtDecision`` /
  ``estleg:EUCourtDecision`` that ``estleg:interpretsLaw`` the input or
  a sibling provision of the input's act. Surfaced with their
  ``estleg:decisionDate``.
* Impact reports — rows from the Seadusloome ``impact_reports`` table
  whose payload references the input URI (PostgreSQL JSON path) plus
  the owning draft / version metadata. Ordered newest-first.
* Pending drafts — ``estleg:DraftLegislation`` /
  ``estleg:DraftingIntent`` rows in the ontology graph that ``amends``
  (forward-looking) the input. Read-only signal: "these draft versions
  would change this entity if enacted".

V2 (per the plan): per-``ProvisionVersion`` text diffs across the
``versionValidFrom`` / ``versionValidTo`` / ``supersededByVersion`` /
``versionText`` chain. Tracked at upstream ontology issue #208; the
banner in the route layer warns the user that v1 is act-level only.

All public helpers degrade gracefully — a dead Jena / missing DB row
yields an empty list, never a 500. The route layer renders missing
sub-sections as muted "ei leitud" rows via the result shell.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from app.ontology.queries import PREFIXES
from app.ontology.sparql_client import SparqlClient

logger = logging.getLogger(__name__)


# Result caps — keep the SPARQL responses scannable on the timeline UI.
# A real act (e.g. KarS) can have 500+ amendment events; the timeline
# truncates and signals "kuvame X esimest" in the section heading.
_MAX_AMENDMENTS = 200
_MAX_COURT_DECISIONS = 100
_MAX_PENDING_DRAFTS = 50
_MAX_IMPACT_REPORTS = 50


# ---------------------------------------------------------------------------
# Dataclasses — structured rows the route layer renders
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ActTimeline:
    """Act-level temporal envelope — the top of the result timeline.

    Every field is optional because the ontology populates them
    best-effort — a brand-new draft act has no ``repealDate``, an old
    consolidated act may carry no ``lastAmendmentDate`` literal.

    Attributes:
        act_uri: The Act *identifier* — either a URI (legacy/fixture
            path where ``estleg:sourceAct`` points at an act node) or
            a literal title string (prod path, where ``sourceAct`` is
            an ``xsd:string`` literal). The field name is kept for
            backward-compat; the route layer treats it as an opaque
            identifier and only the URI form is fed to
            :func:`app.explorer.urls.explorer_focus_url`. Empty for
            provision-only inputs whose parent act is not in the
            ontology (defensive).
        act_label: ``rdfs:label`` on the act, fallback to URI tail
            or — when ``act_uri`` is itself a literal title — the
            same string.
        entry_into_force: ``estleg:entryIntoForce`` parsed as ``date``.
        repeal_date: ``estleg:repealDate`` parsed as ``date``.
        last_amendment_date: ``estleg:lastAmendmentDate`` parsed as
            ``date`` — the most recent amendment that touched any
            member provision.
        temporal_status: ``estleg:temporalStatus`` literal (e.g.
            ``"in_force"`` / ``"repealed"`` / ``"pending"``).
    """

    act_uri: str = ""
    act_label: str = ""
    entry_into_force: date | None = None
    repeal_date: date | None = None
    last_amendment_date: date | None = None
    temporal_status: str = ""


@dataclass(frozen=True)
class AmendmentEventRow:
    """One ``estleg:AmendmentEvent`` row on the timeline.

    Attributes:
        event_uri: The AmendmentEvent URI.
        event_label: ``rdfs:label`` if present; otherwise URI tail.
        event_date: ``estleg:eventDate`` parsed as ``date`` — the date
            the amendment was *adopted* (passed by the Riigikogu).
        entry_into_force_date: ``estleg:entryIntoForceDate`` parsed as
            ``date`` — when the amendment actually started applying;
            often weeks/months after ``event_date``.
        rt_reference: ``estleg:rtReference`` literal — the Riigi
            Teataja citation (e.g. ``"RT I, 04.01.2019, 12"``). The UI
            renders it verbatim as a Tõendid cell.
        affected_provisions: List of ``(provision_uri, provision_label)``
            tuples — every ``estleg:amends`` target of the event,
            de-duplicated. Empty when the event has no ``amends`` edges
            (defensive — should not happen in practice).
    """

    event_uri: str = ""
    event_label: str = ""
    event_date: date | None = None
    entry_into_force_date: date | None = None
    rt_reference: str = ""
    affected_provisions: list[tuple[str, str]] = field(default_factory=list)


@dataclass(frozen=True)
class HistoryCourtDecisionRow:
    """One court decision that interpreted the input entity / act.

    Attributes:
        decision_uri: The ``estleg:CourtDecision`` /
            ``estleg:EUCourtDecision`` URI.
        decision_label: ``rdfs:label`` — typically the case number
            (e.g. ``"3-2-1-100-15"``).
        decision_date: ``estleg:decisionDate`` parsed as ``date``.
        interprets_uri: The provision / act URI the decision
            interprets — surfaced so the timeline can attribute the
            decision to a specific sibling provision.
        interprets_label: ``rdfs:label`` on the interpreted entity.
    """

    decision_uri: str = ""
    decision_label: str = ""
    decision_date: date | None = None
    interprets_uri: str = ""
    interprets_label: str = ""


@dataclass(frozen=True)
class ImpactReportRow:
    """One historical impact report that touched the input entity.

    Attributes:
        report_id: The ``impact_reports.id`` UUID (stringified).
        draft_id: The owning ``drafts.id`` UUID (stringified).
        draft_title: The draft's title — surfaced as the row label.
        version_number: ``draft_versions.version_number`` if present
            (post-migration 032), else ``None``.
        generated_at: ``impact_reports.generated_at`` as ``datetime``
            in UTC — the timeline uses ``date()`` for ordering.
    """

    report_id: str = ""
    draft_id: str = ""
    draft_title: str = ""
    version_number: int | None = None
    generated_at: datetime | None = None


@dataclass(frozen=True)
class PendingDraftRow:
    """One pending ``DraftLegislation`` / ``DraftingIntent`` that would amend the input.

    The forward-look section — answers "what is brewing that would
    change this entity?".

    Attributes:
        draft_uri: The Draft entity URI in the ontology graph.
        draft_label: ``rdfs:label`` if present, fallback to URI tail.
        draft_type: ``"DraftLegislation"`` or ``"DraftingIntent"``
            (the local name of the ``rdf:type`` we matched on).
        submitted_date: ``estleg:submittedDate`` parsed as ``date`` if
            present.
    """

    draft_uri: str = ""
    draft_label: str = ""
    draft_type: str = ""
    submitted_date: date | None = None


@dataclass(frozen=True)
class HistoryBundle:
    """The full A4 v1 result for one input entity.

    Bundles the five sub-result lists so the route layer can pass a
    single value into the result shell. Empty lists / None fields are
    rendered as muted "ei leitud" rows.

    Attributes:
        input_uri: The URI the workflow ran against — the route uses
            it for the "Sisend" card.
        input_type: ``"provision"`` / ``"act"`` / ``"court_decision"``
            — drives whether the banner is shown (provision ⇒ show
            the v1-limitation banner; act ⇒ hide it).
        act_timeline: The act-level envelope (always present, even if
            all fields are empty).
        amendments: All ``AmendmentEvent`` rows touching the input —
            newest first.
        court_decisions: All decisions interpreting the input — newest
            first.
        impact_reports: All historical impact reports — newest first.
        pending_drafts: All pending drafts that would amend the input
            — submitted_date desc, ``None`` last.
    """

    input_uri: str = ""
    input_type: str = ""
    act_timeline: ActTimeline = field(default_factory=ActTimeline)
    amendments: list[AmendmentEventRow] = field(default_factory=list)
    court_decisions: list[HistoryCourtDecisionRow] = field(default_factory=list)
    impact_reports: list[ImpactReportRow] = field(default_factory=list)
    pending_drafts: list[PendingDraftRow] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Estonian display labels — temporal status enum
# ---------------------------------------------------------------------------


TEMPORAL_STATUS_LABELS_ET: dict[str, str] = {
    "in_force": "Kehtib",
    "in_force_partial": "Kehtib osaliselt",
    "repealed": "Tunnistatud kehtetuks",
    "pending": "Jõustumata",
    "draft": "Eelnõu",
    "expired": "Aegunud",
}


def temporal_status_label(value: str) -> str:
    """Estonian display label for *value*, falling back to the raw string."""
    key = (value or "").strip()
    if not key:
        return "—"
    return TEMPORAL_STATUS_LABELS_ET.get(key, key)


# ---------------------------------------------------------------------------
# SPARQL templates
# ---------------------------------------------------------------------------
#
# Each template projects a stable column shape so the per-section
# parsers stay simple. All literal predicates are ``OPTIONAL`` because
# the corpus' completeness varies act-by-act.

_ACT_TIMELINE_QUERY = (
    PREFIXES
    + """
SELECT ?act ?actLabel ?entryIntoForce ?repealDate ?lastAmendmentDate ?temporalStatus
WHERE {
  ?act ?p ?o .
  OPTIONAL { ?act rdfs:label ?actLabel }
  OPTIONAL { ?act estleg:entryIntoForce ?entryIntoForce }
  OPTIONAL { ?act estleg:repealDate ?repealDate }
  OPTIONAL { ?act estleg:lastAmendmentDate ?lastAmendmentDate }
  OPTIONAL { ?act estleg:temporalStatus ?temporalStatus }
  FILTER(?p = rdf:type || ?p = rdfs:label)
}
LIMIT 1
"""
)

# Provision → owning act discovery. In prod ``estleg:sourceAct`` is a
# *literal* title (24,221 triples, all ``xsd:string``) and
# ``estleg:partOf`` carries zero rows — verified by the 2026-05-18
# ontology probe (see ``docs/2026-05-18-bugfix-plan.md`` Wave 2
# Step 1). The query therefore projects the literal title of the act;
# the legacy ``partOf`` arm was dropped because it was dead weight.
#
# The result column is named ``?actLabel`` even though in prod it
# equals the ``estleg:sourceAct`` literal directly — the route
# downstream consumes a "title string", regardless of whether that
# string came from ``rdfs:label`` of an act URI (fixture path) or
# ``estleg:sourceAct`` (prod path).
_PROVISION_OWNING_ACT_QUERY = (
    PREFIXES
    + """
SELECT DISTINCT ?actLabel
WHERE {
  ?provision estleg:sourceAct ?actLabel .
}
LIMIT 1
"""
)


# When the owning-act resolution falls back to a URI (e.g. fixture
# data where ``sourceAct`` points at an act node rather than a literal),
# we peek the act's ``rdfs:label`` so downstream sections can still
# join by a literal title. This is a backwards-compat shim — prod data
# never reaches it because prod's ``sourceAct`` is already a literal.
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

# AmendmentEvent rows — one row per (event, affected_provision) pair.
# The route layer aggregates by event URI. We surface every event whose
# ``amends`` target is either (a) the resolved input URI directly, or
# (b) a sibling provision that shares the input's owning-act *title
# literal* via ``estleg:sourceAct``.
#
# Prod data shape (2026-05-18 ontology probe): ``estleg:sourceAct`` is
# a literal title; ``estleg:partOf`` / ``estleg:partOfAct`` carry zero
# rows. The legacy UNION arms that joined through ``partOf`` or matched
# ``amends ?actUri`` against an act URI were dead in prod and have
# been removed. See ``docs/2026-05-18-bugfix-plan.md`` Wave 2 Step 5.
#
# Bindings:
# * ``?inputUri`` — the resolved entity URI (always set).
# * ``?actLit``   — the owning-act title literal (empty string when no
#                   owning-act could be resolved; the sibling-via-act
#                   arm then matches zero rows naturally).
_AMENDMENT_EVENTS_QUERY = (
    PREFIXES
    + """
SELECT DISTINCT ?event ?eventLabel ?eventDate ?entryIntoForceDate ?rtReference
                ?affectedProvision ?affectedLabel
WHERE {
  ?event a estleg:AmendmentEvent .
  ?event estleg:amends ?affectedProvision .
  {
    ?event estleg:amends ?inputUri .
  } UNION {
    ?affectedProvision estleg:sourceAct ?actLit .
  }
  OPTIONAL { ?event rdfs:label ?eventLabel }
  OPTIONAL { ?event estleg:eventDate ?eventDate }
  OPTIONAL { ?event estleg:entryIntoForceDate ?entryIntoForceDate }
  OPTIONAL { ?event estleg:rtReference ?rtReference }
  OPTIONAL { ?affectedProvision rdfs:label ?affectedLabel }
}
ORDER BY DESC(?eventDate)
LIMIT """
    + str(_MAX_AMENDMENTS * 10)  # 10 affected per event budget
    + "\n"
)

# Court decisions interpreting the input directly OR any sibling
# provision of the input act. ``EUCourtDecision`` extends
# ``CourtDecision`` in SHACL; querying the bare ``interpretsLaw``
# predicate catches both.
#
# Same prod-shape rewrite as the amendment-events query above: the
# legacy arms that interpreted act URIs (``?decision interpretsLaw
# ?actUri`` and ``?interpretsUri partOf ?actUri``) were dead in prod
# and have been replaced with the literal-title join via
# ``estleg:sourceAct``.
_COURT_DECISIONS_QUERY = (
    PREFIXES
    + """
SELECT DISTINCT ?decision ?decisionLabel ?decisionDate
                ?interpretsUri ?interpretsLabel
WHERE {
  ?decision estleg:interpretsLaw ?interpretsUri .
  {
    ?decision estleg:interpretsLaw ?inputUri .
  } UNION {
    ?interpretsUri estleg:sourceAct ?actLit .
  }
  OPTIONAL { ?decision rdfs:label ?decisionLabel }
  OPTIONAL { ?decision estleg:decisionDate ?decisionDate }
  OPTIONAL { ?interpretsUri rdfs:label ?interpretsLabel }
}
ORDER BY DESC(?decisionDate)
LIMIT """
    + str(_MAX_COURT_DECISIONS)
    + "\n"
)

# Pending drafts — DraftLegislation OR DraftingIntent that ``amends``
# the input directly OR a sibling provision sharing the input's
# owning-act title literal. ``submittedDate`` is OPTIONAL (older
# fixtures don't carry it); the Python sort treats ``None`` as oldest.
#
# Prod-shape rewrite (2026-05-18 plan, Wave 2 Step 5): the legacy
# ``?draft estleg:amends ?actUri`` arm was joined against act URIs
# that don't exist atomically in prod; replaced with the sibling
# lookup ``?affected estleg:sourceAct ?actLit``.
_PENDING_DRAFTS_QUERY = (
    PREFIXES
    + """
SELECT DISTINCT ?draft ?draftLabel ?draftType ?submittedDate
WHERE {
  {
    ?draft a estleg:DraftLegislation .
    BIND("DraftLegislation" AS ?draftType)
  } UNION {
    ?draft a estleg:DraftingIntent .
    BIND("DraftingIntent" AS ?draftType)
  }
  {
    ?draft estleg:amends ?inputUri .
  } UNION {
    ?draft estleg:amends ?affected .
    ?affected estleg:sourceAct ?actLit .
  }
  OPTIONAL { ?draft rdfs:label ?draftLabel }
  OPTIONAL { ?draft estleg:submittedDate ?submittedDate }
}
ORDER BY DESC(?submittedDate)
LIMIT """
    + str(_MAX_PENDING_DRAFTS)
    + "\n"
)


# ---------------------------------------------------------------------------
# Helpers — date / literal parsing
# ---------------------------------------------------------------------------


def _parse_date(raw: Any) -> date | None:
    """Parse an ``xsd:date`` / ``xsd:dateTime`` literal into a Python ``date``.

    Returns ``None`` for missing / malformed input. Strips a timezone
    designator tail (``Z`` / ``+02:00``) and a time portion before
    parsing — the timeline doesn't care about times.
    """
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    # Cut a trailing TZ / time designator.
    if len(text) > 10 and text[10] in {"Z", "+", "-", "T"}:
        text = text[:10]
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        logger.debug("Could not parse date literal %r", raw)
        return None


def _as_str(value: Any) -> str:
    """Coerce a SPARQL JSON value to ``str`` (empty for ``None``)."""
    if value is None:
        return ""
    return str(value).strip()


def _uri_tail(uri: str) -> str:
    """Return the URI's local-name tail — the bit after ``#`` or ``/``."""
    if not uri:
        return ""
    return uri.rsplit("#", 1)[-1].rsplit("/", 1)[-1]


# ---------------------------------------------------------------------------
# Public API — resolve owning act + per-section helpers
# ---------------------------------------------------------------------------


def resolve_owning_act(
    provision_uri: str,
    *,
    sparql_client: SparqlClient | None = None,
) -> str:
    """Return the owning act identifier for ``provision_uri``, or ``""``.

    Walks ``estleg:sourceAct``. In prod the value of ``sourceAct`` is a
    *literal title* (e.g. ``"Avaliku teabe seadus"``); the function
    therefore returns a string that may be either a literal title
    (prod path) or, when an older fixture serialises ``sourceAct`` as
    a URI pointing at an act node, an act URI (legacy path —
    downstream helpers reverse-lookup the URI's ``rdfs:label`` to
    bring it back to the literal form).

    Empty input or a provision with no owning-act edge yields ``""``
    — the caller treats that as "no act-level timeline available".

    The legacy ``estleg:partOf`` UNION arm was dropped: the 2026-05-18
    prod-Jena probe found zero ``partOf`` / ``partOfAct`` triples.
    """
    uri = (provision_uri or "").strip()
    if not uri:
        return ""

    client = sparql_client if sparql_client is not None else SparqlClient()
    try:
        rows = client.query(
            _PROVISION_OWNING_ACT_QUERY,
            uri_bindings={"provision": uri},
        )
    except Exception:
        logger.warning("resolve_owning_act: SPARQL query failed for %r", uri, exc_info=True)
        return ""

    for row in rows or []:
        # The new query projects ``?actLabel`` (the literal title in
        # prod, or the URI of a fixture act node). Older test stubs
        # still set ``?act``; check both for back-compat.
        candidate = _as_str(row.get("actLabel")) or _as_str(row.get("act"))
        if candidate:
            return candidate
    return ""


def _act_literal_for(identifier: str, *, client: SparqlClient) -> str:
    """Coerce *identifier* to a literal act-title string.

    Pass-through when *identifier* is not a URI. When it's a URI
    (legacy fixture path), reverse-lookup ``rdfs:label`` and return
    that. Returns ``""`` for empty input or a URI without a label.
    """
    text = (identifier or "").strip()
    if not text:
        return ""
    if not (text.startswith("http://") or text.startswith("https://")):
        return text
    try:
        rows = client.query(
            _ACT_LABEL_FOR_URI_QUERY,
            uri_bindings={"act": text},
        )
    except Exception:
        logger.warning("_act_literal_for: rdfs:label lookup failed for %r", text, exc_info=True)
        return ""
    for row in rows or []:
        label = _as_str(row.get("label"))
        if label:
            return label
    return ""


def get_act_timeline(
    act_uri: str,
    *,
    sparql_client: SparqlClient | None = None,
) -> ActTimeline:
    """Return the act-level timeline envelope for *act_uri*.

    Empty input yields an empty :class:`ActTimeline`. SPARQL errors
    degrade to an empty timeline so the route renders a muted "andmed
    puuduvad" row rather than a 500.

    The ``act_uri`` parameter keeps its historical name but accepts
    either a URI (fixture / legacy path — joined via ``?act ?p ?o``)
    or an act title literal (prod path — used only as the
    ``act_label`` on the returned envelope, since prod has no atomic
    Act URI nodes carrying ``entryIntoForce`` / ``repealDate`` /
    etc.). The graceful-degradation contract: in prod the envelope
    returns with just the title populated and the date fields
    ``None``; the route renders that as a muted timeline section
    rather than a 500.
    """
    identifier = (act_uri or "").strip()
    if not identifier:
        return ActTimeline()

    is_uri = identifier.startswith("http://") or identifier.startswith("https://")

    # Literal-title path (prod): we have nothing to SELECT against in
    # the deployed corpus, so return an envelope with the title only.
    # Date predicates aren't carried by any atomic Act URI in prod
    # (the only ``estleg:Law`` instances are topic-map clusters per
    # the 2026-05-18 probe).
    if not is_uri:
        return ActTimeline(act_uri=identifier, act_label=identifier)

    client = sparql_client if sparql_client is not None else SparqlClient()
    try:
        rows = client.query(
            _ACT_TIMELINE_QUERY,
            uri_bindings={"act": identifier},
        )
    except Exception:
        logger.warning("get_act_timeline: SPARQL query failed for %r", identifier, exc_info=True)
        return ActTimeline(act_uri=identifier)

    if not rows:
        return ActTimeline(act_uri=identifier)

    row = rows[0]
    label = _as_str(row.get("actLabel")) or _uri_tail(identifier)
    return ActTimeline(
        act_uri=identifier,
        act_label=label,
        entry_into_force=_parse_date(row.get("entryIntoForce")),
        repeal_date=_parse_date(row.get("repealDate")),
        last_amendment_date=_parse_date(row.get("lastAmendmentDate")),
        temporal_status=_as_str(row.get("temporalStatus")),
    )


def list_amendment_events(
    input_uri: str,
    act_uri: str,
    *,
    sparql_client: SparqlClient | None = None,
) -> list[AmendmentEventRow]:
    """Return every AmendmentEvent touching the input or its owning act.

    Aggregates raw (event, affected_provision) rows into one row per
    AmendmentEvent. The ``act_uri`` parameter keeps its historical
    name but now holds the **owning-act title literal** in prod
    (legacy URI form is auto-resolved to the literal via
    :func:`_act_literal_for`). Empty inputs short-circuit.

    Binding contract: ``?inputUri`` is the provision/act entity URI,
    and ``?actLit`` is the title literal — the prod data joins through
    ``estleg:sourceAct`` literals, not ``estleg:partOf`` URIs (zero
    rows of that predicate in prod, see ``docs/2026-05-18-bugfix-plan.md``
    Wave 2 Step 1).
    """
    input_clean = (input_uri or "").strip()
    if not input_clean:
        return []

    client = sparql_client if sparql_client is not None else SparqlClient()
    act_literal = _act_literal_for(act_uri, client=client)

    try:
        rows = client.query(
            _AMENDMENT_EVENTS_QUERY,
            uri_bindings={"inputUri": input_clean},
            bindings={"actLit": act_literal},
        )
    except Exception:
        logger.warning(
            "list_amendment_events: SPARQL query failed for input=%r act=%r",
            input_clean,
            act_uri,
            exc_info=True,
        )
        return []

    return _aggregate_amendment_rows(rows)


def list_court_decisions(
    input_uri: str,
    act_uri: str,
    *,
    sparql_client: SparqlClient | None = None,
) -> list[HistoryCourtDecisionRow]:
    """Return court decisions interpreting the input or any sibling provision.

    Same prod-shape binding contract as :func:`list_amendment_events`:
    ``?inputUri`` is the entity URI and ``?actLit`` is the
    owning-act title literal (legacy URI form is auto-coerced via
    :func:`_act_literal_for`). The literal-title join through
    ``estleg:sourceAct`` is the only sibling-discovery path that
    carries rows in prod.

    Sorted newest-first via the SPARQL ``ORDER BY DESC(?decisionDate)``;
    rows with no date sink to the end after the Python tie-break sort.
    """
    input_clean = (input_uri or "").strip()
    if not input_clean:
        return []

    client = sparql_client if sparql_client is not None else SparqlClient()
    act_literal = _act_literal_for(act_uri, client=client)

    try:
        rows = client.query(
            _COURT_DECISIONS_QUERY,
            uri_bindings={"inputUri": input_clean},
            bindings={"actLit": act_literal},
        )
    except Exception:
        logger.warning(
            "list_court_decisions: SPARQL query failed for input=%r act=%r",
            input_clean,
            act_uri,
            exc_info=True,
        )
        return []

    out: list[HistoryCourtDecisionRow] = []
    seen: set[str] = set()
    for row in rows or []:
        decision_uri = _as_str(row.get("decision"))
        if not decision_uri or decision_uri in seen:
            continue
        seen.add(decision_uri)
        out.append(
            HistoryCourtDecisionRow(
                decision_uri=decision_uri,
                decision_label=_as_str(row.get("decisionLabel")) or _uri_tail(decision_uri),
                decision_date=_parse_date(row.get("decisionDate")),
                interprets_uri=_as_str(row.get("interpretsUri")),
                interprets_label=_as_str(row.get("interpretsLabel")),
            )
        )

    # Defensive sort: SPARQL ORDER BY DESC with NULL dates is engine-
    # specific; force a deterministic Python sort with None last.
    out.sort(key=lambda r: r.decision_date or date.min, reverse=True)
    return out[:_MAX_COURT_DECISIONS]


def list_pending_drafts(
    input_uri: str,
    act_uri: str,
    *,
    sparql_client: SparqlClient | None = None,
) -> list[PendingDraftRow]:
    """Return pending DraftLegislation / DraftingIntent rows targeting the input.

    Forward-look section. Same prod-shape binding contract as
    :func:`list_amendment_events` — ``?inputUri`` is the entity URI
    and ``?actLit`` is the owning-act title literal. Empty input ⇒
    ``[]``.
    """
    input_clean = (input_uri or "").strip()
    if not input_clean:
        return []

    client = sparql_client if sparql_client is not None else SparqlClient()
    act_literal = _act_literal_for(act_uri, client=client)

    try:
        rows = client.query(
            _PENDING_DRAFTS_QUERY,
            uri_bindings={"inputUri": input_clean},
            bindings={"actLit": act_literal},
        )
    except Exception:
        logger.warning(
            "list_pending_drafts: SPARQL query failed for input=%r act=%r",
            input_clean,
            act_uri,
            exc_info=True,
        )
        return []

    out: list[PendingDraftRow] = []
    seen: set[str] = set()
    for row in rows or []:
        draft_uri = _as_str(row.get("draft"))
        if not draft_uri or draft_uri in seen:
            continue
        seen.add(draft_uri)
        out.append(
            PendingDraftRow(
                draft_uri=draft_uri,
                draft_label=_as_str(row.get("draftLabel")) or _uri_tail(draft_uri),
                draft_type=_as_str(row.get("draftType")),
                submitted_date=_parse_date(row.get("submittedDate")),
            )
        )

    out.sort(key=lambda r: r.submitted_date or date.min, reverse=True)
    return out[:_MAX_PENDING_DRAFTS]


def list_impact_reports(
    input_uri: str,
    *,
    db_connection: Any | None = None,
) -> list[ImpactReportRow]:
    """Return historical ``impact_reports`` rows touching *input_uri*, newest-first.

    The intent is "did any analysis report mention this entity". The
    ``report_data`` JSONB stores entity URIs in a small, stable set of
    array-of-object fields (``affected_entities[].uri``,
    ``conflicts[].conflicting_entity``, ``eu_compliance[].eu_act`` /
    ``estonian_provision``, ``gaps[].topic_cluster``, and the v2
    ``sanctions_delta.rows[].provision_uri`` /
    ``burden_delta.rows[].provision_uri``). We match with the JSONB
    containment operator ``@>`` against those paths, which the GIN
    index ``idx_impact_reports_report_data`` (migration 042) accelerates
    — replacing the previous ``report_data::text ILIKE %uri%``
    full-table scan. Containment also matches on a *URI field* rather
    than a coincidental substring of some label literal, so it is both
    faster and more precise.

    Every value is passed as a bound parameter (psycopg adapts the dict
    to ``jsonb``), so there are no ILIKE wildcards to escape and no
    injection surface — the previous ``ILIKE %uri%`` wildcard-escaping
    hazard is removed entirely by dropping the text-cast scan.

    Args:
        input_uri: The entity URI to search for. Empty input ⇒ ``[]``.
        db_connection: Optional psycopg connection override. Tests
            inject a MagicMock connection; production uses the lazy
            :func:`app.db.get_connection` import.

    Returns:
        A list of :class:`ImpactReportRow`, newest-first. ``[]`` on
        any DB error or empty input.
    """
    uri = (input_uri or "").strip()
    if not uri:
        return []

    # One containment probe per URI-bearing path. Postgres BitmapOrs the
    # per-branch GIN index scans, so the whole predicate stays indexed.
    # ``Jsonb`` wraps each value so psycopg sends a typed ``jsonb``
    # parameter (no ``::text`` cast, no string interpolation).
    from psycopg.types.json import Jsonb

    containment_params = [
        Jsonb({"affected_entities": [{"uri": uri}]}),
        Jsonb({"conflicts": [{"conflicting_entity": uri}]}),
        Jsonb({"eu_compliance": [{"eu_act": uri}]}),
        Jsonb({"eu_compliance": [{"estonian_provision": uri}]}),
        Jsonb({"gaps": [{"topic_cluster": uri}]}),
        Jsonb({"sanctions_delta": {"rows": [{"provision_uri": uri}]}}),
        Jsonb({"burden_delta": {"rows": [{"provision_uri": uri}]}}),
    ]
    containment_clause = " OR ".join("ir.report_data @> %s" for _ in containment_params)

    sql = f"""
        SELECT ir.id, ir.draft_id, d.title, ir.generated_at,
               dv.version_number
        FROM impact_reports ir
        JOIN drafts d ON d.id = ir.draft_id
        LEFT JOIN draft_versions dv ON dv.id = ir.draft_version_id
        WHERE {containment_clause}
        ORDER BY ir.generated_at DESC NULLS LAST
        LIMIT %s
    """
    params = (*containment_params, _MAX_IMPACT_REPORTS)

    try:
        if db_connection is not None:
            with db_connection.cursor() as cur:
                cur.execute(sql, params)
                rows = cur.fetchall()
        else:
            from app.db import get_connection

            with get_connection() as conn, conn.cursor() as cur:
                cur.execute(sql, params)
                rows = cur.fetchall()
    except Exception:
        logger.warning("list_impact_reports: query failed for %r", uri, exc_info=True)
        return []

    out: list[ImpactReportRow] = []
    for row in rows or []:
        # psycopg's default cursor returns tuples; treat as positional.
        report_id, draft_id, title, generated_at, version_number = (
            row[0],
            row[1],
            row[2],
            row[3],
            row[4] if len(row) > 4 else None,
        )
        gen_at: datetime | None
        if isinstance(generated_at, datetime):
            gen_at = generated_at
        else:
            gen_at = None
        out.append(
            ImpactReportRow(
                report_id=str(report_id or ""),
                draft_id=str(draft_id or ""),
                draft_title=str(title or "").strip(),
                version_number=int(version_number) if version_number is not None else None,
                generated_at=gen_at,
            )
        )
    return out


def get_history_bundle(
    input_uri: str,
    *,
    input_type: str,
    sparql_client: SparqlClient | None = None,
    db_connection: Any | None = None,
) -> HistoryBundle:
    """Aggregate every A4 v1 section into one :class:`HistoryBundle`.

    The route layer calls this once and renders the returned bundle
    through the result shell. Each per-section helper is independently
    fault-tolerant — a single section that fails to load yields an
    empty list, not a 500.

    Args:
        input_uri: The resolved entity URI.
        input_type: ``"provision"`` / ``"act"`` / ``"court_decision"``
            — drives owning-act resolution. A provision walks
            ``estleg:sourceAct`` to find the act title literal (the
            legacy ``estleg:partOf`` arm was dropped because that
            predicate carries zero rows in prod, per the 2026-05-18
            ontology probe); an act-typed input uses itself; a
            court decision uses itself (we still run the queries —
            they'll surface decisions citing this decision via no
            edge, so the section is typically empty).
        sparql_client: Optional override for the SPARQL helpers.
        db_connection: Optional override for the impact-reports query.

    Returns:
        A populated :class:`HistoryBundle`.
    """
    uri = (input_uri or "").strip()
    if not uri:
        return HistoryBundle(input_type=input_type or "")

    if input_type == "provision":
        act_identifier = resolve_owning_act(uri, sparql_client=sparql_client)
    else:
        # Act / court_decision / anything else: treat the input as
        # the act itself for the timeline + sibling queries.
        act_identifier = uri

    # Build the act-level timeline first. For URI-shaped identifiers
    # ``get_act_timeline`` resolves the URI's ``rdfs:label`` once and
    # we can reuse the resulting literal across the three downstream
    # section helpers without each re-issuing the same lookup.
    act_timeline = (
        get_act_timeline(act_identifier, sparql_client=sparql_client)
        if act_identifier
        else ActTimeline()
    )
    # Resolve the act-for-sections string ONCE so the per-section
    # helpers don't re-fire ``rdfs:label`` lookups:
    # * Timeline returned a literal label → use it directly.
    # * Timeline returned nothing AND the identifier is a literal
    #   (prod path) → use the literal identifier directly.
    # * Timeline returned nothing AND the identifier is a URI without
    #   a label → fall through with the empty string so the
    #   sibling-via-``sourceAct`` UNION arm naturally matches zero
    #   rows in each helper.
    if act_timeline.act_label:
        act_for_sections = act_timeline.act_label
    elif act_identifier and not (
        act_identifier.startswith("http://") or act_identifier.startswith("https://")
    ):
        act_for_sections = act_identifier
    else:
        act_for_sections = ""

    amendments = list_amendment_events(uri, act_for_sections, sparql_client=sparql_client)
    court_decisions = list_court_decisions(uri, act_for_sections, sparql_client=sparql_client)
    impact_reports = list_impact_reports(uri, db_connection=db_connection)
    pending_drafts = list_pending_drafts(uri, act_for_sections, sparql_client=sparql_client)

    return HistoryBundle(
        input_uri=uri,
        input_type=input_type or "",
        act_timeline=act_timeline,
        amendments=amendments,
        court_decisions=court_decisions,
        impact_reports=impact_reports,
        pending_drafts=pending_drafts,
    )


# ---------------------------------------------------------------------------
# Internal — amendment aggregation
# ---------------------------------------------------------------------------


def _aggregate_amendment_rows(
    rows: list[dict[str, str]] | None,
) -> list[AmendmentEventRow]:
    """Group raw (event, affected_provision) rows into per-event entries.

    The SPARQL query returns one row per (event × affected provision)
    pair. We bucket by event URI:

    * First row's date / RT-ref / label win (they're stable per event);
    * ``affected_provisions`` is the deduplicated list of (uri, label)
      tuples across every raw row for that event.

    Returns the events newest-first, capped at :data:`_MAX_AMENDMENTS`.
    """
    by_event: dict[str, dict[str, Any]] = {}
    order: list[str] = []  # preserve SPARQL order for events with no date

    for row in rows or []:
        event_uri = _as_str(row.get("event"))
        if not event_uri:
            continue

        bucket = by_event.get(event_uri)
        if bucket is None:
            bucket = {
                "event_uri": event_uri,
                "event_label": _as_str(row.get("eventLabel")) or _uri_tail(event_uri),
                "event_date": _parse_date(row.get("eventDate")),
                "entry_into_force_date": _parse_date(row.get("entryIntoForceDate")),
                "rt_reference": _as_str(row.get("rtReference")),
                "_provisions": {},  # uri → label (preserves insertion order)
            }
            by_event[event_uri] = bucket
            order.append(event_uri)
        # Keep the first non-empty literal we see for stable fields.
        if not bucket["event_label"]:
            bucket["event_label"] = _as_str(row.get("eventLabel")) or _uri_tail(event_uri)
        if bucket["event_date"] is None:
            bucket["event_date"] = _parse_date(row.get("eventDate"))
        if bucket["entry_into_force_date"] is None:
            bucket["entry_into_force_date"] = _parse_date(row.get("entryIntoForceDate"))
        if not bucket["rt_reference"]:
            bucket["rt_reference"] = _as_str(row.get("rtReference"))

        prov_uri = _as_str(row.get("affectedProvision"))
        if prov_uri:
            prov_label = _as_str(row.get("affectedLabel")) or _uri_tail(prov_uri)
            # ``setdefault`` preserves the first label we saw.
            bucket["_provisions"].setdefault(prov_uri, prov_label)

    out: list[AmendmentEventRow] = []
    for event_uri in order:
        b = by_event[event_uri]
        out.append(
            AmendmentEventRow(
                event_uri=b["event_uri"],
                event_label=b["event_label"],
                event_date=b["event_date"],
                entry_into_force_date=b["entry_into_force_date"],
                rt_reference=b["rt_reference"],
                affected_provisions=[(u, lbl) for u, lbl in b["_provisions"].items()],
            )
        )

    # Deterministic newest-first sort; ``None`` event_date sinks last.
    out.sort(key=lambda r: r.event_date or date.min, reverse=True)
    return out[:_MAX_AMENDMENTS]


# ``json`` is imported above because future iterations may parse the
# report_data JSONB column for structured fields instead of the current
# ILIKE scan — keep the import live to signal intent.
_ = json
