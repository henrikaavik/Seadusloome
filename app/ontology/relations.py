"""Canonical Estonian Legal Ontology relation vocabulary (C0).

Single source of truth for predicate URIs, inverses, Estonian
legal-language labels, and semantic groups. Every module that reasons
about ontology relations — impact analysis, the Õiguskaart evidence
card, chat tools, the Koostaja research cards — imports its constants
and helpers from here so a predicate rename only happens in one place.

Vocabulary is verified against the source ontology in
``henrikaavik/estonian-legal-ontology`` (audit dated 2026-05-15, see
``docs/2026-05-15-ontology-six-use-cases-plan.md`` section 2.5).

Predicate canonical forms (subject → object):

  ``interpretsLaw``       CourtDecision → Provision
  ``interpretedBy``       Provision → CourtDecision (inverse)
  ``amends``              AmendmentEvent → Provision
  ``amendedBy``           Provision → Act / Draft (inverse)
  ``topicCluster``        SHACL-defined alias (unused in current data)
  ``requestedCluster``    LegalProvision → TopicCluster (canonical, populated)
  ``transposesDirective`` Estonian Act → EULegislation
  ``transposedBy``        EULegislation → Estonian Act (inverse)
  ``harmonisedWith``      LegalProvision → EU act
  ``references``          DraftLegislation / Provision → any entity
  ``semanticallySimilarTo`` Provision ↔ Provision (similarity edges)
  ``definesConcept``      LegalProvision → LegalConcept
  ``definesTerm``         LegalProvision → Term
  ``competentAuthority``  Provision → Institution

Why the dict keys are local names, not full URIs: the explorer SPARQL
client returns predicate fragments as ``estleg:foo`` or
``https://data.riik.ee/ontology/estleg#foo`` depending on serialiser
quirks. To keep callers from re-implementing the same "strip the
namespace, lowercase" step, we expose helpers that normalise any of
those shapes (``"estleg:amends"`` / ``"amends"`` / ``"…#amends"``) to
the same key.
"""

from __future__ import annotations

from typing import Final

# ---------------------------------------------------------------------------
# Namespace
# ---------------------------------------------------------------------------

#: The estleg namespace URI. Imported here for callers that want the
#: full URI form ("https://data.riik.ee/ontology/estleg#amends") rather
#: than the prefixed ("estleg:amends") form.
ESTLEG_NS: Final[str] = "https://data.riik.ee/ontology/estleg#"


def _uri(local: str) -> str:
    """Build a full estleg URI for the given local name."""
    return f"{ESTLEG_NS}{local}"


# ---------------------------------------------------------------------------
# PREDICATES — canonical URI constants
# ---------------------------------------------------------------------------
#
# Names follow the ontology's CamelCase convention; values are full
# URIs (not prefixed names) so they can be dropped straight into a
# Turtle / N-Triples / SPARQL FILTER context without further
# normalisation. Callers that want the ``estleg:foo`` shorthand can
# prefix-fold themselves; the canonical form here is the URI.


