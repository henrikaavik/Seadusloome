"""Resolve :class:`ExtractedRef` objects to Estonian Legal Ontology URIs.

Entity extraction (``entity_extractor.py``) gives us a list of raw
references Claude spotted in the draft. This module takes each one
and asks Jena whether the ontology knows about it.

Strategy per ref_type (revised 2026-05-18 per spike findings in
``docs/2026-05-18-bugfix-plan.md`` — supersedes the original §6.1 spec):

    law            normalise (strip explicit legal-reference suffixes
                   like ``seadus``/``seadustik``); look up against an
                   ontology-derived abbreviation map built from
                   ``LegalProvision_<TOKEN>`` rdf:type subclasses
                   paired with the most-frequent ``estleg:sourceAct``
                   literal among each subclass's members. Fall back to
                   fuzzy match (``difflib.SequenceMatcher`` ≥ 0.7).
                   **The act identifier returned is the literal title
                   string** — the corpus has no act URIs.

    provision      decompose ``<act-name-or-abbrev> § <num>[ lg <m>][ p <k>]``
                   into act + section; resolve act via the abbreviation
                   map; try a cheap URI-guess fast path
                   (``ASK { <estleg:TOKEN_Par_N> ?p ?o }``); fall back
                   to a single-arm structural SPARQL keyed on
                   ``estleg:sourceAct`` (literal) and ``estleg:paragrahv``
                   matching BOTH ``"§ N."`` and ``"§ N"`` forms.

                   IMPORTANT: when the act resolves but the section
                   does not, return a distinct ``partial_match`` state
                   instead of silently collapsing to the act.

    eu_act         uppercase + strip whitespace; CELEX regex
                   (``re.IGNORECASE``); SPARQL lookup on
                   ``estleg:celexNumber``.

    court_decision exact match on ``estleg:caseNumber``, case-sensitive.
    concept        case-folded ``rdfs:label`` match on ``estleg:LegalConcept``.

A dead Jena must NOT crash the extraction pipeline: every SPARQL call
is guarded, and on failure the ref returns with ``entity_uri=None``
and a structured log line is emitted.

Privacy: ref texts come from pre-publication drafts. Miss-logs never
contain raw ``ref_text`` — only an HMAC-truncated identifier derived
via ``RESOLVER_REF_HASH_SECRET``. See the plan's "Add diagnostic
logging" subsection.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import threading
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any

from app.config import is_stub_allowed
from app.docs.entity_extractor import ExtractedRef
from app.ontology.queries import ESTLEG_NS, PREFIXES
from app.ontology.sparql_client import SparqlClient

logger = logging.getLogger(__name__)


# Minimum fuzzy ratio we accept as a "match". 0.7 keeps "KarS" -> real
# KarS URI but rejects "TsÜS" -> "KarS" typos.
_FUZZY_THRESHOLD = 0.7


# CELEX numbers look like ``32016R0679`` or ``32019L0790``:
# 1-digit sector + 4-digit year + single letter (R/L/D/A etc.) + 1-4
# digits. ``re.IGNORECASE`` so a user pasting ``32016r0679`` still
# matches at the regex step; we uppercase the captured group before
# binding into SPARQL (belt-and-braces with the input-string uppercase
# at the call site).
_CELEX_RE = re.compile(r"\b(\d{5}[A-Z]\d{1,4})\b", re.IGNORECASE)


# Explicit legal-reference suffixes the resolver may strip from a law
# name token. **Do not** widen this to generic Estonian case suffixes
# (-e/-i/-st/-ks/-s/-le) — that creates false positives like
# ``karistusseaduselt`` collapsing to ``karistusseadus``. Order longest
# first so re.sub matches greedy.
_LAW_SUFFIX_RE = re.compile(
    r"(seadustikus|seadustiku|seadustik"
    r"|seaduseni|seadusest|seaduses|seaduse|seadus)\b",
    re.IGNORECASE,
)


# Decomposition of provision references. Captures:
#   1. act half (everything before the §/paragrahv marker)
#   2. section number (digits only)
#   3. optional lõige number
#   4. optional punkt number
#
# The Estonian inflection space is wide: ``lõige`` (nom.), ``lõike``
# (gen./part.), ``lõikest`` (elative), ``lõikele`` (allative), the
# bare abbrev ``lg``. Likewise for ``punkt`` → ``punkti``,
# ``punktist``, ``p``. We use loose stems (``l(õi|oi|õ|o)[gk]`` for
# lõige; ``p(unkt[…])?``) and then allow any letters before the
# digit.
_PROVISION_RE = re.compile(
    r"""
    ^\s*
    (?P<act>.+?)                                     # act half (lazy)
    \s+
    (?:§(?:-s|-st|-le|-ni)?|paragrahv[ia]?|paragrahvist|paragrahvile)
    \s*
    (?P<num>\d+)                                     # section number
    (?:
        \s*
        (?:lg|l(?:õi|oi|õ|o)[gk][a-zõöäü]*)         # lg / lõige / lõike / lõikest …
        \s*
        (?P<lg>\d+)
    )?
    (?:
        \s*
        (?:p|punkt[a-zõöäü]*)                       # p / punkt / punkti / punktist …
        \s*
        (?P<p>\d+)
    )?
    \s*$
    """,
    re.IGNORECASE | re.VERBOSE,
)


# Env var that holds the HMAC secret for miss-log identifiers.
_REF_HASH_SECRET_ENV = "RESOLVER_REF_HASH_SECRET"


# Human-facing Estonian legal abbreviations that users actually type
# (``KarS``, ``AvTS``, ``TLS``, …). The ontology-derived abbreviation
# map only knows the corpus's internal TOKENs (``KRIMIN``, ``ATMOSF``,
# ``RIIGIL_2``, etc., per the Step 1 spike in
# ``docs/2026-05-18-bugfix-plan.md``), so an alias map is the only way
# a paste like ``KarS § 211`` lands on the right URI.
#
# Keys are lowercased + ASCII-folded user input (so ``KarS``,
# ``kars``, and ``KARS`` all hit the same row, and ``VõS`` matches via
# both ``võs`` and ``vos``). Values are the corpus TOKEN.
#
# Each row is annotated with whether it has been verified against the
# prod abbreviation map (built from ``LegalProvision_<TOKEN>`` rdf:type
# rows + most-frequent ``sourceAct`` literal). Verified entries cite
# the prod-confirmed token + canonical title in the comment. Aliases
# that point at tokens not yet verified against prod are intentionally
# **omitted** rather than guessed — a wrong alias is worse than no
# alias because it silently routes the resolver to the wrong act.
#
# Sources for the verified entries:
#   - Step 1 spike output (``docs/2026-05-18-bugfix-plan.md`` lines
#     230-232, 312-314), which enumerated real prod TOKENs:
#     ``KRIMIN``, ``ATMOSF``, ``RIIGIL_2``, ``KAITSE_3``, ``VTMS``,
#     ``VANGIS``, ``VPTS``, ``TMS``, ``KINDLU``, ``VALISM``, ``AVTS``,
#     ``REELS``.
#   - The existing test corpus
#     (``tests/test_docs_reference_resolver.py``) which exercises
#     ``AVTS`` and ``KRIMIN`` end-to-end against the abbreviation map.
_HUMAN_ABBREV_ALIASES: dict[str, str] = {
    # user-facing shortcut → corpus TOKEN
    # ---- VERIFIED against the Step 1 spike's enumerated tokens ----
    "kars": "KRIMIN",  # Karistusseadustik (prod TOKEN KRIMIN, confirmed by spike)
    "avts": "AVTS",  # Avaliku teabe seadus (prod TOKEN AVTS, confirmed by spike)
    "res": "REELS",  # Riigieelarve seadus (prod TOKEN REELS, confirmed by spike)
    "aõks": "ATMOSF",  # Atmosfääriõhu kaitse seadus (prod TOKEN ATMOSF, spike)
    "aoks": "ATMOSF",  # asciified form
    "vtms": "VTMS",  # Väärteomenetluse seadustik (prod TOKEN VTMS, spike)
    "vangis": "VANGIS",  # Vangistusseadus (prod TOKEN VANGIS, spike)
    "vpts": "VPTS",  # Väärtpaberituru seadus (prod TOKEN VPTS, spike)
    "tms": "TMS",  # Täitemenetluse seadustik (prod TOKEN TMS, spike)
    "kindls": "KINDLU",  # Kindlustustegevuse seadus (prod TOKEN KINDLU, spike)
    "kindlus": "KINDLU",  # alternate user-facing form
    "vsms": "VALISM",  # Välismaalaste seadus (prod TOKEN VALISM, spike)
    # NOTE: human-friendly abbreviations whose prod TOKEN we have NOT
    # confirmed from the spike (e.g. ``TLS`` → tööleping?, ``KMS`` →
    # kaubamärgi vs. käibemaksu?, ``VõS``, ``TsÜS``, ``HKMS``,
    # ``ÄS``, ``KMS``, ``ASjaõS``) are intentionally OMITTED here —
    # adding them without prod verification risks silently routing
    # the resolver to the wrong act. A follow-up to extend this map
    # should run the same probe (count ``LegalProvision_<TOKEN>``
    # rows + their canonical ``sourceAct`` literals) and add each
    # confirmed pair as a new row with the spike-citation comment.
}


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
            fuzzy matches, ``0.0`` for unresolved refs, ``0.5`` for
            partial (act-only) provision matches.
        partial_match: Set when the act half of a provision reference
            resolves but the section does not. Carries
            ``{"act_token": str | None, "act_title": str, "section": str}``.
            Downstream impact code MUST check this explicitly before
            treating ``entity_uri is None`` as "fully unresolved".
    """

    extracted: ExtractedRef
    entity_uri: str | None
    matched_label: str | None
    match_score: float
    partial_match: dict[str, Any] | None = field(default=None)


