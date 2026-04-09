# Phase 2 Design: Document Upload + Impact Analysis

**Status:** Approved
**Date:** 2026-04-09
**Depends on:** Phase 1 (auth, Jena, Postgres, Explorer API)

---

## 1. Goals

Phase 2 delivers the first real advisory capability: a drafter uploads a `.docx` or `.pdf`, the system extracts legal references, maps them to ontology entities, integrates the draft as a temporary graph in Jena, and produces an **impact report** with conflict detection, EU compliance checks, and affected-entities heatmap.

**End-to-end milestone:**

> A ministry official uploads `tsiviilseadustiku_muudatused_2026.docx` via the web UI. Within 2 minutes (async), they receive a toast "Analyys valmis" and can open a report showing: 47 matched ontology entities, 3 potential conflicts with existing provisions, 2 EU directives requiring transposition, affected court decisions, and a visual overlay on the explorer graph.

---

## 2. Architecture Additions

### 2.1 New services

| Service | Type | Purpose |
|---------|------|---------|
| `seadusloome-tika` | Docker service | Apache Tika document parser (internal only) |
| Background worker | Python threading pool | Runs impact analysis jobs async |

### 2.2 New PostgreSQL tables

```sql
CREATE TABLE drafts (
    id              UUID PRIMARY KEY,
    user_id         UUID REFERENCES users(id),
    org_id          UUID REFERENCES organizations(id),
    title           TEXT NOT NULL,
    filename        TEXT NOT NULL,
    content_type    TEXT NOT NULL,
    file_size       BIGINT NOT NULL,
    storage_path    TEXT NOT NULL,           -- encrypted on disk
    graph_uri       TEXT NOT NULL UNIQUE,    -- Jena named graph URI
    status          TEXT NOT NULL CHECK (status IN ('uploaded', 'parsing', 'extracting', 'analyzing', 'ready', 'failed')),
    parsed_text     TEXT,                    -- extracted text (may be large)
    entity_count    INTEGER,
    error_message   TEXT,
    created_at      TIMESTAMPTZ DEFAULT now(),
    updated_at      TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_drafts_user_id ON drafts(user_id);
CREATE INDEX idx_drafts_org_id ON drafts(org_id);
CREATE INDEX idx_drafts_status ON drafts(status);

CREATE TABLE draft_entities (
    id              BIGSERIAL PRIMARY KEY,
    draft_id        UUID REFERENCES drafts(id) ON DELETE CASCADE,
    ref_text        TEXT NOT NULL,           -- raw extracted reference, e.g., "TsiviilS § 123 lg 2"
    entity_uri      TEXT,                    -- matched ontology URI, NULL if unmatched
    confidence      REAL,                    -- LLM confidence 0.0-1.0
    ref_type        TEXT NOT NULL,           -- 'law', 'provision', 'eu_act', 'court_decision', 'concept'
    location        JSONB,                   -- {section: "II", paragraph: 5, offset: 1234}
    created_at      TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_draft_entities_draft_id ON draft_entities(draft_id);
CREATE INDEX idx_draft_entities_entity_uri ON draft_entities(entity_uri);

CREATE TABLE impact_reports (
    id              UUID PRIMARY KEY,
    draft_id        UUID REFERENCES drafts(id) ON DELETE CASCADE,
    generated_at    TIMESTAMPTZ DEFAULT now(),
    affected_count  INTEGER NOT NULL,
    conflict_count  INTEGER NOT NULL,
    gap_count       INTEGER NOT NULL,
    impact_score    INTEGER NOT NULL,        -- 0-100
    report_data     JSONB NOT NULL,          -- full findings
    docx_path       TEXT                     -- exported .docx path, lazy
);

CREATE TABLE background_jobs (
    id              UUID PRIMARY KEY,
    job_type        TEXT NOT NULL,           -- 'parse_draft', 'extract_entities', 'analyze_impact', 'export_report'
    payload         JSONB NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('queued', 'running', 'completed', 'failed')),
    priority        INTEGER DEFAULT 5,
    attempts        INTEGER DEFAULT 0,
    max_attempts    INTEGER DEFAULT 3,
    error_message   TEXT,
    created_at      TIMESTAMPTZ DEFAULT now(),
    started_at      TIMESTAMPTZ,
    completed_at    TIMESTAMPTZ
);

CREATE INDEX idx_jobs_status_priority ON background_jobs(status, priority DESC, created_at);
```

