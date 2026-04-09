# Phase 5 Design: Public API + MCP Server

**Status:** Approved
**Date:** 2026-04-09
**Depends on:** Phase 1-4 (all internal service functions must be stable)

---

## 1. Goals

Phase 5 exposes the system's capabilities to external consumers through two channels:

1. **REST API** — Versioned public API with API key authentication for third-party integrations (research tools, ministry integrations, legal tech partners)
2. **MCP Server** — Model Context Protocol adapter so AI assistants like Claude Desktop, Claude Code, or custom agents can use the Estonian Legal Ontology as a tool

**End-to-end milestones:**

> **REST:** A legal tech startup registers for API access, receives a scoped API key (`read:ontology, read:reports`), and builds a provision search integration: `GET /api/v1/provisions/search?q=tsiviilseadustik` returns JSON matching their schema.

> **MCP:** A ministry official using Claude Desktop asks "What existing laws would be affected if we change the definition of 'digital service' in the cybersecurity act?" Claude Desktop calls `query_ontology` via MCP, then `analyze_impact` on a draft, and returns a synthesized answer with citations.

---

## 2. Architecture Additions

### 2.1 New PostgreSQL tables

```sql
CREATE TABLE api_keys (
    id              UUID PRIMARY KEY,
    name            TEXT NOT NULL,
    key_hash        TEXT NOT NULL UNIQUE,    -- SHA-256 of the key
    key_prefix      TEXT NOT NULL,           -- first 8 chars for identification (e.g., 'sdl_live_abc12345')
    owner_user_id   UUID REFERENCES users(id),
    owner_org_id    UUID REFERENCES organizations(id),
    scopes          TEXT[] NOT NULL,          -- ['read:ontology', 'read:reports', 'write:drafts', ...]
    rate_limit_per_hour INTEGER DEFAULT 1000,
    expires_at      TIMESTAMPTZ,
    last_used_at    TIMESTAMPTZ,
    created_at      TIMESTAMPTZ DEFAULT now(),
    revoked_at      TIMESTAMPTZ,
    revoked_reason  TEXT
);

CREATE INDEX idx_api_keys_hash ON api_keys(key_hash);
CREATE INDEX idx_api_keys_owner ON api_keys(owner_user_id);

CREATE TABLE api_usage (
    id              BIGSERIAL PRIMARY KEY,
    api_key_id      UUID REFERENCES api_keys(id),
    endpoint        TEXT NOT NULL,
    method          TEXT NOT NULL,
    status_code     INTEGER NOT NULL,
    duration_ms     INTEGER,
    ip_address      INET,
    user_agent      TEXT,
    created_at      TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_api_usage_key_time ON api_usage(api_key_id, created_at DESC);
CREATE INDEX idx_api_usage_endpoint ON api_usage(endpoint, created_at DESC);

CREATE TABLE webhook_subscriptions (
    id              UUID PRIMARY KEY,
    api_key_id      UUID REFERENCES api_keys(id),
    url             TEXT NOT NULL,
    secret          TEXT NOT NULL,           -- for HMAC signature
    events          TEXT[] NOT NULL,         -- ['draft.analysis.done', 'sync.completed', ...]
    enabled         BOOLEAN DEFAULT TRUE,
    last_fired_at   TIMESTAMPTZ,
    created_at      TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE webhook_deliveries (
    id              BIGSERIAL PRIMARY KEY,
    subscription_id UUID REFERENCES webhook_subscriptions(id),
    event_type      TEXT NOT NULL,
    payload         JSONB NOT NULL,
    status_code     INTEGER,
    response_body   TEXT,
    attempt         INTEGER DEFAULT 1,
    delivered_at    TIMESTAMPTZ,
    created_at      TIMESTAMPTZ DEFAULT now()
);
```

### 2.2 New Python modules

