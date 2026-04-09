# Phase 3 Design: AI Advisory Chat + AI Law Drafter

**Status:** Approved (reconciled 2026-04-09 against `docs/nfr-baseline.md`)
**Date:** 2026-04-09
**Depends on:** Phase 2 (drafts, impact analysis, LLMProvider stub, notifications infrastructure)

**Non-functional requirements:** This phase must meet all requirements in [`docs/nfr-baseline.md`](../../nfr-baseline.md). In particular: chat transcripts and drafter content must be encrypted at rest (NFR §6), all LLM calls must pass through the PII scrubber (NFR §7), rate limits per NFR §8, uncertainty UX per NFR §11, and audit events per NFR §5.

**LLM provider policy:** Both Claude and Codex are first-class adapters (NFR §3). Claude is the default. Both `ClaudeProvider` and `CodexProvider` must implement the full `LLMProvider` interface. Tests must use VCR cassettes recorded against both providers for the critical paths.

---

## 1. Goals

Phase 3 delivers the two most distinctive AI features:

1. **AI Advisory Chat** — A conversational assistant grounded in the ontology + RAG, with streaming responses and tool use
2. **AI Law Drafter** — A 7-step guided workflow that turns natural-language legislative intent into a full draft law document

**End-to-end milestones:**

> **Chat:** A drafter asks "Kuidas mõjutab see VTK olemasolevat tsiviilseadustikku?" while viewing a draft's impact report. The AI retrieves relevant ontology chunks, cross-references the impact analysis, and answers with citations.

> **Drafter:** A ministry official writes "Soovin luua seaduse, mis reguleerib tehisintellekti kasutamist avalikus sektoris." The AI asks 8 clarifying questions, researches related EU AI Act provisions and existing Estonian digital services legislation, proposes a 12-chapter structure, drafts each clause, integrates the result through Phase 2's impact analysis, and exports a formatted .docx.

---

## 2. Architecture Additions

### 2.1 New PostgreSQL tables

```sql
-- Vector embeddings for RAG
CREATE TABLE rag_chunks (
    id              BIGSERIAL PRIMARY KEY,
    source_type     TEXT NOT NULL CHECK (source_type IN ('ontology', 'draft', 'law_text', 'court_decision')),
    source_uri      TEXT NOT NULL,
    chunk_index     INTEGER NOT NULL,
    content         TEXT NOT NULL,
    metadata        JSONB,
    embedding       VECTOR(1024),             -- Voyage AI voyage-multilingual-2 = 1024 dims
    created_at      TIMESTAMPTZ DEFAULT now(),
    UNIQUE(source_type, source_uri, chunk_index)
);

CREATE INDEX idx_rag_chunks_embedding ON rag_chunks USING ivfflat (embedding vector_cosine_ops);
CREATE INDEX idx_rag_chunks_source ON rag_chunks(source_type, source_uri);

-- Chat conversations
CREATE TABLE conversations (
    id              UUID PRIMARY KEY,
    user_id         UUID REFERENCES users(id),
    org_id          UUID REFERENCES organizations(id),
    title           TEXT NOT NULL,
    context_draft_id UUID REFERENCES drafts(id),  -- optional, conversation is about a specific draft
    created_at      TIMESTAMPTZ DEFAULT now(),
    updated_at      TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE messages (
    id              UUID PRIMARY KEY,
    conversation_id UUID REFERENCES conversations(id) ON DELETE CASCADE,
    role            TEXT NOT NULL CHECK (role IN ('system', 'user', 'assistant', 'tool')),
    content         TEXT NOT NULL,
    tool_name       TEXT,              -- for tool messages
    tool_input      JSONB,
    tool_output     JSONB,
    rag_context     JSONB,             -- chunks retrieved for this turn
    tokens_input    INTEGER,
    tokens_output   INTEGER,
    model           TEXT,
    created_at      TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_messages_conversation ON messages(conversation_id, created_at);

-- Law Drafter state machine
CREATE TABLE drafting_sessions (
    id              UUID PRIMARY KEY,
    user_id         UUID REFERENCES users(id),
    org_id          UUID REFERENCES organizations(id),
    workflow        TEXT NOT NULL CHECK (workflow IN ('vtk', 'full_law')),
    current_step    INTEGER NOT NULL DEFAULT 1,
    status          TEXT NOT NULL CHECK (status IN ('active', 'paused', 'completed', 'abandoned')),
    intent          TEXT NOT NULL,
    clarifications  JSONB DEFAULT '[]'::jsonb,    -- list of {question, answer}
    research_data   JSONB,                         -- SPARQL results from step 3
    proposed_structure JSONB,                      -- law outline from step 4
    draft_content   JSONB,                         -- clause-by-clause content
    integrated_draft_id UUID REFERENCES drafts(id), -- link to Phase 2 draft created in step 6
    export_path     TEXT,
    created_at      TIMESTAMPTZ DEFAULT now(),
    updated_at      TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE drafting_session_versions (
    id              BIGSERIAL PRIMARY KEY,
    session_id      UUID REFERENCES drafting_sessions(id) ON DELETE CASCADE,
    step            INTEGER NOT NULL,
    snapshot        JSONB NOT NULL,
    created_at      TIMESTAMPTZ DEFAULT now()
);

-- LLM cost tracking
CREATE TABLE llm_usage (
    id              BIGSERIAL PRIMARY KEY,
    user_id         UUID REFERENCES users(id),
    provider        TEXT NOT NULL,          -- 'claude', 'voyage', etc.
    model           TEXT NOT NULL,
    feature         TEXT NOT NULL,          -- 'chat', 'draft_extract', 'draft_clarify', etc.
    tokens_input    INTEGER NOT NULL,
    tokens_output   INTEGER NOT NULL,
    cost_usd        NUMERIC(10, 6),
    created_at      TIMESTAMPTZ DEFAULT now()
);
```

