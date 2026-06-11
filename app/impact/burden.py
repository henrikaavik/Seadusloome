"""SPARQL helpers for the Halduskoormus workflow (A2 v1, plan section 5).

The ``Halduskoormus`` (administrative-burden / deontic-view) workflow
surfaces, for a chosen act or draft, **how many provisions classify as
each deontic type** — obligations, prohibitions, permissions and rights
— and lets the lawyer drill into each bucket.

Ontology vocabulary
-------------------

Verified populated corpus-wide by the 2026-05-15 audit
(``docs/2026-05-15-ontology-six-use-cases-plan.md`` section 2.5, row A2):

* ``estleg:NormativeType`` — class with four canonical individuals
  ``estleg:NormType_Obligation``, ``estleg:NormType_Right``,
  ``estleg:NormType_Permission``, ``estleg:NormType_Prohibition``.
* ``estleg:normativeType`` — predicate on ``LegalProvision`` pointing at one
  of the four individuals above (and occasionally at a free-text literal
  for older corpus rows).
* ``estleg:dutyHolder`` — free-text literal carrying the "who must do
  this" actor name (e.g. ``"Tööandja"``, ``"Riik"``). Used as the v1
  "target group" fallback bucketing column **until** ontology issue
  ``henrikaavik/estonian-legal-ontology#214`` (multi-valued
  ``estleg:targetGroup`` enum) merges. The v1 UI labels the column
  ``"Kohustatud isik (esialgne, vt #214)"`` so the user knows it's the
  pre-enum fallback.

A2 v2 — once ``estleg:targetGroup`` lands — will replace the
``dutyHolder`` literal bucketing with the closed enum (``citizen`` /
``business`` / ``public_body`` / ``official`` / ``ngo``). Until then, the
v1 helpers here ship the count grid + per-row list using the existing
``normativeType`` + ``dutyHolder`` predicates only. See the deferred
note on the docstring of :func:`bucket_burden_rows`.

Predicates from :mod:`app.ontology.relations`
---------------------------------------------

The four canonical PREDICATES entries used by this module
(``NORMATIVE_TYPE``, ``DUTY_HOLDER``, plus the four ``NORM_TYPE_*``
individuals and the ``NORMATIVE_TYPE_CLASS`` URI) are imported from
``app.ontology.relations`` per the A2 rule that route/handler code must
never hardcode ``estleg:*`` URIs. They were added to ``relations.py``
specifically for A2 (the audit confirmed they are present in the source
ontology).
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from app.ontology.queries import PREFIXES
from app.ontology.relations import (
    NORM_TYPE_INDIVIDUALS,
    NORM_TYPE_KEYS,
    PREDICATES,
    norm_type_key,
)
from app.ontology.sparql_client import SparqlClient
from app.ontology.temporal_scope import (
    DEFAULT_SCOPE,
    TemporalScope,
    temporal_scope_clause,
)

logger = logging.getLogger(__name__)

# Cap row counts so a corpus act (KarS, KMS) with hundreds of provisions
# stays page-weight-friendly. The result UI signals truncation in the
# summary line.
_MAX_BURDEN_ROWS_PER_ACT = 500

# Cap how many distinct dutyHolder buckets the v1 "target group fallback"
# surfaces — a single act can have dozens of free-text actors; keep the
# top-N most-frequent ones and lump the long tail into "Muud kohustatud
# isikud" so the UI stays scannable.
_MAX_DUTY_HOLDER_BUCKETS = 12


# Public deontic-key alias — re-exported for type-friendliness in callers.
BurdenKey = Literal["obligation", "prohibition", "permission", "right", "unknown"]


# Estonian display labels for the four deontic categories. ``unknown`` is
# the catch-all bucket for rows whose ``normativeType`` is missing or
# points at a non-canonical individual / free-text literal we can't
# classify — surfaced in the UI as "Liigitamata".
BURDEN_LABELS_ET: dict[BurdenKey, str] = {
    "obligation": "Kohustused",
    "prohibition": "Keelud",
    "permission": "Load",
    "right": "Õigused",
    "unknown": "Liigitamata",
}

# Estonian one-line description for each bucket — surfaced in the count
# grid card under the count, and in the per-row table caption.
BURDEN_DESCRIPTIONS_ET: dict[BurdenKey, str] = {
    "obligation": "Sätted, mis panevad isikule või asutusele kohustuse teha (või talluda) midagi.",
    "prohibition": "Sätted, mis keelavad konkreetse käitumise.",
    "permission": "Sätted, mis annavad loa midagi teha (kuid ei kohusta).",
    "right": "Sätted, mis sätestavad subjektiivse õiguse.",
    "unknown": "Sätted, mille deontiline liik on ontoloogias määramata.",
}


# Order the buckets appear in the count grid + summary line — obligations
# first because they are the most operationally significant for VTK
# halduskoormus, prohibitions next (also burden-creating), permissions /
# rights last (burden-relieving / neutral). ``unknown`` only renders when
# its count is non-zero.
_BURDEN_KEY_ORDER: tuple[BurdenKey, ...] = (
    "obligation",
    "prohibition",
    "permission",
    "right",
    "unknown",
)


# ---------------------------------------------------------------------------
# BurdenRow + summary dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BurdenRow:
    """One provision projected with its deontic classification.

    Attributes:
        provision_uri: The owning ``LegalProvision`` URI — always set
            because we walked the graph through ``normativeType`` /
            ``dutyHolder`` (we keep rows with neither edge for the
            "Liigitamata" bucket so the count grid is honest).
        provision_label: ``rdfs:label`` on the provision. Falls back
            to the URI tail when absent so the UI cell never renders
            blank.
        act_uri: The Act URI (best-effort). In the prod corpus
            ``estleg:sourceAct`` is a string literal (the act title),
            not a URI, so this is typically empty for prod rows; the
            URI form is still projected when the SPARQL data carries a
            URI object (e.g. the canonical TTL fixture). See the Wave 2
            spike in ``docs/2026-05-18-bugfix-plan.md`` — ``estleg:partOf``
            / ``estleg:partOfAct`` carry zero triples in prod.
        act_label: The act title. Either ``rdfs:label`` on the URI
            (fixture shape) or the literal value of ``sourceAct``
            itself (prod shape — the literal IS the title).
        norm_type_uri: The ``estleg:NormativeType`` individual URI (or
            ``""`` when the ontology row carries a literal /
            non-canonical value). Useful for tests that want to assert
            the canonical URI was reached, not just the bucketed key.
        burden_key: The bucketed deontic key (one of
            :data:`BURDEN_LABELS_ET`'s keys). The bucketing is done by
            :func:`norm_type_key`, which accepts URIs / prefixed names /
            literal strings (``"obligation"`` / ``"Kohustus"`` / …).
        duty_holder: The raw ``estleg:dutyHolder`` literal (e.g.
            ``"Tööandja"``). ``""`` when the predicate is absent.
            Surfaced in the v1 "Kohustatud isik (esialgne, vt #214)"
            column — the v2 ontology issue #214 will replace this with
            a ``targetGroup`` enum.
    """

    provision_uri: str = ""
    provision_label: str = ""
    act_uri: str = ""
    act_label: str = ""
    norm_type_uri: str = ""
    burden_key: BurdenKey = "unknown"
    duty_holder: str = ""
    extras: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class BurdenSummary:
    """Aggregate burden picture for an act / draft.

    Attributes:
        counts: Per-deontic-key count of rows in :attr:`rows`. Always
            populated for every key in :data:`BURDEN_LABELS_ET`, even
            when the count is ``0``, so the UI's count grid can render
            every cell without ``dict.get`` defaults.
        rows: The flat list of :class:`BurdenRow` instances — the count
            grid is derived from this list, and the per-bucket detail
            tables also read from it.
        duty_holder_counts: Top-N most frequent ``dutyHolder`` literals
            with their row counts (v1 "target group" fallback). Long
            tail past :data:`_MAX_DUTY_HOLDER_BUCKETS` is lumped into a
            single ``"Muud"`` bucket. ``""`` (the "no dutyHolder set"
            bucket) is **kept** in the dict (under the empty-string key)
            so the UI can show how many rows lack the literal.
        total: ``len(rows)`` — exposed for the UI summary line.
        truncated: ``True`` when SPARQL hit :data:`_MAX_BURDEN_ROWS_PER_ACT`
            and the row list is therefore not the full corpus answer.
            The UI surfaces "Näidatud N esimest sätet" when truthy.
    """

    counts: dict[BurdenKey, int]
    rows: list[BurdenRow]
    duty_holder_counts: dict[str, int]
    total: int
    truncated: bool


@dataclass(frozen=True)
class BurdenDelta:
    """Burden delta between a draft's affected provisions and the prior law.

    "Prior law" means: the **existing** :class:`BurdenSummary` for the
    same set of provisions before the draft's amendments take effect.
    For v1 we approximate this by intersecting the draft's referenced
    provisions with the existing ontology — the draft itself does **not**
    carry the new ``normativeType`` literals yet in current data, so the
    v1 delta surfaces "the draft touches X provisions; of those, Y are
    Obligations / Z are Prohibitions today" rather than a fully forward-
    looking "the draft will *add* N obligations".

    The richer "before vs. after" diff is deferred to v2 once draft
    provisions carry their own ``normativeType`` edges (planned with
    ontology issue #214's data backfill).

    Attributes:
        before: The existing-law :class:`BurdenSummary` over the
            provisions the draft references / amends. May be empty when
            the draft references no resolved provisions.
        after: ``None`` for v1 (the draft's own deontic edges aren't
            populated yet). Kept on the dataclass so v2 can fill it in
            without churning callers.
        affected_count: Number of distinct provisions the draft
            references / amends — convenience field for the UI summary.
    """

    before: BurdenSummary
    after: BurdenSummary | None
    affected_count: int


# ---------------------------------------------------------------------------
# Estonian display helpers
# ---------------------------------------------------------------------------


def burden_label(key: BurdenKey | str) -> str:
    """Estonian display label for a bucketed deontic key, with fallback."""
    k = str(key or "").strip().lower()
    if k in BURDEN_LABELS_ET:
        return BURDEN_LABELS_ET[k]  # type: ignore[index]
    return BURDEN_LABELS_ET["unknown"]


def burden_description(key: BurdenKey | str) -> str:
    """One-line Estonian description of what the bucket means."""
    k = str(key or "").strip().lower()
    if k in BURDEN_DESCRIPTIONS_ET:
        return BURDEN_DESCRIPTIONS_ET[k]  # type: ignore[index]
    return BURDEN_DESCRIPTIONS_ET["unknown"]


def burden_key_order() -> tuple[BurdenKey, ...]:
    """Return the canonical UI display order for the deontic buckets."""
    return _BURDEN_KEY_ORDER


# ---------------------------------------------------------------------------
# SPARQL templates
# ---------------------------------------------------------------------------
#
# Two templates only — by act / by provision. The "by draft" path reuses
# the by-provision template once we've resolved the draft's referenced
# provisions (a draft does not carry its own normativeType edges in v1
# data, see BurdenDelta docstring).
#
# Act ↔ Provision membership shape (post Wave 2 spike, 2026-05-18):
#
# The Wave 2 diagnostic spike (`docs/2026-05-18-bugfix-plan.md`,
# Step 1) confirmed for the production corpus:
#
#   * ``estleg:sourceAct`` is the only provision-to-act edge present
#     (24,221 triples, all ``xsd:string`` literals — sample
#     ``"Avaliku teabe seadus"``). There are zero URI objects.
#   * ``estleg:partOf`` and ``estleg:partOfAct`` both carry zero
#     triples corpus-wide. The previous UNION arms were silently
#     producing zero rows.
#
# These templates therefore use **only** ``estleg:sourceAct``. The
# binding variable ``?actLit`` is deliberately neutral: it accepts
# either a string literal (prod shape) or a URI (canonical TTL
# fixture shape — ``estleg:Provision_1 estleg:sourceAct estleg:Act_1``).
# The Python caller decides whether to pass the act identifier through
# ``bindings`` (literal VALUES) or ``uri_bindings`` (URI VALUES).
#
# When the bound ``?actLit`` is a URI we project ``?act`` (the URI)
# and try to resolve ``?actLabel`` via ``rdfs:label`` — the fixture
# shape. When the bound ``?actLit`` is a literal we project an empty
# ``?act`` and pass the literal through as ``?actLabel`` because the
# literal IS the act title (no extra label lookup needed).
#
# We OPTIONAL every field except ``provision`` (and the sourceAct
# join, which anchors the membership) because the corpus' completeness
# varies — many provisions have a normativeType but no dutyHolder,
# and a small minority have neither (kept in the "Liigitamata" bucket
# so the count grid is honest).


def _build_act_burden_query(scope: TemporalScope = DEFAULT_SCOPE) -> str:
    """Return the act-level burden SPARQL.

    Joins via ``estleg:sourceAct`` only — the Wave 2 spike confirmed
    ``estleg:partOf`` / ``estleg:partOfAct`` carry zero triples in
    prod, so the historical UNION arms were dead code. ``?actLit``
    is bound by the caller as either a string literal (prod shape)
    or a URI (canonical TTL fixture shape); the ``BIND`` clauses
    derive ``?act`` (URI form when applicable) and ``?actLabel``
    (label-or-literal) from whichever object the data carries.

    The *temporal scope* (#850) is injected as a ``FILTER NOT EXISTS``
    block via :func:`app.ontology.temporal_scope.temporal_scope_clause`.
    The default :data:`~app.ontology.temporal_scope.DEFAULT_SCOPE`
    (current law) drops provisions whose owning act is *positively*
    marked repealed; pass :attr:`TemporalScope.ALL` for the full history.
    """
    return (
        PREFIXES
        + f"""
SELECT ?provision ?provisionLabel ?act ?actLabel ?normType ?dutyHolder
WHERE {{
  ?provision estleg:sourceAct ?actLit .
  OPTIONAL {{ ?provision rdfs:label ?provisionLabel }}
  OPTIONAL {{ ?actLit rdfs:label ?actLabelFromUri }}
  OPTIONAL {{ ?provision <{PREDICATES.NORMATIVE_TYPE}> ?normType }}
  OPTIONAL {{ ?provision <{PREDICATES.DUTY_HOLDER}> ?dutyHolder }}
  BIND(IF(isURI(?actLit), STR(?actLit), "") AS ?act)
  BIND(
    IF(BOUND(?actLabelFromUri), STR(?actLabelFromUri),
       IF(isLiteral(?actLit), STR(?actLit), ""))
    AS ?actLabel
  )
{temporal_scope_clause(scope, "provision")}
}}
ORDER BY ?provision
LIMIT {_MAX_BURDEN_ROWS_PER_ACT}
"""
    )


def _build_provision_burden_query(scope: TemporalScope = DEFAULT_SCOPE) -> str:
    """Return the provision-level burden SPARQL (single-row OPTIONAL fan-out).

    Joins via ``estleg:sourceAct`` only — see :func:`_build_act_burden_query`
    for the rationale (Wave 2 spike, 2026-05-18). The whole
    ``sourceAct`` chain is wrapped in an OPTIONAL because a provision
    URI looked up in isolation may not carry the membership edge yet.

    The temporal scope (#850) is injected as a ``FILTER NOT EXISTS``
    block. Default = current law (positively-repealed provisions
    dropped); :attr:`TemporalScope.ALL` keeps everything. Note the
    ``?provision`` here is bound by the caller's URI VALUES clause, so
    the filter operates on that single provision (it disappears from the
    single-row result when the provision / its act is positively
    repealed and the scope is current).
    """
    return (
        PREFIXES
        + f"""
SELECT ?provision ?provisionLabel ?act ?actLabel ?normType ?dutyHolder
WHERE {{
  OPTIONAL {{ ?provision rdfs:label ?provisionLabel }}
  OPTIONAL {{
    ?provision estleg:sourceAct ?actLit .
    OPTIONAL {{ ?actLit rdfs:label ?actLabelFromUri }}
    BIND(IF(isURI(?actLit), STR(?actLit), "") AS ?act)
    BIND(
      IF(BOUND(?actLabelFromUri), STR(?actLabelFromUri),
         IF(isLiteral(?actLit), STR(?actLit), ""))
      AS ?actLabel
    )
  }}
  OPTIONAL {{ ?provision <{PREDICATES.NORMATIVE_TYPE}> ?normType }}
  OPTIONAL {{ ?provision <{PREDICATES.DUTY_HOLDER}> ?dutyHolder }}
{temporal_scope_clause(scope, "provision")}
}}
LIMIT 1
"""
    )


def _build_draft_affected_provisions_query() -> str:
    """Return the SPARQL that lists provisions a draft references / amends.

    Uses ``estleg:amends`` (AmendmentEvent → Provision; the draft's
    amendment events) plus ``estleg:references`` (Draft → any entity, a
    weaker signal but the corpus uses it for "this draft touches that
    provision" in non-AmendmentEvent contexts). The UNION arm guards
    against the half-populated corpus where some drafts have references
    but no AmendmentEvent rows yet.

    This is the **default-graph** variant: the draft subject is supplied
    by the caller as a ``?draftUri`` URI binding and the triples are
    expected in the default graph. It remains the public ontology path
    (e.g. an enacted-Act draft already merged into the default graph).
    For an *uploaded* draft whose triples live at ``<graph_uri>#self``
    inside a named graph, use :func:`_build_draft_affected_provisions_graph_query`
    instead (see #855).
    """
    return (
        PREFIXES
        + f"""
SELECT DISTINCT ?provision
WHERE {{
  {{
    ?ev <{PREDICATES.AMENDS}> ?provision .
    ?draft ?p ?ev .
    FILTER(?draft = ?draftUri)
  }}
  UNION
  {{ ?draftUri <{PREDICATES.REFERENCES}> ?provision }}
}}
LIMIT {_MAX_BURDEN_ROWS_PER_ACT}
"""
    )


# Mirrors ``SparqlClient._inject_uri_bindings``'s allowlist. The
# batched delta query (#858) interpolates the provision URIs into an
# inline ``VALUES`` clause itself (the client helper only supports one
# URI per variable), so every URI must pass the same strict character
# allowlist before it touches the query text.
_SAFE_PROVISION_URI_RE = re.compile(r"^https?://[A-Za-z0-9./:_#\-]{1,512}$")

# Row ceiling for the batched delta query. The affected-provision lookup
# already caps the VALUES set at ``_MAX_BURDEN_ROWS_PER_ACT`` (500); the
# 4x headroom absorbs corpus provisions that multi-row on
# ``normativeType`` / ``dutyHolder`` echoes (deduped client-side by
# ``_rows_to_burden``) without letting a pathological graph stream an
# unbounded result set.
_MAX_DELTA_BURDEN_ROWS = _MAX_BURDEN_ROWS_PER_ACT * 4


def _build_provisions_burden_values_query(
    provision_uris: Sequence[str],
    scope: TemporalScope = DEFAULT_SCOPE,
) -> str:
    """Return ONE burden SPARQL covering *provision_uris* via ``VALUES`` (#858).

    Replaces the v1 per-provision loop in :func:`burden_delta_for_draft`
    (one SPARQL round-trip per affected provision — up to 500 sequential
    calls). The body is the same OPTIONAL fan-out as
    :func:`_build_provision_burden_query`, including the temporal-scope
    ``FILTER NOT EXISTS`` (#850/#872 semantics preserved: the delta's
    "before" baseline stays current-law by default), but the provision
    variable is bound by an inline ``VALUES ?provision { <u1> <u2> … }``
    block so the whole set resolves in a single round-trip and is
    aggregated client-side.

    Because every VALUES row keeps at least one solution (the body is
    all-OPTIONAL), a provision unknown to the ontology still emits a
    bare ``?provision`` row and lands in the honest "Liigitamata"
    bucket — exactly what the per-provision ``LIMIT 1`` variant did.

    Args:
        provision_uris: Provision URIs, each already validated against
            :data:`_SAFE_PROVISION_URI_RE` by the caller (the function
            re-checks defensively and raises ``ValueError`` on a miss —
            these URIs are interpolated into the query text).
        scope: Temporal scope; defaults to current law, matching the
            scope the per-provision path inherited.
    """
    for uri in provision_uris:
        if not _SAFE_PROVISION_URI_RE.fullmatch(uri):
            raise ValueError(f"Unsafe URI rejected for burden VALUES binding: {uri!r}")
    values = " ".join(f"<{u}>" for u in provision_uris)
    return (
        PREFIXES
        + f"""
SELECT ?provision ?provisionLabel ?act ?actLabel ?normType ?dutyHolder
WHERE {{
  VALUES ?provision {{ {values} }}
  OPTIONAL {{ ?provision rdfs:label ?provisionLabel }}
  OPTIONAL {{
    ?provision estleg:sourceAct ?actLit .
    OPTIONAL {{ ?actLit rdfs:label ?actLabelFromUri }}
    BIND(IF(isURI(?actLit), STR(?actLit), "") AS ?act)
    BIND(
      IF(BOUND(?actLabelFromUri), STR(?actLabelFromUri),
         IF(isLiteral(?actLit), STR(?actLit), ""))
      AS ?actLabel
    )
  }}
  OPTIONAL {{ ?provision <{PREDICATES.NORMATIVE_TYPE}> ?normType }}
  OPTIONAL {{ ?provision <{PREDICATES.DUTY_HOLDER}> ?dutyHolder }}
{temporal_scope_clause(scope, "provision")}
}}
LIMIT {_MAX_DELTA_BURDEN_ROWS}
"""
    )


def _build_draft_affected_provisions_graph_query(graph_uri: str) -> str:
    """Return the GRAPH-scoped affected-provisions SPARQL for an uploaded draft.

    #855 — the C6 burden section was silently always zero. An uploaded
    draft's triples are written by :func:`app.docs.graph_builder.build_draft_graph`
    as ``<graph_uri>#self estleg:references <provision>`` **inside the
    named graph** ``<graph_uri>``. The default-graph variant above (which
    binds ``?draftUri`` to the bare ``graph_uri`` and matches the default
    graph) therefore never matched: wrong subject (missing ``#self``) AND
    wrong graph (Fuseki has no ``unionDefaultGraph`` — see
    ``docker/fuseki-config/ontology.ttl``). The whole burden delta came
    back empty and nothing errored.

    This variant fixes both:

    * the draft-side patterns are wrapped in ``GRAPH <graph_uri> {…}`` so
      they read the named graph where the draft triples actually live;
    * the subject is the ``<graph_uri>#self`` IRI, bound by the caller via
      ``uri_bindings={"draftSelf": "<graph_uri>#self"}`` (the SparqlClient
      URI allowlist admits ``#``).

    ``graph_uri`` is interpolated directly into the ``GRAPH`` clause and
    MUST already be validated by :func:`app.sync.jena_loader._validate_graph_uri`
    — the caller (:func:`burden_delta_for_draft`) does this before calling.
    The amendment-event arm reads the AmendmentEvent → Provision hop from
    the default graph (enacted ontology) since AmendmentEvents are public
    ontology nodes, while the draft → event hop stays inside the graph.
    """
    return (
        PREFIXES
        + f"""
SELECT DISTINCT ?provision
WHERE {{
  {{
    GRAPH <{graph_uri}> {{ ?draftSelf ?p ?ev . }}
    ?ev <{PREDICATES.AMENDS}> ?provision .
  }}
  UNION
  {{
    GRAPH <{graph_uri}> {{ ?draftSelf <{PREDICATES.REFERENCES}> ?provision . }}
  }}
}}
LIMIT {_MAX_BURDEN_ROWS_PER_ACT}
"""
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _looks_like_uri(value: str) -> bool:
    """Return True when *value* is shaped like a SPARQL-bindable URI.

    The Wave 2 spike confirmed prod's ``estleg:sourceAct`` is always a
    literal title; the canonical TTL fixture uses a URI. The act-level
    burden query supports both — this helper decides which VALUES
    binding to emit for the caller's input.
    """
    v = (value or "").strip()
    return v.startswith("http://") or v.startswith("https://")


def list_burden_for_act(
    act: str,
    *,
    scope: TemporalScope = DEFAULT_SCOPE,
    sparql_client: SparqlClient | None = None,
) -> BurdenSummary:
    """Return the deontic-classified rows + counts for every provision of *act*.

    Walks ``?provision estleg:sourceAct ?actLit`` and projects
    ``rdfs:label``, ``estleg:normativeType``, ``estleg:dutyHolder``
    for each member provision. The Wave 2 spike (2026-05-18)
    confirmed ``estleg:partOf`` / ``estleg:partOfAct`` carry zero
    triples in prod, so the only honest provision-to-act join in this
    corpus is the literal ``sourceAct`` title; the historical UNION
    arms were silently producing zero rows.

    Args:
        act: The act identifier. Accepts either a string literal title
            (the prod resolver shape, post Wave 2 Step 2 — e.g.
            ``"Töölepingu seadus"``) or a URI (the canonical TTL
            fixture shape, e.g. ``"https://…#Act_1"``). The function
            detects the shape and emits the appropriate VALUES
            binding. Empty / whitespace input yields an empty
            :class:`BurdenSummary` (no SPARQL hit).
        scope: Temporal scope (#850). Default
            :data:`~app.ontology.temporal_scope.DEFAULT_SCOPE` (current
            law) excludes provisions whose owning act is positively
            marked repealed; :attr:`TemporalScope.ALL` includes the full
            history.
        sparql_client: Optional :class:`SparqlClient` override (tests
            inject a mocked one).

    Returns:
        A :class:`BurdenSummary` — counts dict, full row list, top-N
        ``dutyHolder`` distribution, total, and a ``truncated`` flag.
        A dead Jena / any SPARQL error degrades to an empty summary
        rather than a 500.
    """
    ident = (act or "").strip()
    if not ident:
        return _empty_summary()

    client = sparql_client if sparql_client is not None else SparqlClient()
    query = _build_act_burden_query(scope)
    try:
        if _looks_like_uri(ident):
            rows = client.query(query, uri_bindings={"actLit": ident})
        else:
            rows = client.query(query, bindings={"actLit": ident})
    except Exception:
        logger.warning("list_burden_for_act: SPARQL query failed for %r", ident, exc_info=True)
        return _empty_summary()

    return _summary_from_rows(rows)


def list_burden_for_provision(
    provision_uri: str,
    *,
    scope: TemporalScope = DEFAULT_SCOPE,
    sparql_client: SparqlClient | None = None,
) -> BurdenSummary:
    """Return the deontic-classified single-row summary for a provision URI.

    Same shape as :func:`list_burden_for_act` but for a single provision.
    Useful when the user's input resolves to a §-reference rather than an
    act / draft — the count grid then shows ``1`` in exactly one bucket
    and ``0`` in the rest.

    Args:
        provision_uri: The ``LegalProvision`` URI.
        scope: Temporal scope (#850). Default current law — a
            positively-repealed provision (or one whose owning act is
            repealed) yields an empty summary under the current-law
            scope. :attr:`TemporalScope.ALL` keeps it. The draft-delta
            path (:func:`burden_delta_for_draft`) uses the batched
            ``VALUES`` variant of this query (#858) with the same
            current-law default for its "before" baseline.
        sparql_client: Optional :class:`SparqlClient` override.
    """
    uri = (provision_uri or "").strip()
    if not uri:
        return _empty_summary()

    client = sparql_client if sparql_client is not None else SparqlClient()
    try:
        rows = client.query(
            _build_provision_burden_query(scope),
            uri_bindings={"provision": uri},
        )
    except Exception:
        logger.warning(
            "list_burden_for_provision: SPARQL query failed for %r",
            uri,
            exc_info=True,
        )
        return _empty_summary()

    return _summary_from_rows(rows)


def burden_delta_for_draft(
    draft_uri: str,
    *,
    graph_uri: str | None = None,
    sparql_client: SparqlClient | None = None,
) -> BurdenDelta:
    """Return the burden delta for a draft URI vs. the prior-law baseline.

    V1 implementation: resolve the draft's affected provisions (via
    ``amends`` / ``references``), then aggregate the existing-law
    burden over that set (the ``before`` side of the delta). The
    ``after`` side is left ``None`` until ontology issue #214's data
    backfill populates draft-level ``normativeType`` edges — see the
    :class:`BurdenDelta` docstring.

    Args:
        draft_uri: A ``DraftLegislation`` URI. Empty / whitespace input
            yields an empty delta with ``affected_count=0``.
        graph_uri: The draft's Jena **named graph** URI (#855). When
            provided, the affected-provision lookup is GRAPH-scoped to
            that graph and addresses the ``<graph_uri>#self`` subject the
            graph builder actually wrote — the only shape that works for
            an *uploaded* draft (its triples never reach the default
            graph). When ``None`` (the legacy/default-graph path, used by
            callers that pre-merged the draft into the default graph and
            by the existing unit tests), the default-graph variant binds
            ``?draftUri`` to *draft_uri* as before. Must match the draft
            graph allowlist when supplied; an invalid value degrades to an
            empty delta rather than raising.
        sparql_client: Optional :class:`SparqlClient` override.

    Returns:
        A :class:`BurdenDelta`. A dead Jena degrades to an empty delta
        rather than a 500.
    """
    uri = (draft_uri or "").strip()
    if not uri:
        return BurdenDelta(before=_empty_summary(), after=None, affected_count=0)

    client = sparql_client if sparql_client is not None else SparqlClient()
    scoped_graph = (graph_uri or "").strip()
    try:
        if scoped_graph:
            # #855: GRAPH-scoped lookup against the draft's named graph,
            # addressing the ``#self`` subject. Validate the graph URI
            # before it is interpolated into the GRAPH clause (the same
            # allowlist the GSP transport + impact builders use; widened
            # for ``/v<n>`` in #849).
            from app.sync.jena_loader import _validate_graph_uri

            safe_graph = _validate_graph_uri(scoped_graph)
            rows = client.query(
                _build_draft_affected_provisions_graph_query(safe_graph),
                uri_bindings={"draftSelf": f"{safe_graph}#self"},
            )
        else:
            rows = client.query(
                _build_draft_affected_provisions_query(),
                uri_bindings={"draftUri": uri},
            )
    except Exception:
        logger.warning(
            "burden_delta_for_draft: affected-provision query failed for %r (graph=%r)",
            uri,
            scoped_graph or None,
            exc_info=True,
        )
        return BurdenDelta(before=_empty_summary(), after=None, affected_count=0)

    provisions = [
        (r.get("provision") or "").strip()
        for r in rows or []
        if (r.get("provision") or "").strip()
    ]
    # Dedup while preserving SPARQL order so the resulting summary's row
    # list is stable across reruns (deterministic UI).
    seen: set[str] = set()
    ordered: list[str] = []
    for p in provisions:
        if p in seen:
            continue
        seen.add(p)
        ordered.append(p)

    if not ordered:
        return BurdenDelta(before=_empty_summary(), after=None, affected_count=0)

    # #858: resolve the whole provision set in ONE SPARQL round-trip via
    # an inline ``VALUES ?provision { … }`` block (the v1 loop issued one
    # query per provision — up to ``_MAX_BURDEN_ROWS_PER_ACT`` sequential
    # calls). URIs that fail the strict allowlist are dropped here (they
    # would previously have errored inside the per-provision call and
    # contributed nothing — same outcome, no query churn).
    safe_uris = [u for u in ordered if _SAFE_PROVISION_URI_RE.fullmatch(u)]
    dropped = len(ordered) - len(safe_uris)
    if dropped:
        logger.warning(
            "burden_delta_for_draft: dropped %d non-URI affected entries for %r",
            dropped,
            uri,
        )

    raw_rows: list[dict[str, str]] = []
    if safe_uris:
        try:
            raw_rows = client.query(_build_provisions_burden_values_query(safe_uris))
        except Exception:
            logger.warning(
                "burden_delta_for_draft: batched burden query failed for %r (%d provisions)",
                uri,
                len(safe_uris),
                exc_info=True,
            )
            raw_rows = []

    # Client-side aggregation: restore the affected-provision order so
    # the summary's row list stays deterministic across reruns (the old
    # per-provision loop iterated ``ordered`` directly), then reuse the
    # existing dedupe/bucketing pipeline.
    position = {p: i for i, p in enumerate(ordered)}
    sorted_rows = sorted(
        raw_rows or [],
        key=lambda r: position.get((r.get("provision") or "").strip(), len(position)),
    )
    aggregated_rows = _rows_to_burden(sorted_rows)

    before = _summary_from_burden_rows(aggregated_rows, truncated=False)
    return BurdenDelta(before=before, after=None, affected_count=len(ordered))


# ---------------------------------------------------------------------------
# Bucketing — row → BurdenSummary
# ---------------------------------------------------------------------------


def bucket_burden_rows(rows: list[BurdenRow]) -> dict[BurdenKey, int]:
    """Return the per-deontic-key count of *rows*.

    Every key in :data:`BURDEN_LABELS_ET` is present in the result dict
    even when its count is ``0`` — the UI count grid renders all five
    cells uniformly.

    The bucketing key is :attr:`BurdenRow.burden_key`, which was already
    resolved at row-construction time via :func:`norm_type_key`. This
    helper is a pure aggregator so the UI / tests can re-count a filtered
    subset (e.g. "rows with a non-empty dutyHolder") without re-running
    SPARQL.

    Note: v1 does **not** group by target-group (``estleg:targetGroup``
    isn't in the ontology yet — see ontology issue #214). The
    ``dutyHolder`` literal is the v1 fallback and is surfaced separately
    via :func:`top_duty_holders` below.
    """
    counts: dict[BurdenKey, int] = dict.fromkeys(BURDEN_LABELS_ET.keys(), 0)  # type: ignore[arg-type]
    for r in rows or []:
        key = r.burden_key if r.burden_key in counts else "unknown"
        counts[key] = counts.get(key, 0) + 1
    return counts


def top_duty_holders(
    rows: list[BurdenRow],
    *,
    limit: int = _MAX_DUTY_HOLDER_BUCKETS,
) -> dict[str, int]:
    """Return top-*limit* ``dutyHolder`` literals + their row counts.

    The empty-string key (no dutyHolder set on the provision) is **kept**
    in the result dict — the UI can show "Märkimata: N" so the user
    knows how much of the act lacks the literal entirely.

    Long tail past *limit* is lumped into the bucket ``"Muud"`` so the
    UI stays scannable on a corpus act with dozens of distinct actors.

    Args:
        rows: The full :class:`BurdenRow` list (typically from a
            :class:`BurdenSummary`).
        limit: Cap on the number of explicit buckets; hard-capped to
            :data:`_MAX_DUTY_HOLDER_BUCKETS` so the UI never grows
            unbounded.
    """
    cap = max(1, min(limit, _MAX_DUTY_HOLDER_BUCKETS))
    counter: Counter[str] = Counter()
    for r in rows or []:
        counter[(r.duty_holder or "").strip()] += 1
    # Order by count desc, then alpha for stability — ``most_common`` is
    # already count-desc but ties are insertion-ordered, which can drift
    # across reruns.
    items = sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))

    # Pull empty-string ("Märkimata") + the top-N non-empty into the
    # explicit dict; lump the rest into "Muud".
    explicit: dict[str, int] = {}
    other_total = 0
    seen_non_empty = 0
    for key, count in items:
        if key == "":
            explicit[""] = count
            continue
        if seen_non_empty < cap:
            explicit[key] = count
            seen_non_empty += 1
        else:
            other_total += count
    if other_total > 0:
        explicit["Muud"] = other_total
    return explicit


# ---------------------------------------------------------------------------
# Internal — row dicts → BurdenRow / BurdenSummary
# ---------------------------------------------------------------------------


def _empty_summary() -> BurdenSummary:
    return BurdenSummary(
        counts=dict.fromkeys(BURDEN_LABELS_ET.keys(), 0),  # type: ignore[arg-type]
        rows=[],
        duty_holder_counts={},
        total=0,
        truncated=False,
    )


def _rows_to_burden(rows: list[dict[str, Any]]) -> list[BurdenRow]:
    """Convert SPARQL JSON binding rows into :class:`BurdenRow` instances.

    SPARQL may emit several rows per provision when ``normativeType``
    appears multiple times (some corpus provisions carry both a
    canonical individual *and* a literal echo). We dedupe by URI here
    and keep the first row whose ``norm_type_uri`` resolves to a known
    canonical key, falling back to the first row seen. ``dutyHolder``
    similarly may multi-row — we keep the first non-empty literal.
    """
    by_uri: dict[str, BurdenRow] = {}
    order: list[str] = []
    for row in rows or []:
        provision_uri = (row.get("provision") or "").strip()
        if not provision_uri:
            continue
        norm_raw = (row.get("normType") or "").strip()
        key_str = norm_type_key(norm_raw) if norm_raw else "unknown"
        # Narrow the str back to the BurdenKey Literal union — norm_type_key
        # only ever returns one of those five values, but its signature is
        # ``str`` (so callers can pass anything in). Cast in one place.
        key: BurdenKey = key_str if key_str in BURDEN_LABELS_ET else "unknown"  # type: ignore[assignment]
        # Canonicalise the URI form for the dataclass field: if the
        # bucketing matched a canonical NORM_TYPE individual, store its
        # URI; else store the raw value verbatim (could be a literal).
        canonical_uri = NORM_TYPE_INDIVIDUALS[key] if key in NORM_TYPE_KEYS else norm_raw
        new_row = BurdenRow(
            provision_uri=provision_uri,
            provision_label=(row.get("provisionLabel") or "").strip(),
            act_uri=(row.get("act") or "").strip(),
            act_label=(row.get("actLabel") or "").strip(),
            norm_type_uri=canonical_uri,
            burden_key=key,
            duty_holder=(row.get("dutyHolder") or "").strip(),
        )
        existing = by_uri.get(provision_uri)
        if existing is None:
            by_uri[provision_uri] = new_row
            order.append(provision_uri)
            continue
        # Upgrade the existing row when the new one carries a
        # better-classified key, or fills in a missing dutyHolder /
        # label / act fields.
        upgraded = _merge_rows(existing, new_row)
        by_uri[provision_uri] = upgraded
    return [by_uri[u] for u in order]


def _merge_rows(existing: BurdenRow, new: BurdenRow) -> BurdenRow:
    """Merge two rows for the same provision, preferring the classified one."""
    # Prefer the row with a known bucket key over "unknown".
    if existing.burden_key == "unknown" and new.burden_key != "unknown":
        primary, secondary = new, existing
    else:
        primary, secondary = existing, new
    return BurdenRow(
        provision_uri=primary.provision_uri,
        provision_label=primary.provision_label or secondary.provision_label,
        act_uri=primary.act_uri or secondary.act_uri,
        act_label=primary.act_label or secondary.act_label,
        norm_type_uri=primary.norm_type_uri or secondary.norm_type_uri,
        burden_key=primary.burden_key if primary.burden_key != "unknown" else secondary.burden_key,
        duty_holder=primary.duty_holder or secondary.duty_holder,
    )


def _summary_from_rows(rows: list[dict[str, Any]]) -> BurdenSummary:
    """Build a :class:`BurdenSummary` from SPARQL row dicts."""
    burden_rows = _rows_to_burden(rows)
    truncated = len(rows or []) >= _MAX_BURDEN_ROWS_PER_ACT
    return _summary_from_burden_rows(burden_rows, truncated=truncated)


def _summary_from_burden_rows(rows: list[BurdenRow], *, truncated: bool) -> BurdenSummary:
    """Build a :class:`BurdenSummary` from already-constructed :class:`BurdenRow`s."""
    return BurdenSummary(
        counts=bucket_burden_rows(rows),
        rows=rows,
        duty_holder_counts=top_duty_holders(rows),
        total=len(rows),
        truncated=truncated,
    )
