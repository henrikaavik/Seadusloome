"""Background job handlers for the drafter pipeline (Steps 2-5).

Each handler follows the Phase 2 convention established by
:mod:`app.docs.analyze_handler`:

    - Registered via ``@register_handler(job_type)``
    - Accepts ``(payload, *, attempt, max_attempts, job_id=None)``
    - Returns a summary dict persisted in ``background_jobs.result``
    - Raises on failure; only marks domain state as ``abandoned`` on the
      final attempt (#448 retry-gating pattern)
    - ``job_id`` is accepted for handler-contract compatibility (#610);
      drafter handlers don't currently publish progress, so the value
      is ignored.

The handlers are imported as a side-effect in ``app.drafter.__init__.py``
so they are registered before the worker claims any drafter job.
"""

from __future__ import annotations

import copy
import json
import logging
from typing import Any
from uuid import UUID

from app.chat.rate_limiter import check_org_cost_budget
from app.db import get_connection
from app.drafter.citations import resolve_citations
from app.drafter.prompts import (
    CLARIFY_PROMPT,
    DRAFT_PROMPT,
    STRUCTURE_PROMPT,
    VTK_SECTION_PROMPTS,
    VTK_STRUCTURE,
)
from app.drafter.session_model import (
    abandon_session,
    fetch_session,
    update_session,
)
from app.jobs.worker import register_handler
from app.llm import get_default_provider
from app.ontology.sparql_client import SparqlClient, _sanitize_sparql_value
from app.storage import decrypt_text, encrypt_text

logger = logging.getLogger(__name__)


class LLMOutputError(RuntimeError):
    """LLM returned unusable output (parse failure or missing required fields).

    #852 E2: the provider's ``extract_json`` returns ``{"error": ...}``
    instead of raising when the model's reply is not parseable JSON.
    Treating that dict as data let ``drafter_draft`` persist blank
    clauses while the job "succeeded" — retry-gating never engaged and
    the state machine waved empty clauses into review. Raising this
    (a ``RuntimeError`` subclass, so existing except/re-wrap paths and
    tests keep working) routes bad output through the normal retry
    budget + abandon gating instead.
    """


def _require_llm_json(result: Any, *, context: str) -> dict[str, Any]:
    """Validate a raw ``extract_json`` payload before it is used (#852 E2).

    Raises :class:`LLMOutputError` when the payload is not a dict or
    carries the provider's ``{"error": ...}`` parse-failure marker, so
    the job fails (and retries) instead of silently persisting garbage.

    Stub-mode payloads (``{"stub": True, ...}``) pass through: local
    development without API keys must still complete the pipeline, and
    each call site decides how strict to be about stub content.
    """
    if not isinstance(result, dict):
        raise LLMOutputError(f"{context}: LLM returned a non-dict payload")
    if result.get("error"):
        raise LLMOutputError(f"{context}: LLM output could not be parsed: {result['error']}")
    return result


# ---------------------------------------------------------------------------
# SPARQL helpers
# ---------------------------------------------------------------------------
#
# C2 (2026-05-15): the four research queries below now project a
# ``?relation`` variable on every row, classifying each found entity by
# the predicate that connects it to a keyword-matched provision (or by
# its inherent role for entity types that don't participate in inbound
# legal relations). The Step 3 page groups results by Estonian legal
# phrase (see ``app.ontology.relations.legal_phrase``) so the drafter
# sees not just "5 provisions found" but "3 muudab, 2 tõlgendab".
#
# Design notes:
#   * Predicate URIs are interpolated from ``app.ontology.relations``
#     (PREDICATES.*) rather than hard-coded — a rename in the
#     relations module propagates here automatically.
#   * Each query keeps a small bare-keyword UNION arm (BIND a generic
#     ``estleg:references`` relation) so entities that match by label
#     but participate in no canonical relation still show up under
#     the "viitab" group — preserving the old behaviour.
#   * Per-entity row counts can be > 1 when an entity participates in
#     multiple relations; downstream renderers honour the grouping.

_RELATED_LAWS_QUERY = """\
PREFIX estleg: <https://data.riik.ee/ontology/estleg#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>

SELECT DISTINCT ?law ?label WHERE {{
  ?provision estleg:sourceAct ?law .
  ?law rdfs:label ?label .
  FILTER(CONTAINS(LCASE(?label), LCASE("{keyword}")))
}}
LIMIT 5
"""