### 2.2 New Python modules

```
app/
├── ai/
│   ├── __init__.py
│   ├── llm_provider.py       # Abstract LLMProvider + ClaudeProvider (extended from Phase 2)
│   ├── embedding_provider.py # EmbeddingProvider abstraction + VoyageProvider
│   ├── cost_tracker.py       # Per-call cost calculation + logging
│   ├── streaming.py          # Streaming response helpers
│   └── prompts.py            # Reusable prompt templates
├── rag/
│   ├── __init__.py
│   ├── ingestion.py          # Ingest ontology → chunks → embeddings
│   ├── retriever.py          # Query → retrieve top-k chunks
│   ├── chunker.py            # Text chunking (sliding window with overlap)
│   └── sync_hook.py          # Re-ingest when ontology syncs
├── chat/
│   ├── __init__.py
│   ├── pages.py              # /chat route (full-page UI)
│   ├── websocket.py          # /ws/chat WebSocket handler
│   ├── orchestrator.py       # Orchestrates RAG + tool use + LLM streaming
│   ├── tools.py              # Tool definitions for Claude tool use
│   └── history.py            # Conversation + message persistence
├── drafter/
│   ├── __init__.py
│   ├── pages.py              # Multi-step wizard routes
│   ├── state_machine.py      # Step definitions, transitions, validation
│   ├── steps/
│   │   ├── intent.py         # Step 1: intent capture
│   │   ├── clarify.py        # Step 2: clarification Q&A
│   │   ├── research.py       # Step 3: ontology research
│   │   ├── structure.py      # Step 4: propose structure
│   │   ├── draft.py          # Step 5: clause-by-clause drafting
│   │   ├── review.py         # Step 6: integrated review via Phase 2
│   │   └── export.py         # Step 7: .docx export
│   ├── prompts.py            # Drafter-specific prompts
│   └── docx_template.py      # Estonian legislative formatting
└── evals/
    ├── __init__.py
    ├── chat_evals.py         # Evaluation scenarios for chat
    ├── drafter_evals.py      # Evaluation scenarios for drafter
    └── run_evals.py          # CLI runner
```

---

## 3. LLM Provider Abstraction

### 3.1 Full interface

