"""Parse a free-text Analüüsikeskus input into structured legal references (#722).

The "Normi mõjuahel" workflow lets a ministry lawyer type a single line
into a search box — ``"AvTS § 35"``, a CELEX number like ``32016R0679``,
a Supreme Court case number like ``3-1-1-63-15``, a CJEU case like
``C-131/12``, or free prose. :func:`parse_user_reference` turns that
line into the ``list[ExtractedRef]`` that
:class:`app.docs.reference_resolver.ReferenceResolver` consumes — the
resolver only speaks :class:`~app.docs.entity_extractor.ExtractedRef`,
not raw strings, so this module is the adapter between the search box
and the resolver.

Recognition is **deliberately rule-based and conservative** — the same
spirit as the Estonian-legal-NLP note in ``CLAUDE.md`` ("Start with
rule-based regex for §-references and law names; layer ML later"):

* **CELEX** (``32016R0679``, ``32019L0790`` …) → one ``eu_act`` ref.
* **Court case number** — Estonian ``3-1-1-63-15`` style or CJEU
  ``C-131/12`` / ``T-99/04`` / ``F-1/05`` → one ``court_decision`` ref.
* **§-reference** — ``<LawShortName> § <n>`` optionally followed by
  ``lg <n>`` and/or ``p <n>`` → a ``provision`` ref carrying the full
  matched text *plus* a ``law`` ref carrying just the short name (the
  resolver tries the precise provision first and the law as a fallback).
* **Bare law reference** — a curated Estonian legal abbreviation
  (``KarS``, ``KMS``, ``TLS`` … via the resolver's
  ``_HUMAN_ABBREV_ALIASES`` keyset) or a token ending in
  ``seadus``/``seaduse``/``seadustik``/``seadustiku``/``seadustikust``
  (e.g. ``Töölepingu seadus``) → one ``law`` ref. The resolver returns
  a ``partial_match`` payload carrying the literal act title; the
  routes pick up that payload and route to the ``list_*_for_act``
  helpers. Length-capped at 80 chars to avoid swallowing
  sentence-shaped queries that happen to mention "seadus".
* **Anything else** → ``[]``. The route surfaces a friendly "no
  structured reference recognised" message + (optionally) RAG
  candidates; this function never guesses.

All regexes are module-level constants so they're cheap to reuse and
trivially unit-testable.
"""

from __future__ import annotations

import re

from app.docs.entity_extractor import ExtractedRef
from app.docs.reference_resolver import _HUMAN_ABBREV_ALIASES

# CELEX numbers: 1-digit sector + 4-digit year + single descriptor
# letter + 1-4 digit running number, e.g. ``32016R0679``. Mirrors the
# pattern already used in :mod:`app.docs.reference_resolver` so the
# two stay in lockstep. ``fullmatch`` semantics are enforced by the
# caller anchoring on ``\b`` boundaries below.
_CELEX_RE = re.compile(r"^\d{5}[A-Z]\d{1,4}$", re.IGNORECASE)
_CELEX_SCAN_RE = re.compile(r"\b\d{5}[A-Z]\d{1,4}\b", re.IGNORECASE)

# Estonian court case numbers: ``3-1-1-63-15``, ``3-2-1-4-13``,
# ``5-19-1-2``, ``3-20-1044``… A run of digit groups joined by hyphens,
# at least three groups. The leading group is a single-digit court-type
# code (1–9), which is the key signal that distinguishes a case number
# from an ISO date like ``2026-06-10`` (whose leading group is the
# 4-digit year). We cap the first group at two digits as headroom while
# still excluding any 4-digit year prefix.
_EE_CASE_RE = re.compile(r"^\d{1,2}-\d+-\d+(?:-\d+)*$")

# Strict ISO calendar date (``YYYY-MM-DD``): a 4-digit year, a 1–12
# month, a 1–31 day. Used as a belt-and-braces guard so a date never
# parses as a court case number even if a future case format widened
# the leading group. Anchored fullmatch at the call site.
_ISO_DATE_RE = re.compile(
    r"^\d{4}-(?:0?[1-9]|1[0-2])-(?:0?[1-9]|[12]\d|3[01])$",
)