class PREDICATES:
    """Canonical predicate URIs.

    Use these constants in SPARQL templates instead of bare strings so
    a single rename in this module propagates to every caller. The
    naming preserves the ontology's CamelCase convention.
    """

    # --- Interpretation (court decisions) ---
    INTERPRETS_LAW: Final[str] = _uri("interpretsLaw")
    INTERPRETED_BY: Final[str] = _uri("interpretedBy")

    # --- Amendments ---
    AMENDS: Final[str] = _uri("amends")
    AMENDED_BY: Final[str] = _uri("amendedBy")
    REPEALS: Final[str] = _uri("repeals")
    REPEALED_BY: Final[str] = _uri("repealedBy")

    # --- Topic clusters ---
    TOPIC_CLUSTER: Final[str] = _uri("topicCluster")
    REQUESTED_CLUSTER: Final[str] = _uri("requestedCluster")

    # --- EU transposition / harmonisation ---
    TRANSPOSES_DIRECTIVE: Final[str] = _uri("transposesDirective")
    TRANSPOSED_BY: Final[str] = _uri("transposedBy")
    HARMONISED_WITH: Final[str] = _uri("harmonisedWith")

    # --- References / citations ---
    REFERENCES: Final[str] = _uri("references")
    CITED_BY: Final[str] = _uri("citedBy")

    # --- Similarity ---
    SEMANTICALLY_SIMILAR_TO: Final[str] = _uri("semanticallySimilarTo")
    # Inline similarity score on the (subject) side of a
    # ``semanticallySimilarTo`` edge — populated corpus-wide by the
    # ontology's keyword_jaccard v2 pipeline. Used by A5 (similarity
    # workflow) to rank ontology-declared similarity candidates; a
    # missing score is treated as null in the merge layer rather than 0.
    SIMILARITY_SCORE: Final[str] = _uri("similarityScore")

    # --- Concepts / terms ---
    DEFINES_CONCEPT: Final[str] = _uri("definesConcept")
    DEFINES_TERM: Final[str] = _uri("definesTerm")

    # --- Competence ---
    COMPETENT_AUTHORITY: Final[str] = _uri("competentAuthority")

    # --- Deontic classification (A2 — Halduskoormus) ---
    #
    # Added for A2 (administrative-burden / deontic view): the
    # ontology audit (plan section 2.5 row A2, 2026-05-15) confirmed
    # ``estleg:normativeType`` + four ``NormType_*`` individuals are
    # populated corpus-wide, and ``estleg:dutyHolder`` is a populated
    # free-text literal. They were missing from this module because
    # they're property/literal predicates rather than entity-to-entity
    # relations; the A2 workflow needs canonical URIs so its SPARQL
    # never hardcodes ``estleg:*``.
    NORMATIVE_TYPE: Final[str] = _uri("normativeType")
    DUTY_HOLDER: Final[str] = _uri("dutyHolder")

    # --- Temporal validity (A4 — Ajalooline kehtivus) ---
    #
    # Added for A4 v1 (ajalugu workflow): the ontology audit confirms
    # ``entryIntoForce``, ``repealDate``, ``lastAmendmentDate``,
    # ``temporalStatus`` are populated corpus-wide on Acts; the
    # AmendmentEvent class carries ``eventDate``, ``entryIntoForceDate``,
    # ``rtReference``. The four ``version*`` predicates exist in SHACL
    # but the populated data is sample-only — used by V2 (deferred,
    # ontology issue #208).
    ENTRY_INTO_FORCE: Final[str] = _uri("entryIntoForce")
    REPEAL_DATE: Final[str] = _uri("repealDate")
    LAST_AMENDMENT_DATE: Final[str] = _uri("lastAmendmentDate")
    TEMPORAL_STATUS: Final[str] = _uri("temporalStatus")
    EVENT_DATE: Final[str] = _uri("eventDate")
    ENTRY_INTO_FORCE_DATE: Final[str] = _uri("entryIntoForceDate")
    RT_REFERENCE: Final[str] = _uri("rtReference")
    # ProvisionVersion chain — V2 (deferred):
    VERSION_VALID_FROM: Final[str] = _uri("versionValidFrom")
    VERSION_VALID_TO: Final[str] = _uri("versionValidTo")
    SUPERSEDED_BY_VERSION: Final[str] = _uri("supersededByVersion")
    VERSION_TEXT: Final[str] = _uri("versionText")


# ---------------------------------------------------------------------------
# NormativeType class + individuals (A2 — Halduskoormus / deontic view)
# ---------------------------------------------------------------------------
#
# The ``estleg:NormativeType`` class has four canonical individuals in the
# source ontology — one per deontic category. They are referenced from
# provisions via :attr:`PREDICATES.NORMATIVE_TYPE`. Importing them as
# named constants here lets the A2 ``burden.py`` helper bucket rows
# without hardcoding ``estleg:NormType_*`` URIs at the route layer.
#
# The mapping is **bidirectional** — :func:`norm_type_key` accepts a URI
# / prefixed name / literal value (the corpus occasionally carries a
# free-text echo like ``"obligation"`` / ``"Kohustus"`` on older rows
# instead of the canonical individual) and folds it to a stable
# lower-case bucket key (``"obligation"`` / ``"prohibition"`` /
# ``"permission"`` / ``"right"``).

