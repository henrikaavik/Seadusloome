"""Resolve :class:`ExtractedRef` objects to Estonian Legal Ontology URIs.

Entity extraction (``entity_extractor.py``) gives us a list of raw
references Claude spotted in the draft. This module takes each one
and asks Jena whether the ontology knows about it.

Strategy per ref_type (spec §6.1):

    law            exact short-name match from a cached SPARQL
                   dictionary (``"KarS"`` -> ``estleg:karistusseadustik``);
                   fall back to ``difflib.SequenceMatcher`` fuzzy match
                   against every law's ``rdfs:label`` + ``estleg:shortName``.
    provision      exact string match on ``estleg:paragrahv`` literal;
                   whitespace-normalise and retry if the first pass misses.
    eu_act         CELEX regex match then SPARQL lookup on
                   ``estleg:celexNumber``.
    court_decision exact match on ``estleg:caseNumber``.
    concept        exact ``rdfs:label`` match on ``estleg:LegalConcept``.

A dead Jena must NOT crash the extraction pipeline: every SPARQL call
is guarded, and on failure the ref returns with ``entity_uri=None``
and a warning is logged.

Phase 3 may want to tighten the fuzzy threshold, add EstBERT
embeddings for concept matching, and cache the law short-name dict in
Redis. Today we keep the dict in a module-level global loaded lazily
on first use; the worker thread lives long enough for that to pay off.
"""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass
from difflib import SequenceMatcher

from app.docs.entity_extractor import ExtractedRef
from app.ontology.queries import PREFIXES
from app.ontology.sparql_client import SparqlClient

logger = logging.getLogger(__name__)


# Minimum fuzzy ratio we accept as a "match". 0.7 keeps "KarS" -> real
# KarS URI but rejects "TsÜS" -> "KarS" typos.
_FUZZY_THRESHOLD = 0.7


# CELEX numbers look like ``32016R0679`` or ``32019L0790``:
# 1-digit sector + 4-digit year + single letter (R/L/D/A etc.) + 1-4
# digits. See https://eur-lex.europa.eu/content/help/faq/celex-number.html
_CELEX_RE = re.compile(r"\b(\d{5}[A-Z]\d{1,4})\b")


@dataclass(frozen=True)
class ResolvedRef:
    """A resolver output bundling the extracted ref with its match.

    Attributes:
        extracted: The original :class:`ExtractedRef` we tried to
            resolve. Kept alongside the match so persistence code
            only has to iterate one list.
        entity_uri: The matched ontology URI, or ``None`` when the
            resolver couldn't find anything (unmatched reference —
            still persisted so the UI can surface it).
        matched_label: The ``rdfs:label`` we matched against, or
            ``None`` when the lookup was literal (provision, CELEX).
        match_score: ``1.0`` for exact matches, ``0.0..<1.0`` for
            fuzzy matches, ``0.0`` for unresolved refs.
    """

    extracted: ExtractedRef
    entity_uri: str | None
    matched_label: str | None
    match_score: float


# ---------------------------------------------------------------------------
# Resolver class
# ---------------------------------------------------------------------------