# ---------------------------------------------------------------------------
# Resolver class
# ---------------------------------------------------------------------------


class ReferenceResolver:
    """SPARQL-backed matcher for extracted legal references.

    Instances hold a :class:`SparqlClient` and lazily build a cache of
    the abbreviation map on first resolve call. The cache lives for the
    lifetime of the instance — the worker thread keeps a single
    resolver per draft, so one SPARQL roundtrip per draft is an
    acceptable warm-up cost.
    """

    def __init__(self, sparql_client: SparqlClient | None = None) -> None:
        self._sparql = sparql_client if sparql_client is not None else SparqlClient()
        # Abbreviation map: TOKEN (e.g. "KRIMIN", "ATMOSF", "AVTS")
        # → canonical title literal (e.g. "Karistusseadustik").
        # ``None`` means "not loaded yet"; empty dict means "loaded
        # successfully but Jena returned nothing" — populated dicts
        # are not re-queried.
        self._token_to_title: dict[str, str] | None = None
        # Reverse lookup: normalised title → TOKEN. Built alongside
        # ``_token_to_title``.
        self._title_to_token: dict[str, str] = {}
        # Cache of all distinct sourceAct title literals seen in the
        # corpus, normalised. Used for fuzzy matching when neither
        # token nor exact title hits. Set alongside the maps.
        self._normalised_titles: dict[str, str] = {}
        self._map_lock = threading.Lock()

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
                "resolve: ref_type=%s ref_id=%s raised %s; returning unresolved",
                ref.ref_type,
                _ref_id(ref.ref_text),
                exc,
            )
            return _unresolved(ref)

        logger.debug("resolve: unknown ref_type=%r, marking unresolved", ref.ref_type)
        return _unresolved(ref)

    # -- law ----------------------------------------------------------------

    def _resolve_law(self, ref: ExtractedRef) -> ResolvedRef:
        """Resolve a law-only reference (act name with no ``§ N``).

        The corpus contains no act-level URIs (Step 1 spike — see
        ``docs/2026-05-18-bugfix-plan.md``). So a law-only ref is
        **structurally a partial match**, not a full resolution:

          * ``entity_uri`` is always ``None`` (it would otherwise hold a
            non-URI literal that downstream RDF serialisation would
            mistake for an absolute IRI — the original P1#1 bug).
          * The canonical title literal + optional TOKEN ride along on
            ``partial_match`` in the same shape ``_resolve_provision``
            uses for "act resolved, section not found", with
            ``section=None`` to mark "no section was even asked for".

        Downstream code (graph_builder.py:121-211) already handles the
        partial_match state — it emits an ``estleg:referencesAct``
        literal triple instead of an ``estleg:references`` URI edge,
        so the impact engine's BFS does not try to fan out from a
        string literal masquerading as a URI.

        Lookup order:
          1. Human-abbreviation alias map (``KarS`` → ``KRIMIN``).
          2. Exact corpus TOKEN match (``KRIMIN`` → ``Karistusseadustik``).
          3. Exact title match (normalised).
          4. Fuzzy fallback on normalised titles (``SequenceMatcher``).
        """
        token_map, title_map, _ = self._get_abbrev_maps()
        if not token_map and not title_map:
            self._log_miss(ref, tried_keys=[], candidates=0)
            return _unresolved(ref)

        normalised = _normalise_law_name(ref.ref_text)
        # Token lookup is case-insensitive over the upper-cased form
        # (TOKENs are conventionally uppercase like ``AVTS``, ``KRIMIN``).
        token_key = normalised.upper().replace(" ", "")

        # 0) Human-abbreviation alias map (``KarS`` → ``KRIMIN``).
        #    Checked first because the corpus TOKENs don't carry the
        #    user-facing shortcuts that practitioners actually type.
        alias_token = _HUMAN_ABBREV_ALIASES.get(normalised.replace(" ", ""))
        if alias_token and alias_token in token_map:
            title = token_map[alias_token]
            return _law_partial(ref, token=alias_token, title=title, score=1.0)

        # 1) Exact token match.
        if token_key in token_map:
            title = token_map[token_key]
            return _law_partial(ref, token=token_key, title=title, score=1.0)

        # 2) Exact title match (normalised).
        if normalised in title_map:
            title = title_map[normalised]
            token = self._title_to_token.get(normalised)
            return _law_partial(ref, token=token, title=title, score=1.0)

        # 3) Fuzzy fallback on normalised titles.
        best_title: str | None = None
        best_key: str | None = None
        best_score = 0.0
        for candidate_key, title in title_map.items():
            score = SequenceMatcher(None, normalised, candidate_key).ratio()
            if score > best_score:
                best_score = score
                best_title = title
                best_key = candidate_key

        if best_title is not None and best_score >= _FUZZY_THRESHOLD:
            token = self._title_to_token.get(best_key or "")
            return _law_partial(
                ref,
                token=token,
                title=best_title,
                score=round(best_score, 3),
            )

        self._log_miss(
            ref,
            tried_keys=[token_key, normalised],
            candidates=len(title_map),
        )
        return _unresolved(ref)

    def _resolve_law_half(self, act_text: str) -> tuple[str | None, str | None, float]:
        """Resolve the act half of a provision reference.

        Returns ``(token, title, score)``:
            - ``token`` is the abbreviation TOKEN (e.g. ``"AVTS"``) or
              ``None`` when only a fuzzy title match landed.
            - ``title`` is the canonical literal title or ``None`` when
              nothing matched.
            - ``score`` is ``1.0`` for exact, ``<1.0`` for fuzzy,
              ``0.0`` for miss.

        Lookup order mirrors :meth:`_resolve_law` so a provision like
        ``KarS § 211`` and a bare law reference ``KarS`` route through
        the same alias map. See :data:`_HUMAN_ABBREV_ALIASES`.
        """
        token_map, title_map, _ = self._get_abbrev_maps()
        if not token_map and not title_map:
            return None, None, 0.0

        normalised = _normalise_law_name(act_text)
        token_key = normalised.upper().replace(" ", "")

        # 0) Human-abbreviation alias (``KarS`` → ``KRIMIN``). Checked
        #    first so paste-style provisions like ``KarS § 211`` and
        #    ``AvTS § 35`` find their corpus TOKEN.
        alias_token = _HUMAN_ABBREV_ALIASES.get(normalised.replace(" ", ""))
        if alias_token and alias_token in token_map:
            return alias_token, token_map[alias_token], 1.0

        # 1) Direct corpus TOKEN match.
        if token_key in token_map:
            return token_key, token_map[token_key], 1.0

        # 2) Exact title match (no TOKEN known).
        if normalised in title_map:
            title = title_map[normalised]
            # Reverse-lookup the token if we have one for this title.
            token = self._title_to_token.get(normalised)
            return token, title, 1.0

        # 3) Fuzzy on titles.
        best_title: str | None = None
        best_key: str | None = None
        best_score = 0.0
        for candidate_key, title in title_map.items():
            score = SequenceMatcher(None, normalised, candidate_key).ratio()
            if score > best_score:
                best_score = score
                best_title = title
                best_key = candidate_key

        if best_title is not None and best_score >= _FUZZY_THRESHOLD:
            token = self._title_to_token.get(best_key or "")
            return token, best_title, round(best_score, 3)

        return None, None, 0.0

    def _get_abbrev_maps(self) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
        """Return the lazily-loaded abbreviation maps.

        Returns ``(token_to_title, normalised_titles, title_to_token)``.
        Loaded once per resolver instance with thread-safe
        double-checked locking.

        Cache lifecycle (P2#5 fix — Wave 2 Step 2 review feedback):

          * ``self._token_to_title is None`` — not loaded yet.  The
            initial state.
          * ``self._token_to_title == {}`` (and ``self._normalised_titles == {}``)
            — load **succeeded** but Jena genuinely returned zero rows
            (e.g. a misconfigured deploy pointing at an empty dataset).
            This is a one-shot cache so we don't hammer Jena on every
            resolve call when there is no data to find. Restart fixes
            it.
          * Populated dict — load succeeded, cached for the resolver's
            lifetime.

        Critically, when the SPARQL call **raises** (Jena unreachable,
        timeout, HTTP 5xx — anything ``SparqlClient.query(...,
        on_error="raise")`` propagates), we treat the load as "did not
        happen": ``self._token_to_title`` stays ``None`` so the NEXT
        resolve call retries the load. This prevents the original bug
        where a single transient Jena hiccup at warm-up time
        permanently poisoned the singleton (every subsequent resolve
        returning unresolved until process restart).

        Build strategy (per spike findings):
            1. Walk every ``?prov a ?cls ; estleg:sourceAct ?actLit``
               row where ``?cls`` is in the
               ``estleg:LegalProvision_<TOKEN>`` family.
            2. Derive TOKEN from the cls URI local-name suffix.
            3. For each TOKEN, pick the most-frequent ``?actLit`` as
               the canonical title.
            4. Build the reverse normalised-title → TOKEN map.
        """
        if self._token_to_title is not None:
            return self._token_to_title, self._normalised_titles, self._title_to_token

        with self._map_lock:
            if self._token_to_title is not None:
                return self._token_to_title, self._normalised_titles, self._title_to_token

            sparql = (
                PREFIXES
                + """
                SELECT ?prov ?cls ?actLit WHERE {
                  ?prov a ?cls ;
                        estleg:sourceAct ?actLit .
                  FILTER(STRSTARTS(STR(?cls),
                         \""""
                + ESTLEG_NS
                + """LegalProvision_\"))
                }
                """
            )
            # NOTE: ``on_error="raise"`` distinguishes a transient Jena
            # failure (httpx.ConnectError / TimeoutException / HTTPError)
            # from a genuine empty result.  Without this, a transient
            # failure would surface as ``rows == []`` (the legacy
            # ``swallow`` behaviour) and we'd cache an empty map
            # forever.  See P2#5 in the Wave 2 review.
            try:
                rows = self._sparql.query(sparql, on_error="raise")
            except Exception as exc:  # noqa: BLE001
                # Transient failure — leave ``_token_to_title`` as
                # ``None`` so the next call retries.  WARN-level so ops
                # can see "we tried, Jena was down, we'll retry next
                # call" without grepping for an exception trace.
                logger.warning(
                    "resolver: abbreviation-map load failed transiently "
                    "(will retry on next resolve call): %s",
                    exc,
                )
                return {}, {}, {}

            # token → Counter[title_literal]
            token_titles: dict[str, Counter[str]] = defaultdict(Counter)
            # Track every distinct title literal seen, regardless of TOKEN.
            distinct_titles: set[str] = set()

            cls_prefix = ESTLEG_NS + "LegalProvision_"
            for row in rows:
                cls_uri = row.get("cls", "")
                title = row.get("actLit", "")
                if not cls_uri or not title:
                    continue
                if not cls_uri.startswith(cls_prefix):
                    continue
                token = cls_uri[len(cls_prefix) :]
                if not token:
                    continue
                token_titles[token][title] += 1
                distinct_titles.add(title)

            token_to_title: dict[str, str] = {}
            for token, counter in token_titles.items():
                # most_common returns [(title, count)] tuples; pick the
                # most-frequent literal as the canonical title.
                top = counter.most_common(1)
                if top:
                    token_to_title[token.upper()] = top[0][0]

            # Reverse: normalised title → TOKEN (for the act half
            # lookup when the input was a full title, not an abbrev).
            title_to_token: dict[str, str] = {}
            for token, title in token_to_title.items():
                title_to_token[_normalise_law_name(title)] = token

            # All known normalised titles (so fuzzy matching can pick
            # up titles that aren't part of any TOKEN cluster).
            normalised_titles: dict[str, str] = {}
            for title in distinct_titles:
                normalised_titles[_normalise_law_name(title)] = title

            # Cache the (possibly empty) result.  Empty caches are a
            # one-shot "this deploy has no data" signal — they are NOT
            # retried, per the lifecycle contract documented above.
            self._token_to_title = token_to_title
            self._title_to_token = title_to_token
            self._normalised_titles = normalised_titles
            logger.info(
                "resolve: loaded %d abbreviation tokens and %d distinct titles",
                len(token_to_title),
                len(normalised_titles),
            )
            return token_to_title, normalised_titles, title_to_token

    # -- provision ----------------------------------------------------------

    def _resolve_provision(self, ref: ExtractedRef) -> ResolvedRef:
        """Decompose ``<act> § <num>[ lg <m>][ p <k>]`` and resolve both halves."""
        text = _normalise_whitespace(ref.ref_text)
        match = _PROVISION_RE.match(text)
        if match is None:
            self._log_miss(ref, tried_keys=["regex_no_match"], candidates=0)
            return _unresolved(ref)

        act_text = match.group("act").strip()
        num = match.group("num")

        # Hard injection guard: only accept digits in the section number.
        if not num or not num.isdigit():
            self._log_miss(ref, tried_keys=["non_digit_section"], candidates=0)
            return _unresolved(ref)

        token, title, _ = self._resolve_law_half(act_text)
        if title is None:
            self._log_miss(ref, tried_keys=[act_text, num], candidates=0)
            return _unresolved(ref)

        # URI-guess fast path. Only attempt when TOKEN is known and num
        # is purely digits (validated above).
        if token:
            guess_uri = f"{ESTLEG_NS}{token}_Par_{num}"
            ask_sparql = PREFIXES + f"\nASK {{ <{guess_uri}> ?p ?o }}\n"
            try:
                hit = self._sparql.ask(ask_sparql)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "resolve: URI-guess ASK failed token=%s num=%s: %s",
                    token,
                    num,
                    exc,
                )
                hit = False
            if hit:
                return ResolvedRef(
                    extracted=ref,
                    entity_uri=guess_uri,
                    matched_label=f"{title} § {num}",
                    match_score=1.0,
                )

        # Structural fallback: single-arm sourceAct + paragrahv match.
        # Both literal forms (``"§ N."`` and ``"§ N"``) per spike.
        # ``num`` is digits-only, validated above — safe to interpolate.
        # ``actLit`` is bound via the SparqlClient escape helper.
        struct_sparql = (
            PREFIXES
            + f"""
            SELECT ?p WHERE {{
              ?p estleg:paragrahv ?par ;
                 estleg:sourceAct  ?actLit .
              VALUES ?par {{ "§ {num}." "§ {num}" }}
            }}
            LIMIT 1
            """
        )
        try:
            rows = self._sparql.query(struct_sparql, bindings={"actLit": title})
        except Exception as exc:  # noqa: BLE001
            logger.warning("resolve: provision SPARQL failed: %s", exc)
            rows = []

        if rows:
            row = rows[0]
            uri = row.get("p")
            if uri:
                return ResolvedRef(
                    extracted=ref,
                    entity_uri=uri,
                    matched_label=f"{title} § {num}",
                    match_score=1.0,
                )

        # Partial match: act resolved, section did not. Surface as a
        # distinct state so downstream impact code can flag "act-level
        # only" instead of treating it like a clean miss.
        self._log_miss(
            ref,
            tried_keys=[token or "", title, num],
            candidates=0,
        )
        return ResolvedRef(
            extracted=ref,
            entity_uri=None,
            matched_label=f"{title} (sätet § {num} ei leitud)",
            match_score=0.5,
            partial_match={
                "act_token": token,
                "act_title": title,
                "section": num,
            },
        )

    # -- EU act -------------------------------------------------------------

    def _resolve_eu_act(self, ref: ExtractedRef) -> ResolvedRef:
        # Belt-and-braces: uppercase + strip whitespace *and* compile
        # the regex with IGNORECASE. Either alone would suffice but
        # both together makes the case-handling invariant impossible
        # to miss in code review.
        text = (ref.ref_text or "").strip().upper()
        match = _CELEX_RE.search(text)
        if match is None:
            self._log_miss(ref, tried_keys=["no_celex"], candidates=0)
            return _unresolved(ref)
        celex = match.group(1).upper()

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
            self._log_miss(ref, tried_keys=[celex], candidates=0)
            return _unresolved(ref)
        row = rows[0]
        uri = row.get("uri")
        if not uri:
            self._log_miss(ref, tried_keys=[celex], candidates=0)
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
        # or CJEU ``C-123/20``; they are case-sensitive (CJEU prefixes
        # like ``C-`` matter), so strip whitespace but DO NOT casefold.
        case_number = (ref.ref_text or "").strip()
        if not case_number:
            self._log_miss(ref, tried_keys=["empty"], candidates=0)
            return _unresolved(ref)

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
            self._log_miss(ref, tried_keys=[case_number], candidates=0)
            return _unresolved(ref)
        row = rows[0]
        uri = row.get("uri")
        if not uri:
            self._log_miss(ref, tried_keys=[case_number], candidates=0)
            return _unresolved(ref)
        return ResolvedRef(
            extracted=ref,
            entity_uri=uri,
            matched_label=row.get("label") or case_number,
            match_score=1.0,
        )

    # -- concept ------------------------------------------------------------

    def _resolve_concept(self, ref: ExtractedRef) -> ResolvedRef:
        # Concept labels are matched case-insensitively. ``casefold()``
        # is the Unicode-aware lowercaser — matters for Estonian
        # diacritics in concept labels (``Õ`` vs ``õ`` etc.).
        label = (ref.ref_text or "").strip()
        if not label:
            self._log_miss(ref, tried_keys=["empty"], candidates=0)
            return _unresolved(ref)
        normalised = label.casefold()

        sparql = (
            PREFIXES
            + """
            SELECT ?uri ?label WHERE {
              ?uri a estleg:LegalConcept ;
                   rdfs:label ?label .
              FILTER(LCASE(STR(?label)) = LCASE(?probe))
            }
            LIMIT 1
            """
        )
        try:
            rows = self._sparql.query(sparql, bindings={"probe": normalised})
        except Exception as exc:  # noqa: BLE001
            logger.warning("resolve: concept SPARQL failed: %s", exc)
            return _unresolved(ref)
        if not rows:
            self._log_miss(ref, tried_keys=[normalised], candidates=0)
            return _unresolved(ref)
        row = rows[0]
        uri = row.get("uri")
        if not uri:
            self._log_miss(ref, tried_keys=[normalised], candidates=0)
            return _unresolved(ref)
        return ResolvedRef(
            extracted=ref,
            entity_uri=uri,
            matched_label=row.get("label") or label,
            match_score=1.0,
        )

    # -- miss logging -------------------------------------------------------

    def _log_miss(
        self,
        ref: ExtractedRef,
        *,
        tried_keys: list[str],
        candidates: int,
    ) -> None:
        """Emit a structured miss log line.

        ``ref.ref_text`` is sensitive (pre-publication draft content)
        and never leaves this module; the log carries an HMAC'd
        identifier instead. Tests assert on the format string and the
        ``ref_id`` token.
        """
        logger.info(
            "resolver: %s unresolved ref_id=%s tried_keys=%d candidates=%d",
            ref.ref_type,
            _ref_id(ref.ref_text),
            len(tried_keys),
            candidates,
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
        partial_match=None,
    )