# Provisions: keyword-match on the provision label, then classify by
# any canonical relation the provision participates in. The
# ``BIND(estleg:references AS ?relation)`` fallback arm guarantees a
# row for every keyword match even when no other relation is recorded.
_PROVISIONS_BY_KEYWORD_QUERY = """\
PREFIX estleg: <https://data.riik.ee/ontology/estleg#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>

SELECT DISTINCT ?provision ?label ?actLabel ?relation WHERE {{
  ?provision estleg:paragrahv ?paragrahv .
  ?provision rdfs:label ?label .
  ?provision estleg:sourceAct ?act .
  ?act rdfs:label ?actLabel .
  FILTER(CONTAINS(LCASE(?label), LCASE("{keyword}")))
  {{
    # Fallback: every keyword match shows up at least once under
    # "viitab" so the card never silently drops entities.
    BIND(estleg:references AS ?relation)
  }} UNION {{
    ?_amendmentEvent estleg:amends ?provision .
    BIND(estleg:amends AS ?relation)
  }} UNION {{
    ?provision estleg:amendedBy ?_amender .
    BIND(estleg:amendedBy AS ?relation)
  }} UNION {{
    ?_court estleg:interpretsLaw ?provision .
    BIND(estleg:interpretsLaw AS ?relation)
  }} UNION {{
    ?provision estleg:interpretedBy ?_court .
    BIND(estleg:interpretedBy AS ?relation)
  }} UNION {{
    ?provision estleg:transposesDirective ?_euAct .
    BIND(estleg:transposesDirective AS ?relation)
  }} UNION {{
    ?provision estleg:harmonisedWith ?_euAct .
    BIND(estleg:harmonisedWith AS ?relation)
  }} UNION {{
    ?provision estleg:definesConcept ?_concept .
    BIND(estleg:definesConcept AS ?relation)
  }} UNION {{
    ?provision estleg:definesTerm ?_term .
    BIND(estleg:definesTerm AS ?relation)
  }} UNION {{
    ?provision estleg:requestedCluster ?_cluster .
    BIND(estleg:requestedCluster AS ?relation)
  }}
}}
LIMIT 100
"""

# EU directives: keyword-match the directive label, then classify by
# the inbound predicate from any Estonian provision/act. Fallback:
# ``references`` so every keyword-matched directive appears.
_EU_DIRECTIVES_QUERY = """\
PREFIX estleg: <https://data.riik.ee/ontology/estleg#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>

SELECT DISTINCT ?directive ?label ?relation WHERE {{
  ?directive a estleg:EULegislation .
  ?directive rdfs:label ?label .
  FILTER(CONTAINS(LCASE(?label), LCASE("{keyword}")))
  {{
    BIND(estleg:references AS ?relation)
  }} UNION {{
    ?_act estleg:transposesDirective ?directive .
    BIND(estleg:transposesDirective AS ?relation)
  }} UNION {{
    ?directive estleg:transposedBy ?_act .
    BIND(estleg:transposedBy AS ?relation)
  }} UNION {{
    ?_provision estleg:harmonisedWith ?directive .
    BIND(estleg:harmonisedWith AS ?relation)
  }}
}}
LIMIT 50
"""

# Court decisions: keyword-match, classify by how the decision relates
# to Estonian provisions.
_COURT_DECISIONS_QUERY = """\
PREFIX estleg: <https://data.riik.ee/ontology/estleg#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>

SELECT DISTINCT ?decision ?label ?relation WHERE {{
  ?decision a estleg:CourtDecision .
  ?decision rdfs:label ?label .
  FILTER(CONTAINS(LCASE(?label), LCASE("{keyword}")))
  {{
    BIND(estleg:references AS ?relation)
  }} UNION {{
    ?decision estleg:interpretsLaw ?_provision .
    BIND(estleg:interpretsLaw AS ?relation)
  }} UNION {{
    ?_provision estleg:interpretedBy ?decision .
    BIND(estleg:interpretedBy AS ?relation)
  }}
}}
LIMIT 50
"""

# Topic clusters: keyword-match. Clusters always participate via the
# ``requestedCluster`` / ``topicCluster`` predicates, so we project
# those directly.
_TOPIC_CLUSTERS_QUERY = """\
PREFIX estleg: <https://data.riik.ee/ontology/estleg#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>

SELECT DISTINCT ?cluster ?label ?relation WHERE {{
  ?cluster a estleg:TopicCluster .
  ?cluster rdfs:label ?label .
  FILTER(CONTAINS(LCASE(?label), LCASE("{keyword}")))
  {{
    ?_p estleg:requestedCluster ?cluster .
    BIND(estleg:requestedCluster AS ?relation)
  }} UNION {{
    ?_p estleg:topicCluster ?cluster .
    BIND(estleg:topicCluster AS ?relation)
  }} UNION {{
    BIND(estleg:references AS ?relation)
  }}
}}
LIMIT 50
"""


def _extract_keywords(text: str) -> list[str]:
    """Extract simple keywords from intent text for SPARQL queries.

    Uses a basic heuristic: split on whitespace, keep words longer than
    3 chars, strip punctuation, deduplicate. This is intentionally naive;
    Phase 3B will add EstBERT-based keyword extraction.
    """
    import re

    words = re.findall(r"\b[a-zA-ZouaeiOUAEI\u00e4\u00f6\u00fc\u00f5]{4,}\b", text.lower())
    # Deduplicate while preserving order
    seen: set[str] = set()
    result: list[str] = []
    for w in words:
        if w not in seen:
            seen.add(w)
            result.append(w)
    return result[:10]


def _safe_keyword(kw: str) -> str:
    """Escape a keyword for safe interpolation in a SPARQL string literal."""
    return _sanitize_sparql_value(kw)


