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
from app.docs.reference_resolver import get_default_resolver
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


def _is_ontology_uri(uri: object) -> bool:
    """True only for a real http(s) ontology URI.

    Guards the ``verified`` flag and the rendered href against pseudo-URIs or
    hostile values (``javascript:…``, ``estleg:Fake``): only a genuine
    http(s) URI returned by the resolver counts as a verified citation.
    """
    return isinstance(uri, str) and uri.startswith(("http://", "https://"))


def _enriched(text: str, *, resolved_uri: str | None, label: str | None) -> dict[str, Any]:
    """Build the canonical enriched-citation dict.

    A citation is *verified* only when ``resolved_uri`` is a real http(s)
    ontology URI. ``explorer_url`` is ALWAYS recomputed from it via
    ``explorer_focus_url`` (URL-encoded) — never taken from caller input — so
    a hostile stored ``explorer_url`` can never reach a rendered href.
    """
    uri = resolved_uri if _is_ontology_uri(resolved_uri) else None
    if uri is None:
        return {
            "text": text,
            "resolved_uri": None,
            "label": text,
            "verified": False,
            "explorer_url": None,
        }
    return {
        "text": text,
        "resolved_uri": uri,
        "label": label or text,
        "verified": True,
        "explorer_url": explorer_focus_url(uri),
    }


def coerce_citation(item: Any) -> dict[str, Any]:
    """Coerce a *stored* citation (legacy ``str`` OR enriched ``dict``) to the
    canonical enriched shape WITHOUT re-resolving against Jena.

    Render paths use this so they read old sessions (``list[str]``) and new
    ones (``list[dict]``) uniformly. It **never trusts** a stored ``verified``
    or ``explorer_url`` — both are recomputed from ``resolved_uri`` (which
    must be a real http(s) ontology URI to count as verified). A legacy
    string is always unverified. So a hostile persisted citation (a fake
    ``verified=True`` or a ``javascript:`` ``explorer_url``) can never be
    surfaced as authoritative or as a live link.
    """
    if isinstance(item, dict):
        text = str(item.get("text") or item.get("label") or "")
        raw_label = item.get("label")
        label = str(raw_label) if raw_label else None
        return _enriched(text, resolved_uri=item.get("resolved_uri"), label=label)
    return _enriched(str(item), resolved_uri=None, label=None)


def _raw_text(item: Any) -> str:
    """Extract the human-readable citation text from a raw LLM citation.

    The model is asked for plain strings, but a response could contain dicts.
    We use ONLY the ``text``/``label`` and ignore any caller-supplied
    verification fields — those are (re)computed by resolution, never trusted
    from input.
    """
    if isinstance(item, dict):
        return str(item.get("text") or item.get("label") or "")
    return str(item)


def resolve_citations(
    raw_citations: list[Any] | None,
    *,
    resolver: _ResolverLike | None = None,
) -> list[dict[str, Any]]:
    """Resolve raw drafter citations into enriched citation dicts.

    Every entry is treated as UNTRUSTED model output: only its text is used
    (via :func:`_raw_text`), then classified, converted to an
    :class:`ExtractedRef`, and resolved in one batch via the shared
    :func:`~app.docs.reference_resolver.get_default_resolver` (a process-wide
    singleton — the abbreviation map is warmed once, not per clause). A
    citation is marked ``verified`` only when the ontology returns a real
    http(s) URI, so a model **cannot** persist a fake "verified" citation by
    returning a dict with ``verified=True``.

    Fails open: any resolver error (e.g. Jena down) downgrades *every*
    citation to unverified rather than raising — drafting and export must
    never crash on a citation lookup.
    """
    if not raw_citations:
        return []

    texts = [_raw_text(c) for c in raw_citations]
    refs = [
        ExtractedRef(ref_text=ref_text, ref_type=ref_type, confidence=1.0)
        for ref_type, ref_text in (_classify(t) for t in texts)
    ]
    runner = resolver if resolver is not None else get_default_resolver()
    try:
        results = runner.resolve(refs)
    except Exception:
        logger.warning(
            "drafter citation resolution failed; marking all unverified",
            exc_info=True,
        )
        results = []

    out: list[dict[str, Any]] = []
    for i, text in enumerate(texts):
        res = results[i] if i < len(results) else None
        out.append(
            _enriched(
                text,
                resolved_uri=getattr(res, "entity_uri", None),
                label=getattr(res, "matched_label", None),
            )
        )
    return out
