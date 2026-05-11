"""Act-level EU-transposition query + runner for the EL ülevõtt workflow (#723).

Epic #714, design doc ``docs/2026-05-11-ministry-lawyer-ui-structure.md``
("Workflow 2: EL Ülevõtt ja Harmoneerimine"). Unlike the impact
analyser's :data:`app.docs.impact.queries.EU_COMPLIANCE` — which is a
*provision-level, graph-scoped* query pivoting off a draft's
``estleg:references`` edges inside a named graph — this module answers
a different question: **given an EU legal act URI, which Estonian acts
transpose it, with what status, and which Estonian provisions are
harmonised with it?**

The query is therefore **entity-centered** (it takes an EU act URI
directly, no synthetic named graph, no ``GRAPH`` wrapper) and the URI
is bound safely via ``uri_bindings={"euAct": …}`` (a SPARQL ``VALUES``
clause) rather than string-interpolated — same pattern the explorer's
entity-detail queries and :mod:`app.docs.reference_resolver` use.

Data model (verified against the ``estonian-legal-ontology`` source
repo):

* ``estleg:transposesDirective``  — Estonian ``Act`` → ``EULegislation``
  (the canonical predicate).
* ``estleg:transposedBy``         — ``EULegislation`` → Estonian ``Act``
  (the inverse; some data only carries one direction, so the query
  ``UNION``-s both — exactly as ``EU_COMPLIANCE`` does for its
  ``implementsEU`` alias).
* ``estleg:transpositionStatus``  — a *literal* on the transposing act
  (likely values like "complete"/"partial"/"pending" or Estonian
  equivalents). Read where present; **absent → the row's status is
  ``"ebaselge"``**.
* ``estleg:harmonisedWith``       — Estonian ``LegalProvision`` → EU act
  (provision-level harmonisation, where present). ``OPTIONAL`` in the
  query; a transposing act with no harmonised provisions still
  produces one row (provision columns blank).

**No EU Article/Obligation entity model exists** in the ontology — so
the MVP is an act/provision-level transposition table, NOT the
article-by-article matrix sketched under "Workflow 2 / UI Output" in
the design doc. That matrix is a separate ontology-enrichment ticket.

**No competent-authority / responsible-institution predicate is wired
in the app today** (the ``estonian-legal-ontology`` repo has
institution-authority nodes, but the app-side SPARQL integration is
deferred to the Section-7 follow-up epic), so this query does **not**
project an authority column — we don't invent a predicate.

Status-derivation rules (raw ``transpositionStatus`` literal →
the four Estonian buckets the workflow renders):

* ``"complete"`` / ``"transposed"`` / ``"täielik"`` / ``"kaetud"``
  (case-insensitive) → ``"kaetud"``
* ``"partial"`` / ``"osaline"`` → ``"osaline"``
* anything else *present* (e.g. ``"pending"``, ``"unknown"``) →
  ``"ebaselge"``
* literal *absent* → ``"ebaselge"``
* the synthesised "EU act exists but has zero transposing Estonian
  acts" row (built in Python after an empty query result, not in
  SPARQL — simpler) → ``"puudub"``

A dead Jena must not 500 the route: :func:`run_eu_transposition` wraps
the SPARQL call in try/except → logs + returns ``[]`` so the route
shows a graceful "ei õnnestunud" message.
"""

from __future__ import annotations

import logging
from typing import Any

from app.docs.impact.queries import PREFIXES
from app.ontology.sparql_client import SparqlClient

logger = logging.getLogger(__name__)

# Cap the row count — an EU act with hundreds of harmonised provisions
# would otherwise return a large cartesian product; the workflow renders
# at most a few dozen rows anyway. Mirrors the LIMITs in
# :mod:`app.docs.impact.queries`.
_EU_TRANSPOSITION_LIMIT = 500

#: The four Estonian status buckets the EL ülevõtt workflow renders.
TranspositionStatus = str  # one of: "kaetud" | "osaline" | "puudub" | "ebaselge"

# Raw ``transpositionStatus`` literals we treat as "fully covered".
_STATUS_COVERED = frozenset(
    {
        "complete",
        "completed",
        "transposed",
        "fully transposed",
        "full",
        "täielik",
        "täielikult",
        "kaetud",
        "üle võetud",
        "ülevõetud",
    }
)
# Raw literals we treat as "partially covered".
_STATUS_PARTIAL = frozenset(
    {
        "partial",
        "partially transposed",
        "osaline",
        "osaliselt",
        "osaliselt üle võetud",
    }
)


