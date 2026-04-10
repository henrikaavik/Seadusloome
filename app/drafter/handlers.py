"""Background job handlers for the drafter pipeline (Steps 2-5).

Each handler follows the Phase 2 convention established by
:mod:`app.docs.analyze_handler`:

    - Registered via ``@register_handler(job_type)``
    - Accepts ``(payload, *, attempt, max_attempts)``
    - Returns a summary dict persisted in ``background_jobs.result``
    - Raises on failure; only marks domain state as ``abandoned`` on the
      final attempt (#448 retry-gating pattern)

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


# ---------------------------------------------------------------------------
# SPARQL helpers
# ---------------------------------------------------------------------------

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

_PROVISIONS_BY_KEYWORD_QUERY = """\
PREFIX estleg: <https://data.riik.ee/ontology/estleg#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>

SELECT DISTINCT ?provision ?label ?actLabel WHERE {{
  ?provision estleg:paragrahv ?paragrahv .
  ?provision rdfs:label ?label .
  ?provision estleg:sourceAct ?act .
  ?act rdfs:label ?actLabel .
  FILTER(CONTAINS(LCASE(?label), LCASE("{keyword}")))
}}
LIMIT 20
"""

_EU_DIRECTIVES_QUERY = """\
PREFIX estleg: <https://data.riik.ee/ontology/estleg#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>

SELECT DISTINCT ?directive ?label WHERE {{
  ?directive a estleg:EULegislation .
  ?directive rdfs:label ?label .
  FILTER(CONTAINS(LCASE(?label), LCASE("{keyword}")))
}}
LIMIT 10
"""

_COURT_DECISIONS_QUERY = """\
PREFIX estleg: <https://data.riik.ee/ontology/estleg#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>

SELECT DISTINCT ?decision ?label WHERE {{
  ?decision a estleg:CourtDecision .
  ?decision rdfs:label ?label .
  FILTER(CONTAINS(LCASE(?label), LCASE("{keyword}")))
}}
LIMIT 10
"""

_TOPIC_CLUSTERS_QUERY = """\
PREFIX estleg: <https://data.riik.ee/ontology/estleg#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>

SELECT DISTINCT ?cluster ?label WHERE {{
  ?cluster a estleg:TopicCluster .
  ?cluster rdfs:label ?label .
  FILTER(CONTAINS(LCASE(?label), LCASE("{keyword}")))
}}
LIMIT 10
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
    """Execute SPARQL queries for ontology research (Step 3)."""
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

    seen_uris: set[str] = set()

    for kw in keywords[:5]:
        escaped_kw = _safe_keyword(kw)
        # Provisions
        try:
            rows = client.query(_PROVISIONS_BY_KEYWORD_QUERY.format(keyword=escaped_kw))
            for row in rows:
                uri = row.get("provision", "")
                if uri and uri not in seen_uris:
                    seen_uris.add(uri)
                    provisions.append(
                        {
                            "uri": uri,
                            "label": row.get("label", ""),
                            "act_label": row.get("actLabel", ""),
                        }
                    )
        except Exception:
            logger.warning("Provisions query failed for keyword=%s", kw)

        # EU directives
        try:
            rows = client.query(_EU_DIRECTIVES_QUERY.format(keyword=escaped_kw))
            for row in rows:
                uri = row.get("directive", "")
                if uri and uri not in seen_uris:
                    seen_uris.add(uri)
                    eu_directives.append(
                        {
                            "uri": uri,
                            "label": row.get("label", ""),
                        }
                    )
        except Exception:
            logger.warning("EU directives query failed for keyword=%s", kw)

        # Court decisions
        try:
            rows = client.query(_COURT_DECISIONS_QUERY.format(keyword=escaped_kw))
            for row in rows:
                uri = row.get("decision", "")
                if uri and uri not in seen_uris:
                    seen_uris.add(uri)
                    court_decisions.append(
                        {
                            "uri": uri,
                            "label": row.get("label", ""),
                        }
                    )
        except Exception:
            logger.warning("Court decisions query failed for keyword=%s", kw)

        # Topic clusters
        try:
            rows = client.query(_TOPIC_CLUSTERS_QUERY.format(keyword=escaped_kw))
            for row in rows:
                uri = row.get("cluster", "")
                if uri and uri not in seen_uris:
                    seen_uris.add(uri)
                    topic_clusters.append(
                        {
                            "uri": uri,
                            "label": row.get("label", ""),
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
    """Find structurally similar laws based on topic cluster overlap."""
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

    For now, return the first few provisions and EU directives as
    context. Phase 3B can add semantic relevance ranking.
    """
    parts: list[str] = []
    for p in research.get("provisions", [])[:5]:
        parts.append(f"- {p.get('label', '')} ({p.get('act_label', '')})")
    for eu in research.get("eu_directives", [])[:3]:
        parts.append(f"- EU: {eu.get('label', '')}")
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

    try:
        result = provider.extract_json(
            prompt,
            feature="drafter_clarify",
            user_id=session.user_id,
            org_id=session.org_id,
        )
    except Exception as exc:
        if attempt >= max_attempts:
            logger.exception("drafter_clarify permanently failed for session %s", session_id)
            with get_connection() as conn:
                abandon_session(conn, session_id)
                conn.commit()
        raise RuntimeError(f"LLM call failed: {exc}") from exc

    questions = result.get("questions", [])
    if not questions:
        # Fallback: generate minimal questions
        questions = [
            {"question": "Milliseid asutusi see seadus mojutab?", "rationale": "scope"},
            {
                "question": "Kas see taiendab voi asendab olemasolevat seadust?",
                "rationale": "relationship",
            },
            {
                "question": "Kas on EL-i noudeid, mida tuleb arvestada?",
                "rationale": "EU compliance",
            },
        ]

    clarifications = [
        {"question": q.get("question", ""), "answer": None, "rationale": q.get("rationale", "")}
        for q in questions
    ]

    with get_connection() as conn:
        update_session(conn, session_id, clarifications=clarifications)
        conn.commit()

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

    try:
        result = provider.extract_json(
            prompt,
            feature="drafter_structure",
            user_id=session.user_id,
            org_id=session.org_id,
        )
    except Exception as exc:
        if attempt >= max_attempts:
            logger.exception("drafter_structure permanently failed for session %s", session_id)
            with get_connection() as conn:
                abandon_session(conn, session_id)
                conn.commit()
        raise RuntimeError(f"LLM call failed: {exc}") from exc

    structure = result
    # Validate structure has minimum fields
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

                clauses.append(
                    {
                        "chapter": chapter.get("number", ""),
                        "chapter_title": chapter.get("title", ""),
                        "paragraph": section.get("paragraph", ""),
                        "title": section_title,
                        "text": result.get("text", ""),
                        "citations": result.get("citations", []),
                        "notes": result.get("notes", ""),
                    }
                )

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

    encrypted = encrypt_text(json.dumps({"clauses": clauses}, ensure_ascii=False))

    with get_connection() as conn:
        update_session(conn, session_id, draft_content_encrypted=encrypted)
        conn.commit()

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
        clause["text"] = result.get("text", clause.get("text", ""))
        clause["citations"] = result.get("citations", clause.get("citations", []))
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
