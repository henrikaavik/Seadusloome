# Sub-project A — Metadata & Discovery (Eelnõud + VTKd)

**Status:** Approved 2026-04-16
**Epic:** [#597 Eelnõud production polish sweep](https://github.com/henrikaavik/Seadusloome/issues/597)
**Closes:** #616 (search + filter), #620 (VTK linking). Closes #617 as out-of-scope (org == ministry, so `responsible_ministry` is redundant with `org_id`).
**Depends on:** Phase 1 (auth + orgs), Phase 2 (drafts + impact pipeline).

---

## 1. Goals

Make the /drafts surface a real workspace for ministry lawyers by:

1. Letting users **find** any of their org's drafts quickly — by title, filename, or referenced entity.
2. Letting users **link** an eelnõu to its preceding VTK (väljatöötamiskavatsus) so the legislative lineage is preserved.
3. Treating uploaded VTKs as first-class documents that flow through the same parse → extract → analyze → report pipeline as eelnõud.
4. Asserting the VTK→eelnõu lineage in the ontology with a single `estleg:basedOn` triple, unlocking richer queries in later sub-projects.

**Visibility scope (locked):** org-only. No cross-ministry visibility in this sub-project; that decision is revisited in Sub-project B.

**Search depth (locked):** title + filename + entity names extracted by the analyze pipeline. No full-text search of parsed draft content (would erode encryption-at-rest).

**Filter set (locked):** search box + status multi-select + uploader single-select + date range. Sort by upload date / title / status.

**Org model (locked):** one org == one ministry, 1:1. `responsible_ministry` field is redundant and not introduced.

**VTK model (locked):** VTK is a document uploaded into this system, classified by a new `doc_type` enum on the existing `drafts` table. Same pipeline, same detail page shape, same report.

**Lineage in upload (locked):** optional VTK picker on the upload form. Picker hidden when type=VTK.

**UI placement (locked):** unified `/drafts` listing with a "Tüüp" filter; no separate URL or tab.

**Ontology (locked):** lineage triples written into the eelnõu's named graph during analyze. Richer SPARQL surface deferred to Sub-project B.

---

## 2. Data model

### 2.1 Migration `019_draft_doc_type_and_vtk_lineage.sql`

```sql
-- One document, one row. doc_type discriminates eelnõu vs VTK.
ALTER TABLE drafts
  ADD COLUMN doc_type TEXT NOT NULL DEFAULT 'eelnou'
    CHECK (doc_type IN ('eelnou', 'vtk')),
  ADD COLUMN parent_vtk_id UUID REFERENCES drafts(id) ON DELETE SET NULL;

-- A VTK cannot have a parent VTK.
ALTER TABLE drafts
  ADD CONSTRAINT chk_vtk_has_no_parent
    CHECK (doc_type = 'eelnou' OR parent_vtk_id IS NULL);

-- Composite covers the default filtered listing.
CREATE INDEX idx_drafts_org_doctype_status_created
  ON drafts (org_id, doc_type, status, created_at DESC);

-- Hot for the "child eelnõud of this VTK" query on VTK detail page.
CREATE INDEX idx_drafts_parent_vtk
  ON drafts (parent_vtk_id) WHERE parent_vtk_id IS NOT NULL;

-- Trigram indexes power ILIKE %q% search on title/filename.
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE INDEX idx_drafts_title_trgm
  ON drafts USING gin (title gin_trgm_ops);
CREATE INDEX idx_drafts_filename_trgm
  ON drafts USING gin (filename gin_trgm_ops);

-- Entity-name search reuses the existing draft_entities.label column.
CREATE INDEX idx_draft_entities_label_trgm
  ON draft_entities USING gin (label gin_trgm_ops);
```

**Rationale:**
- `doc_type` defaults to `'eelnou'` so every existing row is valid without backfill.
- `parent_vtk_id` allows NULL for both VTKs (no parent) and unlinked eelnõud.
- `ON DELETE SET NULL` preserves the eelnõu if its VTK is deleted — owner's work isn't cascaded away.
- The CHECK constraint prevents VTK→VTK chains.
- `pg_trgm` is a stable, in-tree PostgreSQL 18 extension; no runtime dependency change.

### 2.2 `Draft` dataclass (`app/docs/draft_model.py`)

Add two fields:

```python
@dataclass(frozen=True)
class Draft:
    ...
    doc_type: Literal["eelnou", "vtk"] = "eelnou"
    parent_vtk_id: UUID | None = None
```

`fetch_draft`, `list_drafts_for_org`, and any other reader propagates them. No change to the constructor signature for callers that don't care (defaults handle it).

---

## 3. Upload flow

### 3.1 Form changes (`_upload_form` in `app/docs/routes.py`)

Add two fields between title and file input:

- **Dokumendi tüüp** — radio group `doc_type`, options "Eelnõu" (default, value `eelnou`) and "VTK" (value `vtk`).
- **Seotud VTK** — `<select>` named `parent_vtk_id`. Populated server-side at form-render time with the org's VTKs in `status IN ('ready', 'analyzing')`, sorted by `created_at DESC`. First option is "— vali —" (empty value = no link). Optional.

A small inline `<script>` toggles `Seotud VTK` to disabled when `doc_type=vtk` is checked, and re-enables it when `doc_type=eelnou` is checked.

### 3.2 Handler validation (`create_draft_handler`)

After existing validation:

- `doc_type` must be one of `{'eelnou', 'vtk'}` — server-side enforcement; reject with 400 otherwise.
- If `parent_vtk_id` is set:
  - reject if `doc_type == 'vtk'` (VTK cannot have a parent)
  - reject if FK target does not exist OR does not belong to `auth.org_id` OR has `doc_type != 'vtk'`
- Both fields persist into the new columns.
- `handle_upload(...)` is unchanged otherwise; pipeline starts as today.

### 3.3 Linking after upload

New route: `POST /drafts/{draft_id}/link-vtk`

- Body: form-encoded `parent_vtk_id` (UUID) or empty (unlink).
- Permission: `can_edit_draft(auth, draft)` (same as keep_draft_handler).
- Validation as above.
- On success: writes the lineage triple immediately (see §5) and returns the updated detail-page metadata fragment via HTMX.
- Triggered from a small "Seo VTKga" button on the detail-page metadata block, which opens the existing Modal primitive containing the picker.

---

## 4. List page (`GET /drafts`)

### 4.1 Filter bar

Above the table, HTMX-driven, all state URL-encoded so links are shareable and the browser back button works.

| Filter | Control | Default | Querystring |
|---|---|---|---|
| Search | text input, debounced 300 ms | empty | `q` |
| Tüüp | checkbox group (Eelnõu / VTK) | both checked | `type=eelnou&type=vtk` |
| Staatus | checkbox group (6 options) | all checked | `status=ready&status=...` |
| Üleslaadija | `<select>` of org users | any | `uploader=<uuid>` |
| Kuupäev "alates" | date input | empty | `from=YYYY-MM-DD` |
| Kuupäev "kuni" | date input | empty | `to=YYYY-MM-DD` |
| Sort | `<select>` | upload date desc | `sort=created_desc` |
| Reset filters | link | — | clears querystring |

`hx-get="/drafts"` + `hx-target="#drafts-table"` + `hx-push-url="true"` on the form. The whole table re-renders on each filter change. Pagination state is preserved per-filter via `page` querystring (existing).

### 4.2 Backend query strategy

Filtering and search run in two phases (`app/docs/draft_model.py::list_drafts_for_org_filtered`):

```python
def list_drafts_for_org_filtered(
    org_id: UUID, *,
    q: str | None,
    doc_types: set[str],
    statuses: set[str],
    uploader_id: UUID | None,
    date_from: date | None,
    date_to: date | None,
    sort: str,
    limit: int,
    offset: int,
) -> tuple[list[Draft], int]:
```

**Phase 1 — title + filename matches**

```sql
SELECT id FROM drafts
 WHERE org_id = :org_id
   AND (title ILIKE :q OR filename ILIKE :q)
```

**Phase 2 — entity-name matches** (only if `q` provided)

```sql
SELECT DISTINCT draft_id AS id
  FROM draft_entities
 WHERE label ILIKE :q
   AND draft_id IN (SELECT id FROM drafts WHERE org_id = :org_id)
```

**Phase 3 — merge ID set, apply remaining filters, sort, paginate**

Final SQL is built dynamically with the merged ID set (capped at 500 candidates before final filtering — fine for any single org). Total count is a separate `COUNT(*)` over the same filter+search WHERE clause for pagination.

Two queries + UNION-of-IDs is cheaper than a single 4-way join with DISTINCT. Trigram indexes make both ILIKE queries index-supported.

### 4.3 Table

Adds one column. Existing columns unchanged.

| Tüüp | Pealkiri | Failinimi | Staatus | Üles laaditud | Tegevused |
|---|---|---|---|---|---|

`Tüüp` renders a small Badge: "Eelnõu" (variant=info) or "VTK" (variant=neutral). Sortable columns: Pealkiri, Staatus, Üles laaditud (existing). New `Tüüp` column not sortable (use the Tüüp filter instead).

### 4.4 Empty states

- No drafts at all: existing `EmptyState` (#631 already shipped).
- No drafts matching filters: `EmptyState` with title "Filtritele vastavaid eelnõusid pole" + "Lähtesta filtrid" action.

---

## 5. Ontology write-path

### 5.1 New helper (`app/docs/graph_builder.py`)

```python
def write_doc_lineage(draft: Draft, parent_vtk: Draft | None) -> None:
    """Assert doc_type class + optional basedOn lineage into Jena.

    Idempotent; safe to call repeatedly. Writes into the draft's
    own named graph (draft.graph_uri).
    """
```

Behavior:

1. `<draft_uri> a estleg:DraftLegislation .` if `doc_type == 'eelnou'`.
2. `<draft_uri> a estleg:DraftingIntent .` if `doc_type == 'vtk'`.
3. If `parent_vtk` is provided: `<draft_uri> estleg:basedOn <parent_vtk.uri> .`
4. Triples written via SPARQL UPDATE (`INSERT DATA`) scoped to `GRAPH <draft.graph_uri>`.
5. On link-change (user re-links to a different VTK): `DELETE WHERE { <draft> estleg:basedOn ?old }` then re-insert.

### 5.2 Call sites

- `app/docs/analyze_handler.py` — at the end of the existing graph-population step, call `write_doc_lineage(draft, parent_vtk)` so the type assertion + lineage land in the same transaction-equivalent window.
- `app/docs/routes.py::link_vtk_handler` — call directly after the DB write, so lineage is reflected immediately even if analyze already finished.

### 5.3 What's intentionally NOT here

No new SPARQL query surface in this sub-project. The triple is written; consumption is for Sub-project B. Explorer and dashboard remain unchanged.

---

## 6. Detail page additions

### 6.1 Eelnõu detail

In the existing metadata `<dl>`, add a new row:

- **Seotud VTK** — if `parent_vtk_id` set: hyperlink "{vtk.title}" → `/drafts/{vtk.id}` + small "Eemalda" link. If unset: "—" + "Seo VTKga" Button that opens a Modal containing the same VTK picker as the upload form.

The picker reuses the same component logic.

### 6.2 VTK detail

Add a new Card after the existing impact-summary card:

- **Sellest VTKst tulenevad eelnõud**
  - List of child eelnõud (`SELECT * FROM drafts WHERE parent_vtk_id = :vtk_id ORDER BY created_at DESC`)
  - Each row: badge (status), title (linked), uploader, upload date
  - Empty state: "VTKga pole veel eelnõusid seotud."

---

## 7. Permissions

Reuses existing matrix:

- View VTK = same rule as view eelnõu (`can_view_draft`, org-scoped).
- Link / unlink VTK on an eelnõu = `can_edit_draft` (owner or admin). If `can_edit_draft` doesn't exist yet in `app/auth/policy.py`, add it as the same rule used by `keep_draft_handler` (currently inline). One-line addition.
- Picker only ever shows VTKs from the user's own org (no cross-org leak).

No new permission classes.

---

## 8. Testing

### 8.1 Migration
- `019_draft_doc_type_and_vtk_lineage.sql` up + down round-trip
- Existing rows survive with `doc_type='eelnou'`, `parent_vtk_id=NULL`
- Cannot insert a row with `doc_type='vtk' AND parent_vtk_id IS NOT NULL`

### 8.2 Upload
- VTK upload sets `doc_type='vtk'`, no lineage
- Eelnõu upload with VTK linked sets both columns
- Eelnõu upload rejecting cross-org VTK FK returns 400 + Estonian error
- Eelnõu upload rejecting `parent_vtk_id` referencing another eelnõu (not VTK) returns 400

### 8.3 Search
- Title trigram match (`Maantee` finds "Maanteeseaduse muutmine")
- Filename trigram match
- Entity-name match (a draft referencing `KarS § 121` is found by query "121" via `draft_entities.label`)
- Combined search-and-filter (`q=ka` + `status=ready` + `type=eelnou`) returns intersection

### 8.4 Filter state
- Filter querystring round-trips: GET with `?q=foo&status=ready&page=2` returns the same filtered slice as constructing those filters via the form
- Browser back button restores prior filtered view

### 8.5 List page rendering
- VTK badge variant differs from eelnõu badge variant
- DataTable sortable by upload date works after filter applied
- Pagination links preserve filter querystring

### 8.6 Lineage triple
- Eelnõu with `parent_vtk_id` set ⇒ named graph contains `<eelnou> estleg:basedOn <vtk> .`
- VTK named graph contains `<vtk> a estleg:DraftingIntent .`
- Eelnõu named graph contains `<eelnou> a estleg:DraftLegislation .`
- Re-link: old `basedOn` triple removed, new one inserted; only one such edge exists at any time
- `write_doc_lineage` is idempotent (calling twice produces the same triple set)

### 8.7 Detail page
- Eelnõu page shows "Seotud VTK" with link
- Eelnõu page with no VTK shows "Seo VTKga" button
- VTK page lists child eelnõud
- VTK page with no children shows empty state

### 8.8 Permission isolation
- User from org A cannot pick VTK from org B in any picker
- User from org A cannot link an eelnõu in org A to a VTK in org B (even by URL-tampering the FK)
- User from org A cannot view org B's VTK detail page

---

## 9. Out of scope (deferred)

These belong to other sub-projects in the Eelnõud epic:

- Cross-ministry visibility — Sub-project B (lifecycle)
- Draft versioning + diff (#618) — Sub-project B
- "Similar drafts" auto-suggest (#621) — Sub-project B
- Annotations on report rows (#619) — Sub-project C
- EU directive deadlines (#622) — Sub-project D
- Lineage SPARQL queries beyond writing the `basedOn` triple — Sub-project B
- Lineage edges in the explorer / timeline view — Sub-project B
- Auto-suggest VTK→eelnõu linkage based on entity overlap — future

---

## 10. Files touched

New:
- `migrations/019_draft_doc_type_and_vtk_lineage.sql`
- `tests/test_drafts_metadata_search.py`
- `tests/test_drafts_vtk_lineage.py`

Modified:
- `app/docs/draft_model.py` — `Draft` dataclass; new `list_drafts_for_org_filtered` reader
- `app/docs/routes.py` — upload form, create handler, list page, link-vtk handler, detail page
- `app/docs/graph_builder.py` — `write_doc_lineage` helper
- `app/docs/analyze_handler.py` — call `write_doc_lineage` at end of graph step
- `app/docs/__init__.py` — export new symbols if needed
- `tests/test_docs_*` — extend existing tests for new fields

No code in `app/explorer`, `app/chat`, or `app/drafter` is touched.
