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
* **Anything else** → ``[]``. The route surfaces a friendly "no
  structured reference recognised" message + (optionally) RAG
  candidates; this function never guesses.

All regexes are module-level constants so they're cheap to reuse and
trivially unit-testable.
"""

from __future__ import annotations

import re

from app.docs.entity_extractor import ExtractedRef

# CELEX numbers: 1-digit sector + 4-digit year + single descriptor
# letter + 1-4 digit running number, e.g. ``32016R0679``. Mirrors the
# pattern already used in :mod:`app.docs.reference_resolver` so the
# two stay in lockstep. ``fullmatch`` semantics are enforced by the
# caller anchoring on ``\b`` boundaries below.
_CELEX_RE = re.compile(r"^\d{5}[A-Z]\d{1,4}$", re.IGNORECASE)
_CELEX_SCAN_RE = re.compile(r"\b\d{5}[A-Z]\d{1,4}\b", re.IGNORECASE)

# Estonian court case numbers: ``3-1-1-63-15``, ``3-2-1-4-13``, ``5-19-1-2``…
# A run of digit groups joined by hyphens, at least three groups, last
# group typically a 2-digit year but we don't enforce that.
_EE_CASE_RE = re.compile(r"^\d+-\d+-\d+(?:-\d+)*$")

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
    (?P<section>\d+(?:[.\^]\d+)?)                       # section number, optional .N / ^N suffix
    (?:\s+lg\s*(?P<lg>\d+))?                            # optional 'lg N'
    (?:\s+p\s*(?P<p>\d+))?                              # optional 'p N'
    \s*$
    """,
    re.IGNORECASE | re.VERBOSE,
)


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
    #    range inside prose to be misread as a case number.
    if _EE_CASE_RE.fullmatch(text) or _EU_CASE_RE.fullmatch(text):
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

    # 4. Nothing structured recognised.
    return []


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ref(ref_text: str, ref_type: str) -> ExtractedRef:
    """Build a flat-confidence :class:`ExtractedRef` from a recognised token.

    ``location`` records that the ref came from the Analüüsikeskus
    search box rather than a parsed document — handy for debugging and
    harmless to the resolver, which only reads ``ref_text`` / ``ref_type``.
    """
    return ExtractedRef(
        ref_text=ref_text,
        ref_type=ref_type,
        confidence=1.0,
        location={"source": "analyysikeskus_input"},
    )


def _canonical_provision_text(
    law_short: str,
    section: str,
    lg: str | None,
    p: str | None,
) -> str:
    """Render a canonical ``"Law § N[ lg N][ p N]"`` provision string.

    The ontology stores provision literals in a consistent shape; the
    user's typing may differ in spacing ("§35", "lg2"). Re-spelling
    here gives the resolver its best shot at an exact literal match
    before it falls back to the law short-name lookup.
    """
    out = f"{law_short} § {section}"
    if lg:
        out += f" lg {lg}"
    if p:
        out += f" p {p}"
    return out