def _law_partial(
    ref: ExtractedRef,
    *,
    token: str | None,
    title: str,
    score: float,
) -> ResolvedRef:
    """Build a partial-match :class:`ResolvedRef` for a law-only ref.

    Law-only references (no ``§ N`` half) cannot resolve to a real
    ontology URI — the corpus has no act-level URIs (Step 1 spike).
    We therefore mirror the shape ``_resolve_provision`` uses for
    "act resolved, section not found": ``entity_uri=None`` plus
    ``partial_match={"act_token", "act_title", "section": None}``.

    ``section=None`` is the distinguishing marker between
    "law-only ref" and "provision ref whose section we couldn't
    find" — downstream consumers (graph_builder, renderer,
    .docx export) can use it to choose appropriate UI copy.
    """
    return ResolvedRef(
        extracted=ref,
        entity_uri=None,
        matched_label=title,
        match_score=score,
        partial_match={
            "act_token": token,
            "act_title": title,
            "section": None,
        },
    )


def _normalise_law_name(name: str) -> str:
    """Normalise a law name token for case-insensitive matching.

    Steps:
        1. Replace NBSP and tabs/newlines with plain spaces.
        2. Strip explicit Estonian legal-reference suffixes
           (``seadus``, ``seaduse``, ``seaduses``, …, ``seadustik``,
           ``seadustiku``, ``seadustikus``). Other tokens are NOT
           touched — generic case-suffix stripping (-e/-i/-st/…)
           creates false positives like ``karistusseaduselt`` →
           ``karistus``.
        3. Strip leading/trailing dashes (handles ``AvTS-i`` →
           ``AvTS``).
        4. Lowercase + collapse whitespace.

    The output is the canonical normalisation key used by both the
    abbreviation map's title index and the law-name fuzzy matcher.
    """
    cleaned = (name or "").replace(" ", " ")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    # Strip explicit legal-reference suffixes from EACH whitespace
    # token. ``karistusseadustiku §`` → ``karistus``; ``karistusseaduselt``
    # is unaffected because ``seaduselt`` is not in the suffix list.
    tokens = []
    for tok in cleaned.split(" "):
        # Trim trailing punctuation/dashes commonly attached to act
        # abbreviations (``AvTS-i`` → ``AvTS``).
        stripped = tok.rstrip(",.;:")
        stripped = re.sub(r"[-‒–—]+(?:[a-zõöäü]+)?$", "", stripped, flags=re.IGNORECASE)
        stripped = _LAW_SUFFIX_RE.sub("", stripped)
        if stripped:
            tokens.append(stripped)
    out = " ".join(tokens).strip().lower()
    return re.sub(r"\s+", " ", out)