### 2.3 New Python modules

```
app/
├── documents/
│   ├── __init__.py
│   ├── upload.py            # Upload routes and storage
│   ├── parser.py            # Tika client + fallback
│   ├── extractor.py         # LLM-based entity extraction
│   ├── resolver.py          # Match extracted refs to ontology URIs
│   ├── storage.py           # Encrypted file storage
│   └── pages.py             # /drafts, /drafts/{id}, /drafts/{id}/report
├── impact/
│   ├── __init__.py
│   ├── analyzer.py          # Core impact analysis engine
│   ├── conflict_detector.py # Rule-based + SPARQL conflict detection
│   ├── eu_compliance.py     # EU directive/regulation cross-checking
│   ├── gap_analysis.py      # Topic cluster coverage analysis
│   ├── scoring.py           # Impact score calculation
│   └── report.py            # Report builder + .docx export
└── jobs/
    ├── __init__.py
    ├── queue.py             # Postgres-backed job queue
    ├── worker.py            # Background worker thread pool
    └── handlers.py          # Job type → handler function
```

---

## 3. Document Upload Flow

### 3.1 UI

**Route:** `GET /drafts` — list of user's drafts (or org's drafts for reviewers).

**Upload page:** `GET /drafts/upload`
- Drag-and-drop area using HTMX file upload
- Accepts `.docx`, `.pdf`, `.odt`, `.rtf`, `.txt`
- Max size: 25 MB
- Optional title field (auto-filled from filename)
- Upload button triggers `POST /drafts`

**Draft detail page:** `GET /drafts/{id}`
- Draft metadata (title, uploaded by, date, status, file info)
- Status indicator with progress: Uploaded → Parsing → Extracting → Analyzing → Ready
- Once ready: links to impact report, explorer view with overlay, extracted entities list
- Actions: re-analyze, delete, download original, export report as .docx

### 3.2 Upload handler

```python
@rt("/drafts", methods=["POST"])
async def upload_draft(req: Request, file: UploadFile, title: str = ""):
    user = req.scope["auth"]

    # Validate file
    if file.size > 25 * 1024 * 1024:
        raise HTTPException(413, "Fail on liiga suur (max 25 MB)")
    if file.content_type not in ALLOWED_CONTENT_TYPES:
        raise HTTPException(415, "Toetamata failitüüp")

    # Create draft record
    draft_id = uuid4()
    graph_uri = f"urn:draft:{draft_id}"

    # Store file encrypted
    storage_path = await storage.save_encrypted(file, draft_id)

    # Insert DB record
    with get_connection() as conn:
        conn.execute("""
            INSERT INTO drafts (id, user_id, org_id, title, filename, content_type,
                                file_size, storage_path, graph_uri, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'uploaded')
        """, (draft_id, user["id"], user["org_id"], title or file.filename,
              file.filename, file.content_type, file.size, storage_path, graph_uri))
        conn.commit()

    # Enqueue parsing job
    queue.enqueue("parse_draft", {"draft_id": str(draft_id)})

    # Audit log
    log_action(user["id"], "draft.upload", {"draft_id": str(draft_id)})

    return RedirectResponse(f"/drafts/{draft_id}", status_code=303)
```

### 3.3 Encrypted storage

- Files stored in `/var/lib/seadusloome/drafts/{draft_id}.enc`
- Encrypted with AES-256-GCM using a key derived from `DRAFT_ENCRYPTION_KEY` env var (Coolify secret)
- Key rotation: new uploads use current key; old files remain readable by tracking key version in a `key_version` column
- Decryption happens in-memory only when parsing or downloading

```python
from cryptography.fernet import Fernet

class EncryptedStorage:
    def __init__(self, key: bytes, base_path: Path):
        self.fernet = Fernet(key)
        self.base_path = base_path

    async def save(self, file: UploadFile, draft_id: UUID) -> str:
        data = await file.read()
        encrypted = self.fernet.encrypt(data)
        path = self.base_path / f"{draft_id}.enc"
        path.write_bytes(encrypted)
        return str(path)

    def read(self, storage_path: str) -> bytes:
        encrypted = Path(storage_path).read_bytes()
        return self.fernet.decrypt(encrypted)
```