def _find_related_laws(intent: str, client: SparqlClient) -> list[dict[str, str]]:
    """Find laws related to the intent via keyword search."""
    keywords = _extract_keywords(intent)
    all_laws: list[dict[str, str]] = []
    seen_uris: set[str] = set()
    for kw in keywords[:5]:
        query = _RELATED_LAWS_QUERY.format(keyword=_safe_keyword(kw))
        try:
            rows = client.query(query)
        except Exception:
            logger.warning("SPARQL query failed for keyword=%s", kw)
            continue
        for row in rows:
            uri = row.get("law", "")
            if uri and uri not in seen_uris:
                seen_uris.add(uri)
                all_laws.append({"uri": uri, "label": row.get("label", "")})
    return all_laws[:5]


def _run_research_queries(
    intent: str, clarifications: list[dict[str, Any]], client: SparqlClient
) -> dict[str, Any]:
    """Execute SPARQL queries for ontology research (Step 3).

    Each item dict carries a ``relation`` key (canonical estleg
    predicate URI) classifying how the entity participates in the legal
    framework. The same URI can appear under multiple ``relation``
    values when the entity engages in several relations (e.g. a
    provision that both ``amends`` and ``transposesDirective``);
    dedup is per ``(uri, relation)`` pair so the Step 3 renderer can
    group by Estonian legal phrase. The ``references`` predicate is
    used as a neutral fallback for entities that match the keyword
    filter but participate in no other canonical relation.
    """
    # Combine keywords from intent and clarification answers
    combined_text = intent
    for c in clarifications:
        if c.get("answer"):
            combined_text += " " + c["answer"]

    keywords = _extract_keywords(combined_text)

    provisions: list[dict[str, str]] = []
    eu_directives: list[dict[str, str]] = []
    court_decisions: list[dict[str, str]] = []
    topic_clusters: list[dict[str, str]] = []

    # Dedup per (uri, relation) so we can show the same entity under
    # multiple relation groups when applicable.
    seen_pairs: set[tuple[str, str]] = set()

    for kw in keywords[:5]:
        escaped_kw = _safe_keyword(kw)
        # Provisions
        try:
            rows = client.query(_PROVISIONS_BY_KEYWORD_QUERY.format(keyword=escaped_kw))
            for row in rows:
                uri = row.get("provision", "")
                relation = row.get("relation", "")
                key = (uri, relation)
                if uri and key not in seen_pairs:
                    seen_pairs.add(key)
                    provisions.append(
                        {
                            "uri": uri,
                            "label": row.get("label", ""),
                            "act_label": row.get("actLabel", ""),
                            "relation": relation,
                        }
                    )
        except Exception:
            logger.warning("Provisions query failed for keyword=%s", kw)

        # EU directives
        try:
            rows = client.query(_EU_DIRECTIVES_QUERY.format(keyword=escaped_kw))
            for row in rows:
                uri = row.get("directive", "")
                relation = row.get("relation", "")
                key = (uri, relation)
                if uri and key not in seen_pairs:
                    seen_pairs.add(key)
                    eu_directives.append(
                        {
                            "uri": uri,
                            "label": row.get("label", ""),
                            "relation": relation,
                        }
                    )
        except Exception:
            logger.warning("EU directives query failed for keyword=%s", kw)

        # Court decisions
        try:
            rows = client.query(_COURT_DECISIONS_QUERY.format(keyword=escaped_kw))
            for row in rows:
                uri = row.get("decision", "")
                relation = row.get("relation", "")
                key = (uri, relation)
                if uri and key not in seen_pairs:
                    seen_pairs.add(key)
                    court_decisions.append(
                        {
                            "uri": uri,
                            "label": row.get("label", ""),
                            "relation": relation,
                        }
                    )
        except Exception:
            logger.warning("Court decisions query failed for keyword=%s", kw)

        # Topic clusters
        try:
            rows = client.query(_TOPIC_CLUSTERS_QUERY.format(keyword=escaped_kw))
            for row in rows:
                uri = row.get("cluster", "")
                relation = row.get("relation", "")
                key = (uri, relation)
                if uri and key not in seen_pairs:
                    seen_pairs.add(key)
                    topic_clusters.append(
                        {
                            "uri": uri,
                            "label": row.get("label", ""),
                            "relation": relation,
                        }
                    )
        except Exception:
            logger.warning("Topic clusters query failed for keyword=%s", kw)

    return {
        "provisions": provisions,
        "eu_directives": eu_directives,
        "court_decisions": court_decisions,
        "topic_clusters": topic_clusters,
    }


def _find_similar_laws(research: dict[str, Any], client: SparqlClient) -> list[dict[str, str]]:
    """Find structurally similar laws based on topic cluster overlap.

    Legacy fallback retained for back-compat with tests that still patch
    this name. New code paths should use
    :func:`_find_similar_provisions` (A5b) which uses the full hybrid
    similarity engine instead of an act-label dedup.
    """
    # Use the first few provisions' act labels as candidates
    provisions = research.get("provisions", [])
    act_labels: list[str] = []
    seen: set[str] = set()
    for p in provisions:
        label = p.get("act_label", "")
        if label and label not in seen:
            seen.add(label)
            act_labels.append(label)
    return [{"label": label} for label in act_labels[:3]]