def _normalise_whitespace(text: str) -> str:
    """Replace NBSP, tabs and newlines with plain spaces and collapse runs."""
    cleaned = (text or "").replace(" ", " ")  # non-breaking space
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip()


def _get_ref_hash_secret() -> bytes:
    """Resolve the HMAC secret for the miss-log ``ref_id``.

    Module-import-time reads break local dev, CI, and unit tests, any
    of which can ``import app.docs.reference_resolver`` without the env
    var set. Instead:

      - In production (``APP_ENV=production``, i.e.
        ``not is_stub_allowed()``): require the var; raise a clear
        ``RuntimeError`` if missing so the app refuses to start with
        unredacted logging.
      - Outside production: fall back to a dev sentinel so imports and
        tests work; the resulting ``ref_id`` is still stable
        per-process and never leaves the dev machine.

    Tests monkeypatch this helper directly to keep assertions on the
    hashed identifier stable.
    """
    secret = os.environ.get(_REF_HASH_SECRET_ENV)
    if secret:
        return secret.encode("utf-8")
    if not is_stub_allowed():  # production
        raise RuntimeError(
            f"{_REF_HASH_SECRET_ENV} must be set in production "
            "(see docs/2026-05-18-bugfix-plan.md and .env.example)."
        )
    return b"dev-only-resolver-ref-id-secret"


def _ref_id(ref_text: str) -> str:
    """HMAC-truncated ref identifier — stable across runs, not enumerable.

    Plain SHA-256 over a short legal reference (e.g. ``"KarS § 211"``)
    is low-entropy enough to dictionary-attack offline. HMAC with an
    app secret blocks that without changing the call-site shape.
    """
    return hmac.new(
        _get_ref_hash_secret(),
        (ref_text or "").encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()[:12]


# ---------------------------------------------------------------------------
# Singleton + public convenience wrapper
# ---------------------------------------------------------------------------


_default_resolver: ReferenceResolver | None = None
# #453: protect singleton init from concurrent worker threads.
_default_resolver_lock = threading.Lock()


def get_default_resolver() -> ReferenceResolver:
    """Return the process-wide default :class:`ReferenceResolver`.

    The singleton lets the abbreviation map survive across
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