```python
class LLMProvider(ABC):
    @abstractmethod
    async def complete(self, prompt: str, model: str, max_tokens: int,
                       temperature: float = 0.0,
                       system: str | None = None) -> CompletionResult: ...

    @abstractmethod
    async def stream(self, prompt: str, model: str, max_tokens: int,
                     temperature: float = 0.0,
                     system: str | None = None) -> AsyncIterator[StreamEvent]: ...

    @abstractmethod
    async def tool_use(self, messages: list[Message], tools: list[Tool],
                       model: str, max_tokens: int,
                       system: str | None = None) -> ToolUseResult: ...

@dataclass
class CompletionResult:
    content: str
    tokens_input: int
    tokens_output: int
    model: str
    finish_reason: str

@dataclass
class StreamEvent:
    type: Literal["content", "tool_use", "stop"]
    delta: str | None = None
    tool_name: str | None = None
    tool_input: dict | None = None
```

### 3.2 Claude implementation

Uses `anthropic.AsyncAnthropic` with:
- Prompt caching for the system prompt (RAG context header)
- Streaming via `messages.stream()`
- Tool use via `messages.create(tools=[...])`
- Model: `claude-sonnet-4-6` for chat, `claude-opus-4-6` for drafter (higher quality for long-form generation)

### 3.3 Cost tracking

Every LLM call is wrapped:

```python
async def with_cost_tracking(feature: str, user_id: UUID, fn):
    result = await fn()
    log_usage(
        user_id=user_id,
        provider=provider.name,
        model=result.model,
        feature=feature,
        tokens_input=result.tokens_input,
        tokens_output=result.tokens_output,
        cost_usd=calculate_cost(provider.name, result.model, result.tokens_input, result.tokens_output),
    )
    return result
```

Cost rates stored in `app/ai/pricing.py`:

```python
PRICING = {
    ("claude", "claude-sonnet-4-6"): {"input": 3.00, "output": 15.00},  # $/M tokens
    ("claude", "claude-opus-4-6"): {"input": 15.00, "output": 75.00},
    ("voyage", "voyage-multilingual-2"): {"input": 0.12, "output": 0.0},
}
```

---

## 4. RAG Pipeline

### 4.1 Embedding provider

```python
class EmbeddingProvider(ABC):
    @abstractmethod
    async def embed(self, texts: list[str]) -> list[list[float]]: ...

    @property
    @abstractmethod
    def dimensions(self) -> int: ...

class VoyageProvider(EmbeddingProvider):
    def __init__(self, api_key: str):
        self.client = voyageai.AsyncClient(api_key=api_key)
        self.model = "voyage-multilingual-2"

    async def embed(self, texts: list[str]) -> list[list[float]]:
        result = await self.client.embed(texts=texts, model=self.model, input_type="document")
        return result.embeddings

    @property
    def dimensions(self) -> int:
        return 1024
```

**Swapping providers** requires only changing `EMBEDDING_PROVIDER` env var and running `scripts/reindex_rag.py`. Future MS AI Foundry provider fits the same interface.

### 4.2 Ingestion

Ingestion happens:
1. **Initial bulk load** — `scripts/ingest_rag.py` runs once after ontology sync
2. **Incremental updates** — hook in `app/sync/orchestrator.py` re-ingests changed entities after sync completes
3. **Draft ingestion** — on draft upload (Phase 2), draft text is also chunked and embedded

### 4.3 Chunking strategy

- Per provision / court decision: each entity becomes 1+ chunks
- Short entities (<500 chars): single chunk
- Long entities: sliding window of 800 chars with 150 char overlap
- Metadata preserved: `{"entity_uri": "...", "type": "provision", "source_act": "TsiviilS"}`
- Estonian-aware chunking: split at sentence boundaries, avoid breaking `§` references

### 4.4 Retrieval

```python
class Retriever:
    def __init__(self, embeddings: EmbeddingProvider):
        self.embeddings = embeddings

    async def retrieve(self, query: str, k: int = 10,
                       filter: dict | None = None) -> list[RetrievedChunk]:
        # Embed query
        [query_embedding] = await self.embeddings.embed([query])

        # Build WHERE clause from filter
        where_clause = self._build_filter(filter)

        # pgvector cosine similarity search
        sql = f"""
            SELECT id, source_type, source_uri, content, metadata,
                   1 - (embedding <=> %s::vector) AS similarity
            FROM rag_chunks
            {where_clause}
            ORDER BY embedding <=> %s::vector
            LIMIT %s
        """
        # ... execute and return ...
```

### 4.5 Hybrid retrieval (future)

Phase 3 ships with pure vector search. Hybrid (BM25 + vector, with reranking) is noted as a Phase 6 optimization if quality issues arise.