# A5b — Koostaja Step 3 + 4 integration.
#
# How many seed provisions we draw from the research bucket. Each seed
# triggers two SPARQL hits + one embedding lookup (cheap when there are
# no chunks), so we keep the seed count modest.
_SIMILAR_PROVISIONS_SEED_COUNT = 3
# How many merged candidates we surface in research_data["similar_provisions"].
_SIMILAR_PROVISIONS_LIMIT = 10
# How many of those are injected into the Step 4 draft prompts as
# verbatim provision text (the plan: "actual TEXT of the closest
# provision matches, not just act names").
_SIMILAR_PROVISIONS_TEXT_INJECT = 3


def _find_similar_provisions(
    research: dict[str, Any],
    *,
    sparql_client: SparqlClient | None = None,
) -> list[dict[str, Any]]:
    """A5b: find similar provisions across the three hybrid tracks.

    Seeded on the top N provisions discovered during Step 3 ontology
    research. Returns merged + de-duplicated rows in a flat dict shape
    (no :class:`SimilarityRow` leakage into the encrypted research
    payload — JSON-friendly).

    Privacy: this runs on **server-side ontology research**, not on
    the user's free-text intent. The embedding call (if it fires) is
    seeded on the *labels* of already-resolved provisions; the same
    labels are public ontology data already in pgvector. No new user
    text crosses any SaaS boundary here.
    """
    seed_provisions = list(research.get("provisions") or [])[:_SIMILAR_PROVISIONS_SEED_COUNT]
    if not seed_provisions:
        return []

    # Lazy-import so the legacy code paths don't pay the cost.
    from app.analyysikeskus.similarity import find_similar

    out_by_uri: dict[str, dict[str, Any]] = {}
    for seed in seed_provisions:
        seed_uri = str(seed.get("uri") or "").strip()
        seed_label = str(seed.get("label") or "").strip()
        if not seed_uri:
            continue
        try:
            rows = find_similar(
                seed_uri=seed_uri,
                query_text=seed_label or None,
                limit=_SIMILAR_PROVISIONS_LIMIT,
                sparql_client=sparql_client,
            )
        except Exception:
            logger.warning(
                "drafter: similar-provision lookup failed for seed=%r",
                seed_uri,
                exc_info=True,
            )
            continue
        for r in rows:
            if not r.entity_uri or r.entity_uri == seed_uri:
                continue
            existing = out_by_uri.get(r.entity_uri)
            if existing is None or r.score > existing["score"]:
                out_by_uri[r.entity_uri] = {
                    "uri": r.entity_uri,
                    "label": r.label or r.entity_uri.rsplit("#", 1)[-1] or "",
                    "score": float(r.score),
                    "reasons": list(r.reasons),
                    "snippet": r.snippet,
                    "seed_uri": seed_uri,
                }

    # Sort by score desc, stable on URI for determinism.
    out = sorted(
        out_by_uri.values(),
        key=lambda d: (-float(d["score"]), str(d["uri"])),
    )
    return out[:_SIMILAR_PROVISIONS_LIMIT]


def _similar_provisions_text_for_prompt(research: dict[str, Any]) -> str:
    """Return the Step 4 prompt's ``{similar_laws}`` block from research data.

    Per the A5b plan: inject the **actual text** of the closest provision
    matches (not just act names) so drafted clauses can mirror
    established wording. We use the snippet from the embedding track
    when available; otherwise we degrade to the label + URI tail.

    Returns the literal "(no similar provisions found)" sentinel when
    nothing is available, matching the existing prompt's "no similar
    laws found" idiom so the prompt template never has a blank line.
    """
    similar = list(research.get("similar_provisions") or [])
    if not similar:
        # Back-compat: if A5b research hasn't populated similar_provisions
        # yet (older sessions), fall back to the act-label list.
        return ""
    parts: list[str] = []
    for row in similar[:_SIMILAR_PROVISIONS_TEXT_INJECT]:
        label = str(row.get("label") or "").strip()
        snippet = str(row.get("snippet") or "").strip()
        uri = str(row.get("uri") or "").strip()
        header = label or uri.rsplit("#", 1)[-1] or "Sarnane säte"
        if snippet:
            parts.append(f'- {header}\n  Sõnastus: "{snippet}"')
        else:
            parts.append(f"- {header}")
    return "\n".join(parts) if parts else ""


def _format_clarifications_for_prompt(clarifications: list[dict[str, Any]]) -> str:
    """Format clarification Q&A pairs for inclusion in prompts."""
    parts: list[str] = []
    for i, c in enumerate(clarifications, 1):
        q = c.get("question", "")
        a = c.get("answer", "Vastamata")
        parts.append(f"Q{i}: {q}\nA{i}: {a}")
    return "\n\n".join(parts) if parts else "(no clarifications)"