NORMATIVE_TYPE_CLASS: Final[str] = _uri("NormativeType")

NORM_TYPE_OBLIGATION: Final[str] = _uri("NormType_Obligation")
NORM_TYPE_RIGHT: Final[str] = _uri("NormType_Right")
NORM_TYPE_PERMISSION: Final[str] = _uri("NormType_Permission")
NORM_TYPE_PROHIBITION: Final[str] = _uri("NormType_Prohibition")


# Canonical bucket-key → individual-URI lookup. Bucket keys are stable
# lower-case English identifiers (so they can serve as both the
# ``Literal[...]`` type in :mod:`app.analyysikeskus.burden` and as
# stable JSON dict keys). The UI translates them to Estonian labels at
# the render site.
NORM_TYPE_INDIVIDUALS: dict[str, str] = {
    "obligation": NORM_TYPE_OBLIGATION,
    "right": NORM_TYPE_RIGHT,
    "permission": NORM_TYPE_PERMISSION,
    "prohibition": NORM_TYPE_PROHIBITION,
}

#: The closed set of canonical bucket keys (handy for ``in`` checks).
NORM_TYPE_KEYS: frozenset[str] = frozenset(NORM_TYPE_INDIVIDUALS.keys())


# Literal-string aliases the corpus has historically used in place of
# the canonical individuals — folded to the same bucket key by
# :func:`norm_type_key`. Lower-case lookup. Estonian labels are included
# because the older corpus rows carry the Estonian word as a literal
# (e.g. ``"Kohustus"`` on a ``normativeType`` literal).
_NORM_TYPE_LITERAL_ALIASES: dict[str, str] = {
    "obligation": "obligation",
    "kohustus": "obligation",
    "kohustused": "obligation",
    "right": "right",
    "oigus": "right",
    "õigus": "right",
    "õigused": "right",
    "oigused": "right",
    "permission": "permission",
    "luba": "permission",
    "load": "permission",
    "prohibition": "prohibition",
    "keeld": "prohibition",
    "keelud": "prohibition",
}


def norm_type_key(name_or_uri: str) -> str:
    """Fold a ``normativeType`` value to its canonical bucket key.

    Accepts a full URI (``…#NormType_Obligation``), a prefixed name
    (``estleg:NormType_Obligation``), a bare local name
    (``NormType_Obligation``), or a literal alias (``"obligation"`` /
    ``"Kohustus"`` / …). Returns one of the four
    :data:`NORM_TYPE_KEYS` strings on a match, else ``"unknown"`` —
    never raises.
    """
    if not name_or_uri:
        return "unknown"
    raw = str(name_or_uri).strip()
    if not raw:
        return "unknown"
    # Try the canonical-individual path first (URI / prefixed / local
    # name forms).
    local = _local_name(raw)
    if local:
        # Direct match on the local name part of NormType_Obligation etc.
        for key, uri in NORM_TYPE_INDIVIDUALS.items():
            if _local_name(uri).lower() == local.lower():
                return key
    # Literal-alias path — strip any namespace tail (some serialisers
    # emit ``"Kohustus"`` already; others emit ``"Kohustus"@et`` — strip
    # the language tag).
    literal = raw.split("@", 1)[0].strip().lower()
    if literal in _NORM_TYPE_LITERAL_ALIASES:
        return _NORM_TYPE_LITERAL_ALIASES[literal]
    return "unknown"


# ---------------------------------------------------------------------------
# INVERSES — forward predicate URI → inverse predicate URI
# ---------------------------------------------------------------------------
#
# Symmetric: both directions are recorded so :func:`inverse_of` works
# for either side. A predicate without an inverse is simply absent.

INVERSES: dict[str, str] = {
    PREDICATES.INTERPRETS_LAW: PREDICATES.INTERPRETED_BY,
    PREDICATES.INTERPRETED_BY: PREDICATES.INTERPRETS_LAW,
    PREDICATES.AMENDS: PREDICATES.AMENDED_BY,
    PREDICATES.AMENDED_BY: PREDICATES.AMENDS,
    PREDICATES.REPEALS: PREDICATES.REPEALED_BY,
    PREDICATES.REPEALED_BY: PREDICATES.REPEALS,
    PREDICATES.TRANSPOSES_DIRECTIVE: PREDICATES.TRANSPOSED_BY,
    PREDICATES.TRANSPOSED_BY: PREDICATES.TRANSPOSES_DIRECTIVE,
    PREDICATES.REFERENCES: PREDICATES.CITED_BY,
    PREDICATES.CITED_BY: PREDICATES.REFERENCES,
}


