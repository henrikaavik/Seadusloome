"""Resolve drafter LLM citation strings against the Estonian Legal Ontology.

The AI law drafter (Koostaja) asks the model to cite existing provisions.
Previously those citations were stored and rendered as raw, unvalidated
strings — often fabricated ``estleg:`` pseudo-URIs — and presented as
authoritative in the exported DOCX and the step-5 UI (issue #842). This
module turns each raw citation into an enriched, *verified-or-marked*
object so downstream surfaces only present a citation as authoritative
when the ontology actually contains it.

Mirrors the chat-advisor fix in PR #841: never trust an LLM-emitted URI —
resolve by law name + § / CELEX / case number via the shared
:class:`~app.docs.reference_resolver.ReferenceResolver`.

The enriched citation is a plain JSON-serialisable dict (it persists in
the drafting-session blob) with these keys:

    text:         human-readable citation text (always present)
    resolved_uri: ontology URI when verified, else None
    label:        display label (resolver match label when verified, else text)
    verified:     True iff the ontology resolved it to a real entity
    explorer_url: /explorer?focus=<uri> when verified, else None
"""

from __future__ import annotations

import logging
import re
from typing import Any, Protocol

from app.docs.entity_extractor import ExtractedRef
from app.docs.reference_resolver import ReferenceResolver
from app.docs.report_routes import explorer_focus_url

logger = logging.getLogger(__name__)


class _ResolverLike(Protocol):
    """Structural type for the resolver dependency.

    The real :class:`ReferenceResolver` satisfies this, and so does any test
    double — ``resolve_citations`` only needs a ``resolve(refs)`` method, so
    typing the parameter structurally keeps it injectable without coupling
    callers (or tests) to the concrete class.
    """

    def resolve(self, refs: list[ExtractedRef]) -> list[Any]: ...


# Legacy pseudo-URI forms the OLD drafter prompt asked the model to emit,
# e.g. "[estleg:TsiviilS/par/3]" and "[eu:2016-679/art/6]". The resolver's
# provision path expects an "Act § N" form (not the slash form), so these
# must be normalised before resolution.
_LEGACY_ESTLEG_RE = re.compile(
    r"^estleg:\s*([A-Za-zÕÄÖÜõäöü0-9_]+)\s*/\s*par\s*/\s*(\d+)$",
    re.IGNORECASE,
)
_LEGACY_EU_RE = re.compile(r"^eu:\s*(.+?)\s*/\s*art\s*/\s*\w+$", re.IGNORECASE)

# CELEX number, e.g. 32016R0679 / 32019L0790.
_CELEX_RE = re.compile(r"\b\d{5}[A-Z]{1,2}\d{4}\b")
# Estonian Supreme Court case numbers (3-2-1-100-15) or CJEU (C-123/45).
_CASE_RE = re.compile(r"\b(?:\d+-\d+-\d+-\d+-\d+|[CT]-\d+/\d+)\b")

_UNVERIFIED_PREFIX = "kontrollimata viide"


def unverified_label(text: str) -> str:
    """Estonian label for a citation the ontology could not verify.

    Shared by every render surface so the wording stays consistent.
    """
    return f"{_UNVERIFIED_PREFIX}: {text}"


def _strip_brackets(text: str) -> str:
    t = text.strip()
    if t.startswith("[") and t.endswith("]"):
        t = t[1:-1].strip()
    return t


def _classify(raw: str) -> tuple[str, str]:
    """Map a raw citation string to ``(ref_type, ref_text)`` for the resolver.

    Handles both the new human-readable forms (post-#842 prompt: ``Act § N``,
    CELEX, case number) and the legacy ``estleg:``/``eu:`` pseudo-URIs left
    in older drafting sessions. Misclassification is safe: an unmatched ref
    simply stays unresolved (marked "kontrollimata"), never a crash.
    """
    text = _strip_brackets(raw)

    m = _LEGACY_ESTLEG_RE.match(text)
    if m:
        return "provision", f"{m.group(1)} § {m.group(2)}"
    m = _LEGACY_EU_RE.match(text)
    if m:
        return "eu_act", m.group(1)

    if "§" in text:
        return "provision", text
    if _CELEX_RE.search(text):
        return "eu_act", text
    if _CASE_RE.search(text):
        return "court_decision", text
    return "law", text