---

## 5. AI Advisory Chat

### 5.1 UI

**Route:** `GET /chat` (list of conversations) and `GET /chat/{conversation_id}` (specific chat)

**Layout:**
- Left sidebar: conversation list with titles, "New chat" button
- Main area: message history with streaming support
- Optional right panel: "context" (showing retrieved RAG chunks + tool calls for the current turn, toggleable)
- Bottom: textarea + send button
- Chat can be opened in "draft context" mode: when `?draft=ID` is passed, the conversation is bound to that draft and the system prompt includes its impact report

### 5.2 WebSocket protocol

**Client → Server:**
```json
{"type": "send_message", "content": "Kuidas see eelnõu mõjutab tsiviilseadustikku?"}
```

**Server → Client (streaming):**
```json
{"type": "retrieval_started"}
{"type": "retrieval_done", "chunks": [...]}
{"type": "tool_use", "tool": "query_ontology", "input": {...}}
{"type": "tool_result", "tool": "query_ontology", "output": {...}}
{"type": "content_delta", "delta": "Eelnõu "}
{"type": "content_delta", "delta": "muudab "}
{"type": "content_delta", "delta": "oluliselt §..."}
{"type": "done", "message_id": "uuid"}
```

### 5.3 Orchestrator

```python
class ChatOrchestrator:
    def __init__(self, llm: LLMProvider, retriever: Retriever, sparql: SparqlClient):
        self.llm = llm
        self.retriever = retriever
        self.sparql = sparql

    async def handle_message(self, conversation_id: UUID, user_message: str,
                             send: Callable) -> None:
        # 1. Load conversation history
        history = load_messages(conversation_id)

        # 2. RAG retrieval
        await send({"type": "retrieval_started"})
        chunks = await self.retriever.retrieve(user_message, k=10)
        await send({"type": "retrieval_done", "chunks": chunks})

        # 3. Assemble system prompt with retrieved context
        system_prompt = build_chat_system_prompt(chunks, history.context_draft_id)

        # 4. Build messages for Claude
        messages = history.to_claude_messages() + [{"role": "user", "content": user_message}]

        # 5. Stream response with tool use enabled
        async for event in self.llm.stream_with_tools(
            messages=messages, tools=CHAT_TOOLS,
            system=system_prompt, model="claude-sonnet-4-6"
        ):
            await send(event.to_ws_dict())

            if event.type == "tool_use":
                # Execute the tool
                result = await execute_tool(event.tool_name, event.tool_input, self.sparql)
                await send({"type": "tool_result", "tool": event.tool_name, "output": result})
                # Append to messages and re-stream
                messages.append({"role": "assistant", "content": [{"type": "tool_use", ...}]})
                messages.append({"role": "user", "content": [{"type": "tool_result", ...}]})

        # 6. Persist final message with full RAG context
        save_message(conversation_id, role="assistant", content=full_content,
                     rag_context=chunks, tokens=...)
```

### 5.4 Tools for Claude

Defined in `app/chat/tools.py`:

```python
CHAT_TOOLS = [
    {
        "name": "query_ontology",
        "description": "Execute a SPARQL query against the Estonian Legal Ontology",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "SPARQL SELECT query"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "search_provisions",
        "description": "Search legal provisions by keyword",
        "input_schema": {
            "type": "object",
            "properties": {
                "keywords": {"type": "string"},
                "limit": {"type": "integer", "default": 10},
            },
            "required": ["keywords"],
        },
    },
    {
        "name": "get_draft_impact",
        "description": "Retrieve the impact report for a specific draft",
        "input_schema": {
            "type": "object",
            "properties": {"draft_id": {"type": "string"}},
            "required": ["draft_id"],
        },
    },
    {
        "name": "get_provision_versions",
        "description": "Get the version history of a specific provision",
        "input_schema": {
            "type": "object",
            "properties": {"provision_uri": {"type": "string"}},
            "required": ["provision_uri"],
        },
    },
]
```

### 5.5 System prompt template