# ---------------------------------------------------------------------------
# LEGAL_PHRASES — predicate URI → Estonian legal-language label
# ---------------------------------------------------------------------------
#
# The phrases are how a lawyer would name the relationship in
# Estonian, not a raw predicate name. They are used wherever a UI
# surface renders the "Seose liik" (relation type) column: the
# evidence-card detail panel, the impact-report rows, the Koostaja
# research cards. Keep them grammatically light — they will appear
# inline in compact UI cells.

LEGAL_PHRASES: dict[str, str] = {
    # Interpretation.
    PREDICATES.INTERPRETS_LAW: "tõlgendab",
    PREDICATES.INTERPRETED_BY: "on tõlgendatud",
    # Amendments / repeals.
    PREDICATES.AMENDS: "muudab",
    PREDICATES.AMENDED_BY: "muudetud õigusaktiga",
    PREDICATES.REPEALS: "tunnistab kehtetuks",
    PREDICATES.REPEALED_BY: "tunnistatud kehtetuks õigusaktiga",
    # Topic clusters.
    PREDICATES.TOPIC_CLUSTER: "kuulub teemavaldkonda",
    PREDICATES.REQUESTED_CLUSTER: "kuulub teemavaldkonda",
    # EU transposition / harmonisation.
    PREDICATES.TRANSPOSES_DIRECTIVE: "võtab üle direktiivi",
    PREDICATES.TRANSPOSED_BY: "üle võetud õigusaktiga",
    PREDICATES.HARMONISED_WITH: "on harmoneeritud aktiga",
    # References / citations.
    PREDICATES.REFERENCES: "viitab",
    PREDICATES.CITED_BY: "viidatud õigusaktiga",
    # Similarity.
    PREDICATES.SEMANTICALLY_SIMILAR_TO: "sarnane sisuga",
    # Concepts / terms.
    PREDICATES.DEFINES_CONCEPT: "defineerib mõistet",
    PREDICATES.DEFINES_TERM: "defineerib terminit",
    # Competence.
    PREDICATES.COMPETENT_AUTHORITY: "pädev asutus",
}


# ---------------------------------------------------------------------------
# RELATION_GROUPS — predicate URI → semantic group string
# ---------------------------------------------------------------------------
#
# Allows callers to ask "is this an amendment-flavoured relation?"
# without enumerating every predicate. The strings are stable identifiers
# (snake_case singular nouns) — UI surfaces may translate them at the
# render site.

RELATION_GROUPS: dict[str, str] = {
    PREDICATES.INTERPRETS_LAW: "interpretation",
    PREDICATES.INTERPRETED_BY: "interpretation",
    PREDICATES.AMENDS: "amendment",
    PREDICATES.AMENDED_BY: "amendment",
    PREDICATES.REPEALS: "amendment",
    PREDICATES.REPEALED_BY: "amendment",
    PREDICATES.TOPIC_CLUSTER: "concept",
    PREDICATES.REQUESTED_CLUSTER: "concept",
    PREDICATES.TRANSPOSES_DIRECTIVE: "transposition",
    PREDICATES.TRANSPOSED_BY: "transposition",
    PREDICATES.HARMONISED_WITH: "transposition",
    PREDICATES.REFERENCES: "reference",
    PREDICATES.CITED_BY: "reference",
    PREDICATES.SEMANTICALLY_SIMILAR_TO: "similarity",
    PREDICATES.DEFINES_CONCEPT: "concept",
    PREDICATES.DEFINES_TERM: "concept",
    PREDICATES.COMPETENT_AUTHORITY: "competence",
}


