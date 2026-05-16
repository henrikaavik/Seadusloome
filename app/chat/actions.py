"""Outbound action links for chat assistant answers (C1).

Renders the ``Tegevused`` strip below an assistant message — the
chat's launchpad into deeper analysis surfaces (Normi mõjuahel, EL
ülevõtt, related-provisions seed). See
``docs/2026-05-15-ontology-six-use-cases-plan.md`` section C1.

Today the chat is a one-way street: every cited source links *out*
to the Õiguskaart but nothing routes onward into the
Analüüsikeskus workflows that turn an answer into the next step.
This module closes that loop.

Design notes
------------
* **Static entity → action map.** :data:`ACTIONS_BY_ENTITY_TYPE`
  is intentionally a small dict — three rows today (Provision,
  EULegislation, CourtDecision). Each action carries a stable
  Estonian label and a builder that turns a URI into the target URL
  on the right capability page. URLs are sourced from
  :data:`app.ui.capabilities.CAPABILITIES` (looked up by slug) so a
  capability rename in one place propagates to the chat strip.
* **Entity-type detection is heuristic.** Chat ``rag_context``
  entries only carry ``source_uri`` (see ``ChatOrchestrator``);
  there is no explicit ``rdf:type`` column. We detect the entity
  type from the URI shape using the same conventions the rest of
  Seadusloome already uses (``LegalProvision`` / ``_p<n>``
  suffixes for provisions, ``EULegislation`` / CELEX numerics for
  EU acts, ``CourtDecision`` / ``RK…`` / EU-court patterns for
  court decisions). When in doubt the URI is classified as
  ``"unknown"`` and no action link is rendered for it.
* **CELEX is special.** The EL ülevõtt workflow keys off a CELEX
  number, not an ontology URI, so :func:`extract_celex` pulls the
  CELEX out of the URI (or label) if one is present. URIs without
  a recoverable CELEX still classify as ``"eu_act"`` for grouping
  but do not produce an "EL ülevõtt" link.
* **Lisa märkus is intentionally omitted.** The plan calls for an
  optional annotation link, but the existing
  :class:`AnnotationButton` already renders on every assistant
  message (a level above this strip) — adding a second annotation
  entry point would be visual noise. If a future design wants a
  per-source annotation, this module is the place to wire it.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from fasthtml.common import *  # noqa: F403

from app.ontology.relations import _local_name
from app.ui.capabilities import get_capability

# ---------------------------------------------------------------------------
# Entity type detection
# ---------------------------------------------------------------------------

#: CELEX number — embedded match. Mirrors
#: :data:`app.analyysikeskus.input_parser._CELEX_RE` so the two stay in
#: lockstep — a chat answer that cites ``32016R0679`` produces an EL
#: ülevõtt link to the same workflow the user could reach by typing it
#: into the Analüüsikeskus search. The boundary uses a custom
#: lookaround so the CELEX number is recognised even when wrapped in an
#: underscore-joined URI local name (``CELEX_32016R0679``) — ``\b``
#: alone fails there because ``_`` is a word character.
_CELEX_RE = re.compile(
    r"(?<![0-9A-Za-z])\d{5}[A-Z]\d{1,4}(?![0-9A-Za-z])",
    re.IGNORECASE,
)
#: Exact-match variant for full-string classification.
_CELEX_FULL_RE = re.compile(r"^\d{5}[A-Z]\d{1,4}$", re.IGNORECASE)

#: Estonian Supreme Court chamber prefixes (Riigikohus) and the
#: common ``EKL``/``EUR`` markers EU-court decisions tend to carry. Used
#: purely as a URI heuristic — case-insensitive on the local name.
_COURT_LOCALNAME_HINTS: tuple[str, ...] = (
    "courtdecision",
    "rkkk",  # Riigikohus, kriminaalkolleegium
    "rkhk",  # Riigikohus, halduskolleegium
    "rktk",  # Riigikohus, tsiviilkolleegium
    "rkek",  # Riigikohus, erikogu
    "rkpk",  # Riigikohus, põhiseaduslikkuse järelevalve
    "rkkjk",
)

#: EU legislation hints — class name or directive/regulation tokens.
_EU_LOCALNAME_HINTS: tuple[str, ...] = (
    "eulegislation",
    "eu_dir",
    "eu_reg",
    "eulaw",
)

#: Provision-shape hints — explicit class names and the ``_p<n>``
#: provision suffix Seadusloome uses across the codebase.
_PROVISION_LOCALNAME_HINTS: tuple[str, ...] = (
    "legalprovision",
    "provision",
)

#: Matches the ``<Act>_p<digits>[_…]`` provision-localname pattern, e.g.
#: ``KarS_p121`` or ``AvTS_p35_lg1_p5``.
_PROVISION_SUFFIX_RE = re.compile(r"_p\d+", re.IGNORECASE)


def detect_entity_type(source_uri: str) -> str:
    """Classify a cited URI into ``"provision"``, ``"eu_act"``,
    ``"court_decision"``, or ``"unknown"``.

    Detection is heuristic — chat RAG chunks don't carry an
    ``rdf:type``. We look at the URI's local name (everything after
    the last ``#`` or ``/``) for class-name hints, then fall back to
    suffix patterns. An empty / non-string input returns ``"unknown"``.

    Args:
        source_uri: A full URI (``https://…#Foo``), a prefixed
            curie (``estleg:KarS_p121``), or just a local name. All
            three are accepted because chat sources arrive in mixed
            shapes from the orchestrator.

    Returns:
        One of ``"provision"``, ``"eu_act"``, ``"court_decision"``,
        ``"unknown"``.
    """
    if not source_uri or not isinstance(source_uri, str):
        return "unknown"

    local = _local_name(source_uri).lower()
    if not local:
        return "unknown"

    # Court decisions first — the prefix check is the most specific.
    for hint in _COURT_LOCALNAME_HINTS:
        if hint in local:
            return "court_decision"

    # EU legislation — class name or directive/regulation tokens or a
    # bare CELEX as the local name.
    for hint in _EU_LOCALNAME_HINTS:
        if hint in local:
            return "eu_act"
    if _CELEX_FULL_RE.fullmatch(local):
        return "eu_act"
    # Embedded CELEX (``estleg:CELEX_32016R0679``-style) — only treat
    # as EU when the CELEX is the dominant token, not a substring of
    # an unrelated label.
    if _CELEX_RE.search(local) and "celex" in local:
        return "eu_act"

    # Provisions — explicit class name or the ``_p<n>`` suffix pattern.
    for hint in _PROVISION_LOCALNAME_HINTS:
        if hint in local:
            return "provision"
    if _PROVISION_SUFFIX_RE.search(local):
        return "provision"

    return "unknown"


def extract_celex(source_uri: str) -> str | None:
    """Return the CELEX number contained in *source_uri*, or ``None``.

    Looks at the local name first (so ``estleg:CELEX_32016R0679``
    resolves to ``32016R0679``); falls back to scanning the full URI
    string. The match is normalised to upper-case because CELEX
    descriptors (``R``, ``L``, ``D``…) are upper-case by convention
    in EUR-Lex.
    """
    if not source_uri or not isinstance(source_uri, str):
        return None
    local = _local_name(source_uri)
    if local:
        m = _CELEX_RE.search(local)
        if m:
            return m.group(0).upper()
    m = _CELEX_RE.search(source_uri)
    if m:
        return m.group(0).upper()
    return None


# ---------------------------------------------------------------------------
# Action map
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ChatActionLink:
    """One outbound action link rendered in the ``Tegevused`` strip.

    The ``label`` is the user-facing Estonian text (taken from the
    capability dictionary when applicable so wording stays in sync
    with the capability cards). The ``href`` is the absolute app
    path the link navigates to; if ``method`` is ``"post"`` the link
    is rendered as a one-button form so the seed text travels through
    a real POST body, not a URL.
    """

    label: str
    href: str
    title: str
    # Either "get" (plain anchor) or "post" (single-button form). The
    # chat-seed action is POST because it carries a server-side seed.
    method: str = "get"
    # Optional form fields for POST actions — rendered as hidden inputs.
    form_fields: tuple[tuple[str, str], ...] = ()


def _normi_mojuahel_link(*, label: str, source_uri: str) -> ChatActionLink:
    """Build the "Käivita Normi mõjuahel" link for a provision URI.

    The Analüüsikeskus accepts free-text inputs (act short name, §
    reference, CELEX, case number, …). The cleanest payload is the
    human-readable label — that's what the user typed to get there
    in the first place — falling back to the local name when the
    chunk has no title.
    """
    cap = get_capability("normi-mojuahel")
    base = cap.target_url if cap else "/analyysikeskus/normi-mojuahel"
    sisend = (label or _local_name(source_uri) or "").strip()
    href = f"{base}?sisend={quote(sisend, safe='')}" if sisend else base
    return ChatActionLink(
        label="Käivita Normi mõjuahel",
        href=href,
        title="Vaata, mida selle sätte muudatus mõjutab.",
    )


def _el_ulevott_link(*, label: str, source_uri: str) -> ChatActionLink | None:
    """Build the "Vaata EL ülevõttu" link for an EU act, if it has a CELEX.

    Returns ``None`` when no CELEX can be recovered — the
    transposition workflow takes a CELEX, not an arbitrary URI, so a
    link without one would 404 on the destination page.
    """
    celex = extract_celex(source_uri) or extract_celex(label or "")
    if not celex:
        return None
    cap = get_capability("el-ulevott")
    base = cap.target_url if cap else "/analyysikeskus/el-ulevott"
    return ChatActionLink(
        label="Vaata EL ülevõttu",
        href=f"{base}?sisend={quote(celex, safe='')}",
        title="Vaata, kas Eesti õigus katab selle EL akti.",
    )


def _related_provisions_seed_link(*, label: str, source_uri: str) -> ChatActionLink:
    """Build the "Küsi seotud sätete kohta" POST link for a court decision.

    The seed text becomes a new chat conversation pre-filled with a
    question about provisions this decision interprets. Routes
    through the existing ``POST /chat/seed`` handler so the seed
    text is stashed server-side, not URL-leaked.
    """
    name = (label or _local_name(source_uri) or "lahend").strip()
    seed_text = (
        f"Milliseid sätteid see kohtulahend «{name}» tõlgendab? "
        "Too välja viited ja Riigikohtu seisukohad."
    )
    return ChatActionLink(
        label="Küsi seotud sätete kohta",
        href="/chat/seed",
        title="Avab uue vestluse seotud sätete küsimusega.",
        method="post",
        form_fields=(("seed_text", seed_text),),
    )


#: Static map: entity type → list of action builders. The builder
#: takes the label + URI and returns either a :class:`ChatActionLink`
#: or ``None`` (when this action doesn't apply — e.g. an EU act
#: without a recoverable CELEX). Order is the order the links render.
ACTIONS_BY_ENTITY_TYPE: dict[str, tuple[Callable[..., ChatActionLink | None], ...]] = {
    "provision": (_normi_mojuahel_link,),
    "eu_act": (_el_ulevott_link,),
    "court_decision": (_related_provisions_seed_link,),
}


def build_actions_for_uri(source_uri: str, label: str = "") -> list[ChatActionLink]:
    """Return the action links that apply to one cited entity.

    Args:
        source_uri: The cited URI from a RAG chunk.
        label: The human-readable title (typically the chunk's
            title — last URI segment or a derived display name).

    Returns:
        A list of :class:`ChatActionLink` in render order. Empty
        when the entity type doesn't map to any action, or when
        every applicable builder returned ``None`` (e.g. an EU act
        URI without a recoverable CELEX).
    """
    if not source_uri:
        return []
    entity_type = detect_entity_type(source_uri)
    builders = ACTIONS_BY_ENTITY_TYPE.get(entity_type, ())
    out: list[ChatActionLink] = []
    for builder in builders:
        link = builder(label=label, source_uri=source_uri)
        if link is not None:
            out.append(link)
    return out


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _dedupe_actions(links: list[ChatActionLink]) -> list[ChatActionLink]:
    """Drop duplicate actions — same label + href + form payload."""
    seen: set[tuple[str, str, tuple[tuple[str, str], ...]]] = set()
    unique: list[ChatActionLink] = []
    for link in links:
        key = (link.label, link.href, link.form_fields)
        if key in seen:
            continue
        seen.add(key)
        unique.append(link)
    return unique


def collect_actions(rag_context: list[dict] | None) -> list[ChatActionLink]:
    """Collect the de-duplicated action links for every cited source.

    Walks ``rag_context`` once (the same shape ``_rag_sources_block``
    consumes), classifies each URI, and concatenates the per-URI
    action lists. Duplicates are dropped so a chat answer that cites
    AvTS § 35 three times still only produces one "Käivita Normi
    mõjuahel" link.
    """
    if not rag_context:
        return []
    collected: list[ChatActionLink] = []
    for chunk in rag_context:
        if not isinstance(chunk, dict):
            continue
        source_uri = str(chunk.get("source_uri") or "").strip()
        if not source_uri:
            continue
        label = str(chunk.get("title") or "").strip()
        if not label:
            # Mirror :func:`_rag_sources_block`'s title-derivation: the
            # last segment of the URI, falling back to the URI itself.
            label = source_uri.rstrip("/").rsplit("/", 1)[-1] or source_uri
        collected.extend(build_actions_for_uri(source_uri, label))
    return _dedupe_actions(collected)


def _render_action(link: ChatActionLink) -> Any:
    """Render one ChatActionLink as an anchor or a one-button form."""
    if link.method.lower() == "post":
        # One-button form so the seed payload travels in a real
        # POST body rather than a query string.
        hidden_inputs = [
            Hidden(name=name, value=value)  # noqa: F405
            for name, value in link.form_fields
        ]
        return Form(  # noqa: F405
            *hidden_inputs,
            Button(  # noqa: F405
                f"→ {link.label}",
                type="submit",
                cls="chat-action-link chat-action-link--button",
                title=link.title,
            ),
            method="post",
            action=link.href,
            cls="chat-action-form inline-form",
        )
    return A(  # noqa: F405
        f"→ {link.label}",
        href=link.href,
        cls="chat-action-link",
        title=link.title,
    )


def chat_actions_block(rag_context: list[dict] | None) -> Any:
    """Render the collapsible ``Tegevused`` strip below an answer.

    Mirrors the markup shape of :func:`_rag_sources_block` —
    ``<details class="chat-actions">`` with a ``<summary>`` showing
    the action count and a ``<ul>`` of action links / forms — so
    the CSS rules for ``.chat-sources`` cascade cleanly via a
    shared ``.chat-disclosure`` class on the wrapper.

    Returns an empty string when there are no applicable actions so
    the assistant bubble doesn't grow a useless empty disclosure.
    """
    actions = collect_actions(rag_context)
    if not actions:
        return ""
    items = [Li(_render_action(link)) for link in actions]  # noqa: F405
    return Details(  # noqa: F405
        Summary(f"Tegevused ({len(actions)})"),  # noqa: F405
        Ul(*items, cls="chat-actions-list"),  # noqa: F405
        cls="chat-actions",
        # Open by default — the whole point of C1 is to make the
        # next-step affordance visible, not hide it behind a click.
        open=True,
    )


__all__ = [
    "ChatActionLink",
    "ACTIONS_BY_ENTITY_TYPE",
    "detect_entity_type",
    "extract_celex",
    "build_actions_for_uri",
    "collect_actions",
    "chat_actions_block",
]
