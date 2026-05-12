# Õiguskaart — contextual evidence map

**Status:** PLANNED — epic to be opened 2026-05-12. Follows on from `docs/2026-05-12-ui-plan-explorer-home.html` (phases 1–3 of that report shipped as #746/#747: authenticated `/` → `/dashboard`; Õiguskaart now wears the standard `PageShell` chrome with an in-page toolbar; `?focus=<uri>` deep-links work from the impact report and Analüüsikeskus). This doc covers what's left — the report's phases 4–5, its "Make Õiguskaart More Useful" items, and the additional asks from the 2026-05-12 review.

## The problem

Õiguskaart today is a **graph-first cold blob**: direct entry to `/explorer` renders the category-overview of the whole 90k-entity ontology. The graph is a 2D D3 force-directed layout. Its detail panel is entity metadata + relation list + a bookmark button. Its controls speak "simulation" ("Taaskäivita simulatsioon" → relabelled "Lähtesta paigutus" in #718, but still raw graph knobs). Koostaja, the AI chat, and the draft detail page cannot open the map with context. Net effect: a ministry lawyer who lands here sees a hairball, not their work.

The graph is genuinely valuable — **once you know what you're looking at**. So the fix is to make Õiguskaart a *supporting evidence map* that you almost always enter with a focus (a draft's impact subgraph, a §-reference, a law, a court case), and that other tools can open with context already applied. The full graph stays one explicit click away.

## Direction (decided 2026-05-12)

- **Rendering:** stay 2D D3 — it's legible at 90k scale, deep-linkable, and what every other view (centering, `?focus=`, `?draft=`) expects — but make it *feel* like a map: mini-map, zoom-to-region, a "you are here" badge, and a stable layout (seeded sim / persisted positions) so the same query always looks the same. Not actual-3D.
- **Cold entry:** `/explorer` with no `?focus=` / `?draft=` / `?search=` shows a **contextual start panel**, graph hidden until you pick something. Direct entry to the 90k graph is the exception, behind an explicit "Näita kogu kaarti".
- **Delivery:** a full epic, child-issue breakdown, parallel-agent implementation — same playbook as epic #714.

## Workstreams

### A — Focus-first entry + contextual start panel  *(foundation; everything else builds on this)*
`/explorer` with no context renders a compact start panel inside the (otherwise empty) graph area:
- **Search** — a law / §-reference / CELEX / court case number → focus that entity (reuses the existing `?search=` / `?focus=` plumbing).
- **Sinu järjehoidjad** — the current user's bookmarks (from `bookmarks`).
- **Hiljutised kõrge riskiga leiud** — recent high-risk impact reports for the user's org (impact band ≥ kõrge, joins `impact_reports` ↔ `drafts`, org-scoped).
- **Sinu hiljutised eelnõud** — drafts the user has touched recently → "Ava mõjukaart" (`?draft=`).
- **Alusta *Normi mõjuahelat*** — link to `/analyysikeskus/normi-mojuahel`.
- **Sirvi liikide kaupa** — loads today's category-overview (now opt-in, not the default).

`?focus=` / `?draft=` / `?search=` bypass the panel and load the subgraph directly. A "Näita kogu kaarti" button (in the panel and in `Vaate seaded ▾`) loads the full overview. Sidebar "Õiguskaart" → start panel.
*Files:* new `app/explorer/start_panel.py` (org-scoped DB queries), `app/explorer/routes.py` (a `/explorer/start` data fragment, or render server-side), `app/explorer/pages.py`, `app/static/js/explorer.js` (gate the graph load behind a choice; render the panel state), `app/static/css/explorer.css`. Tests.

### B — `?draft=<id>` → the draft's impact subgraph
Entered with `?draft=<uuid>`, render only the draft's affected / conflicting / gap provisions and their inter-relations — not the full graph. Reuse the impact data already computed in `impact_reports` / `ImpactAnalyzer` rather than a fresh 90k traversal. Keep the existing `#draft-overlay-data` mechanism where it fits. "← Tagasi" goes back to the draft / report.
*Files:* `app/explorer/pages.py`, `app/explorer/routes.py` (draft-subgraph data), `app/static/js/explorer.js`. Tests. *Based on A.*

### C — Legal-view presets
The toolbar gets preset buttons that each apply a named filter (entity types + relation types + timeline mode): **Kehtiv õigus** · **Eelnõu mõjud** · **EL seosed** · **Kohtupraktika** · **Ajalugu**. The raw simulation knobs (`Lähtesta paigutus`, `Näita/peida seosenimed`, `Rühmita liigi järgi`, the timeline slider) stay under `Vaate seaded ▾`. Presets are URL-addressable (`?vaade=el-seosed`) so they're deep-linkable too.
*Files:* `app/explorer/pages.py`, `app/static/js/explorer.js`, `app/static/css/explorer.css`. Tests. *Based on A.*

### D — Evidence-card detail panel
The node detail panel becomes an evidence card: **source** (which law / which draft / which court) · **kuupäev / versioon** · **seose liik** in legal language ("muudab", "tunnistab kehtetuks", "võtab üle direktiivi", "viitab", "kohaldab") · **miks see oluline on** (a one-line plain-language note derived from the relation type + impact band) · **tegevused**: `Küsi nõustajalt selle kohta` (server-side `pending_chat_seed` → `GET /chat/new`) · `Ava analüüsikeskuses` (`/analyysikeskus/normi-mojuahel?sisend=<uri>`) · `Lisa märkus` · `Lisa järjehoidja` (the #743 XHR path).
*Files:* `app/explorer/pages.py`, `app/explorer/routes.py` (entity-detail data may need relation-type labels + dates), `app/static/js/explorer.js`, `app/static/css/explorer.css`. Tests. *Based on A; sequence with C (shared explorer.js/css).*

### E — Spatial-map polish
Mini-map (a small overview rect showing the viewport on the full extent), zoom-to-region (drag-select or double-click a cluster → fit it), a "you are here" badge for the focused node, and a stable layout (deterministic sim seed or persisted node positions per query) so re-opening a focus always looks the same.
*Files:* `app/static/js/explorer.js`, `app/static/css/explorer.css` (+ `app/explorer/pages.py` for the mini-map DOM). Tests where feasible. *Based on A; sequence with C/D (shared explorer.js/css).*

### F — Deep-links *in* from the remaining tools  *(parallel with B–E — touches no `app/explorer/` files)*
- **Koostaja** (`app/drafter/routes.py`): the ontology-research step cards get an "Ava õiguskaardil →" link (`explorer_focus_url`).
- **AI chat** (`app/chat/*`, `app/static/js/chat.js`): when an assistant message cites a provision/act/case URI, render a "vaata kaardil" affordance — the orchestrator already surfaces cited URIs to RAG; thread them to the message UI.
- **Draft detail** (`app/docs/routes/_detail.py`): a "Vaata mõjukaarti" CTA → `/explorer?draft=<id>`.
- **Helpers** (`app/docs/report_routes.py`): add `explorer_draft_url(draft_id)` next to `explorer_focus_url`.
*Files:* `app/drafter/routes.py`, `app/chat/handlers.py` / `app/chat/orchestrator.py` / `app/static/js/chat.js` (as needed), `app/docs/routes/_detail.py`, `app/docs/report_routes.py`. Tests. *Needs A's `?draft=` URL contract; otherwise independent.*

### G — Responsive + accessibility QA pass  *(last — QAs the final state)*
Keyboard can reach: toolbar, search, presets, `Vaate seaded`, the timeline, the detail panel, the start panel. Mobile (≤768px): start panel, toolbar, graph and detail panel all usable, no overlap. ARIA roles/labels on the new surfaces. Desktop + mobile screenshots into the doc.
*Files:* `app/static/css/explorer.css`, `app/static/js/explorer.js`, `app/explorer/pages.py`. *After everything else lands.*

### H — Docs  *(last)*
`CLAUDE.md` (Module 2 — Õiguskaart as evidence map: start panel, `?draft=` subgraph, presets, evidence card), this doc's status section, the `project_explorer_status.md` memory.

## Phasing & critical path

```
A (foundation, merge first)
 ├── B  ?draft= impact subgraph        ┐
 ├── C  legal presets ──► D evidence   ├─ developed in parallel, rebased onto main, merged one at a time
 ├── E  map polish                     ┘
 └── F  deep-links from Koostaja/chat/draft  (genuinely independent — only needs A's URL contract)
G  responsive + a11y  (after A–F)
H  docs  (after A–F)
```

A/B/C/D/E all touch `pages.py` + `explorer.js` (+ `explorer.css`) — develop them in worktrees in parallel, but **rebase each onto `main` before merge and merge them one at a time** (lesson from #714's stacked-PR incident). F touches drafter/chat/docs only — fully parallel.

## Acceptance criteria

- Direct `/explorer` (no params) shows the start panel — search, your bookmarks, recent high-risk findings, recent drafts, `Normi mõjuahel`, "Sirvi liikide kaupa" — and the 90k graph loads only after a choice or via "Näita kogu kaarti".
- `/explorer?draft=<id>` renders that draft's impact subgraph (affected/conflict/gap provisions + inter-relations), not the full graph.
- The toolbar offers the five legal-view presets; the raw graph knobs live under `Vaate seaded`.
- The node detail panel shows source, date/version, relation type in legal language, a "why it matters" line, and the four actions (`Küsi nõustajalt`, `Ava analüüsikeskuses`, `Lisa märkus`, `Lisa järjehoidja`).
- Koostaja research cards, assistant-message provision citations, and the draft detail page can each open Õiguskaart with context.
- A mini-map + zoom-to-region + "you are here" are present; re-opening the same focus produces the same layout.
- Keyboard reaches every control; mobile layout has no overlap; screenshots in this doc.

## Status

*(updated as PRs land)*
- 2026-05-12 — epic opened, child issues filed. (Prereqs #746/#747 shipped.)