# ---------------------------------------------------------------------------
# Normalisation helpers
# ---------------------------------------------------------------------------
#
# Predicate names arrive at the helpers in any of these shapes:
#   - Full URI: ``"https://data.riik.ee/ontology/estleg#amends"``
#   - Prefixed: ``"estleg:amends"``
#   - Bare local name: ``"amends"``
#
# We normalise once, in one place, so callers don't all re-implement
# the same "strip namespace / lowercase" dance.


def _local_name(name_or_uri: str) -> str:
    """Reduce any predicate form to its bare local name.

    Strips a ``…#local`` fragment / ``…/local`` path tail / ``prefix:local``
    pair and returns the local name unchanged in case. Returns ``""`` for
    an empty / non-string input.
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
    return s


def _to_uri(name_or_uri: str) -> str:
    """Promote any predicate form to its canonical estleg URI.

    Full URIs pass through unchanged. Prefixed / bare names are
    rewritten into the ``ESTLEG_NS`` namespace. Returns ``""`` for an
    empty input.
    """
    if not name_or_uri:
        return ""
    s = str(name_or_uri).strip()
    if s.startswith("http://") or s.startswith("https://"):
        return s
    local = _local_name(s)
    if not local:
        return ""
    return _uri(local)


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def legal_phrase(name_or_uri: str) -> str:
    """Return the Estonian legal-language label for a predicate.

    Accepts the full URI, the prefixed name, or the bare local name.
    Matching is case-insensitive on the local name (so
    ``"estleg:AmendsProvision"`` and ``"amends"`` both resolve to
    ``"muudab"``). Legacy local-name aliases — the historical names
    Seadusloome used before C0 (``amendsProvision`` → ``amends``,
    ``interpretsProvision`` → ``interpretsLaw``, ``hasTopic`` →
    ``topicCluster``, ``implementsEU`` → ``transposesDirective``) — also
    resolve so older callers / cached data don't break during the
    transition.

    Unknown predicates fall back to the bare local name (preserving
    case) so a UI cell is never empty.
    """
    if not name_or_uri:
        return ""
    local = _local_name(name_or_uri)
    if not local:
        return ""
    lc = local.lower()
    # Try canonical URI first.
    uri = _uri(local)
    phrase = LEGAL_PHRASES.get(uri)
    if phrase:
        return phrase
    # Try legacy / lower-case-spelled local names.
    legacy_uri = _LEGACY_ALIASES.get(lc)
    if legacy_uri is not None:
        phrase = LEGAL_PHRASES.get(legacy_uri)
        if phrase:
            return phrase
    # Try the local-name-only fallback table (handles non-canonical
    # predicates the explorer surfaces).
    fallback = _LOCAL_NAME_PHRASES.get(lc)
    if fallback:
        return fallback
    return local


def inverse_of(name_or_uri: str) -> str | None:
    """Return the inverse predicate URI, or ``None`` if none is recorded.

    Accepts any predicate form. The result is always the full canonical
    URI so callers can drop it straight into a SPARQL template.
    """
    if not name_or_uri:
        return None
    local = _local_name(name_or_uri)
    if not local:
        return None
    uri = _uri(local)
    inverse = INVERSES.get(uri)
    if inverse is not None:
        return inverse
    # Legacy alias path.
    legacy_uri = _LEGACY_ALIASES.get(local.lower())
    if legacy_uri is not None:
        return INVERSES.get(legacy_uri)
    return None


def group_of(name_or_uri: str) -> str | None:
    """Return the predicate's semantic group (``"amendment"``, …) or ``None``."""
    if not name_or_uri:
        return None
    local = _local_name(name_or_uri)
    if not local:
        return None
    uri = _uri(local)
    group = RELATION_GROUPS.get(uri)
    if group is not None:
        return group
    legacy_uri = _LEGACY_ALIASES.get(local.lower())
    if legacy_uri is not None:
        return RELATION_GROUPS.get(legacy_uri)
    return None


def predicate_for_label(label: str) -> str | None:
    """Reverse lookup: find the canonical predicate URI for a legal phrase.

    Case-insensitive on the label. Returns ``None`` when no predicate
    maps to the given phrase. Used by routes that accept a
    user-selected "Seose liik" filter and need to translate it back to a
    SPARQL predicate (e.g. the Koostaja research-card grouper, future
    impact-report relation filters).
    """
    if not label:
        return None
    target = label.strip().lower()
    if not target:
        return None
    for uri, phrase in LEGAL_PHRASES.items():
        if phrase.lower() == target:
            return uri
    return None