```
You are a legal advisory assistant for Estonian government officials. You help drafters,
reviewers, and ministry staff understand how proposed legislation connects to the existing
legal framework.

Your knowledge base is the Estonian Legal Ontology, accessible via SPARQL queries through
the `query_ontology` tool. You can also search provisions, retrieve draft impact reports,
and get provision version histories.

RULES:
1. Always cite your sources by URI or act name + paragraph
2. When unsure, execute a SPARQL query to verify rather than guessing
3. Use Estonian legal terminology consistently
4. Flag any legal claims you cannot verify with a disclaimer
5. For drafting suggestions, show the existing law's language as reference
6. Do not provide final legal advice — you assist research, not replace lawyers

RETRIEVED CONTEXT:
{chunks}

{draft_context_if_any}
```

---

## 6. AI Law Drafter

### 6.1 Wizard structure

Routes under `/drafter/`:

- `GET /drafter` — dashboard: list active sessions, "Start new draft" button
- `GET /drafter/new` — workflow selection (VTK / Full law)
- `GET /drafter/{session_id}` — redirects to current step
- `GET /drafter/{session_id}/step/{n}` — specific step view
- `POST /drafter/{session_id}/step/{n}` — step submission
- `GET /drafter/{session_id}/export` — download final .docx

### 6.2 State machine

```python
class Step(IntEnum):
    INTENT = 1
    CLARIFY = 2
    RESEARCH = 3
    STRUCTURE = 4
    DRAFT = 5
    REVIEW = 6
    EXPORT = 7

@dataclass
class StepTransition:
    current: Step
    next: Step
    guard: Callable[[DraftingSession], bool]  # returns True if transition allowed

TRANSITIONS = [
    StepTransition(Step.INTENT, Step.CLARIFY, lambda s: bool(s.intent)),
    StepTransition(Step.CLARIFY, Step.RESEARCH,
                   lambda s: len(s.clarifications) >= MIN_CLARIFICATIONS),
    StepTransition(Step.RESEARCH, Step.STRUCTURE, lambda s: s.research_data is not None),
    StepTransition(Step.STRUCTURE, Step.DRAFT, lambda s: s.proposed_structure is not None),
    StepTransition(Step.DRAFT, Step.REVIEW,
                   lambda s: s.draft_content and len(s.draft_content.get("clauses", [])) > 0),
    StepTransition(Step.REVIEW, Step.EXPORT, lambda s: s.integrated_draft_id is not None),
]
```

Each step transition creates a `drafting_session_versions` snapshot for audit and rollback.

### 6.3 Step 1 — Intent Capture

Form with a textarea (max 2000 chars) for legislative intent in Estonian. Examples shown below the form.

On submit: save intent, advance to step 2, enqueue clarification question generation.

### 6.4 Step 2 — Clarification Q&A

LLM generates 5-10 clarifying questions based on the intent + initial ontology research:

```
Prompt to Claude:
"The user wants to create a law: '{intent}'

Here are related existing laws I found in the ontology:
{top_5_related_laws}

Generate 5-10 clarifying questions to scope the legislation. Cover:
- Which institutions/entities are affected?
- Relationship to existing laws (supplement vs replace)?
- EU compliance requirements
- Enforcement mechanisms
- Transition periods
- Specific edge cases based on the intent

Return as JSON array: [{\"question\": \"...\", \"rationale\": \"...\"}, ...]"
```

UI shows questions one at a time (or all at once — user choice). User answers each. State saved after each answer.

### 6.5 Step 3 — Ontology Research

Background job runs deep SPARQL queries based on intent + clarifications:
- All provisions containing keywords from intent
- All EU directives with matching subject matter
- All court decisions interpreting similar concepts
- Related topic clusters
- Version histories of potentially affected provisions
- Previous VTKs in related areas

Results stored in `research_data` JSONB. UI shows a summary card per category with counts, drill-down to full lists.

### 6.6 Step 4 — Structure Generation

LLM proposes a chapter/section outline:

```
Prompt:
"Based on the intent '{intent}' and research findings, propose a law structure following
Estonian legislative conventions. Similar laws for reference:

{top_3_similar_laws_with_structure}

Return JSON:
{
  \"title\": \"Full proposed title\",
  \"chapters\": [
    {
      \"number\": \"1. peatükk\",
      \"title\": \"Üldsätted\",
      \"sections\": [
        {\"paragraph\": \"§ 1\", \"title\": \"Seaduse reguleerimisala\"},
        ...
      ]
    },
    ...
  ]
}"
```