```
app/
├── api/
│   ├── __init__.py
│   ├── v1/
│   │   ├── __init__.py
│   │   ├── router.py         # Main v1 APIRouter
│   │   ├── auth.py           # API key validation middleware
│   │   ├── ontology.py       # /api/v1/ontology/*
│   │   ├── provisions.py     # /api/v1/provisions/*
│   │   ├── drafts.py         # /api/v1/drafts/*
│   │   ├── reports.py        # /api/v1/reports/*
│   │   ├── chat.py           # /api/v1/chat/*
│   │   ├── drafter.py        # /api/v1/drafter/*
│   │   └── schemas.py        # Pydantic schemas for request/response
│   ├── openapi.py            # OpenAPI spec generator
│   ├── rate_limit.py         # Token bucket rate limiter
│   └── webhooks.py           # Webhook delivery service
├── api_keys/
│   ├── __init__.py
│   ├── db.py                 # CRUD
│   ├── routes.py             # UI routes for key management
│   └── pages.py              # /settings/api-keys
└── mcp/
    ├── __init__.py
    ├── server.py             # MCP server entry point
    ├── tools.py              # MCP tool definitions
    ├── resources.py          # MCP resource definitions
    └── protocol.py           # Protocol handler (JSON-RPC over stdio/HTTP)
```

---

## 3. REST API (v1)

### 3.1 Endpoint catalog

All endpoints under `/api/v1/`. Return JSON. Consistent error format.

**Ontology queries:**
- `GET /ontology/overview` — categories + counts
- `GET /ontology/classes` — list all class types
- `GET /ontology/search?q=...` — search entities by label
- `POST /ontology/sparql` — execute raw SPARQL (requires `sparql:execute` scope)
- `GET /ontology/entities/{uri}` — entity detail
- `GET /ontology/entities/{uri}/neighbors` — 1-hop neighbors
- `GET /ontology/entities/{uri}/versions` — version history

**Provisions:**
- `GET /provisions/search?q=...` — search legal provisions
- `GET /provisions/{uri}` — single provision with metadata
- `GET /provisions/{uri}/interpretations` — court decisions interpreting this provision
- `GET /provisions/{uri}/amendments` — amendment history

**Drafts (requires ownership scope):**
- `POST /drafts` — upload a draft (multipart/form-data)
- `GET /drafts` — list drafts owned by key's org
- `GET /drafts/{id}` — draft metadata
- `GET /drafts/{id}/report` — impact analysis report (JSON)
- `GET /drafts/{id}/report.docx` — impact report as .docx
- `DELETE /drafts/{id}` — delete draft
- `POST /drafts/{id}/reanalyze` — trigger re-analysis

**Reports:**
- `GET /reports/{id}` — full impact report
- `GET /reports/{id}/affected` — just affected entities
- `GET /reports/{id}/conflicts` — just conflicts

**Chat (session-based):**
- `POST /chat/sessions` — create new chat session
- `POST /chat/sessions/{id}/messages` — send message (non-streaming, returns full response)
- `GET /chat/sessions/{id}/messages` — list messages
- `DELETE /chat/sessions/{id}` — end session

**Drafter:**
- `POST /drafter/sessions` — create drafting session (body: `{intent, workflow}`)
- `GET /drafter/sessions/{id}` — get current state
- `POST /drafter/sessions/{id}/step` — advance to next step (body depends on current step)
- `GET /drafter/sessions/{id}/export.docx` — download final draft

**Meta:**
- `GET /api/v1/healthz` — health check (no auth)
- `GET /api/v1/me` — info about the current API key
- `GET /api/v1/usage` — current key's usage stats

### 3.2 Response envelope

All responses follow:

```json
{
  "data": { ... } or [ ... ],
  "meta": {
    "request_id": "req_abc123",
    "timestamp": "2026-04-15T10:30:00Z"
  }
}
```

Paginated responses add:
```json
{
  "data": [...],
  "meta": {
    "request_id": "...",
    "page": 1,
    "page_size": 50,
    "total": 1234,
    "total_pages": 25
  }
}
```

