# Service-layer convention — framework-free orchestration (Phase-5 ready)

**Status:** adopted 2026-06-12 (#860 DoD item 3).
**Scope:** Analüüsikeskus workflows today; the reference pattern for every
internal service function that Phase 5 (Public API + MCP server, Module 8)
will wrap.

CLAUDE.md states the goal directly:

> Internal service functions should have clean signatures that can be wrapped
> as both REST endpoints and MCP tools (Phase 5 readiness).

This doc pins *how*. The two worked examples are
`app/analyysikeskus/services/normi_mojuahel.py` and
`app/analyysikeskus/services/el_ulevott.py`, extracted from the
`Normi mõjuahel` and `EL ülevõtt ja harmoneerimine` routes in #860.

---

## The rule in one line

> A service function takes plain domain inputs, returns a typed result
> dataclass, and imports **no** web framework. A route, a REST endpoint, and
> an MCP tool are all thin adapters over the *same* service.

---

## 1. Signature shape

```python
def analyse_<workflow>(<positional domain input>, *, <keyword domain inputs>) -> <Result>:
```

* **Inputs are domain values, not transport objects.** Pass `sisend: str`,
  `org_id: str | None`, ids, flags — never a Starlette `Request`, a FastHTML
  component, form data, or a session. The caller (route / REST handler / MCP
  tool) is responsible for pulling those values out of its transport and
  handing them in.
* **Keyword-only for everything but the primary input.** `sisend` is
  positional (it is *the* thing being analysed); everything else
  (`org_id`, scope flags, …) is keyword-only so call sites stay readable and
  new optional inputs don't reorder existing ones.
* **No I/O framework leakage.** Hitting PostgreSQL or Jena is fine — that is
  domain work (see `_load_owned_draft_report`, which the Normi service owns).
  What is *not* fine is returning, raising, or importing anything that only
  makes sense inside an HTTP request.

```python
# Normi mõjuahel — the reference
def analyse_normi_mojuahel(sisend: str, *, org_id: str | None) -> NormiResult: ...

# EL ülevõtt — the reference
def analyse_el_ulevott(sisend: str) -> ElUlevottResult: ...
```

## 2. Typed results

Return a **frozen `@dataclass`**, or a small **discriminated union** of frozen
dataclasses keyed by a `kind` field when the workflow has genuinely different
outcomes:

```python
@dataclass(frozen=True)
class NormiAdhocResult:
    kind: str = field(default="adhoc", init=False)
    entity_uri: str = ""
    label: str = ""
    type_label: str = ""
    findings: ImpactFindings = field(default_factory=ImpactFindings)
    score: int = 0

NormiResult = (
    NormiDraftBackedResult | NormiAdhocResult | NormiDisambiguation | NormiUnresolved
)
```

Rules:

* **Frozen + defaulted.** `frozen=True` makes results hashable and safe to
  pass around; every field has a default (or a `default_factory`) so a result
  is always constructible.
* **Discriminate with a non-`init` `kind` field.** The caller matches on the
  concrete type (`isinstance(result, NormiAdhocResult)`) for static-typing
  wins; the `kind` string is what a JSON / MCP serialiser emits so a remote
  client can branch without Python types.
* **Carry domain data, not rendered output.** A result holds
  `ImpactFindings`, row dicts, candidate lists, labels — never an `FT` node,
  an HTML string, or a Starlette `Response`. Rendering is the adapter's job.
* **Reuse existing engine dataclasses.** `ImpactFindings`,
  `AdhocAnalysisResult`, etc. are already typed — compose them, don't reshape
  them.

## 3. Error contract

> Errors are result *fields* / result *kinds*, or typed domain exceptions —
> **never** an HTTP status.

* **Expected "no answer" outcomes are results, not exceptions.** "Nothing
  resolved" is `NormiUnresolved()` / `ElUlevottUnresolved(...)`, not a 404.
  "Several matches" is `…Disambiguation(candidates=[...])`, not a 300.
* **Infrastructure failure degrades gracefully.** A dead Jena (resolver /
  analyser / transposition query raising) is caught inside the service and
  mapped to the safe empty/unresolved result, exactly as the routes already
  expected — it must never bubble up as a 500 from the service itself. The
  service logs at `warning` and returns the degraded result.
* **Truly exceptional, caller-actionable failures** (a programming error, an
  invariant violation) may raise a typed domain exception. They must not be
  `HTTPException` or anything transport-specific.

## 4. The no-framework rule (enforced)

A service module must not `import fasthtml` or `import starlette` (or any
submodule). This is enforced by `tests/test_analyysikeskus_services_no_framework.py`,
which AST-scans every `app/analyysikeskus/services/*.py` and fails on a
forbidden import root. Add new services to that package and the test guards
them automatically.

What a service *may* import: stdlib, `dataclasses`, the project's engine
modules (`app.analyysikeskus.input_parser`, `app.analyysikeskus.adhoc_analysis`,
`app.impact.*`, `app.analyysikeskus.eu_lookup`, `app.docs.reference_resolver`,
…), and `app.db` for DB access.

## 5. How a route wraps a service

The route is a thin adapter: **parse request → call service → render**.

```python
def normi_mojuahel_page(req: Request):
    auth = req.scope.get("auth") or None
    theme = get_theme_from_request(req)
    org_id = auth.get("org_id") if auth else None
    sisend = (req.query_params.get("sisend") or "").strip()
    if not sisend:
        return RedirectResponse(url="/analyysikeskus", status_code=303)

    result = analyse_normi_mojuahel(sisend, org_id=org_id)   # ← the service

    if isinstance(result, NormiDraftBackedResult):
        return _render_draft_backed_result(..., findings=result.findings, ...)
    if isinstance(result, NormiAdhocResult):
        return _render_adhoc_result(..., findings=result.findings, score=result.score, ...)
    if isinstance(result, NormiDisambiguation):
        return _render_disambiguation(..., candidates=[...], ...)
    return _render_unresolved(...)   # NormiUnresolved
```

The render helpers stay in the route module — they speak FastHTML and that's
fine; only the *orchestration* moved to the service. Rendering output is
unchanged (the route registration parity test + the existing route tests are
the guard).

### Patch-path consequence

Because orchestration moved into the service, the "patch where used" target
for an orchestration dependency moves with it. Tests that used to patch
`app.analyysikeskus.routes._normi.run_adhoc_impact_analysis` now patch where
the service uses it — at the dependency's canonical home
(`app.analyysikeskus.adhoc_analysis.…`, `app.docs.reference_resolver.…`) or at
the service module (`app.analyysikeskus.services.normi_mojuahel.…`). Render-side
helpers (`_build_results_block`, `_rag_candidates`) stay patched on the route
submodule. See `tests/test_analyysikeskus_routes_patch_paths.py`.

## 6. How a Phase-5 REST endpoint / MCP tool wraps the same service

No new business logic — just a different adapter and a JSON serialiser over
the same dataclass.

```python
# Phase-5 REST (sketch) — Starlette/FastHTML route returning JSON
async def api_normi_mojuahel(req: Request) -> JSONResponse:
    body = await req.json()
    result = analyse_normi_mojuahel(body["sisend"], org_id=body.get("org_id"))
    return JSONResponse(asdict(result))   # dataclasses.asdict → JSON-ready
```

```python
# Phase-5 MCP tool (sketch) — the tool schema mirrors the service signature
@mcp_tool(name="analyysikeskus.normi_mojuahel")
def normi_mojuahel_tool(sisend: str, org_id: str | None = None) -> dict:
    """Impact-chain analysis over a legal reference."""
    return asdict(analyse_normi_mojuahel(sisend, org_id=org_id))
```

Because the result is a frozen dataclass discriminated by `kind`,
`dataclasses.asdict()` gives a stable JSON shape, and a remote client branches
on `result["kind"]` exactly as the route branches on `isinstance`. The MCP
tool's input schema is a literal transcription of the service signature —
that is the whole point of keeping the signature framework-free.

---

## Checklist for a new service

- [ ] Lives in `app/analyysikeskus/services/<workflow>.py` (or another
      `services/` package following the same rule).
- [ ] `def analyse_<x>(primary, *, ...) -> <Result>` — domain inputs only.
- [ ] Returns a frozen dataclass / discriminated union with a `kind` field.
- [ ] No `fasthtml` / `starlette` import (the AST test enforces it).
- [ ] Infra failure → degraded result + `warning` log, never a raised 500.
- [ ] The route is `parse → call service → render`; rendering output unchanged.
- [ ] Re-export the public service + result types from
      `app/analyysikeskus/services/__init__.py`.