UI renders as editable tree. User can add/remove/rename chapters and sections.

### 6.7 Step 5 — Clause-by-Clause Drafting

For each section, LLM drafts the legal text with ontology citations:

```
Prompt:
"Draft the content for:
Chapter: {chapter_title}
Section: {section_title} ({paragraph})

Context:
- Law intent: {intent}
- Research findings relevant to this section: {relevant_findings}
- Similar existing provisions: {similar_provisions}

Requirements:
- Write in formal Estonian legislative style
- Follow Õigustehnika reeglid (Estonian legislative drafting rules)
- Cite specific existing provisions being amended or referenced as [estleg:...]
- If transposing an EU directive, cite the specific article
- Return JSON: {\"text\": \"...\", \"citations\": [\"estleg:...\"], \"notes\": \"...\"}"
```

Drafted clauses are shown inline with citations as clickable links. User can edit any clause and regenerate.

Progress bar shows X of N clauses drafted.

### 6.8 Step 6 — Integrated Review (Phase 2 handoff)

The drafted law is assembled into a synthetic .docx in memory, then passed through Phase 2's upload pipeline:
- Creates a `drafts` row with source `drafter_session_id`
- Runs parse → extract → analyze
- Links back: `drafting_sessions.integrated_draft_id = drafts.id`

UI shows the impact report inline (not as a separate page). User can go back to step 5 and revise based on findings.

### 6.9 Step 7 — Export

Generates the final .docx following Estonian legislative template:
- Cover page with title, VTK reference if applicable, date
- Table of contents (auto-generated from structure)
- Main body with formatted chapters/sections
- Appendix: ontology citation index
- Appendix: impact analysis summary
- Appendix: EU compliance checklist

Template in `app/drafter/templates/seadus_template.docx`.

### 6.10 VTK workflow variant