def _filter_research_for_section(research: dict[str, Any], section: dict[str, Any]) -> str:
    """Select research findings relevant to a specific section.

    For now, return the first few provisions, EU directives, and (per
    A5b) the closest similar-provision *snippets* as context. Phase 3B
    can add semantic relevance ranking.
    """
    parts: list[str] = []
    for p in research.get("provisions", [])[:5]:
        parts.append(f"- {p.get('label', '')} ({p.get('act_label', '')})")
    for eu in research.get("eu_directives", [])[:3]:
        parts.append(f"- EU: {eu.get('label', '')}")
    # A5b: include the verbatim text of similar provisions so the LLM
    # can mirror established phrasing. The snippet is the highest-
    # matching chunk from the embedding track; act / label only when
    # no snippet is available (fallback).
    for sim in research.get("similar_provisions", [])[:_SIMILAR_PROVISIONS_TEXT_INJECT]:
        label = str(sim.get("label") or "").strip()
        snippet = str(sim.get("snippet") or "").strip()
        if snippet:
            parts.append(f'- Sarnane säte: {label} — "{snippet}"')
        elif label:
            parts.append(f"- Sarnane säte: {label}")
    return "\n".join(parts) if parts else "(no research findings)"


# ---------------------------------------------------------------------------
# Step 2: Clarification Q&A
# ---------------------------------------------------------------------------


@register_handler("drafter_clarify")
def drafter_clarify(
    payload: dict[str, Any],
    *,
    attempt: int = 1,
    max_attempts: int = 3,
    job_id: int | None = None,
) -> dict[str, Any]:
    """Generate clarifying questions for a drafting session.

    Queries the ontology for related laws, then asks the LLM to
    generate 5-8 scoping questions based on the intent + context.
    """
    session_id = UUID(str(payload["session_id"]))
    session = fetch_session(session_id)
    if session is None:
        raise ValueError(f"Drafting session {session_id} not found")

    # Cost budget guard — fail fast before burning LLM tokens
    if session.org_id:
        check_org_cost_budget(session.org_id)

    logger.info("drafter_clarify: starting for session %s", session_id)

    # Find related laws via SPARQL
    client = SparqlClient()
    related_laws = _find_related_laws(session.intent or "", client)
    laws_text = (
        "\n".join(f"- {law['label']} ({law['uri']})" for law in related_laws)
        or "(no related laws found in ontology)"
    )

    # Generate clarifying questions via LLM
    provider = get_default_provider()
    prompt = CLARIFY_PROMPT.format(intent=session.intent or "", laws=laws_text)

    # #852 E2: post-LLM parsing + persistence used to live OUTSIDE this
    # gated region, so a malformed payload or a DB write failure skipped
    # the abandon-on-final-attempt gating entirely. Everything from the
    # LLM call to the session write is now inside one gate.
    try:
        try:
            result = provider.extract_json(
                prompt,
                feature="drafter_clarify",
                user_id=session.user_id,
                org_id=session.org_id,
            )
        except Exception as exc:
            raise RuntimeError(f"LLM call failed: {exc}") from exc

        result = _require_llm_json(result, context="drafter_clarify")

        questions = result.get("questions", [])
        if not questions:
            # Fallback: generate minimal questions. Deliberately NOT an
            # error — an empty-but-valid payload (or stub mode) still has
            # a safe deterministic interview to fall back on, unlike the
            # parse-failure case handled by ``_require_llm_json`` above.
            questions = [
                {"question": "Milliseid asutusi see seadus mõjutab?", "rationale": "scope"},
                {
                    "question": "Kas see täiendab või asendab olemasolevat seadust?",
                    "rationale": "relationship",
                },
                {
                    "question": "Kas on EL-i nõudeid, mida tuleb arvestada?",
                    "rationale": "EU compliance",
                },
            ]

        clarifications = [
            {
                "question": q.get("question", ""),
                "answer": None,
                "rationale": q.get("rationale", ""),
            }
            for q in questions
        ]

        with get_connection() as conn:
            update_session(conn, session_id, clarifications=clarifications)
            conn.commit()
    except Exception:
        if attempt >= max_attempts:
            logger.exception("drafter_clarify permanently failed for session %s", session_id)
            with get_connection() as conn:
                abandon_session(conn, session_id)
                conn.commit()
        raise

    logger.info(
        "drafter_clarify: generated %d questions for session %s",
        len(clarifications),
        session_id,
    )
    return {
        "session_id": str(session_id),
        "question_count": len(clarifications),
    }


# ---------------------------------------------------------------------------
# Step 3: Ontology Research
# ---------------------------------------------------------------------------