Errors:
```json
{
  "error": {
    "code": "invalid_api_key",
    "message": "API key is invalid or expired",
    "details": {}
  },
  "meta": {
    "request_id": "...",
    "timestamp": "..."
  }
}
```

### 3.3 Authentication

**Header:** `Authorization: Bearer sdl_live_abc123...`

**Key format:** `sdl_{env}_{random}` where env is `live` or `test`. 32 chars of random base32.

**Validation:**
1. Extract key from header
2. Hash with SHA-256
3. Look up `api_keys` row by `key_hash`
4. Check `revoked_at IS NULL` and `expires_at > now()`
5. Update `last_used_at`
6. Set `request.scope["api_key"]` for downstream handlers

### 3.4 Scopes

| Scope | Allows |
|-------|--------|
| `read:ontology` | All `/ontology/*` GET + `/provisions/*` GET |
| `sparql:execute` | `POST /ontology/sparql` (raw SPARQL execution) |
| `read:drafts` | List and read drafts in the key's org |
| `write:drafts` | Upload, delete, reanalyze drafts |
| `read:reports` | Read impact reports |
| `use:chat` | Create chat sessions |
| `use:drafter` | Create drafting sessions |
| `admin:keys` | Manage API keys (meta operation) |

Keys can have multiple scopes. The API key management UI lets users select scopes per key.

### 3.5 Rate limiting

**Algorithm:** Token bucket, per API key.

**Defaults:**
- 1000 requests/hour per key (customizable)
- `POST /ontology/sparql`, `POST /chat/*`, `POST /drafter/*` count as 10 tokens each (expensive operations)

**Implementation:** Redis-less, using Postgres with advisory locks:

```python
async def check_rate_limit(api_key_id: UUID, cost: int = 1) -> bool:
    """Returns True if request should be allowed, False if rate limited."""
    with get_connection() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (str(api_key_id),))
        row = conn.execute("""
            SELECT COUNT(*) FROM api_usage
            WHERE api_key_id = %s AND created_at > now() - interval '1 hour'
        """, (api_key_id,)).fetchone()
        used = row[0]
        limit = get_key_rate_limit(api_key_id)
        return used + cost <= limit
```

For higher scale, switch to Traefik's built-in rate limiting middleware.

**Rate limit headers in every response:**
```
X-RateLimit-Limit: 1000
X-RateLimit-Remaining: 872
X-RateLimit-Reset: 1713178800
```

When exceeded: `429 Too Many Requests` with `Retry-After` header.

### 3.6 API key management UI

**Route:** `GET /settings/api-keys`

- List of active keys with name, prefix, scopes, last used, rate limit
- Create key form (name, scopes, expiration, rate limit)
- Revoke button (soft delete)
- View usage stats per key
- Keys shown in full only once (on creation) — subsequent views show only the prefix

### 3.7 OpenAPI / Swagger

Auto-generated OpenAPI 3.1 spec at `/api/v1/openapi.json`.

Uses Pydantic schemas for request/response models:

```python
class ProvisionSearchResponse(BaseModel):
    data: list[ProvisionItem]
    meta: PaginationMeta

class ProvisionItem(BaseModel):
    uri: str
    label: str
    act: str
    paragraph: str
    summary: str | None
```

Swagger UI at `/api/v1/docs` (Stoplight Elements or similar static HTML).

### 3.8 Webhooks

**Events:**
- `draft.uploaded`
- `draft.analysis.started`
- `draft.analysis.done`
- `draft.analysis.failed`
- `sync.completed`
- `sync.failed`
- `drafter.session.completed`

**Delivery:**
- HTTP POST to subscriber URL with JSON payload
- HMAC signature in `X-Seadusloome-Signature: sha256=...` header
- Retry on failure: 3 attempts (10s, 60s, 300s)
- Delivery records in `webhook_deliveries`

