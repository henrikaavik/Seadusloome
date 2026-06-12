"""EU transposition deadline surveillance for the Töölaud (A6).

Task A6 from ``docs/2026-05-15-ontology-six-use-cases-plan.md`` (section 5,
Direction A — "Töölaud widget: EU transposition deadlines").

This module is the SPARQL layer behind the Töölaud "EL ülevõtu tähtajad"
widget: a *proactive* surveillance signal that surfaces EU directives whose
transposition deadline is approaching (or has already passed) **and** for
which Estonia's transposition status is not "fully transposed". The widget
turns use case 5 (EU traceability) from a reactive workflow ("open EL
ülevõtt when I remember to check") into an operational one ("see at a
glance on login what's about to expire").

Predicates used (verified populated by the 2026-05-15 audit — section 2.5
of the plan, row A6):

* ``estleg:transpositionDeadline`` — ``xsd:date`` literal on an
  ``EULegislation`` directive (populated on 2,600+ directives).
* ``estleg:transposesDirective``   — Estonian ``Act`` → ``EULegislation``
  (canonical direction).
* ``estleg:transposedBy``          — ``EULegislation`` → Estonian ``Act``
  (the inverse; the query ``UNION``-s both directions, mirroring
  :mod:`app.impact.eu_transposition`).
* ``estleg:transpositionStatus``   — literal on the transposing Estonian
  act (raw values like ``"complete"`` / ``"partial"`` / ``"osaline"`` /
  ``"pending"`` — normalised via
  :func:`app.impact.eu_transposition.normalise_transposition_status`).

The query filters directives where:

* ``transpositionDeadline < (today + horizon_days)`` — i.e. the deadline
  is *already passed* or falls within the configured horizon window.
* status bucket is one of ``puudub`` / ``osaline`` / ``ebaselge`` — i.e.
  **not** ``kaetud`` ("fully transposed"). A directive with no transposing
  Estonian act at all (``transposedBy`` / ``transposesDirective`` absent)
  has bucket ``puudub`` and is surfaced.

Rows are ordered by deadline ascending so the most urgent items float to
the top of the widget.

A dead Jena (or any SPARQL error) ⇒ ``[]`` — the widget then hides itself
(empty rows ⇒ no render) and the dashboard degrades gracefully rather
than 500-ing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from app.impact.eu_transposition import (
    TranspositionStatus,
    normalise_transposition_status,
)
from app.impact.queries import PREFIXES
from app.ontology.sparql_client import SparqlClient

logger = logging.getLogger(__name__)


# How far ahead in days the Töölaud widget looks for upcoming transposition
# deadlines. 90 days = roughly one quarter — long enough to give a ministry
# lawyer lead time, short enough to keep the list operationally focused.
# Configurable per-call via the ``horizon_days`` parameter; this constant is
# the default the widget uses.
DEFAULT_TRANSPOSITION_HORIZON_DAYS = 90

# Cap the row count from Jena. The widget renders only the top 5 anyway
# (the "Näita kõiki" link is for the future expanded view); going above
# ~50 rows would also push the response payload past what feels reasonable
# for a dashboard sub-query.
_DEADLINES_QUERY_LIMIT = 50


# The four status buckets are the same as :mod:`app.impact.eu_transposition`.
# We surface only the three that mean "Estonia has not finished":
_INCOMPLETE_STATUSES: frozenset[TranspositionStatus] = frozenset({"puudub", "osaline", "ebaselge"})


@dataclass(frozen=True)
class TranspositionDeadlineRow:
    """One row of the "EL ülevõtu tähtajad" widget.

    Attributes:
        celex: The directive's CELEX number (e.g. ``"32022L2555"``). When
            the ontology row has no ``celexNumber`` literal, this falls
            back to a short tail of the URI so the widget always has *some*
            identifier to show.
        directive_label_et: Estonian-language label for the directive
            (``rdfs:label``). When absent, falls back to the URI tail so
            the cell never renders blank.
        deadline: The ``transpositionDeadline`` parsed as a ``date``.
        days_remaining: Signed integer — **positive** when the deadline is
            in the future, **zero** on the deadline day itself, and
            **negative** when the deadline has already passed (e.g. ``-12``
            means twelve days overdue).
        status: The normalised transposition status bucket — one of
            ``"puudub"`` / ``"osaline"`` / ``"ebaselge"``. (``"kaetud"`` is
            never present because the query filters it out.)
        transposing_acts: List of URIs of the Estonian acts that already
            transpose this directive (possibly empty for the ``"puudub"``
            case). The widget uses the *count* of this list, not the URIs
            themselves; the URIs are kept on the dataclass so a future
            "show transposing acts inline" expansion has the data without
            another query.
    """

    celex: str
    directive_label_et: str
    deadline: date
    days_remaining: int
    status: TranspositionStatus
    transposing_acts: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# SPARQL query
# ---------------------------------------------------------------------------
#
# The deadline cut-off is a server-side ``(today + horizon_days)`` ``xsd:date``
# literal — bound into the query at build time, not parameterised via VALUES,
# because (a) it's not user input (it's a server clock value derived from a
# whitelisted integer) and (b) ``FILTER(?d < ?cutoff)`` with a VALUES-bound
# ``?cutoff`` ``xsd:date`` works less reliably across Jena versions than a
# straight literal.
#
# We do NOT filter by transpositionStatus in SPARQL — we project the raw
# literal and apply :func:`normalise_transposition_status` in Python, then
# drop rows whose bucket is ``"kaetud"``. That keeps the status-bucket logic
# in exactly one place (the :mod:`app.impact.eu_transposition`
# module that already owns the vocabulary).


#: Server-side floor for deadline literals. The ontology carries ~50 sentinel
#: rows with implausibly old dates (e.g. ``"1001-01-01"``) that swallow the
#: query LIMIT before any real overdue row surfaces. ``1980-01-01`` is a
#: pragmatic floor — pre-dates the EEA agreement (1994) and the entire
#: corpus of meaningful Estonian transposition data, while admitting every
#: real directive in the current ingest. See bug #800.
_DEADLINE_FLOOR_LITERAL = "1980-01-01"


def _build_deadlines_query(cutoff: date) -> str:
    """Return the directives-with-deadline SPARQL query.

    The cutoff date is injected as an ``xsd:date`` literal — never as a
    user-supplied string. ``cutoff`` is computed by
    :func:`list_overdue_or_upcoming_transpositions` from
    ``date.today() + timedelta(days=horizon_days)`` where ``horizon_days``
    is server-controlled.

    The query joins each ``EULegislation`` directive with its deadline
    (mandatory — no deadline means it's not in scope), its label and CELEX
    (both optional), and any Estonian transposing acts via *both* edge
    directions (``UNION`` of ``transposesDirective`` / ``transposedBy``).
    ``transpositionStatus`` is ``OPTIONAL`` and projected raw — the caller
    normalises and filters.

    The WHERE clause also (a) floors the deadline at
    :data:`_DEADLINE_FLOOR_LITERAL` to drop the ~50 sentinel pre-1980 rows
    in the corpus that would otherwise eat the ``LIMIT`` (bug #800), and
    (b) scopes to ``estleg:inForce true`` directives so repealed acts
    don't surface as live transposition debt.
    """
    cutoff_literal = cutoff.isoformat()
    body = f"""
SELECT DISTINCT ?euAct ?euLabel ?celex ?deadline ?eeAct ?status
WHERE {{
  ?euAct a estleg:EULegislation .
  ?euAct estleg:inForce true .
  ?euAct estleg:transpositionDeadline ?deadline .
  OPTIONAL {{ ?euAct rdfs:label ?euLabel }}
  OPTIONAL {{ ?euAct estleg:celexNumber ?celex }}
  OPTIONAL {{
    {{
      ?eeAct estleg:transposesDirective ?euAct .
    }} UNION {{
      ?euAct estleg:transposedBy ?eeAct .
    }}
    OPTIONAL {{ ?eeAct estleg:transpositionStatus ?status }}
  }}
  FILTER(?deadline >= "{_DEADLINE_FLOOR_LITERAL}"^^xsd:date)
  FILTER(?deadline < "{cutoff_literal}"^^xsd:date)
}}
ORDER BY ASC(?deadline)
LIMIT {_DEADLINES_QUERY_LIMIT}
"""
    return PREFIXES + body


# ---------------------------------------------------------------------------
# Row aggregation
# ---------------------------------------------------------------------------


#: Python-side year floor — defence-in-depth against any sentinel literal
#: that slips past the server-side
#: ``FILTER(?deadline >= "1980-01-01"^^xsd:date)``. Kept in sync with
#: :data:`_DEADLINE_FLOOR_LITERAL` deliberately. See bug #800.
_DEADLINE_FLOOR_YEAR = 1980


def _parse_deadline(raw: str | None) -> date | None:
    """Parse an ``xsd:date`` literal into a Python ``date``.

    Returns ``None`` when the input is missing, malformed, or carries an
    implausibly old year (anything before :data:`_DEADLINE_FLOOR_YEAR` is
    treated as a sentinel and rejected — see bug #800: the ontology has
    ~50 directives whose deadline literal is ``"1001-01-01"`` and that
    sentinel produced widget rows like ``"01.01.1001 · 374511 p möödunud"``
    when accepted at face value).

    ``xsd:date`` literals come back from Jena as either ``"YYYY-MM-DD"``
    or, less commonly, ``"YYYY-MM-DDZ"`` / with a timezone suffix — we
    strip the timezone tail before parsing. A row with an unparseable or
    sentinel deadline is skipped by the caller (logged at debug — not a
    500-worthy event).
    """
    if not raw:
        return None
    text = str(raw).strip()
    if not text:
        return None
    # Drop a trailing timezone designator if present ("2024-12-31Z" or
    # "2024-12-31+02:00"). Date types ignore TZ anyway.
    if len(text) > 10 and text[10] in {"Z", "+", "-", "T"}:
        text = text[:10]
    try:
        parsed = datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        logger.debug("Could not parse transposition deadline literal %r", raw)
        return None
    if parsed.year < _DEADLINE_FLOOR_YEAR:
        # Sentinel pre-1980 date — the SPARQL server-side filter should
        # already have dropped it, but treat as unparseable here so the
        # widget never renders a year-1001-style row even if the data
        # shifts in the future.
        logger.debug(
            "Rejecting pre-%d sentinel transposition deadline literal %r",
            _DEADLINE_FLOOR_YEAR,
            raw,
        )
        return None
    return parsed


def _celex_fallback(uri: str, raw_celex: str | None) -> str:
    """Return the best CELEX-like identifier for the widget cell.

    Prefer the explicit ``estleg:celexNumber`` literal. When absent, the
    fragment tail of the URI is a reasonable second-best (``…#EU-32016R0679``
    → ``"32016R0679"``); when neither is available, return the bare URI so
    the cell is never literally empty.
    """
    if raw_celex and raw_celex.strip():
        return raw_celex.strip()
    if not uri:
        return ""
    tail = uri.rsplit("#", 1)[-1].rsplit("/", 1)[-1]
    # Strip a leading ``EU-`` prefix that the ontology sometimes uses on
    # the URI fragment (``…#EU-32016R0679``).
    if tail.startswith("EU-"):
        tail = tail[3:]
    return tail or uri


def _label_fallback(label: str | None, celex: str) -> str:
    """Return the best human-readable label for the widget cell.

    Prefer ``rdfs:label``. When absent, fall back to the CELEX (which is
    already non-empty by construction). The widget calls
    :func:`_celex_fallback` first and threads the result through here.
    """
    if label and label.strip():
        return label.strip()
    return celex


def _aggregate_rows(
    raw_rows: list[dict[str, Any]],
    *,
    today: date,
) -> list[TranspositionDeadlineRow]:
    """Aggregate raw SPARQL rows (one per (directive, transposing-act) pair)
    into one row per directive.

    The SPARQL query ``UNION``-s the two transposition edge directions and
    ``OPTIONAL``-joins on the transposing act, so a single directive
    typically appears as several raw rows — one per Estonian act that
    transposes it. We bucket the raw rows by ``euAct`` URI:

    * the deadline / label / celex come from the first row (they're
      stable per directive);
    * ``transposing_acts`` is the deduplicated set of ``?eeAct`` URIs;
    * the ``status`` bucket is the *worst* (most incomplete) of the
      per-act statuses — i.e. if any transposing act says ``"puudub"`` /
      ``"osaline"`` / ``"ebaselge"`` we keep that, never letting one
      ``"kaetud"`` row hide an incomplete sibling. Severity order:
      ``puudub`` > ``ebaselge`` > ``osaline`` > ``kaetud``.

    After aggregation, the directive is **kept** only when its rolled-up
    status is in :data:`_INCOMPLETE_STATUSES` — fully-transposed
    directives drop out so the widget surfaces only actual debt.

    ``today`` is passed in (rather than re-read from the clock) so the
    function is deterministic in tests.
    """
    # Severity ordering for the status rollup. Higher number = surface this
    # over a less-severe sibling.
    severity = {"kaetud": 0, "osaline": 1, "ebaselge": 2, "puudub": 3}

    by_directive: dict[str, dict[str, Any]] = {}

    for r in raw_rows or []:
        eu_uri = str(r.get("euAct") or "").strip()
        if not eu_uri:
            continue
        deadline = _parse_deadline(str(r.get("deadline") or ""))
        if deadline is None:
            # An unparseable deadline literal means we can't compute
            # days_remaining or order the row — skip it rather than
            # surfacing a "deadline ?" cell.
            continue

        bucket = by_directive.setdefault(
            eu_uri,
            {
                "euAct": eu_uri,
                "euLabel": str(r.get("euLabel") or "").strip(),
                "celex": str(r.get("celex") or "").strip() or None,
                "deadline": deadline,
                "statuses": set(),  # type: set[TranspositionStatus]
                "transposing_acts": [],  # ordered, deduped via _seen
                "_seen_acts": set(),  # type: set[str]
            },
        )
        # Keep the first non-empty label/celex we see (they should agree
        # across rows for the same directive, but the OPTIONAL can leave
        # them unbound).
        if not bucket["euLabel"]:
            bucket["euLabel"] = str(r.get("euLabel") or "").strip()
        if bucket["celex"] is None:
            celex_raw = str(r.get("celex") or "").strip()
            bucket["celex"] = celex_raw or None

        raw_status = r.get("status")
        # Determine the bucket *per raw row*. If the raw row has no eeAct
        # at all (the OPTIONAL didn't match), that means "this directive
        # has no transposing Estonian act" — status is ``puudub``,
        # regardless of any ``transpositionStatus`` literal.
        ee_act = str(r.get("eeAct") or "").strip()
        if not ee_act:
            row_status: TranspositionStatus = "puudub"
        else:
            row_status = normalise_transposition_status(raw_status)
            if ee_act not in bucket["_seen_acts"]:
                bucket["_seen_acts"].add(ee_act)
                bucket["transposing_acts"].append(ee_act)
        bucket["statuses"].add(row_status)

    out: list[TranspositionDeadlineRow] = []
    for b in by_directive.values():
        statuses: set[TranspositionStatus] = b["statuses"]
        if not statuses:
            # Defensive fallback — shouldn't happen, but if it does we
            # treat the directive as "puudub" (we saw the deadline but no
            # transposition info at all).
            statuses = {"puudub"}
        rolled = max(statuses, key=lambda s: severity.get(s, 0))
        if rolled not in _INCOMPLETE_STATUSES:
            continue

        bucket_deadline: date = b["deadline"]
        celex = _celex_fallback(b["euAct"], b["celex"])
        label = _label_fallback(b["euLabel"], celex)
        days_remaining = (bucket_deadline - today).days

        out.append(
            TranspositionDeadlineRow(
                celex=celex,
                directive_label_et=label,
                deadline=bucket_deadline,
                days_remaining=days_remaining,
                status=rolled,
                transposing_acts=list(b["transposing_acts"]),
            )
        )

    # SPARQL ORDER BY ASC(?deadline) gives raw-row order; bucketing may
    # have shuffled it. Re-sort by deadline ascending so the most urgent
    # row is first (overdue → today → upcoming).
    out.sort(key=lambda row: row.deadline)
    return out


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def list_overdue_or_upcoming_transpositions(
    horizon_days: int = DEFAULT_TRANSPOSITION_HORIZON_DAYS,
    org_id: str | None = None,
    *,
    sparql_client: SparqlClient | None = None,
    today: date | None = None,
    timeout_s: float | None = None,
) -> list[TranspositionDeadlineRow]:
    """Return EU directives whose transposition deadline is approaching or passed.

    The result set is the directives where the transposition deadline is
    within ``horizon_days`` from ``today`` (i.e. the cutoff is
    ``today + horizon_days``) **and** Estonia's transposition status bucket
    is one of ``"puudub"`` / ``"osaline"`` / ``"ebaselge"`` (everything
    *except* ``"kaetud"``).

    Args:
        horizon_days: How many days ahead to scan. Defaults to
            :data:`DEFAULT_TRANSPOSITION_HORIZON_DAYS` (90 — roughly one
            quarter). Negative or zero is accepted but only surfaces
            directives whose deadline is already in the past (cutoff =
            today + 0 ≙ today, so the strict ``< cutoff`` filter keeps only
            yesterday-and-earlier rows). A very large value scans further
            ahead but the result is still capped to
            :data:`_DEADLINES_QUERY_LIMIT` rows server-side.
        org_id: Reserved for future filtering by responsible ministry —
            **currently accepted and ignored.** The ontology does not yet
            carry a ``responsibleMinistry`` predicate on directives or
            transposing acts; once it does, this argument will scope the
            result to the caller's org. Today every authenticated user
            sees the same nation-wide list. The parameter is on the
            signature now so the call sites in
            :mod:`app.dashboard.service` don't need to change when the
            scoping lands.
        sparql_client: Optional :class:`SparqlClient` override (tests
            inject one whose ``.query`` is mocked).
        today: Optional ``date`` override (tests freeze the clock).
            Defaults to :meth:`date.today`.
        timeout_s: When constructing a default :class:`SparqlClient`,
            cap the underlying HTTP request at this many seconds. The
            dashboard helper passes its own soft timeout here so a stuck
            Jena fails fast at the network layer rather than holding the
            worker thread alive in the background (F8, 2026-05-15
            review). Ignored when ``sparql_client`` is provided — the
            caller owns the timeout in that case.

    Returns:
        A list of :class:`TranspositionDeadlineRow`, ordered by deadline
        ascending (most overdue first). Empty on any SPARQL error so the
        widget hides gracefully rather than 500-ing the dashboard.
    """
    if today is None:
        today = date.today()

    cutoff = today + timedelta(days=horizon_days)

    # ``org_id`` is intentionally accepted-but-ignored per the docstring.
    # Reference it explicitly to silence ruff's unused-arg lint.
    _ = org_id

    if sparql_client is not None:
        client = sparql_client
    elif timeout_s is not None:
        client = SparqlClient(timeout=timeout_s)
    else:
        client = SparqlClient()
    query = _build_deadlines_query(cutoff)

    try:
        raw_rows = client.query(query)
    except Exception:
        logger.warning(
            "list_overdue_or_upcoming_transpositions: SPARQL query failed for "
            "horizon=%d cutoff=%s",
            horizon_days,
            cutoff.isoformat(),
            exc_info=True,
        )
        return []

    return _aggregate_rows(raw_rows or [], today=today)