@register_handler("drafter_research")
def drafter_research(
    payload: dict[str, Any],
    *,
    attempt: int = 1,
    max_attempts: int = 3,
    job_id: int | None = None,
) -> dict[str, Any]:
    """Run deep SPARQL queries based on intent + clarifications."""
    session_id = UUID(str(payload["session_id"]))
    session = fetch_session(session_id)
    if session is None:
        raise ValueError(f"Drafting session {session_id} not found")

    logger.info("drafter_research: starting for session %s", session_id)

    client = SparqlClient()

    try:
        research = _run_research_queries(
            session.intent or "", session.clarifications or [], client
        )
    except Exception as exc:
        if attempt >= max_attempts:
            logger.exception("drafter_research permanently failed for session %s", session_id)
            with get_connection() as conn:
                abandon_session(conn, session_id)
                conn.commit()
        raise RuntimeError(f"Research queries failed: {exc}") from exc

    # A5b: enrich the research bucket with hybrid similarity over the top
    # researched provisions. Wrapped in a try/except — a similarity
    # lookup failure must not block the session from advancing to
    # Step 3 (the legacy act-label fallback in
    # :func:`_similar_provisions_text_for_prompt` keeps Step 4 working).
    try:
        # #854: the similarity lookup may fire a Voyage embedding call
        # deep inside ``analyysikeskus.similarity`` → ``Retriever`` —
        # a chain we can't thread kwargs through. The contextvar-based
        # attribution stamps that spend with this session's owner and a
        # drafter-specific feature label instead of the old anonymous
        # ``feature="embedding"`` / user_id=org_id=NULL rows.
        from app.rag.embedding import embedding_attribution

        with embedding_attribution(
            user_id=session.user_id,
            org_id=session.org_id,
            feature="drafter_research_embedding",
        ):
            research["similar_provisions"] = _find_similar_provisions(
                research, sparql_client=client
            )
    except Exception:
        logger.warning(
            "drafter_research: similar-provision enrichment failed for session %s",
            session_id,
            exc_info=True,
        )
        research["similar_provisions"] = []

    encrypted = encrypt_text(json.dumps(research, ensure_ascii=False))

    with get_connection() as conn:
        update_session(conn, session_id, research_data_encrypted=encrypted)
        conn.commit()

    logger.info(
        "drafter_research: completed for session %s provisions=%d eu=%d court=%d clusters=%d",
        session_id,
        len(research.get("provisions", [])),
        len(research.get("eu_directives", [])),
        len(research.get("court_decisions", [])),
        len(research.get("topic_clusters", [])),
    )
    return {
        "session_id": str(session_id),
        "provision_count": len(research.get("provisions", [])),
        "eu_directive_count": len(research.get("eu_directives", [])),
        "court_decision_count": len(research.get("court_decisions", [])),
        "topic_cluster_count": len(research.get("topic_clusters", [])),
    }


# ---------------------------------------------------------------------------
# Step 4: Structure Generation
# ---------------------------------------------------------------------------


@register_handler("drafter_structure")
def drafter_structure(
    payload: dict[str, Any],
    *,
    attempt: int = 1,
    max_attempts: int = 3,
    job_id: int | None = None,
) -> dict[str, Any]:
    """Generate a proposed law structure via LLM or use VTK fixed structure."""
    session_id = UUID(str(payload["session_id"]))
    session = fetch_session(session_id)
    if session is None:
        raise ValueError(f"Drafting session {session_id} not found")

    # Cost budget guard — fail fast before burning LLM tokens
    if session.org_id:
        check_org_cost_budget(session.org_id)

    logger.info("drafter_structure: starting for session %s", session_id)

    # VTK workflow: use fixed structure, skip LLM call
    if session.workflow_type == "vtk":
        structure = copy.deepcopy(VTK_STRUCTURE)
        # Set the title based on intent
        structure["title"] = f"VTK eelanaluus: {(session.intent or '')[:100]}"

        with get_connection() as conn:
            update_session(conn, session_id, proposed_structure=structure)
            conn.commit()

        logger.info("drafter_structure: VTK fixed structure for session %s", session_id)
        return {
            "session_id": str(session_id),
            "chapters": len(structure.get("chapters", [])),
            "workflow_type": "vtk",
        }

    # Full law workflow: generate structure via LLM
    research_data: dict[str, Any] = {}
    if session.research_data_encrypted:
        try:
            research_data = json.loads(decrypt_text(session.research_data_encrypted))
        except Exception:
            logger.warning("Could not decrypt research data for session %s", session_id)

    client = SparqlClient()
    # A5b: prefer the hybrid-similarity output if the research bucket has
    # it (populated by Step 3 — :func:`drafter_research`). The injected
    # text carries the *actual wording* of the closest provision matches
    # (via the snippet from the embedding track), so drafted clauses can
    # mirror established phrasing — not just act names. Falls back to the
    # legacy act-label list for older sessions whose research_data was
    # serialised before A5b landed.
    similar_laws_text = _similar_provisions_text_for_prompt(research_data)
    if not similar_laws_text:
        similar_laws = _find_similar_laws(research_data, client)
        similar_laws_text = (
            "\n".join(f"- {law['label']}" for law in similar_laws) or "(no similar laws found)"
        )

    clarifications_text = _format_clarifications_for_prompt(session.clarifications or [])

    provider = get_default_provider()
    prompt = STRUCTURE_PROMPT.format(
        intent=session.intent or "",
        clarifications=clarifications_text,
        similar_laws=similar_laws_text,
    )

    # #852 E2: same gating fix as ``drafter_clarify`` — post-LLM parsing
    # and the session write now live inside the retry-gated region so a
    # malformed payload or DB failure also engages abandon-on-final-attempt.
    try:
        try:
            result = provider.extract_json(
                prompt,
                feature="drafter_structure",
                user_id=session.user_id,
                org_id=session.org_id,
            )
        except Exception as exc:
            raise RuntimeError(f"LLM call failed: {exc}") from exc

        structure = _require_llm_json(result, context="drafter_structure")

        # Validate structure has minimum fields. An empty-but-valid
        # payload (or stub mode) falls back to a deterministic skeleton —
        # only the parse-failure marker above is treated as a hard error.
        if "chapters" not in structure or not structure["chapters"]:
            structure = {
                "title": session.intent or "Uus seadus",
                "chapters": [
                    {
                        "number": "1. peatukk",
                        "title": "Uldsatted",
                        "sections": [
                            {"paragraph": "par 1", "title": "Seaduse reguleerimisala"},
                            {"paragraph": "par 2", "title": "Moistete selgitused"},
                        ],
                    },
                    {
                        "number": "2. peatukk",
                        "title": "Rakendussatted",
                        "sections": [
                            {"paragraph": "par 3", "title": "Seaduse joustumise aeg"},
                        ],
                    },
                ],
            }

        with get_connection() as conn:
            update_session(conn, session_id, proposed_structure=structure)
            conn.commit()
    except Exception:
        if attempt >= max_attempts:
            logger.exception("drafter_structure permanently failed for session %s", session_id)
            with get_connection() as conn:
                abandon_session(conn, session_id)
                conn.commit()
        raise

    logger.info(
        "drafter_structure: generated %d chapters for session %s",
        len(structure.get("chapters", [])),
        session_id,
    )
    return {
        "session_id": str(session_id),
        "chapters": len(structure.get("chapters", [])),
        "workflow_type": "full_law",
    }