def normalise_transposition_status(raw: str | None) -> TranspositionStatus:
    """Map a raw ``estleg:transpositionStatus`` literal to one of the four buckets.

    Liberal/forgiving by design (the source data's exact vocabulary is
    not pinned): ``complete``-ish → ``"kaetud"``, ``partial``-ish →
    ``"osaline"``, anything else present → ``"ebaselge"``, absent /
    blank → ``"ebaselge"``. The ``"puudub"`` bucket is *not* produced
    here — it's synthesised by :func:`run_eu_transposition` for the
    "no transposing act at all" case.
    """
    if raw is None:
        return "ebaselge"
    key = str(raw).strip().lower()
    if not key:
        return "ebaselge"
    if key in _STATUS_COVERED:
        return "kaetud"
    if key in _STATUS_PARTIAL:
        return "osaline"
    return "ebaselge"


def build_eu_transposition_query(eu_act_uri: str) -> str:
    """Return the entity-centered EU-transposition SPARQL query for *eu_act_uri*.

    The URI is **bound** by the caller via
    ``SparqlClient.query(..., uri_bindings={"euAct": eu_act_uri})`` — a
    ``VALUES ?euAct { <uri> }`` clause the client appends before the
    closing ``}`` and validates against a strict allowlist — so it is
    **never string-interpolated** here. This function only assembles the
    static query body.

    Returned rows project::

        ?euAct ?euLabel ?celex
        ?eeAct ?eeActLabel ?status
        ?eeProvision ?eeProvisionLabel

    Both transposition directions are covered with a ``UNION``:

    * ``?eeAct estleg:transposesDirective ?euAct`` (canonical), and
    * ``?euAct estleg:transposedBy ?eeAct`` (the inverse)

    so the query works regardless of which direction the loaded data
    carries. ``estleg:transpositionStatus`` and the harmonised-provision
    join are ``OPTIONAL`` — an act with no status literal still returns
    a row (``?status`` unbound → :func:`run_eu_transposition` maps it to
    ``"ebaselge"``); an act with no harmonised provisions still returns a
    row (provision columns unbound).

    The "EU act exists but is transposed by *no* Estonian act" case is
    **not** handled in SPARQL — it's simpler to detect an empty result
    in Python (:func:`run_eu_transposition`) and synthesise a single
    ``"puudub"`` row there.
    """
    return (
        PREFIXES
        + """
SELECT DISTINCT ?euAct ?euLabel ?celex ?eeAct ?eeActLabel ?status ?eeProvision ?eeProvisionLabel
WHERE {
  ?euAct a estleg:EULegislation .
  OPTIONAL { ?euAct rdfs:label ?euLabel }
  OPTIONAL { ?euAct estleg:celexNumber ?celex }
  {
    ?eeAct estleg:transposesDirective ?euAct .
  } UNION {
    ?euAct estleg:transposedBy ?eeAct .
  }
  OPTIONAL { ?eeAct rdfs:label ?eeActLabel }
  OPTIONAL { ?eeAct estleg:transpositionStatus ?status }
  OPTIONAL {
    ?eeProvision estleg:harmonisedWith ?euAct .
    OPTIONAL { ?eeProvision rdfs:label ?eeProvisionLabel }
  }
}
LIMIT """
        + str(_EU_TRANSPOSITION_LIMIT)
        + "\n"
    )


def _row_dict(
    *,
    eu_act: str,
    eu_label: str,
    celex: str | None,
    ee_act: str | None,
    ee_act_label: str | None,
    ee_provision: str | None,
    ee_provision_label: str | None,
    status: TranspositionStatus,
) -> dict[str, Any]:
    """Build one normalised result dict (the shape the route consumes).

    ``authority`` / ``authority_label`` are always ``None`` — no
    competent-authority predicate is wired in the app yet (see the
    module docstring); kept in the shape so the route's column logic
    (and a future enrichment ticket) has a stable contract.
    """
    return {
        "eu_act": eu_act,
        "eu_label": eu_label,
        "celex": celex or None,
        "ee_act": ee_act or None,
        "ee_act_label": ee_act_label or None,
        "ee_provision": ee_provision or None,
        "ee_provision_label": ee_provision_label or None,
        "status": status,
        "authority": None,
        "authority_label": None,
    }