---

## 4. Document Parser (Apache Tika)

### 4.1 Tika service setup

Deploy `apache/tika:latest-full` as a Coolify Docker service:
- Internal network only (not exposed publicly)
- Port 9998
- No persistent storage (stateless)
- Health check: `GET /tika` returns Tika version

### 4.2 Tika client

```python
import httpx

TIKA_URL = os.environ.get("TIKA_URL", "http://seadusloome-tika:9998")

class TikaClient:
    async def extract_text(self, file_bytes: bytes, content_type: str) -> str:
        async with httpx.AsyncClient() as client:
            response = await client.put(
                f"{TIKA_URL}/tika",
                content=file_bytes,
                headers={"Content-Type": content_type, "Accept": "text/plain"},
                timeout=60.0,
            )
            response.raise_for_status()
            return response.text

    async def extract_metadata(self, file_bytes: bytes, content_type: str) -> dict:
        async with httpx.AsyncClient() as client:
            response = await client.put(
                f"{TIKA_URL}/meta",
                content=file_bytes,
                headers={"Content-Type": content_type, "Accept": "application/json"},
                timeout=60.0,
            )
            response.raise_for_status()
            return response.json()
```

### 4.3 Parse job handler

```python
async def handle_parse_draft(payload: dict):
    draft_id = payload["draft_id"]

    with get_connection() as conn:
        conn.execute("UPDATE drafts SET status = 'parsing' WHERE id = %s", (draft_id,))
        conn.commit()
        row = conn.execute("SELECT storage_path, content_type FROM drafts WHERE id = %s",
                           (draft_id,)).fetchone()

    file_bytes = storage.read(row[0])
    text = await tika.extract_text(file_bytes, row[1])

    with get_connection() as conn:
        conn.execute("UPDATE drafts SET parsed_text = %s, status = 'extracting' WHERE id = %s",
                     (text, draft_id))
        conn.commit()

    # Enqueue next stage
    queue.enqueue("extract_entities", {"draft_id": draft_id})
```

---

## 5. LLM-Based Entity Extraction

### 5.1 Extraction prompt

The system sends parsed text in chunks (max 8000 tokens per chunk) to Claude with a structured extraction prompt:

```
You are extracting legal references from an Estonian legislative draft.

For each reference found, return a JSON object with:
- ref_text: the exact text of the reference
- ref_type: one of "law" (whole law), "provision" (specific § or lg), "eu_act" (EU directive/regulation), "court_decision" (Riigikohus/CJEU case), "concept" (legal term)
- location: approximate location in text (section heading, paragraph number)
- confidence: your confidence 0.0-1.0
- canonical_form: normalized form (e.g., "Tsiviilseadustik § 123 lg 2" → "TsiviilS § 123 lg 2")

Return ONLY valid JSON array, no explanation.

Text to analyze:
{chunk}
```

### 5.2 Chunking strategy

- Split text at paragraph boundaries (`\n\n`)
- Target 6000 tokens per chunk with 500-token overlap (to catch references that span paragraphs)
- Use `tiktoken` with Claude's tokenizer approximation

### 5.3 Extractor implementation

```python
from app.ai.llm_provider import LLMProvider  # created in Phase 3, simple shim for now

class EntityExtractor:
    def __init__(self, llm: LLMProvider):
        self.llm = llm

    async def extract(self, draft_id: UUID, text: str) -> list[ExtractedEntity]:
        chunks = self._chunk(text)
        all_entities = []
        for i, chunk in enumerate(chunks):
            prompt = EXTRACTION_PROMPT.format(chunk=chunk)
            response = await self.llm.complete(
                prompt=prompt,
                model="claude-sonnet-4-6",
                max_tokens=4000,
                temperature=0.0,
            )
            entities = self._parse_response(response, chunk_offset=i)
            all_entities.extend(entities)

        return self._deduplicate(all_entities)
```

### 5.4 Provider abstraction (Phase 3 foundation)

Phase 2 introduces a minimal `LLMProvider` interface that Phase 3 will extend:

```python
class LLMProvider(ABC):
    @abstractmethod
    async def complete(self, prompt: str, model: str, max_tokens: int,
                       temperature: float = 0.0) -> str: ...

class ClaudeProvider(LLMProvider):
    def __init__(self, api_key: str):
        self.client = anthropic.AsyncAnthropic(api_key=api_key)

    async def complete(self, prompt, model, max_tokens, temperature=0.0):
        response = await self.client.messages.create(
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text
```

---

## 6. Reference Resolver (Matching to Ontology)

Once entities are extracted, each reference needs to be mapped to an ontology URI.

### 6.1 Matching strategy

1. **Exact canonical match** — look up normalized law short names in a dictionary (`TsiviilS` → `https://data.riik.ee/ontology/estleg#Tsiviilseadustik`)
2. **Fuzzy law name search** — SPARQL `CONTAINS(LCASE(?label), LCASE(?input))` with edit distance fallback
3. **Provision lookup** — once law is matched, look up specific `§` and `lg` via SPARQL
4. **EU act lookup** — match CELEX numbers (e.g., `32019L0790`) and "EL direktiiv YYYY/NNN" patterns
5. **Court decision lookup** — match case numbers (`3-2-1-234-12` for Riigikohus, case numbers for CJEU)

### 6.2 Resolver implementation

```python
class ReferenceResolver:
    def __init__(self, sparql_client: SparqlClient):
        self.sparql = sparql_client
        self.law_shortnames = self._load_shortname_dict()

    async def resolve(self, entity: ExtractedEntity) -> ResolvedReference:
        if entity.ref_type == "law":
            return self._resolve_law(entity)
        if entity.ref_type == "provision":
            return self._resolve_provision(entity)
        # ... etc
```

### 6.3 Output

For each extracted entity, insert into `draft_entities` with:
- `entity_uri`: matched URI or NULL
- `confidence`: LLM's confidence × match confidence
- `location`: structured position in the draft

---

## 7. Jena Named Graph Integration

### 7.1 Graph URI scheme

Each draft creates a named graph:

```
urn:draft:{draft_id}
```

### 7.2 Graph contents

For each matched `draft_entity`, insert triples that link the draft's sections to ontology entities:

```turtle
@prefix estleg: <https://data.riik.ee/ontology/estleg#> .
@prefix draft: <urn:draft:UUID#> .

draft:section-1 a estleg:DraftSection ;
    rdfs:label "§ 1. Üldsätted" ;
    estleg:references <https://data.riik.ee/ontology/estleg#TsiviilS_Par_1> ;
    estleg:transposesEU <https://data.riik.ee/ontology/estleg#EU_Dir_2019_790> .

draft:document a estleg:DraftDocument ;
    rdfs:label "Tsiviilseadustiku muudatused 2026" ;
    estleg:uploadedBy <urn:user:UUID> ;
    estleg:hasSection draft:section-1, draft:section-2, ... .
```

### 7.3 Graph loading

The `jena_loader` module is extended with a `load_named_graph()` function:

```python
def load_named_graph(graph_uri: str, turtle: str) -> bool:
    endpoint = f"{JENA_URL}/{JENA_DATASET}/data"
    response = httpx.post(
        endpoint,
        content=turtle.encode("utf-8"),
        headers={"Content-Type": "text/turtle"},
        params={"graph": graph_uri},
        auth=JENA_AUTH,
    )
    return response.status_code in (200, 201, 204)
```

### 7.4 Graph lifecycle

Per Q10 decision: **persistent until explicitly deleted**. Compensating controls:

- Every access to a draft's graph is audit-logged (`draft.view`, `draft.query`, `draft.analyze`)
- 90-day auto-archive warning: email notification if draft hasn't been accessed in 90 days with "delete or keep" action
- Explicit delete: `DELETE /drafts/{id}` removes DB row, decrypts + deletes file, drops Jena named graph
- Drafts belong to one user (owner); sharing is explicit via Phase 4 collaboration features

---

## 8. Impact Analysis Engine

The core of Phase 2. Runs after entity resolution is complete.

### 8.1 Analysis passes

The analyzer runs 5 passes in sequence, each producing a section of the report:

1. **Affected entities traversal** — BFS from draft sections, N hops (default 2), collect all reachable ontology entities
2. **Conflict detection** — Compare draft provisions' subjects to existing provisions' subjects; flag overlap/contradiction
3. **EU compliance check** — For each EU directive referenced, verify all its articles have Estonian transpositions; flag missing ones
4. **Gap analysis** — For each TopicCluster the draft touches, check if it addresses the cluster's main concepts (via SPARQL count)
5. **Court decision cross-reference** — Find Supreme Court and CJEU decisions that interpret related provisions

### 8.2 SPARQL query templates

Stored in `app/impact/queries.py`. Example — affected entities traversal:

```sparql
PREFIX estleg: <https://data.riik.ee/ontology/estleg#>
SELECT DISTINCT ?entity ?label ?type ?hops
FROM <urn:draft:DRAFT_UUID>
FROM <https://data.riik.ee/ontology/estleg/default>
WHERE {
  {
    ?section estleg:references ?entity .
    BIND(1 AS ?hops)
  } UNION {
    ?section estleg:references ?direct .
    ?direct ?p ?entity .
    BIND(2 AS ?hops)
  }
  ?entity rdfs:label ?label .
  ?entity a ?type .
}
LIMIT 500
```

### 8.3 Conflict detection

Rule-based + SPARQL. Conflicts detected:

- **Subject overlap**: two provisions with the same topic cluster + overlapping scope keywords
- **Definitional conflict**: draft introduces a term already defined differently elsewhere
- **Supersession**: draft amends a provision that was recently amended (<6 months)
- **Authority conflict**: draft claims authority over an area already governed by another act

Each conflict has: severity (low/medium/high), description, affected entities, recommendation.

### 8.4 Analyzer implementation

```python
class ImpactAnalyzer:
    def __init__(self, sparql: SparqlClient):
        self.sparql = sparql

    async def analyze(self, draft_id: UUID) -> ImpactReport:
        graph_uri = f"urn:draft:{draft_id}"

        affected = await self._find_affected(graph_uri)
        conflicts = await self._detect_conflicts(graph_uri, affected)
        eu_issues = await self._check_eu_compliance(graph_uri)
        gaps = await self._analyze_gaps(graph_uri, affected)
        cases = await self._cross_reference_cases(affected)

        score = self._calculate_score(affected, conflicts, eu_issues, gaps)

        report = ImpactReport(
            draft_id=draft_id,
            affected_entities=affected,
            conflicts=conflicts,
            eu_issues=eu_issues,
            gaps=gaps,
            court_decisions=cases,
            impact_score=score,
        )

        self._save_report(report)
        return report
```

### 8.5 Impact score formula

```
score = min(100,
    (affected_count * 0.3) +
    (high_conflict_count * 15) +
    (medium_conflict_count * 8) +
    (eu_missing_count * 10) +
    (gap_count * 5)
)
```

- 0-20: Low impact (routine amendment)
- 21-50: Medium impact (requires review)
- 51-80: High impact (significant review needed)
- 81-100: Critical impact (major legislative change)

---

## 9. Background Job Queue

### 9.1 Design

Lightweight PostgreSQL-backed queue. No Celery, no Redis. Worker is a Python threading pool inside the main FastHTML process.

**Why in-process:**
- 5-50 users, low volume
- No need for separate worker deployment
- Jobs are I/O bound (HTTP calls to Tika, Claude, Jena), so threads work well
- Simplifies deployment and monitoring

**Trade-offs:**
- Restarting the app kills running jobs (handled by retry logic)
- Cannot scale workers independently of the app (acceptable for the scale)

### 9.2 Queue API

```python
class JobQueue:
    def enqueue(self, job_type: str, payload: dict, priority: int = 5) -> UUID:
        """Add a job to the queue, returns job ID."""

    def claim_next(self) -> Job | None:
        """Atomically claim the next pending job for this worker."""

    def mark_completed(self, job_id: UUID): ...
    def mark_failed(self, job_id: UUID, error: str): ...
    def get_status(self, job_id: UUID) -> JobStatus: ...
```

### 9.3 Worker pool