# ---------------------------------------------------------------------------
# Step 5: Clause-by-Clause Drafting
# ---------------------------------------------------------------------------


@register_handler("drafter_draft")
def drafter_draft(
    payload: dict[str, Any],
    *,
    attempt: int = 1,
    max_attempts: int = 3,
    job_id: int | None = None,
) -> dict[str, Any]:
    """Draft legal text for every section in the proposed structure."""
    session_id = UUID(str(payload["session_id"]))
    session = fetch_session(session_id)
    if session is None:
        raise ValueError(f"Drafting session {session_id} not found")

    # Cost budget guard — fail fast before burning LLM tokens
    if session.org_id:
        check_org_cost_budget(session.org_id)

    logger.info("drafter_draft: starting for session %s", session_id)

    structure = session.proposed_structure
    if not structure or not structure.get("chapters"):
        raise ValueError(f"Session {session_id} has no proposed structure")

    # Decrypt research data
    research: dict[str, Any] = {}
    if session.research_data_encrypted:
        try:
            research = json.loads(decrypt_text(session.research_data_encrypted))
        except Exception:
            logger.warning("Could not decrypt research data for session %s", session_id)

    provider = get_default_provider()
    is_vtk = session.workflow_type == "vtk"
    clarifications_text = _format_clarifications_for_prompt(session.clarifications or [])
    clauses: list[dict[str, Any]] = []

    try:
        for chapter in structure["chapters"]:
            for section in chapter.get("sections", []):
                section_title = section.get("title", "")
                relevant_research = _filter_research_for_section(research, section)

                if is_vtk and section_title in VTK_SECTION_PROMPTS:
                    # VTK: use section-specific prompt
                    prompt = VTK_SECTION_PROMPTS[section_title].format(
                        intent=session.intent or "",
                        clarifications=clarifications_text,
                        relevant_research=relevant_research,
                    )
                else:
                    # Full law: use generic clause drafting prompt
                    prompt = DRAFT_PROMPT.format(
                        chapter_title=chapter.get("title", ""),
                        chapter_number=chapter.get("number", ""),
                        section_title=section_title,
                        paragraph=section.get("paragraph", ""),
                        intent=session.intent or "",
                        relevant_research=relevant_research,
                    )

                result = provider.extract_json(
                    prompt,
                    feature="drafter_draft",
                    user_id=session.user_id,
                    org_id=session.org_id,
                )

                # #852 E2: a parse-failure payload ({"error": ...}) or a
                # clause without text must FAIL the job (and consume the
                # retry budget) instead of being persisted as a blank
                # clause that the state machine happily waves into review.
                result = _require_llm_json(
                    result,
                    context=f"drafter_draft section {section.get('paragraph', '?')!r}",
                )
                clause_text = str(result.get("text") or "").strip()
                if not clause_text and not result.get("stub"):
                    raise LLMOutputError(
                        "drafter_draft: LLM returned no clause text for section "
                        f"{section.get('paragraph', '?')!r} ({section_title!r})"
                    )

                clauses.append(
                    {
                        "chapter": chapter.get("number", ""),
                        "chapter_title": chapter.get("title", ""),
                        "paragraph": section.get("paragraph", ""),
                        "title": section_title,
                        "text": result.get("text", ""),
                        # #842: resolve LLM citations against the ontology so
                        # only verified ones are later shown as authoritative;
                        # fabricated/unresolved ones are marked "kontrollimata".
                        "citations": resolve_citations(result.get("citations", [])),
                        "notes": result.get("notes", ""),
                    }
                )

        # #852 E2: an empty clause list means the structure had no
        # sections (or every section was skipped) — persisting it would
        # let the step-5→6 guard pass on encrypted-but-empty content.
        if not clauses:
            raise LLMOutputError(
                f"drafter_draft produced no clauses for session {session_id} "
                "(structure has no sections?)"
            )

        # The persist step sits inside the gated region too, so a DB
        # failure here also engages abandon-on-final-attempt.
        encrypted = encrypt_text(json.dumps({"clauses": clauses}, ensure_ascii=False))

        with get_connection() as conn:
            update_session(conn, session_id, draft_content_encrypted=encrypted)
            conn.commit()

    except Exception as exc:
        if attempt >= max_attempts:
            logger.exception(
                "drafter_draft permanently failed for session %s after %d attempts",
                session_id,
                attempt,
            )
            with get_connection() as conn:
                abandon_session(conn, session_id)
                conn.commit()
        raise RuntimeError(f"Clause drafting failed: {exc}") from exc

    logger.info(
        "drafter_draft: drafted %d clauses for session %s",
        len(clauses),
        session_id,
    )
    return {
        "session_id": str(session_id),
        "clause_count": len(clauses),
    }


