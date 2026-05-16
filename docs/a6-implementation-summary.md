# A6 — EU Transposition Deadlines Töölaud Widget

**Task source:** `docs/2026-05-15-ontology-six-use-cases-plan.md`, section 5, Direction A, A6.

**Branch:** `a6-eu-deadlines-widget`.

## What landed

Three things:

1. **SPARQL helper** — new `app/analyysikeskus/eu_transposition.py` exposes
   `list_overdue_or_upcoming_transpositions(horizon_days, org_id)` and a
   frozen `TranspositionDeadlineRow` dataclass. The query joins
   `estleg:EULegislation` with `transpositionDeadline`, `rdfs:label`,
   `celexNumber`, both transposition-edge directions (`UNION` of
   `transposesDirective` / `transposedBy`), and the raw
   `transpositionStatus` literal. The cutoff is baked into the query as
   an `xsd:date` literal (server-controlled, never user input). The
   Python rollup uses the existing
   `app.docs.impact.eu_transposition.normalise_transposition_status` so
   the status-bucket vocabulary lives in exactly one place; rows with
   the `kaetud` bucket are dropped, multi-act directives roll up to the
   worst sibling status, and unparseable deadline literals are skipped.
   `org_id` is accepted-but-ignored for forward compatibility (the
   ontology has no `responsibleMinistry` predicate today).

2. **Töölaud widget** — `app/templates/dashboard.py` gains
   `_get_eu_transposition_deadlines()` (wraps the SPARQL helper in a
   1-second wall-clock timeout via `ThreadPoolExecutor` so a slow Jena
   never blocks the page) and `_eu_deadlines_card()`. The widget is
   placed between **`Kõrge riskiga leiud`** and **`Aegunud analüüsid`**.
   When the helper returns no rows the entire card is omitted — no
   empty placeholder. Populated state renders a `DataTable` with
   Estonian status labels (`Tähtaeg möödunud`, `Tähtaeg läheneb`,
   `Ülevõtt puudub`, `Ülevõtt osaline`), CTA links to
   `/analyysikeskus/el-ulevott?sisend=<CELEX>`, and a `Näita kõiki (X)`
   row when more than 5 rows exist.

3. **Configuration** — module-level constant
   `DEFAULT_TRANSPOSITION_HORIZON_DAYS = 90` in
   `app/analyysikeskus/eu_transposition.py` is the single source of
   truth.

## Tests

`tests/test_eu_transposition_deadlines.py` — 23 tests across query
construction, row aggregation (mixed overdue/upcoming/transposed fixture
graph), empty-state hiding, populated-state rendering smoke checks,
widget placement, and the soft-timeout gating. `ruff check`, `ruff
format`, and `pyright` all green on the three touched files. The pre-
existing `tests/test_dashboard.py` and `tests/test_eu_transposition.py`
suites pass unchanged.