class ReferenceResolver:
    """SPARQL-backed matcher for extracted legal references.

    Instances hold a :class:`SparqlClient` and lazily build a cache of
    all law short names on first resolve call. The cache lives for the
    lifetime of the instance — the worker thread keeps a single
    resolver per draft, so one SPARQL roundtrip per draft is an
    acceptable warm-up cost.
    """

    def __init__(self, sparql_client: SparqlClient | None = None) -> None:
        self._sparql = sparql_client if sparql_client is not None else SparqlClient()
        # ``_law_dict`` maps normalised short name -> (uri, label).
        # ``None`` means "not loaded yet"; empty dict means "loaded,
        # but Jena returned nothing" so we don't re-query every call.
        self._law_dict: dict[str, tuple[str, str]] | None = None

    # -- public API ---------------------------------------------------------

    def resolve(self, refs: list[ExtractedRef]) -> list[ResolvedRef]:
        """Resolve a list of refs to ontology URIs (or ``None``)."""
        return [self._resolve_one(ref) for ref in refs]

    # -- per-type dispatch --------------------------------------------------

    def _resolve_one(self, ref: ExtractedRef) -> ResolvedRef:
        try:
            if ref.ref_type == "law":
                return self._resolve_law(ref)
            if ref.ref_type == "provision":
                return self._resolve_provision(ref)
            if ref.ref_type == "eu_act":
                return self._resolve_eu_act(ref)
            if ref.ref_type == "court_decision":
                return self._resolve_court_decision(ref)
            if ref.ref_type == "concept":
                return self._resolve_concept(ref)
        except Exception as exc:  # noqa: BLE001 — never crash on one bad ref
            logger.warning(
                "resolve: ref_type=%s ref_text=%r raised %s; returning unresolved",
                ref.ref_type,
                ref.ref_text,
                exc,
            )
            return _unresolved(ref)

        logger.debug("resolve: unknown ref_type=%r, marking unresolved", ref.ref_type)
        return _unresolved(ref)

    # -- law ----------------------------------------------------------------

    def _resolve_law(self, ref: ExtractedRef) -> ResolvedRef:
        law_dict = self._get_law_dict()
        if not law_dict:
            return _unresolved(ref)

        key = _normalise_law_name(ref.ref_text)

        # 1) Exact short-name match.
        exact = law_dict.get(key)
        if exact is not None:
            uri, label = exact
            return ResolvedRef(
                extracted=ref,
                entity_uri=uri,
                matched_label=label,
                match_score=1.0,
            )

        # 2) Fuzzy fallback via difflib against every known name.
        best_uri: str | None = None
        best_label: str | None = None
        best_score = 0.0
        for candidate_key, (uri, label) in law_dict.items():
            score = SequenceMatcher(None, key, candidate_key).ratio()
            if score > best_score:
                best_score = score
                best_uri = uri
                best_label = label

        if best_uri is not None and best_score >= _FUZZY_THRESHOLD:
            return ResolvedRef(
                extracted=ref,
                entity_uri=best_uri,
                matched_label=best_label,
                match_score=round(best_score, 3),
            )
        return _unresolved(ref)

    def _get_law_dict(self) -> dict[str, tuple[str, str]]:
        """Return the cached ``{normalised_name: (uri, label)}`` dict.

        Lazily loads on first call. On Jena failure returns an empty
        dict and logs a warning — subsequent calls will re-attempt the
        load (unlike a populated cache, an empty cache is treated as
        "not yet loaded" on purpose so transient outages can recover).
        """
        if self._law_dict:
            return self._law_dict

        sparql = (
            PREFIXES
            + """
            SELECT ?uri ?shortName ?fullName WHERE {
              ?uri a estleg:Law .
              OPTIONAL { ?uri estleg:shortName ?shortName }
              OPTIONAL { ?uri rdfs:label ?fullName }
            }
            """
        )
        try:
            rows = self._sparql.query(sparql)
        except Exception as exc:  # noqa: BLE001
            logger.warning("resolve: law dict SPARQL load failed: %s", exc)
            return {}

        out: dict[str, tuple[str, str]] = {}
        for row in rows:
            uri = row.get("uri", "")
            if not uri:
                continue
            full_name = row.get("fullName", "")
            short_name = row.get("shortName", "")
            # Index by BOTH short and full name, normalised.
            if short_name:
                out[_normalise_law_name(short_name)] = (uri, full_name or short_name)
            if full_name:
                out[_normalise_law_name(full_name)] = (uri, full_name)

        self._law_dict = out
        logger.info("resolve: loaded %d law short-name entries from Jena", len(out))
        return out

    # -- provision ----------------------------------------------------------

    def _resolve_provision(self, ref: ExtractedRef) -> ResolvedRef:
        # Attempt 1: exact match on the raw text as it came from the LLM.
        hit = self._query_provision(ref.ref_text)
        if hit is not None:
            uri, label = hit
            return ResolvedRef(
                extracted=ref,
                entity_uri=uri,
                matched_label=label,
                match_score=1.0,
            )

        # Attempt 2: normalise whitespace (collapse runs of spaces,
        # trim) and retry. This catches the common case where the LLM
        # inserts a non-breaking space or a stray newline.
        normalised = _normalise_whitespace(ref.ref_text)
        if normalised != ref.ref_text:
            hit = self._query_provision(normalised)
            if hit is not None:
                uri, label = hit
                return ResolvedRef(
                    extracted=ref,
                    entity_uri=uri,
                    matched_label=label,
                    match_score=1.0,
                )

        return _unresolved(ref)

    def _query_provision(self, paragrahv_literal: str) -> tuple[str, str] | None:
        sparql = (
            PREFIXES
            + """
            SELECT ?uri ?paragrahv WHERE {
              ?uri estleg:paragrahv ?paragrahv .
            }
            LIMIT 1
            """
        )
        try:
            rows = self._sparql.query(sparql, bindings={"paragrahv": paragrahv_literal})
        except Exception as exc:  # noqa: BLE001
            logger.warning("resolve: provision SPARQL failed: %s", exc)
            return None
        if not rows:
            return None
        row = rows[0]
        uri = row.get("uri")
        if not uri:
            return None
        return (uri, row.get("paragrahv", paragrahv_literal))

    # -- EU act -------------------------------------------------------------

    def _resolve_eu_act(self, ref: ExtractedRef) -> ResolvedRef:
        match = _CELEX_RE.search(ref.ref_text)
        if match is None:
            return _unresolved(ref)
        celex = match.group(1)

        sparql = (
            PREFIXES
            + """
            SELECT ?uri ?label WHERE {
              ?uri a estleg:EULegislation ;
                   estleg:celexNumber ?celex .
              OPTIONAL { ?uri rdfs:label ?label }
            }
            LIMIT 1
            """
        )
        try:
            rows = self._sparql.query(sparql, bindings={"celex": celex})
        except Exception as exc:  # noqa: BLE001
            logger.warning("resolve: eu_act SPARQL failed: %s", exc)
            return _unresolved(ref)
        if not rows:
            return _unresolved(ref)
        row = rows[0]
        uri = row.get("uri")
        if not uri:
            return _unresolved(ref)
        return ResolvedRef(
            extracted=ref,
            entity_uri=uri,
            matched_label=row.get("label") or celex,
            match_score=1.0,
        )

    # -- court decision -----------------------------------------------------

    def _resolve_court_decision(self, ref: ExtractedRef) -> ResolvedRef:
        # Case numbers follow patterns like ``3-1-1-63-15`` (Riigikohus)
        # or CJEU ``C-123/20``; we just hand the raw text to SPARQL and
        # let the literal match succeed-or-fail.
        case_number = ref.ref_text.strip()
        sparql = (
            PREFIXES
            + """
            SELECT ?uri ?label WHERE {
              ?uri estleg:caseNumber ?caseNumber .
              OPTIONAL { ?uri rdfs:label ?label }
            }
            LIMIT 1
            """
        )
        try:
            rows = self._sparql.query(sparql, bindings={"caseNumber": case_number})
        except Exception as exc:  # noqa: BLE001
            logger.warning("resolve: court_decision SPARQL failed: %s", exc)
            return _unresolved(ref)
        if not rows:
            return _unresolved(ref)
        row = rows[0]
        uri = row.get("uri")
        if not uri:
            return _unresolved(ref)
        return ResolvedRef(
            extracted=ref,
            entity_uri=uri,
            matched_label=row.get("label") or case_number,
            match_score=1.0,
        )

    # -- concept ------------------------------------------------------------

    def _resolve_concept(self, ref: ExtractedRef) -> ResolvedRef:
        sparql = (
            PREFIXES
            + """
            SELECT ?uri ?label WHERE {
              ?uri a estleg:LegalConcept ;
                   rdfs:label ?label .
            }
            LIMIT 1
            """
        )
        try:
            rows = self._sparql.query(sparql, bindings={"label": ref.ref_text.strip()})
        except Exception as exc:  # noqa: BLE001
            logger.warning("resolve: concept SPARQL failed: %s", exc)
            return _unresolved(ref)
        if not rows:
            return _unresolved(ref)
        row = rows[0]
        uri = row.get("uri")
        if not uri:
            return _unresolved(ref)
        return ResolvedRef(
            extracted=ref,
            entity_uri=uri,
            matched_label=row.get("label") or ref.ref_text,
            match_score=1.0,
        )


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _unresolved(ref: ExtractedRef) -> ResolvedRef:
    return ResolvedRef(
        extracted=ref,
        entity_uri=None,
        matched_label=None,
        match_score=0.0,
    )