def run_eu_transposition(
    eu_act_uri: str,
    *,
    sparql_client: SparqlClient | None = None,
) -> list[dict[str, Any]]:
    """Execute :func:`build_eu_transposition_query` and return normalised rows.

    Args:
        eu_act_uri: The resolved ``estleg:EULegislation`` URI to inspect.
            An empty/whitespace value short-circuits to ``[]`` without
            touching Jena.
        sparql_client: Optional :class:`SparqlClient` override (tests
            inject one whose ``.query`` is mocked). Defaults to a fresh
            client.

    Returns:
        A list of dicts shaped::

            {"eu_act": str, "eu_label": str, "celex": str|None,
             "ee_act": str|None, "ee_act_label": str|None,
             "ee_provision": str|None, "ee_provision_label": str|None,
             "status": "kaetud"|"osaline"|"puudub"|"ebaselge",
             "authority": None, "authority_label": None}

        * Raw ``transpositionStatus`` literals are mapped to the four
          Estonian buckets via :func:`normalise_transposition_status`.
        * When the query returns rows that name the EU act but **no**
          transposing Estonian act (``?eeAct`` unbound everywhere) — or
          returns nothing at all but the URI is non-empty — a single
          synthesised ``"puudub"`` row is returned so the workflow can
          surface "this EU act is transposed by no Estonian act".
        * On **any** SPARQL exception the function logs and returns
          ``[]`` (a dead Jena ⇒ empty ⇒ the route shows a graceful "ei
          õnnestunud" message rather than 500).
    """
    if not eu_act_uri or not eu_act_uri.strip():
        return []

    client = sparql_client if sparql_client is not None else SparqlClient()
    query = build_eu_transposition_query(eu_act_uri)

    try:
        raw_rows = client.query(query, uri_bindings={"euAct": eu_act_uri})
    except Exception:
        logger.warning(
            "run_eu_transposition: SPARQL query failed for euAct=%s", eu_act_uri, exc_info=True
        )
        return []

    # Best label/CELEX we saw for the EU act itself — used both for the
    # real rows and (if the act has zero transposing acts) the
    # synthesised "puudub" row.
    eu_label = ""
    celex: str | None = None
    out: list[dict[str, Any]] = []
    for row in raw_rows or []:
        eu_act = str(row.get("euAct") or eu_act_uri).strip() or eu_act_uri
        row_eu_label = str(row.get("euLabel") or "").strip()
        if row_eu_label and not eu_label:
            eu_label = row_eu_label
        row_celex = str(row.get("celex") or "").strip()
        if row_celex and celex is None:
            celex = row_celex
        ee_act = str(row.get("eeAct") or "").strip()
        if not ee_act:
            # A row that only names the EU act (its label/CELEX) but no
            # transposing act — skip it as a data row; it'll be covered
            # by the synthesised "puudub" row below if it's the *only*
            # thing we got back.
            continue
        out.append(
            _row_dict(
                eu_act=eu_act,
                eu_label=row_eu_label or eu_label or eu_act,
                celex=row_celex or celex,
                ee_act=ee_act,
                ee_act_label=str(row.get("eeActLabel") or "").strip() or None,
                ee_provision=str(row.get("eeProvision") or "").strip() or None,
                ee_provision_label=str(row.get("eeProvisionLabel") or "").strip() or None,
                status=normalise_transposition_status(row.get("status")),
            )
        )

    if out:
        # Backfill the EU label/CELEX onto every row if some rows missed
        # them (the OPTIONAL can leave them unbound on the harmonisation
        # branch).
        for r in out:
            if not r["eu_label"] or r["eu_label"] == r["eu_act"]:
                r["eu_label"] = eu_label or r["eu_act"]
            if r["celex"] is None and celex is not None:
                r["celex"] = celex
        return out

    # Empty (or only act-naming) result → the EU act has no transposing
    # Estonian act. Surface a single "puudub" row.
    return [
        _row_dict(
            eu_act=eu_act_uri,
            eu_label=eu_label or eu_act_uri,
            celex=celex,
            ee_act=None,
            ee_act_label=None,
            ee_provision=None,
            ee_provision_label=None,
            status="puudub",
        )
    ]
