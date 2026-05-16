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
from collections import Counter
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
        act_uri: The Act URI (best-effort; may be empty for orphan
            sandbox provisions). Pulled via ``estleg:partOf`` /
            ``estleg:sourceAct``.
        act_label: ``rdfs:label`` on the act.
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
# We OPTIONAL every field except ``provision`` because the corpus'
# completeness varies — many provisions have a normativeType but no
# dutyHolder, and a small minority have neither (kept in the
# "Liigitamata" bucket so the count grid is honest).


def _build_act_burden_query() -> str:
    """Return the act-level burden SPARQL.

    Joins via ``estleg:partOf`` *or* ``estleg:sourceAct`` (the corpus
    uses both for the act↔provision membership relation) so we don't
    silently miss provisions in one shape or the other.
    """
    return (
        PREFIXES
        + f"""
SELECT ?provision ?provisionLabel ?act ?actLabel ?normType ?dutyHolder
WHERE {{
  {{ ?provision estleg:partOf ?act . }}
  UNION
  {{ ?provision estleg:sourceAct ?act . }}
  OPTIONAL {{ ?provision rdfs:label ?provisionLabel }}
  OPTIONAL {{ ?act rdfs:label ?actLabel }}
  OPTIONAL {{ ?provision <{PREDICATES.NORMATIVE_TYPE}> ?normType }}
  OPTIONAL {{ ?provision <{PREDICATES.DUTY_HOLDER}> ?dutyHolder }}
}}
ORDER BY ?provision
LIMIT {_MAX_BURDEN_ROWS_PER_ACT}
"""
    )


def _build_provision_burden_query() -> str:
    """Return the provision-level burden SPARQL (single-row OPTIONAL fan-out)."""
    return (
        PREFIXES
        + f"""
SELECT ?provision ?provisionLabel ?act ?actLabel ?normType ?dutyHolder
WHERE {{
  OPTIONAL {{ ?provision rdfs:label ?provisionLabel }}
  OPTIONAL {{
    {{ ?provision estleg:partOf ?act . }}
    UNION
    {{ ?provision estleg:sourceAct ?act . }}
    OPTIONAL {{ ?act rdfs:label ?actLabel }}
  }}
  OPTIONAL {{ ?provision <{PREDICATES.NORMATIVE_TYPE}> ?normType }}
  OPTIONAL {{ ?provision <{PREDICATES.DUTY_HOLDER}> ?dutyHolder }}
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


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def list_burden_for_act(
    act_uri: str,
    *,
    sparql_client: SparqlClient | None = None,
) -> BurdenSummary:
    """Return the deontic-classified rows + counts for every provision of *act_uri*.

    Walks ``?provision estleg:partOf <act_uri>`` (or ``sourceAct``) and
    projects ``rdfs:label``, ``estleg:normativeType``, ``estleg:dutyHolder``
    for each member provision.

    Args:
        act_uri: The Act URI. Empty / whitespace input yields an empty
            :class:`BurdenSummary` (no SPARQL hit).
        sparql_client: Optional :class:`SparqlClient` override (tests
            inject a mocked one).

    Returns:
        A :class:`BurdenSummary` — counts dict, full row list, top-N
        ``dutyHolder`` distribution, total, and a ``truncated`` flag.
        A dead Jena / any SPARQL error degrades to an empty summary
        rather than a 500.
    """
    uri = (act_uri or "").strip()
    if not uri:
        return _empty_summary()

    client = sparql_client if sparql_client is not None else SparqlClient()
    try:
        rows = client.query(
            _build_act_burden_query(),
            uri_bindings={"act": uri},
        )
    except Exception:
        logger.warning("list_burden_for_act: SPARQL query failed for %r", uri, exc_info=True)
        return _empty_summary()

    return _summary_from_rows(rows)


def list_burden_for_provision(
    provision_uri: str,
    *,
    sparql_client: SparqlClient | None = None,
) -> BurdenSummary:
    """Return the deontic-classified single-row summary for a provision URI.

    Same shape as :func:`list_burden_for_act` but for a single provision.
    Useful when the user's input resolves to a §-reference rather than an
    act / draft — the count grid then shows ``1`` in exactly one bucket
    and ``0`` in the rest.
    """
    uri = (provision_uri or "").strip()
    if not uri:
        return _empty_summary()

    client = sparql_client if sparql_client is not None else SparqlClient()
    try:
        rows = client.query(
            _build_provision_burden_query(),
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
        sparql_client: Optional :class:`SparqlClient` override.

    Returns:
        A :class:`BurdenDelta`. A dead Jena degrades to an empty delta
        rather than a 500.
    """
    uri = (draft_uri or "").strip()
    if not uri:
        return BurdenDelta(before=_empty_summary(), after=None, affected_count=0)

    client = sparql_client if sparql_client is not None else SparqlClient()
    try:
        rows = client.query(
            _build_draft_affected_provisions_query(),
            uri_bindings={"draftUri": uri},
        )
    except Exception:
        logger.warning(
            "burden_delta_for_draft: affected-provision query failed for %r",
            uri,
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

    # For v1 we batch the per-provision lookups — one SPARQL call per
    # affected provision. The cap inside the affected-provisions query
    # already limits this to ``_MAX_BURDEN_ROWS_PER_ACT`` calls in the
    # worst case; corpus drafts touch ~10-100 provisions in practice,
    # which stays well below any timeout budget.
    aggregated_rows: list[BurdenRow] = []
    for p_uri in ordered:
        sub = list_burden_for_provision(p_uri, sparql_client=client)
        aggregated_rows.extend(sub.rows)

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