# CJEU / EU General Court case numbers: ``C-131/12``, ``T-99/04``,
# ``F-1/05``, ``C-362/14`` … letter + dash + digits + slash + digits.
_EU_CASE_RE = re.compile(r"^[CTF]-\d+/\d+$", re.IGNORECASE)

# §-reference: a law short name (a token of letters incl. Estonian
# diacritics, possibly with internal digits like ``KOKS``), then ``§``,
# then a section number that may carry a literal index suffix (``§
# 35^1``-style data uses a caret; we keep it permissive), optionally
# followed by ``lg N`` and/or ``p N``. The short name is captured so we
# can emit it as a standalone ``law`` ref. Case-insensitive on the
# ``lg`` / ``p`` keywords; the law name itself keeps its original case
# because Estonian law abbreviations are case-significant (``AvTS`` vs
# ``avts``).
_SECTION_RE = re.compile(
    r"""
    ^\s*
    (?P<law>[A-Za-zÕÄÖÜŠŽõäöüšž][\wÕÄÖÜŠŽõäöüšž]*)   # law short name, e.g. AvTS / KarS / KOKS
    \s+§\s*
    (?P<section>\d+(?:[.\^]\d+|[⁰¹²³⁴⁵⁶⁷⁸⁹]+)?)        # section + optional .N/^N/superscript
    (?:\s+lg\.?\s*(?P<lg>\d+))?                         # optional 'lg N' / 'lg. N'
    (?:\s+p\.?\s*(?P<p>\d+))?                           # optional 'p N' / 'p. N'
    \s*$
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Maximum length of a bare law-name input that we will recognise. The
# longest curated abbreviation alias is ~8 chars; the longest realistic
# law title is ``"Karistusseadustiku rakendamise seadus"`` ≈ 38 chars.
# 80 chars is plenty of headroom while still rejecting sentence-shaped
# queries that happen to mention "seadus".
_BARE_LAW_MAX_LEN = 80

# Tokens that mark a string as a likely bare-law reference (case-
# insensitive, suffix-only). Mirrors the explicit suffix list curated
# in :func:`app.docs.reference_resolver._normalise_law_name`. Order
# does NOT matter here — endswith() short-circuits on first hit.
_BARE_LAW_SUFFIXES = (
    "seadustikust",
    "seadustikus",
    "seadustiku",
    "seadustik",
    "seadustes",
    "seadusest",
    "seaduseni",
    "seaduses",
    "seaduse",
    "seadusi",
    "seadust",
    "seadus",
)

# Pattern for "looks like an Estonian legal abbreviation":
#   * 2-10 characters
#   * letters only (incl. Estonian diacritics + optional internal lowercase)
#   * at least one uppercase letter (avoids matching plain lowercase words
#     like ``tööleping`` that are not abbreviations)
#   * no whitespace, no digits, no punctuation
#
# Examples that match: ``KarS``, ``KMS``, ``TLS``, ``KOKS``, ``HMS``,
# ``VõS``, ``AvTS``, ``TsÜS``, ``ÄS``.
# Examples that DO NOT match: ``karistusseadus`` (no uppercase),
# ``my-question`` (punctuation), ``2024`` (digits), ``Töölepingu seadus``
# (whitespace — those are handled by the suffix heuristic).
_BARE_LAW_ABBREV_RE = re.compile(
    r"^[A-ZÕÄÖÜŠŽ][A-Za-zÕÄÖÜŠŽõäöüšž]{1,9}$",
)


def _looks_like_bare_law(text: str) -> tuple[bool, float]:
    """Return ``(is_law, confidence)`` for a bare law-name candidate.

    Recognition heuristic, in priority order:

    * Exact alias match against the resolver's curated
      :data:`_HUMAN_ABBREV_ALIASES` (case-insensitive, whitespace-
      stripped) → 1.0 confidence. These are the ``KarS`` / ``AvTS`` /
      ``AõKS``-style shortcuts whose corpus TOKEN the resolver knows.
    * Suffix match against :data:`_BARE_LAW_SUFFIXES`
      (``Töölepingu seadus``, ``Karistusseadustik``, …) → 0.8.
    * Abbreviation-shape match against :data:`_BARE_LAW_ABBREV_RE`
      (``KMS``, ``TLS``, ``KOKS`` — short, mixed-case, no whitespace,
      no digits) → 0.8. These pass through to the resolver which will
      then attempt direct TOKEN match / fuzzy title match / miss —
      the resolver may surface them as resolved or unresolved, but
      *not* emitting them here would deny the resolver the chance.
    * Anything else → ``(False, 0.0)``.

    The length cap (:data:`_BARE_LAW_MAX_LEN`) is enforced **before**
    suffix matching so a long sentence ending in "…seaduses" doesn't
    get misread as a bare law name.
    """
    token = (text or "").strip()
    if not token:
        return False, 0.0
    if len(token) > _BARE_LAW_MAX_LEN:
        return False, 0.0

    # 1. Alias hit (case-insensitive, whitespace-stripped). Mirrors how
    #    the resolver keys :data:`_HUMAN_ABBREV_ALIASES` — lowercase,
    #    no internal whitespace.
    alias_key = token.lower().replace(" ", "")
    if alias_key in _HUMAN_ABBREV_ALIASES:
        return True, 1.0

    # 2. Suffix heuristic. Case-insensitive endswith() on the curated
    #    set of explicit Estonian legal-reference suffixes.
    lower = token.lower()
    for suffix in _BARE_LAW_SUFFIXES:
        if lower.endswith(suffix):
            return True, 0.8

    # 3. Abbreviation-shape heuristic — covers ``KMS``, ``TLS``,
    #    ``KOKS`` etc. that aren't yet in the curated alias map but
    #    *look* like Estonian legal abbreviations a user would type.
    #    The resolver's downstream lookups (TOKEN match / fuzzy) get
    #    a chance to resolve or report a miss; without this branch
    #    they're denied even the attempt.
    if _BARE_LAW_ABBREV_RE.fullmatch(token):
        return True, 0.8

    return False, 0.0


def parse_user_reference(sisend: str) -> list[ExtractedRef]:
    """Turn a free-text Analüüsikeskus input into structured references.

    Args:
        sisend: The raw search-box string. Leading/trailing whitespace
            is stripped; an empty/whitespace-only value yields ``[]``.

    Returns:
        A list of :class:`ExtractedRef` ready to hand to
        :meth:`app.docs.reference_resolver.ReferenceResolver.resolve`.
        Empty when nothing structured is recognised (the route handles
        that case with a friendly message + optional RAG candidates).
        For a §-reference the list has two entries — the precise
        ``provision`` ref first, then the ``law`` short-name ref — so
        the resolver can fall back to the law if the exact provision
        literal doesn't match. ``confidence`` is a flat ``1.0`` because
        a regex match is a deterministic recognition, not a probability.
    """
    text = (sisend or "").strip()
    if not text:
        return []

    # 1. CELEX — exact token, or a CELEX embedded in a short phrase
    #    (people paste "GDPR (32016R0679)"). The exact-match branch wins
    #    first so a bare "32016R0679" doesn't fall through to the scan.
    if _CELEX_RE.fullmatch(text):
        return [_ref(text, "eu_act")]
    celex_scan = _CELEX_SCAN_RE.search(text)
    if celex_scan is not None:
        return [_ref(celex_scan.group(0), "eu_act")]

    # 2. Court case numbers — Estonian (``3-1-1-63-15``) or CJEU
    #    (``C-131/12``). Exact-match only: we don't want a stray number
    #    range inside prose to be misread as a case number. An ISO date
    #    (``2026-06-10``) is explicitly excluded so it never lands here.
    if not _ISO_DATE_RE.fullmatch(text) and (
        _EE_CASE_RE.fullmatch(text) or _EU_CASE_RE.fullmatch(text)
    ):
        return [_ref(text, "court_decision")]

    # 3. §-reference — ``AvTS § 35``, ``KarS § 133 lg 2 p 1`` … The
    #    matched text becomes a ``provision`` ref; the law short name
    #    also gets emitted as a ``law`` ref so the resolver has a
    #    fallback when the provision literal doesn't resolve exactly.
    sec = _SECTION_RE.match(text)
    if sec is not None:
        law_short = sec.group("law")
        # Normalise the provision ref text to a canonical "Law § N[ lg N][ p N]"
        # spelling so it matches the literals the ontology stores
        # regardless of how the user spaced/cased "lg"/"p".
        provision_text = _canonical_provision_text(
            law_short,
            sec.group("section"),
            sec.group("lg"),
            sec.group("p"),
        )
        return [
            _ref(provision_text, "provision"),
            _ref(law_short, "law"),
        ]

    # 4. Bare law reference — ``KarS``, ``KMS``, ``Töölepingu seadus``…
    #    Only fires for inputs the heuristic recognises as a law name
    #    (curated alias OR explicit Estonian legal-reference suffix);
    #    everything else (free prose, descriptive intent) falls through
    #    to the empty-list branch.
    is_law, confidence = _looks_like_bare_law(text)
    if is_law:
        return [_ref(text, "law", confidence=confidence)]

    # 5. Nothing structured recognised.
    return []


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ref(ref_text: str, ref_type: str, *, confidence: float = 1.0) -> ExtractedRef:
    """Build an :class:`ExtractedRef` from a recognised token.

    ``location`` records that the ref came from the Analüüsikeskus
    search box rather than a parsed document — handy for debugging and
    harmless to the resolver, which only reads ``ref_text`` / ``ref_type``.

    ``confidence`` defaults to ``1.0`` (a regex match is deterministic
    recognition, not a probability). Bare law-name refs override this
    to ``0.8`` for suffix-only matches and ``1.0`` for curated alias
    hits — see :func:`_looks_like_bare_law` and the ranking documented
    in this module's docstring.
    """
    return ExtractedRef(
        ref_text=ref_text,
        ref_type=ref_type,
        confidence=confidence,
        location={"source": "analyysikeskus_input"},
    )


# Unicode superscript digits → ASCII, so a section typed as ``113¹``
# canonicalises to the caret form ``113^1`` that the resolver and
# ontology literal probing share.
_SUPERSCRIPT_DIGITS = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹", "0123456789")


def _canonical_section(section: str) -> str:
    """Canonicalise a captured section to ``N`` or ``N^M``.

    Folds a Unicode-superscript index (``113¹``) to the caret form
    (``113^1``); a dotted index (``113.1``) likewise; bare digits pass
    through unchanged. Keeps the §-reference spelling stable regardless
    of how the user typed the superscript.
    """
    m = re.match(r"^(\d+)([⁰¹²³⁴⁵⁶⁷⁸⁹]+)$", section)
    if m:
        return f"{m.group(1)}^{m.group(2).translate(_SUPERSCRIPT_DIGITS)}"
    dotted = re.match(r"^(\d+)\.(\d+)$", section)
    if dotted:
        return f"{dotted.group(1)}^{dotted.group(2)}"
    return section


def _canonical_provision_text(
    law_short: str,
    section: str,
    lg: str | None,
    p: str | None,
) -> str:
    """Render a canonical ``"Law § N[ lg N][ p N]"`` provision string.

    The ontology stores provision literals in a consistent shape; the
    user's typing may differ in spacing ("§35", "lg2") or superscript
    spelling ("§ 113¹" vs "§ 113^1"). Re-spelling here gives the
    resolver its best shot at an exact literal match before it falls
    back to the law short-name lookup.
    """
    out = f"{law_short} § {_canonical_section(section)}"
    if lg:
        out += f" lg {lg}"
    if p:
        out += f" p {p}"
    return out