```python
class JobWorker:
    def __init__(self, num_threads: int = 4):
        self.executor = ThreadPoolExecutor(max_workers=num_threads)
        self.handlers = {
            "parse_draft": handle_parse_draft,
            "extract_entities": handle_extract_entities,
            "analyze_impact": handle_analyze_impact,
            "export_report": handle_export_report,
        }
        self._running = True

    def start(self):
        for _ in range(self.executor._max_workers):
            self.executor.submit(self._worker_loop)

    def _worker_loop(self):
        while self._running:
            job = self.queue.claim_next()
            if job is None:
                time.sleep(1)
                continue
            try:
                handler = self.handlers[job.type]
                asyncio.run(handler(job.payload))
                self.queue.mark_completed(job.id)
            except Exception as e:
                logger.exception("Job %s failed", job.id)
                self.queue.mark_failed(job.id, str(e))
```

### 9.4 Job chaining

Each handler can enqueue the next stage:
- `parse_draft` → `extract_entities` → `analyze_impact`
- `analyze_impact` sends a WebSocket toast to the user when done

### 9.5 Retry policy

- Jobs retry up to 3 times with exponential backoff (10s, 60s, 300s)
- After max attempts, job is marked `failed` and draft status becomes `failed`
- User sees error message and "Try again" button

---

## 10. Impact Report UI

### 10.1 Report page

**Route:** `GET /drafts/{id}/report`

**Sections:**
1. **Summary card** — impact score gauge, counts (affected, conflicts, gaps), timestamp
2. **Affected entities** — categorized list (laws, EU acts, court decisions, etc.), clickable to explorer
3. **Conflicts** — accordion list, each with severity badge, description, affected entities, recommendation
4. **EU compliance** — checklist of referenced directives with transposition status
5. **Gaps** — list of topic clusters the draft touches but may underaddress
6. **Court decisions** — relevant Supreme Court and CJEU cases
7. **Export button** — triggers `POST /drafts/{id}/export` → enqueues `export_report` job → toast when ready with download link

### 10.2 Visual overlay on explorer

**Route:** `GET /explorer?draft={id}`

The explorer page accepts a `draft` query parameter. When present:
- Loads the draft's named graph in addition to the default graph
- Highlights affected nodes with pulsing glow
- Colors edges from draft sections to matched entities in orange
- Shows a side panel listing the draft's sections with click-to-focus

### 10.3 Export to .docx

```python
from docx import Document

def export_report_docx(report: ImpactReport, draft: Draft) -> Path:
    doc = Document()
    doc.add_heading(f"Mõjuanalüüs: {draft.title}", level=1)

    # Metadata
    doc.add_paragraph(f"Genereeritud: {report.generated_at:%Y-%m-%d %H:%M}")
    doc.add_paragraph(f"Mõjuskoor: {report.impact_score}/100")

    # Sections
    doc.add_heading("Mõjutatud üksused", level=2)
    # ... iterate report.affected_entities ...

    doc.add_heading("Konfliktid", level=2)
    # ... iterate report.conflicts ...

    # Save
    path = REPORTS_DIR / f"report_{draft.id}.docx"
    doc.save(path)
    return path
```

Estonian legislative formatting conventions (margins, font sizes, numbering) are applied via a template docx.

---

## 11. Security & Privacy

### 11.1 Data sensitivity controls

Per CLAUDE.md and Q10 decision:

- Draft files encrypted at rest (AES-256-GCM)
- Database columns with draft content (`parsed_text`) in plain text but DB-level encryption assumed (Postgres TDE or encrypted volume)
- Every access to a draft logged in `audit_log` with action, user, timestamp
- Drafts scoped to their owner's org (reviewers can see org members' drafts, but not other orgs')
- LLM API calls for entity extraction include only the draft text, not metadata like user ID
- Draft filenames stripped of user-identifying information before sending to LLM

### 11.2 Audit events

New audit action types:

- `draft.upload`
- `draft.view`
- `draft.parse` (system action, not user)
- `draft.extract` (system action)
- `draft.analyze` (system action)
- `draft.report.view`
- `draft.report.export`
- `draft.delete`

---

## 12. Dependencies

New Python packages (add to `pyproject.toml`):

- `python-docx` — .docx export
- `cryptography` (already included via httpx) — Fernet for file encryption
- `tiktoken` — token counting for LLM chunks
- `anthropic` — Claude API client (also used in Phase 3)