# ---------------------------------------------------------------------------
# Step 5b: Regenerate single clause (background job)
# ---------------------------------------------------------------------------


@register_handler("drafter_regenerate_clause")
def drafter_regenerate_clause(
    payload: dict[str, Any],
    *,
    attempt: int = 1,
    max_attempts: int = 3,
    job_id: int | None = None,
) -> dict[str, Any]:
    """Regenerate a single clause via LLM and update the session."""
    session_id = UUID(str(payload["session_id"]))
    clause_index = int(payload["clause_index"])

    session = fetch_session(session_id)
    if session is None:
        raise ValueError(f"Drafting session {session_id} not found")

    # Cost budget guard — fail fast before burning LLM tokens
    if session.org_id:
        check_org_cost_budget(session.org_id)

    logger.info(
        "drafter_regenerate_clause: starting for session %s clause %d",
        session_id,
        clause_index,
    )

    # Decrypt existing clauses
    clauses: list[dict[str, Any]] = []
    if session.draft_content_encrypted:
        try:
            data = json.loads(decrypt_text(session.draft_content_encrypted))
            clauses = data.get("clauses", [])
        except Exception:
            raise ValueError(f"Cannot decrypt draft content for session {session_id}")

    if clause_index < 0 or clause_index >= len(clauses):
        raise ValueError(f"Clause index {clause_index} out of range for session {session_id}")

    clause = clauses[clause_index]

    # Decrypt research data
    research: dict[str, Any] = {}
    if session.research_data_encrypted:
        try:
            research = json.loads(decrypt_text(session.research_data_encrypted))
        except Exception:
            pass

    relevant_research = _filter_research_for_section(research, clause)
    section_title = clause.get("title", "")

    provider = get_default_provider()

    if session.workflow_type == "vtk" and section_title in VTK_SECTION_PROMPTS:
        clarifications_text = _format_clarifications_for_prompt(session.clarifications or [])
        prompt = VTK_SECTION_PROMPTS[section_title].format(
            intent=session.intent or "",
            clarifications=clarifications_text,
            relevant_research=relevant_research,
        )
    else:
        prompt = DRAFT_PROMPT.format(
            chapter_title=clause.get("chapter_title", ""),
            chapter_number=clause.get("chapter", ""),
            section_title=section_title,
            paragraph=clause.get("paragraph", ""),
            intent=session.intent or "",
            relevant_research=relevant_research,
        )

    try:
        result = provider.extract_json(
            prompt,
            feature="drafter_regenerate",
            user_id=session.user_id,
            org_id=session.org_id,
        )
        # #852 E2: a parse-failure payload must fail the job (visibly,
        # with retries) — silently keeping the old clause would look
        # like a successful regeneration that changed nothing.
        result = _require_llm_json(
            result, context=f"drafter_regenerate_clause clause {clause_index}"
        )
        # #852 review F2: same nonblank contract as ``drafter_draft`` —
        # ``{"text": ""}`` (or whitespace-only) passes the parse check
        # above but used to OVERWRITE a good clause with blank text. A
        # blank regeneration now raises (retry-gating engages) and the
        # existing clause is left untouched; only a non-blank result may
        # replace the text. Stub payloads carry no ``text`` and keep the
        # existing clause so keyless local dev still completes.
        new_text = str(result.get("text") or "").strip()
        if not new_text and not result.get("stub"):
            raise LLMOutputError(
                "drafter_regenerate_clause: LLM returned no clause text for "
                f"clause {clause_index} ({clause.get('paragraph', '?')!r})"
            )
        if new_text:
            clause["text"] = result["text"]
        # #842: resolve citations through the ontology (see drafter_draft).
        clause["citations"] = resolve_citations(
            result.get("citations", clause.get("citations", []))
        )
        clause["notes"] = result.get("notes", clause.get("notes", ""))
    except Exception as exc:
        if attempt >= max_attempts:
            logger.exception(
                "drafter_regenerate_clause permanently failed for session %s clause %d",
                session_id,
                clause_index,
            )
        raise RuntimeError(f"Clause regeneration failed: {exc}") from exc

    clauses[clause_index] = clause
    encrypted = encrypt_text(json.dumps({"clauses": clauses}, ensure_ascii=False))

    with get_connection() as conn:
        update_session(conn, session_id, draft_content_encrypted=encrypted)
        conn.commit()

    logger.info(
        "drafter_regenerate_clause: completed for session %s clause %d",
        session_id,
        clause_index,
    )
    return {
        "session_id": str(session_id),
        "clause_index": clause_index,
    }