def _normalise_law_name(name: str) -> str:
    """Collapse whitespace and lowercase for case-insensitive matching."""
    return re.sub(r"\s+", " ", name).strip().lower()


def _normalise_whitespace(text: str) -> str:
    """Replace NBSP, tabs and newlines with plain spaces and collapse runs."""
    cleaned = text.replace("\u00a0", " ")  # non-breaking space
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip()


# ---------------------------------------------------------------------------
# Singleton + public convenience wrapper
# ---------------------------------------------------------------------------


_default_resolver: ReferenceResolver | None = None
# #453: protect singleton init from concurrent worker threads.
_default_resolver_lock = threading.Lock()


def get_default_resolver() -> ReferenceResolver:
    """Return the process-wide default :class:`ReferenceResolver`.

    The singleton lets the law short-name dict survive across
    ``extract_entities`` jobs in a single worker process, saving one
    SPARQL roundtrip per draft after the first warm-up.

    Uses double-checked locking (#453) so two extract jobs landing
    on a fresh process at the same time can't both pay the warm-up
    cost (and risk producing two resolvers with subtly different
    cached state).
    """
    global _default_resolver
    if _default_resolver is None:
        with _default_resolver_lock:
            if _default_resolver is None:
                _default_resolver = ReferenceResolver()
    return _default_resolver


def resolve_refs(refs: list[ExtractedRef]) -> list[ResolvedRef]:
    """Top-level helper used by the extract_entities job handler.

    Delegates to :func:`get_default_resolver`. Kept as a bare function
    so tests can patch ``app.docs.reference_resolver.resolve_refs``
    without having to reach into the class.
    """
    return get_default_resolver().resolve(refs)