New services:
- Apache Tika (Docker: `apache/tika:latest-full`)

---

## 13. Testing Strategy

### 13.1 Unit tests

- `test_parser.py` — Tika client mocked, text extraction from sample files
- `test_extractor.py` — LLM responses mocked with VCR cassettes, extraction prompt formatting, chunking
- `test_resolver.py` — SPARQL client mocked, law name dictionary lookup, fuzzy matching
- `test_storage.py` — encryption round-trip, key version handling
- `test_queue.py` — enqueue, claim, complete, fail cycle
- `test_analyzer.py` — SPARQL responses mocked, scoring math, conflict detection logic

### 13.2 Integration tests

- `test_upload_flow.py` — upload → parse → extract → resolve → analyze end-to-end with mocked Tika + LLM + real Postgres + real Jena
- Run against local `docker compose up` stack
- Uses small fixture draft (.docx with 5-10 known references)

### 13.3 Fixtures

- `tests/fixtures/drafts/sample_draft.docx` — small valid draft
- `tests/fixtures/drafts/sample_draft.txt` — extracted text
- `tests/fixtures/extraction_responses/` — VCR cassettes with recorded LLM responses

---

## 14. GitHub Issues

### Epic: Design System Foundation (blocks Phase 2)
1. Create `app/ui/` package with tokens, theme, PageShell, TopBar, Sidebar
2. Create core form components (Button, Input, Textarea, Select, FormField, Checkbox, Radio)
3. Create feedback components (Alert, Toast, LoadingSpinner, Skeleton)
4. Create data components (Card, Badge, StatusBadge)
5. Create validators + live validation endpoint
6. Migrate existing dashboard pages to use new components
7. Add Aino font files + CSS
8. Create `/design-system` reference page

### Epic: Document Upload Infrastructure
9. Deploy Apache Tika service in Coolify
10. Create `drafts` and related PostgreSQL tables (migration 004)
11. Implement encrypted file storage module
12. Create upload route and UI
13. Create drafts list page
14. Create draft detail page with status tracker

### Epic: Background Job Queue
15. Create `background_jobs` table
16. Implement `JobQueue` class (enqueue, claim, complete, fail)
17. Implement `JobWorker` thread pool
18. Integrate worker startup with FastHTML app lifespan
19. Create admin job monitoring page

### Epic: Entity Extraction Pipeline
20. Implement Tika client
21. Implement `parse_draft` job handler
22. Implement `LLMProvider` abstraction + `ClaudeProvider`
23. Implement chunking with `tiktoken`
24. Implement `EntityExtractor` with Claude
25. Implement `extract_entities` job handler

### Epic: Reference Resolver
26. Load law shortname dictionary from ontology
27. Implement exact + fuzzy law name matching
28. Implement provision lookup via SPARQL
29. Implement EU act CELEX lookup
30. Implement court decision lookup
31. Implement `ReferenceResolver` and integrate into extraction pipeline

### Epic: Jena Named Graph Integration
32. Extend `jena_loader` with named graph support
33. Implement draft → Turtle conversion
34. Implement graph loading in `extract_entities` handler
35. Implement graph deletion on draft delete

### Epic: Impact Analysis Engine
36. Create impact analysis SPARQL query templates
37. Implement affected entities traversal (BFS N-hop)
38. Implement conflict detector (rule-based + SPARQL)
39. Implement EU compliance checker
40. Implement gap analyzer
41. Implement court decision cross-reference
42. Implement impact score calculation
43. Implement `analyze_impact` job handler

### Epic: Impact Report UI + Export
44. Create impact report page (summary + sections)
45. Integrate draft overlay on explorer (`?draft=ID` query param)
46. Implement .docx export with Estonian legislative formatting
47. Create `export_report` job handler
48. Add download link with expiring token

### Epic: Security & Audit
49. Implement draft encryption key management
50. Add audit logging for all draft actions
51. Implement 90-day auto-archive warning (daily cron job)
52. Add draft access control (org-scoped)

### Epic: Testing
53. Create VCR fixtures for LLM extraction responses
54. Write unit tests for all new modules
55. Write integration test for upload → report flow
56. Add sample drafts to `tests/fixtures/`

**Total: 56 issues for Phase 2**

---