def is_amendment_relation(name_or_uri: str) -> bool:
    """Return True if the predicate is in the ``"amendment"`` group."""
    return group_of(name_or_uri) == "amendment"


def is_interpretation_relation(name_or_uri: str) -> bool:
    """Return True if the predicate is in the ``"interpretation"`` group."""
    return group_of(name_or_uri) == "interpretation"


def is_transposition_relation(name_or_uri: str) -> bool:
    """Return True if the predicate is in the ``"transposition"`` group."""
    return group_of(name_or_uri) == "transposition"


# ---------------------------------------------------------------------------
# Legacy aliases — for back-compat phrase lookups only
# ---------------------------------------------------------------------------
#
# These are *not* SPARQL aliases — Seadusloome's queries now project the
# canonical names (C0 Part 2 rename). The legacy table only exists to
# keep :func:`legal_phrase` and friends robust to cached UI payloads
# that still carry an old predicate name. Once C5 lands and no surface
# stores predicate URIs across deploys, this table can shrink to the
# explorer's "kept for parity" entries (repeals, replaces, etc.).
#
# Keys are lowercased local names; values are canonical URIs.

_LEGACY_ALIASES: dict[str, str] = {
    # interpretsProvision → interpretsLaw
    "interpretsprovision": PREDICATES.INTERPRETS_LAW,
    "interprets": PREDICATES.INTERPRETS_LAW,
    # amendsProvision → amends
    "amendsprovision": PREDICATES.AMENDS,
    # repealsProvision → repeals
    "repealsprovision": PREDICATES.REPEALS,
    # hasTopic → topicCluster (treat as canonical lookup target;
    # requestedCluster is the populated form, both share the same phrase).
    "hastopic": PREDICATES.TOPIC_CLUSTER,
    # implementsEU → transposesDirective
    "implementseu": PREDICATES.TRANSPOSES_DIRECTIVE,
    "implementseulaw": PREDICATES.TRANSPOSES_DIRECTIVE,
    # transposes (without "Directive") → transposesDirective
    "transposes": PREDICATES.TRANSPOSES_DIRECTIVE,
    # harmonizedWith (US spelling) → harmonisedWith
    "harmonizedwith": PREDICATES.HARMONISED_WITH,
    # cites → references
    "cites": PREDICATES.REFERENCES,
}


# Local-name → phrase fallback for explorer predicates that don't have
# a canonical entry in :data:`LEGAL_PHRASES` (because they're structural
# metadata, not first-class relations). Used by :func:`legal_phrase` for
# the long-tail of predicates the evidence-card panel may surface.

_LOCAL_NAME_PHRASES: dict[str, str] = {
    # Structure / membership — read off the explorer's outgoing rows.
    "sourceact": "kuulub õigusakti",
    "partof": "on osa",
    "hasprovision": "sisaldab sätet",
    # Court relations from older data exports.
    "applies": "kohaldab",
    "appliesprovision": "kohaldab",
    # Replacement chain — keep parity with the historical explorer table.
    "replaces": "asendab",
    "replacedby": "asendatud õigusaktiga",
    # "Related to" — historically used as a generic catch-all.
    "relatedto": "on seotud",
    # Citation — kept for back-compat with old explorer dumps.
    "basedon": "tugineb",
}


__all__ = [
    "ESTLEG_NS",
    "PREDICATES",
    "INVERSES",
    "LEGAL_PHRASES",
    "RELATION_GROUPS",
    "NORMATIVE_TYPE_CLASS",
    "NORM_TYPE_OBLIGATION",
    "NORM_TYPE_RIGHT",
    "NORM_TYPE_PERMISSION",
    "NORM_TYPE_PROHIBITION",
    "NORM_TYPE_INDIVIDUALS",
    "NORM_TYPE_KEYS",
    "norm_type_key",
    "legal_phrase",
    "inverse_of",
    "group_of",
    "predicate_for_label",
    "is_amendment_relation",
    "is_interpretation_relation",
    "is_transposition_relation",
]