Same state machine but with different prompts and final template:
- Steps 1-3 same
- Step 4: VTK structure is fixed (Problem, Proposed solution, Affected parties, Impact assessment, Timeline)
- Step 5: Each VTK section generated in sequence, not clause-by-clause
- Step 6: Optional (VTKs don't need impact analysis)
- Step 7: Exports as VTK document format (simpler than full law)

---

## 7. Evaluation Framework

### 7.1 VCR cassettes for CI

Using `pytest-recording`, we record real Claude + Voyage responses once per test scenario and replay them in CI:

```python
@pytest.mark.vcr
async def test_chat_answers_with_citations():
    conversation = create_test_conversation()
    response = await orchestrator.handle_message(conversation.id, "Mis on tsiviilseadustiku § 123 sisu?")
    assert len(response.citations) > 0
    assert "TsiviilS" in response.content
```

Cassettes stored in `tests/fixtures/vcr_cassettes/`. Updated by running tests with `--record-mode=new_episodes` locally.

### 7.2 Eval suite

Separate from unit tests, runs manually or in scheduled CI:

```
evals/
├── chat/
│   ├── accuracy.jsonl        # {question, expected_topics, reference_answer}
│   ├── citations.jsonl       # {question, must_cite: [...]}
│   └── refusal.jsonl         # {question, should_refuse: true}
├── drafter/
│   ├── structure_quality.jsonl
│   ├── clause_quality.jsonl
│   └── full_drafts/          # Complete sessions to replay end-to-end
└── run_evals.py              # CLI: python -m app.evals.run_evals --feature chat
```

Evaluations use a rubric-based LLM judge:

```python
async def evaluate_chat_answer(question: str, answer: str, reference: str) -> EvalResult:
    prompt = f"""
    Evaluate this legal advisory answer against the reference.

    Question: {question}
    Answer: {answer}
    Reference answer: {reference}

    Score 1-5 on:
    - Accuracy (facts correct)
    - Citations (sources cited)
    - Estonian legal language
    - Helpfulness

    Return JSON: {{\"accuracy\": N, \"citations\": N, \"language\": N, \"helpfulness\": N, \"notes\": \"...\"}}
    """
    # ... call Claude, parse response ...
```

Results stored in `evals/results/YYYY-MM-DD-{feature}.json` for trend analysis.

---

## 8. Dependencies

New Python packages:
- `anthropic` (already in Phase 2)
- `voyageai` — Voyage AI embeddings client
- `pgvector` — Postgres vector extension (already enabled in Phase 1)
- `pytest-recording` — VCR cassettes for tests
- `python-docx` (already in Phase 2)
- `sse-starlette` — Server-Sent Events (optional, if not using pure WebSocket)

---

## 9. Security Considerations

- LLM API calls include ontology context but **never user PII** — user IDs, names, org info stripped from prompts
- Chat transcripts contain full messages → encrypted at rest (Postgres-level)
- Law drafts generated by AI marked with `ai_generated: true` metadata — cannot be exported without "AI-generated draft — requires human review" watermark
- Rate limiting per user: max 100 messages/hour, max 5 drafter sessions/day (configurable)
- Cost cap per org: monthly budget alert at 80% → hard stop at 100%

---

## 10. GitHub Issues

### Epic: LLM Provider & Cost Tracking
1. Design `LLMProvider` full interface (complete, stream, tool_use)
2. Implement `ClaudeProvider` with streaming and tool use
3. Implement cost tracker with pricing table
4. Create `llm_usage` table + audit views
5. Add per-user and per-org cost caps

### Epic: RAG Pipeline
6. Create `rag_chunks` table with pgvector index
7. Implement `EmbeddingProvider` abstraction + `VoyageProvider`
8. Implement chunker (sliding window, Estonian-aware)
9. Implement bulk ingestion script for ontology
10. Implement incremental re-ingestion hook in sync pipeline
11. Implement `Retriever` with cosine similarity search
12. Add metadata filtering support
13. Create admin page showing RAG index stats

### Epic: Chat UI + WebSocket
14. Create chat pages (`/chat`, `/chat/{id}`)
15. Create conversation + messages tables
16. Implement WebSocket handler at `/ws/chat`
17. Implement chat orchestrator (RAG + LLM streaming)
18. Implement conversation history loading
19. Add "new chat" flow
20. Add draft-context mode (`?draft=ID`)
21. Implement context panel (RAG chunks + tool calls display)

### Epic: Chat Tools
22. Define tool schemas (query_ontology, search_provisions, etc.)
23. Implement tool executors
24. Wire tool use into orchestrator with iterative loop
25. Add tool call logging to messages table

### Epic: Law Drafter State Machine
26. Create `drafting_sessions` and `drafting_session_versions` tables
27. Implement state machine with transitions + guards
28. Create wizard pages scaffolding
29. Implement version snapshots on transitions
30. Add session list + resume functionality

### Epic: Drafter Steps 1-4 (Intent → Structure)
31. Step 1: Intent capture form + validation
32. Step 2: Clarification question generation
33. Step 2: Interactive Q&A UI
34. Step 3: Research background job (deep SPARQL)
35. Step 3: Research results UI
36. Step 4: Structure generation prompt + output parsing
37. Step 4: Editable structure tree UI

### Epic: Drafter Steps 5-7 (Draft → Export)
38. Step 5: Clause-by-clause drafting pipeline
39. Step 5: Drafting UI with inline citations
40. Step 5: Edit + regenerate per clause
41. Step 6: Integrated review handoff to Phase 2
42. Step 6: Inline impact report display
43. Step 7: .docx template with Estonian legislative formatting
44. Step 7: Export generation + download

### Epic: VTK Workflow Variant
45. VTK structure template (fixed sections)
46. VTK step 4 override (skip structure generation)
47. VTK step 5 override (section-by-section, not clause)
48. VTK .docx template + export

### Epic: Evaluation Framework
49. Set up pytest-recording for VCR cassettes
50. Create chat unit tests with cassettes
51. Create drafter unit tests with cassettes
52. Create eval scenarios (chat accuracy, citations, refusal)
53. Create eval scenarios (drafter structure, clause quality)
54. Implement LLM judge for eval scoring
55. Create `run_evals.py` CLI + output format
56. Add eval runs to scheduled CI (weekly)

### Epic: Security & Rate Limiting
57. Strip PII from LLM prompts
58. Per-user rate limits (messages/hour, sessions/day)
59. Per-org cost caps (monthly budget)
60. AI-generated draft watermark in exports
61. Chat transcript encryption at rest (review DB-level setup)

**Total: 61 issues for Phase 3**

---