**Subscription management:**
- `POST /api/v1/webhooks/subscriptions` — create
- `GET /api/v1/webhooks/subscriptions` — list
- `DELETE /api/v1/webhooks/subscriptions/{id}` — delete
- `POST /api/v1/webhooks/subscriptions/{id}/test` — send test event

---

## 4. MCP Server

### 4.1 Deployment model

**Embedded in the main app** (Q20 decision). MCP endpoints live under `/mcp/*` in the Starlette app.

**Transport:** HTTP (MCP's newer transport, better suited for deployed servers than stdio). Compatible with Claude Desktop via MCP's HTTP config.

### 4.2 Authentication

MCP uses a bearer token that maps to an API key with the `mcp:*` scopes. Same key management UI lets users create "MCP keys" (just a preset scope combination).

### 4.3 Tool definitions

```python
MCP_TOOLS = [
    {
        "name": "query_ontology",
        "description": "Execute a SPARQL SELECT query against the Estonian Legal Ontology",
        "inputSchema": {
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
        "inputSchema": {
            "type": "object",
            "properties": {
                "keywords": {"type": "string"},
                "limit": {"type": "integer", "default": 10},
            },
            "required": ["keywords"],
        },
    },
    {
        "name": "get_provision",
        "description": "Get detailed information about a specific legal provision including its text, amendments, and court interpretations",
        "inputSchema": {
            "type": "object",
            "properties": {"uri": {"type": "string"}},
            "required": ["uri"],
        },
    },
    {
        "name": "analyze_draft",
        "description": "Upload a draft law document and run impact analysis. Returns a job ID to poll for results.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "filename": {"type": "string"},
                "content_base64": {"type": "string"},
                "content_type": {"type": "string"},
            },
            "required": ["filename", "content_base64", "content_type"],
        },
    },
    {
        "name": "get_impact_report",
        "description": "Retrieve the impact analysis report for a previously uploaded draft",
        "inputSchema": {
            "type": "object",
            "properties": {"draft_id": {"type": "string"}},
            "required": ["draft_id"],
        },
    },
    {
        "name": "draft_law",
        "description": "Start an AI law drafting session from a natural language intent. Returns a session ID and the next step to execute.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "intent": {"type": "string"},
                "workflow": {"type": "string", "enum": ["vtk", "full_law"]},
            },
            "required": ["intent", "workflow"],
        },
    },
    {
        "name": "get_provision_versions",
        "description": "Get the version history of a specific provision including all amendments",
        "inputSchema": {
            "type": "object",
            "properties": {"uri": {"type": "string"}},
            "required": ["uri"],
        },
    },
]
```

### 4.4 Resources

MCP resources expose static data:

```python
MCP_RESOURCES = [
    {
        "uri": "ontology://classes",
        "name": "Ontology class definitions",
        "mimeType": "application/json",
    },
    {
        "uri": "ontology://relationships",
        "name": "Ontology relationship schemas",
        "mimeType": "application/json",
    },
    {
        "uri": "ontology://statistics",
        "name": "Live ontology statistics (entity counts, last sync)",
        "mimeType": "application/json",
    },
]
```

### 4.5 Protocol handler

Uses Python MCP SDK (`mcp` package):

```python
from mcp.server import Server
from mcp.server.http import create_http_app

def create_mcp_server():
    server = Server("seadusloome")

    @server.list_tools()
    async def list_tools():
        return MCP_TOOLS

    @server.call_tool()
    async def call_tool(name: str, arguments: dict):
        handler = TOOL_HANDLERS[name]
        return await handler(arguments)

    @server.list_resources()
    async def list_resources():
        return MCP_RESOURCES

    @server.read_resource()
    async def read_resource(uri: str):
        return RESOURCE_HANDLERS[uri]()

    return server

# Mount on main app
mcp_server = create_mcp_server()
mcp_app = create_http_app(mcp_server)
app.mount("/mcp", mcp_app)
```

### 4.6 Tool handlers

Each tool handler is a thin wrapper around existing internal service functions:

```python
async def handle_query_ontology(args: dict) -> dict:
    client = SparqlClient()
    results = client.query(args["query"])
    return {"results": results}

async def handle_analyze_draft(args: dict) -> dict:
    content = base64.b64decode(args["content_base64"])
    draft_id = await documents.upload_api(
        filename=args["filename"],
        content=content,
        content_type=args["content_type"],
    )
    return {"draft_id": str(draft_id), "status": "processing",
            "poll_url": f"/api/v1/drafts/{draft_id}"}
```

### 4.7 MCP authentication

MCP clients (Claude Desktop, Claude Code) authenticate by sending the API key in the `Authorization` header on every request. Seadusloome validates the key has `mcp:*` scope before executing tools.

Claude Desktop config example (documented for users):
```json
{
  "mcpServers": {
    "seadusloome": {
      "url": "https://seadusloome.sixtyfour.ee/mcp",
      "transport": "http",
      "headers": {
        "Authorization": "Bearer sdl_live_xxxxxxx"
      }
    }
  }
}
```

---

## 5. API Documentation Site

A dedicated docs site at `/api/docs` with:

- **Getting Started** — API key creation, first request, error handling
- **Reference** — Swagger UI with all endpoints
- **Guides** — Common use cases (search provisions, upload draft, use MCP)
- **Webhooks** — Event types, payload schemas, HMAC verification
- **MCP** — Setup for Claude Desktop, Claude Code, custom clients
- **Rate Limits** — Default limits, how to request increases
- **Changelog** — Versioning policy, breaking changes

Served as static HTML from `app/static/api-docs/`.

---

## 6. Dependencies

New Python packages:
- `mcp` — Python MCP SDK
- `pydantic` — already a FastHTML/Starlette transitive dependency, used directly for API schemas

---

## 7. Testing

### 7.1 API tests

- `test_api_auth.py` — API key validation, scopes, rate limits
- `test_api_v1_ontology.py` — all ontology endpoints
- `test_api_v1_drafts.py` — draft CRUD via API
- `test_api_webhooks.py` — webhook delivery + HMAC signing

### 7.2 MCP tests

- `test_mcp_protocol.py` — protocol compliance via Python MCP SDK test utilities
- `test_mcp_tools.py` — each tool handler with mocked backends
- `test_mcp_integration.py` — end-to-end with a real MCP client simulating Claude Desktop

### 7.3 OpenAPI spec validation

- `test_openapi_schema.py` — validates generated spec against OpenAPI 3.1 schema
- `test_openapi_examples.py` — validates example requests/responses in spec

---

## 8. Migration & Rollout

Phase 5 is additive — existing web UI continues working unchanged. Rollout:

1. Deploy with API + MCP disabled (feature flag)
2. Internal testing with a test API key
3. Invite 2-3 pilot partners to test
4. Enable for production, announce API availability
5. Monitor usage + error rates for 2 weeks
6. Publish docs, marketing

---

## 9. Backward Compatibility Commitment

API v1 is stable once released. Breaking changes require v2.

**Breaking change policy:**
- New endpoints: non-breaking
- New optional request fields: non-breaking
- New response fields: non-breaking (clients must ignore unknown fields)
- Removing endpoints or fields: breaking → v2
- Changing status codes or error shapes: breaking → v2

**Deprecation:**
- 6 months notice via `Deprecation` and `Sunset` headers
- Email notifications to key owners

---

## 10. GitHub Issues

### Epic: API Key Management
1. Create `api_keys` and `api_usage` tables (migration 006)
2. Implement API key CRUD module
3. Create API key management UI (`/settings/api-keys`)
4. Implement key hashing (SHA-256) and prefix display
5. Implement scopes system
6. Add key expiration handling
7. Implement revocation flow

### Epic: REST API Framework
8. Create `app/api/v1/` router structure
9. Implement API key authentication middleware
10. Implement response envelope helpers
11. Implement error response helpers with stable error codes
12. Implement pagination helpers
13. Create Pydantic schema base classes
14. Add request ID generation and logging

### Epic: Rate Limiting
15. Implement token bucket rate limiter (Postgres-based)
16. Add rate limit headers to every response
17. Handle 429 responses with Retry-After
18. Admin view: rate limit config per key

### Epic: Ontology API Endpoints
19. Implement `/api/v1/ontology/overview`
20. Implement `/api/v1/ontology/classes`
21. Implement `/api/v1/ontology/search`
22. Implement `/api/v1/ontology/sparql` with scope gate
23. Implement `/api/v1/ontology/entities/{uri}`
24. Implement `/api/v1/ontology/entities/{uri}/neighbors`
25. Implement `/api/v1/ontology/entities/{uri}/versions`

### Epic: Provisions API Endpoints
26. Implement `/api/v1/provisions/search`
27. Implement `/api/v1/provisions/{uri}`
28. Implement `/api/v1/provisions/{uri}/interpretations`
29. Implement `/api/v1/provisions/{uri}/amendments`

### Epic: Drafts API Endpoints
30. Implement `POST /api/v1/drafts` (upload)
31. Implement `GET /api/v1/drafts` (list)
32. Implement `GET /api/v1/drafts/{id}`
33. Implement `GET /api/v1/drafts/{id}/report`
34. Implement `.docx` export endpoint
35. Implement `DELETE /api/v1/drafts/{id}`
36. Implement `POST /api/v1/drafts/{id}/reanalyze`

### Epic: Chat + Drafter API Endpoints
37. Implement `POST /api/v1/chat/sessions`
38. Implement `POST /api/v1/chat/sessions/{id}/messages` (non-streaming)
39. Implement `GET /api/v1/chat/sessions/{id}/messages`
40. Implement `POST /api/v1/drafter/sessions`
41. Implement `GET /api/v1/drafter/sessions/{id}`
42. Implement `POST /api/v1/drafter/sessions/{id}/step`
43. Implement `GET /api/v1/drafter/sessions/{id}/export.docx`

### Epic: Meta + Health Endpoints
44. Implement `/api/v1/healthz`
45. Implement `/api/v1/me`
46. Implement `/api/v1/usage`

### Epic: OpenAPI + Docs
47. Generate OpenAPI 3.1 spec from Pydantic schemas
48. Serve spec at `/api/v1/openapi.json`
49. Integrate Swagger UI at `/api/v1/docs`
50. Create docs site structure at `/api/docs`
51. Write Getting Started guide
52. Write Webhooks guide
53. Write MCP setup guide
54. Write rate limits doc

### Epic: Webhooks
55. Create `webhook_subscriptions` and `webhook_deliveries` tables
56. Implement subscription CRUD
57. Implement webhook delivery service (async queue)
58. Implement HMAC signature generation
59. Implement retry logic with exponential backoff
60. Implement webhook test endpoint
61. Wire up event emitters in sync, drafts, drafter modules

### Epic: MCP Server
62. Install and configure Python MCP SDK
63. Create MCP server + HTTP app
64. Mount MCP on main app at `/mcp`
65. Implement all 7 MCP tools with backend wiring
66. Implement MCP resources
67. Add MCP key scope validation
68. Document Claude Desktop setup

### Epic: API Testing & Validation
69. Write API auth tests
70. Write tests for each endpoint group
71. Write webhook delivery tests
72. Validate OpenAPI spec compliance
73. MCP protocol tests with SDK test utilities
74. MCP end-to-end test simulating Claude Desktop

### Epic: Deployment
75. Add API key creation to admin panel (for internal use)
76. Feature flag to enable/disable API endpoints
77. Feature flag to enable/disable MCP server
78. Add API metrics to admin dashboard (usage, errors, top endpoints)
79. Configure Traefik for API route priority

**Total: 79 issues for Phase 5**

---