def _enriched(text: str, *, resolved_uri: str | None, label: str | None) -> dict[str, Any]:
    """Build the canonical enriched-citation dict."""
    verified = bool(resolved_uri)
    return {
        "text": text,
        "resolved_uri": resolved_uri if verified else None,
        "label": (label or text) if verified else text,
        "verified": verified,
        "explorer_url": explorer_focus_url(resolved_uri) if verified else None,
    }


def coerce_citation(item: Any) -> dict[str, Any]:
    """Coerce a *stored* citation (legacy ``str`` OR enriched ``dict``) to the
    canonical enriched shape WITHOUT re-resolving against Jena.

    Render paths use this so they read old sessions (``list[str]``) and new
    ones (``list[dict]``) uniformly. A legacy string is surfaced as
    *unverified* — never authoritative — per #842.
    """
    if isinstance(item, dict):
        text = str(item.get("text") or item.get("label") or "")
        resolved_uri = item.get("resolved_uri") or None
        verified = bool(item.get("verified")) and resolved_uri is not None
        return {
            "text": text,
            "resolved_uri": resolved_uri if verified else None,
            "label": str(item.get("label") or text) if verified else text,
            "verified": verified,
            "explorer_url": (
                item.get("explorer_url")
                or (explorer_focus_url(resolved_uri) if resolved_uri else None)
            )
            if verified
            else None,
        }
    return _enriched(str(item), resolved_uri=None, label=None)


def resolve_citations(
    raw_citations: list[Any] | None,
    *,
    resolver: _ResolverLike | None = None,
) -> list[dict[str, Any]]:
    """Resolve raw drafter citation strings into enriched citation dicts.

    Each raw string is classified, converted to an :class:`ExtractedRef`,
    and resolved in one batch via :class:`ReferenceResolver`. Verified
    entries carry the ontology URI + an ``/explorer?focus=`` link; the rest
    come back unverified so callers can mark them "kontrollimata".

    Fails open: any resolver error (e.g. Jena down) downgrades *every*
    citation to unverified rather than raising — drafting and export must
    never crash on a citation lookup. Already-enriched dicts pass through
    :func:`coerce_citation` unchanged, so re-running is idempotent.
    """
    if not raw_citations:
        return []

    # Collect the raw strings that still need resolution (preserve order).
    texts = [str(c) for c in raw_citations if not isinstance(c, dict)]

    resolved_by_text: dict[str, dict[str, Any]] = {}
    if texts:
        refs = [
            ExtractedRef(ref_text=ref_text, ref_type=ref_type, confidence=1.0)
            for ref_type, ref_text in (_classify(t) for t in texts)
        ]
        runner = resolver if resolver is not None else ReferenceResolver()
        try:
            results = runner.resolve(refs)
        except Exception:
            logger.warning(
                "drafter citation resolution failed; marking all unverified",
                exc_info=True,
            )
            results = []
        for raw_text, res in zip(texts, results):
            resolved_by_text[raw_text] = _enriched(
                raw_text,
                resolved_uri=getattr(res, "entity_uri", None),
                label=getattr(res, "matched_label", None),
            )
        # Any text the resolver didn't return (failure / length mismatch).
        for raw_text in texts:
            resolved_by_text.setdefault(
                raw_text, _enriched(raw_text, resolved_uri=None, label=None)
            )

    # Reassemble in original order, preserving already-enriched dicts.
    out: list[dict[str, Any]] = []
    for c in raw_citations:
        if isinstance(c, dict):
            out.append(coerce_citation(c))
        else:
            out.append(resolved_by_text[str(c)])
    return out
