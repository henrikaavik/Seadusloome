# 2026-05-18 — Bug-fix plan after live testing pass

This plan covers the eight bugs filed on 2026-05-18 after an end-to-end live test of
`seadusloome.sixtyfour.ee` (drafter role, real climate-law PDF). It is grounded in the
user's targeted triage comments on each ticket and direct prod-Jena + prod-Postgres
inspection of the `Kliimakindla majanduse seadus` upload (`218eea92-…`).

## Issue map (GitHub)

| #   | Severity | Title                                                                                  | Root cause family              |
| --- | -------- | -------------------------------------------------------------------------------------- | ------------------------------ |
| 801 | **P0**   | Entity URI resolver returns NULL on high-confidence refs                               | resolver ↔ ontology data shape |
| 802 | **P0**   | Nõustaja chat hangs indefinitely (>3 min, no first token)                              | chat orchestrator / deploy     |
| 803 | P1       | Normi mõjuahel can't resolve canonical example `AvTS § 35`                             | same as 801 + data coverage    |
| 804 | P1       | Õiguskaart TDZ ReferenceError `minimapViewportRect` (explorer.js:263 → 305)            | explorer.js init order         |
| 805 | P1       | EL ülevõtt can't resolve `32016R0679` (GDPR)                                           | EU ontology data coverage      |
| 806 | P1       | Õiguskaart `?search=` deep link does nothing                                           | likely blocked by 804          |
| 807 | P1       | Õiguskaart `?vaade=koik` renders empty canvas                                          | likely blocked by 804          |
| 808 | P2       | Drafts upload — `— vali —` "Seotud VTK" dropdown has only the empty option             | empty-state UX                 |
| 800 | comment  | Dashboard EU-deadlines widget shows `01.01.1001 · 374511 p möödunud`                   | sentinel-date filter           |

Note the early summary in chat got #804/#805 swapped — GitHub has **#804 = TDZ** and
**#805 = CELEX/GDPR**.

## Order of execution

The bugs are deliberately ordered to minimise rework:

```
Wave 1 (1 day): #804 (TDZ) → retest #806/#807; #800 sentinel filter; #808 empty state
Wave 2 (2–4 days): #801 + #803 (resolver) shared work; data fix for #805 + AvTS coverage
Wave 3 (1–2 days): #802 chat — diagnose deployed SHA first, then code if needed
```

The TDZ fix is the highest-leverage Wave 1 item because it unblocks two other tickets
without resolver work. Wave 2 is the meat: the resolver rewrite **and** the ontology
coverage gaps that surface there. Wave 3 is gated on production diagnostics before any
code lands.

---

## Wave 1 — small mechanical fixes

### #804 — Õiguskaart TDZ ReferenceError

**File:** `app/static/js/explorer.js`

**Root cause** (user-confirmed): the mini-map DOM block (`minimapEl`, `minimapSvg`,
`minimapLinkLayer`, `minimapNodeLayer`, `minimapFocusLayer`, `minimapViewportRect`,
`_minimapTransform`) is declared with `const` at lines 296–311. The zoom setup at
line 242 attaches an `on('zoom', …)` handler that calls `updateMinimapViewport`. At
line 263 we call `svg.call(zoomBehavior.transform, d3.zoomIdentity…)` which fires that
handler synchronously. `updateMinimapViewport` reads `minimapViewportRect`, which is
still in the temporal dead zone → `ReferenceError`. `typeof` cannot guard a TDZ `const`,
so the only fix is reordering.

**Fix:** move the mini-map block (lines 296–311) above the zoom-behaviour definition
(currently at line 242). All references to those consts inside `zoomBehavior` /
`updateMinimapViewport` become legal.

**Verification:**
1. Reload `/explorer`. Console should be clean.
2. Pan / zoom — the mini-map viewport rect should track the main view.
3. After this lands, re-test #806 and #807 *before* doing any other work on them —
   the user already flagged that both bootstrap paths likely sit below this throw.

**Acceptance:** No `ReferenceError` in console on any `/explorer*` URL. Add a Playwright
smoke (or extend an existing one) that asserts `page.on('pageerror')` fires zero times
after navigating to `/explorer`, `/explorer?focus=<known-uri>`, `/explorer?search=foo`,
`/explorer?vaade=koik`.

### #806 — `?search=` deep link

**Status:** retest after #804.

If the deep-link still doesn't work after the TDZ fix:
- Locate the URL-param bootstrap in `app/static/js/explorer.js` (look for
  `URLSearchParams` near `init()`).
- Verify it reads `search`, sets `#search-input.value`, and either calls the same
  handler the toolbar "Otsi" button uses or dispatches an `input` + form-submit.
- If the input is populated but no fetch happens, the issue is the missing event
  dispatch; if neither happens, the param isn't read.

**Acceptance:** `/explorer?search=AvTS%20%C2%A7%2035` shows the search input
pre-populated and either lists results or an explicit "Tulemusi ei leitud" empty state.

### #807 — `?vaade=koik` empty canvas

**Status:** retest after #804.

If still empty: trace the same URL-param bootstrap and check that `vaade=koik` triggers
the same code path as the "Näita kõigi liikide ülevaadet" button. Likely a missing
case in the URL handler.

**Acceptance:** `/explorer?vaade=koik` renders ≥5 category nodes within 2s, identical
to clicking the in-app button.

### #800 — sentinel date filter (comment, not new issue)

**File:** `app/analyysikeskus/eu_transposition.py` (the `_parse_deadline()` /
`_aggregate_rows()` helpers — user-confirmed locations).

**Root cause** (user-confirmed): `1001-01-01` is a syntactically valid date, so
`_parse_deadline()` accepts it. `_aggregate_rows()` computes a huge negative
`days_remaining` from it and renders `01.01.1001 · 374511 p möödunud`.

**Fix:** reject implausible-but-valid sentinel dates at a sensible floor. The two
candidate rules:

- `deadline.year < 1957` — Treaty of Rome floor; admits all real EU directives.
- `deadline.year < 1980` — pragmatic floor; admits everything in the current ingestion
  with real deadlines.

Apply this in `_parse_deadline()` (return `None` for years below the floor) so the
filtering happens once and uses one rule throughout the aggregation.

**SPARQL filter — ground in real predicates AND push the year floor server-side.**
Prod-Jena confirms `estleg:inForce` (26,313 boolean triples, sample
`"true"^^xsd:boolean`) and `estleg:entryIntoForce` (date) exist. There is **no**
`estleg:repealedAt`. Use what's there.

Critically, the year floor must also live in the SPARQL — not only in
`_parse_deadline()`. `_build_deadlines_query()` currently does
`ORDER BY ASC(?deadline) LIMIT 50` (`app/analyysikeskus/eu_transposition.py` around
line 168). If the 50 oldest rows are all sentinel `1001-01-01` dates, the Python
filter drops them and the widget silently goes empty even though valid rows exist
further down the result set. Add the lower-bound filter to the WHERE clause so
sentinels are excluded *before* the `LIMIT`:

```sparql
?euAct estleg:transpositionDeadline ?deadline .
FILTER(?deadline >= "1980-01-01"^^xsd:date)
FILTER(?deadline <  "{cutoff_literal}"^^xsd:date)
?euAct estleg:inForce true .
```

Then keep the Python `_parse_deadline()` floor as a defence-in-depth check (in case
the source data ever lands an unexpected sentinel above 1980).

Avoid hand-rolling `repealedAt`-style filters — that was a guess in an earlier draft
of this plan and the data doesn't carry that predicate.

**Acceptance:**
- No row on `/dashboard` shows year < 1980 in the EU-deadlines widget.
- The widget either hides or shows "Tähtaeg teadmata" for rows where
  `_parse_deadline()` returns `None`.
- Only `estleg:inForce true` directives surface.
- Add unit tests in **`tests/test_eu_transposition_deadlines.py`** (existing file —
  this is where `_parse_deadline()` tests live) that feed the sentinel literal and
  assert the row is dropped, plus a fixture covering an in-force/out-of-force pair.

### #808 — VTK dropdown empty state

**File:** `app/docs/routes/_upload.py` (the `_vtk_picker()` helper — user-confirmed
location; the routes package uses underscore-prefixed module names).

**Root cause** (user-confirmed): `list_vtks_for_org()` correctly filters by
`doc_type='vtk'` and eligible statuses. The org currently has zero VTK rows, so the
picker correctly renders the empty sentinel and nothing else. UX issue, not a data bug.

**Fix:** in `_vtk_picker()`, branch on `not vtks`:
- Render the select **disabled** with the bare `— vali —` option.
- Add an inline help message below it: *"Organisatsioonis pole veel VTKsid — saate
  eelnõu üles laadida ilma VTK-ta."*
- Keep the field optional in the form spec; this is purely visual.

**Acceptance:**
- Empty-org test: form renders a disabled select with help text.
- Non-empty-org test: form renders the VTK list (regression).
- Two unit tests in `tests/test_docs_routes.py` cover both branches.

---

## Wave 2 — resolver + ontology coverage (the big one)

This is the bulk of work. #801 and #803 share a root cause; #805 partly shares it; all
three need both code and data work.

### Shared root cause (user-confirmed via local triage)

`app/docs/reference_resolver.py` makes three assumptions the data does not honour:

1. **`_get_law_dict()` expects `estleg:sourceAct` to be an object URI** with
   `estleg:shortName` / `rdfs:label` reachable from there. **Reality:** the ontology
   stores `sourceAct` as a **literal title string** (e.g. `"Atmosfääriõhu kaitse
   seadus"`), and the structural join path is `estleg:partOfAct`. So the law dict
   loads zero rows.
2. **`_resolve_provision()` exact-matches the full extracted text against
   `estleg:paragrahv`**. **Reality:** provisions store the section literal alone
   (e.g. `"§ 143."` — with the trailing period!), not the act-name prefix. Even
   whitespace-normalised, `"atmosfääriõhu kaitse seaduse §-s 143"` ≠ `"§ 143."`.
3. **No two-step decomposition.** The resolver never decomposes
   `<act-name-or-abbrev> § <num> [lg <m>]` into separate act lookup + section lookup.

Prod-Jena confirms (see queries below the plan):
- `estleg:shortName` triples in the entire store: **0**.
- `estleg:paragrahv` literals sample like `"§ 1."` (with period).
- `estleg:Law` instances: **627**.
- `estleg:sourceAct` triples: **24,221** — but as literals, per the user's local
  source-data audit.
- `estleg:EULegislation` instances: **33,242**.

### Step 1 — Diagnostic spike (½ day) — **DONE 2026-05-18**

Spike script is committed at `scripts/probe_ontology_shape.py` and was run against
prod. Findings below supersede the original plan assumptions for Step 2. Three
significant surprises:

1. **`estleg:sourceAct` is 100% literal in prod** (24,221 triples, all
   `xsd:string`, sample `"Alaealise mõjutusvahendite seadus"`). There are zero
   URI objects. The act half of any reference resolves to a literal title, never
   a URI.

2. **Neither `estleg:partOf` nor `estleg:partOfAct` exists in prod** — both
   counts are zero. The only working provision-to-act join is the literal
   `sourceAct` edge. App-side code (`app/analyysikeskus/{burden,competency,…}.py`
   and `app/docs/impact/queries.py`) using `estleg:partOf` is producing zero
   rows from those branches today; this is now confirmed, not just suspected.

3. **`paragrahv` is *mostly* `"§ N."` but ~6% of rows omit the period.** The
   top-20 sample is 100% with-period (`"§ 1.", "§ 2.", …`), but 1,467 of the
   24,215 literals (~6%) are `"§ 3", "§ 15", …` with no period. The resolver
   must match both forms.

4. **`estleg:Law` instances are *topic-map* clusters, not canonical acts.** All
   627 `?act a estleg:Law` rows have `rdfs:label` values like `"Alkoholiseaduse
   teemakaardistus (alkoholiõigus)"` and URIs like `ALKS_Map_2026`. They are
   *not* the atomic act URIs the abbreviation map needs to point at. The two
   real sources of act identity are: (a) the literal titles in
   `estleg:sourceAct`, and (b) the `LegalProvision_<TOKEN>` rdf:type subclasses
   that carry the real abbreviations (`KRIMIN`, `ATMOSF`, `RIIGIL_2`, `KAITSE_3`,
   `VTMS`, `VANGIS`, `VPTS`, `TMS`, `KINDLU`, `VALISM`, …).

5. **Provision URI naming is stable and predictable**: `estleg:<TOKEN>_Par_<N>`
   (e.g. `AMVS_Par_1`, `ATMOSF_Par_143`). The resolver should try this as a
   direct URI guess with `ASK { <uri> ?p ?o }` before falling back to
   structural SPARQL — wins the common case for one cheap roundtrip.

6. **Plan SPARQL nit (Jena 5.x):** Query A's
   `GROUP BY (DATATYPE(?o))` raises "Non-group key variable in SELECT" on Jena
   5.x. Script uses a `BIND … AS ?dt` + `GROUP BY ?dt` rewrite that runs cleanly.

These findings change the Step 2 design — see Step 2 below. They also make
Step 5 (downstream `sourceAct`/`partOf` audit) load-bearing rather than nice-to-
have: every `?_parentAct estleg:transposesDirective ?euAct` join in
`app/docs/impact/queries.py` is silently producing zero rows for this entire
corpus because `?_parentAct` is a string literal, not a URI.

For historical reference, the spike's exact queries:

```sparql
# A. sourceAct: literal title vs object URI? (Datatype histogram.)
SELECT (DATATYPE(?o) AS ?dt) (SAMPLE(?o) AS ?ex) (COUNT(*) AS ?n)
WHERE { ?s estleg:sourceAct ?o } GROUP BY (DATATYPE(?o))

# B. provision literal canonical form (does it really carry the trailing period?).
SELECT DISTINCT ?paragrahv (COUNT(*) AS ?n)
WHERE { ?s estleg:paragrahv ?paragrahv }
GROUP BY ?paragrahv ORDER BY DESC(?n) LIMIT 20

# C. Parent predicate coverage — count BOTH partOf and partOfAct.
SELECT ?p (COUNT(*) AS ?n) (SAMPLE(?o) AS ?ex)
WHERE { ?s ?p ?o . FILTER(?p IN (estleg:partOf, estleg:partOfAct)) }
GROUP BY ?p

# D. Provision → act join paths (sample 10 to eyeball).
SELECT ?prov ?actLit ?partOf ?partOfAct
WHERE {
  ?prov estleg:paragrahv ?par .
  OPTIONAL { ?prov estleg:sourceAct ?actLit }
  OPTIONAL { ?prov estleg:partOf    ?partOf }
  OPTIONAL { ?prov estleg:partOfAct ?partOfAct }
} LIMIT 10
```

Output of these queries dictates which join paths the new resolver uses **and**
informs Step 5 below (downstream audit).

### Step 2 — Rewrite the resolver (#801 + #803, 1.5 days)

**File:** `app/docs/reference_resolver.py` (and tests).

**New strategy (revised per spike findings — Step 1):**

```
_resolve_law(ref):
  1. Normalise input. Do NOT strip arbitrary Estonian case suffixes
     (-e/-i/-st/-ks/-s/-le) from any token — that creates false positives
     against unrelated law names. Instead match explicit legal-reference
     patterns ("seadus", "seaduse", "seaduses", "seadusest", "seaduseni",
     "seadustik", "seadustiku", "seadustikus", …) and strip those alone.
     Everything else stays untouched.

  2. Abbreviation lookup against an **ontology-derived map** built at
     resolver-instance construction. The spike confirmed `estleg:Law`
     instances are topic-map clusters (e.g. `ALKS_Map_2026`), NOT atomic
     acts. Do NOT use `?act a estleg:Law ; rdfs:label ?label`. Instead
     build the map by walking the `LegalProvision_<TOKEN>` rdf:type
     subclasses and pairing each <TOKEN> with the most-frequent
     `sourceAct` literal among its members:

       SELECT ?prov ?cls ?actLit WHERE {
         ?prov a ?cls ;
               estleg:sourceAct ?actLit .
         FILTER(STRSTARTS(STR(?cls),
                "https://data.riik.ee/ontology/estleg#LegalProvision_"))
       }

     Then in Python: derive <TOKEN> from the URI local-name suffix
     (`LegalProvision_KRIMIN_2` → `KRIMIN_2`); for each <TOKEN> pick the
     most-frequent `?actLit` as the canonical title. Output:
        token_to_title: {"AVTS": "Avaliku teabe seadus",
                         "ATMOSF": "Atmosfääriõhu kaitse seadus",
                         "REELS": "Riigieelarve seadus", …}

     The act "identifier" produced by `_resolve_law` is the **literal title
     string**, not a URI — because the corpus has no act URIs to point at.

     Cache on the resolver instance with the same lazy-load + Lock pattern
     as today's `_law_dict`; rebuild on worker restart.

  3. Fall back to fuzzy match on `_normalise_law_name(title)` keys across
     all distinct `?title` values from `?prov estleg:sourceAct ?title`.

_resolve_provision(ref):
  1. Decompose ref.ref_text via regex: capture (<act-name-or-abbrev>) and
     (<section>). Estonian inflection on the section side: "§ 35",
     "§-s 35", "§ 35 lg 1 p 5", "paragrahv 35", "paragrahvi 35".

  2. Resolve the act half via _resolve_law → produces (TOKEN, title_literal)
     or (None, title_literal_fuzzy) on fuzzy-only match.

  3. **Cheap URI-guess fast path.** Provision URIs follow the convention
     `estleg:<TOKEN>_Par_<N>` (spike confirmed across 10 sampled rows). If
     TOKEN is known, build the guess URI and `ASK` for its existence:

       ASK { <https://data.riik.ee/ontology/estleg#<TOKEN>_Par_<N>> ?p ?o }

     On hit, return the URI directly. One roundtrip, no joins.

  4. **Structural fallback** when the guess misses or TOKEN is unknown.
     The spike confirmed `partOf` / `partOfAct` do NOT exist in this corpus,
     so the section-match query is single-arm (no UNION):

       SELECT ?p WHERE {
         ?p estleg:paragrahv ?par ;
            estleg:sourceAct  ?actLit .
         VALUES ?actLit { "<resolved title>" }
         VALUES ?par     { "§ <num>." "§ <num>" }   # BOTH forms — spike
                                                      # shows ~6% lack period
       }
       LIMIT 1

     If the source data later gains `partOf` triples, widen this to a
     dynamic UNION gated on a one-time predicate-presence probe at
     instance construction. Don't ship empty UNION arms today.

  5. Optionally narrow with lõige / punkt if present in the input.

  IMPORTANT — partial-match semantics. If the act resolves but the section
  does NOT (e.g. "AvTS § 35" where source data has only thematic AvTS
  nodes), return a distinct ResolvedRef state — do NOT silently collapse
  to the act. The corpus has no act URIs, so the partial state carries
  the LITERAL TITLE:

      ResolvedRef(
          extracted=ref,
          entity_uri=None,
          partial_match={
              "act_token": "AVTS",                       # nullable
              "act_title": "Avaliku teabe seadus",       # always set on partial
              "section": "35",
          },
          matched_label="Avaliku teabe seadus (sätet § 35 ei leitud)",
          match_score=0.5,
      )

  Downstream impact code must check `partial_match` explicitly. Track the
  schema addition + DB column `draft_entities.partial_match jsonb` in the
  same PR. See Step 5 for how the impact compliance branch needs to change
  to consume this state (the corpus has no act URIs, so the existing branch
  that joins `?_parentAct estleg:transposesDirective ?euAct` was always
  producing zero rows — now we know that for certain, fix it there).

_resolve_eu_act, _resolve_concept, _resolve_court_decision:
  Keep current literal-match shape; add normalisation for case + whitespace.
  See #805 for data-coverage work.
```

**Add diagnostic logging** in every resolver path on a "no match" outcome — but
treat the ref text as sensitive. These references come from pre-publication drafts;
emitting them at INFO into prod logs is a leak.

Default behaviour (production):

```python
# app/docs/reference_resolver.py
import hmac, hashlib, os
from app.config import is_stub_allowed  # existing helper; False ⇔ APP_ENV=production

_REF_HASH_SECRET_ENV = "RESOLVER_REF_HASH_SECRET"

def _get_ref_hash_secret() -> bytes:
    """Resolve the HMAC secret lazily.

    Module-import-time reads break local dev, CI, and unit tests, any of
    which can ``import app.docs.reference_resolver`` without the env var
    set. Instead:
      - In production (``APP_ENV=production``, i.e. ``not is_stub_allowed()``):
        require the var; raise a clear RuntimeError if missing so the app
        refuses to start with unredacted logging.
      - Outside production: fall back to a dev sentinel so imports and
        tests work; the resulting ``ref_id`` is still stable per-process
        and never leaves the dev machine.
    Tests monkeypatch this helper directly.
    """
    secret = os.environ.get(_REF_HASH_SECRET_ENV)
    if secret:
        return secret.encode("utf-8")
    if not is_stub_allowed():  # production
        raise RuntimeError(
            f"{_REF_HASH_SECRET_ENV} must be set in production "
            "(see docs/2026-05-18-bugfix-plan.md and .env.example)."
        )
    return b"dev-only-resolver-ref-id-secret"

def _ref_id(ref_text: str) -> str:
    """HMAC-truncated ref identifier — stable across runs, not enumerable.

    Plain SHA-256 over a short legal reference (e.g. "KarS § 211") is
    low-entropy enough to dictionary-attack offline. HMAC with an app
    secret blocks that without changing the call-site shape.
    """
    return hmac.new(
        _get_ref_hash_secret(), ref_text.encode("utf-8"), hashlib.sha256
    ).hexdigest()[:12]

logger.info(
    "resolver: %s unresolved draft_id=%s ref_id=%s tried_keys=%d candidates=%d",
    ref.ref_type, draft_id, _ref_id(ref.ref_text),
    len(tried_keys), len(candidates),
)
```

Add `RESOLVER_REF_HASH_SECRET=` to `.env.example` (empty placeholder, with a
comment pointing to this plan) and set it in Coolify. Tests in
`tests/test_docs_reference_resolver.py` monkeypatch `_get_ref_hash_secret` to a
fixed deterministic secret so assertions on the hashed identifier are stable.

Plus per-draft aggregate counters (`resolver_miss_total{draft_id=…, ref_type=…}`)
so dashboards can see "draft X had 5 unresolved provisions" without per-ref
identifiers at all — these counters are the preferred signal for routine
monitoring. The `ref_id` line is for the case where ops needs to correlate the
same miss across the resolver log, the analyzer log, and the reverse-fill audit
log without ever materialising the underlying text.

The HMAC secret in prod (`RESOLVER_REF_HASH_SECRET`) is rotated on the same
cadence as other app secrets. If it ever leaks, the re-derivation cost forces an
attacker back to brute force per-ref.

For deep debugging, raw `ref.ref_text` is gated behind a debug log level and an
env flag (`SEADUSLOOME_RESOLVER_DEBUG_LOG_TEXT=1`) that's never set in prod. Same
gate protects per-row audit-log writes in the reverse-fill script (see Step 3).

Currently we have no idea why a 0.97-confidence extraction missed; the HMAC'd
line + counters pay for themselves the first time, without leaking the
underlying text or being trivially enumerable.

**Tests (extend existing file):**

- `tests/test_docs_reference_resolver.py` (existing — do not create a new file).
  - `AvTS § 35` → act half resolves; provision section returns the
    `partial_match` state (act-level only) — assert the impact code path
    does **not** silently treat that as a whole-act match.
  - `karistusseadustiku §-s 211` → both halves resolve to URIs.
  - `riigieelarve seaduse § 20 lõike 5` → either both halves resolve, OR
    `partial_match` if RES is thematic-only in prod (verify with the
    Step 1 spike first).
  - `Atmosfääriõhu kaitse seaduse §-s 143` → matches the real
    `estleg:ATMOSF_Par_143` (or equivalent — verify the URI in prod first).
  - Inflection cases: `AvTS-i § 35`, `KarS § 211 lg 2`, `paragrahvi 143`.
  - False-positive guard: `karistusseaduselt` (unrelated suffix) must NOT
    resolve via overly aggressive suffix stripping.
  - Each miss case asserts the structured log line is emitted.

**Acceptance:**

- Re-run extract on draft `218eea92-…`: at least the two provision refs (AvTS-style)
  resolve to URIs.
- Scoped resolved-count query (don't use a global count — unrelated drafts resolving
  in the background would falsely make this look "passing"):

  ```sql
  SELECT
    COUNT(*) FILTER (WHERE entity_uri IS NOT NULL)         AS resolved,
    COUNT(*) FILTER (WHERE partial_match IS NOT NULL)      AS partial,
    COUNT(*)                                               AS total
  FROM draft_entities
  WHERE draft_id = '218eea92-a23d-4907-a8ab-b47c7203dedd';
  ```

  Expect `resolved >= 1` and `total = 7`.
- The climate-law impact report shows non-zero "Mõjutatud üksused" for at least the
  AõKS § 143 provision.

### Step 3 — Reverse-fill on existing drafts (½ day)

Once the resolver works, the existing `Mõjuanalüüsi test 2` and other drafts are still
sitting on `entity_uri = NULL`. Add an admin job (one-off script under
`scripts/reresolve_existing_drafts.py`) — drafts are sensitive prod data, so this is
**not** a "run once and trust" script. Required flags:

```
--dry-run                 default ON; preview proposed updates without writing
--draft-id <uuid> [...]   scope to one or more specific drafts
--org-id <uuid>           OR scope to a single org
--before <iso-date>       only touch rows with created_at < cutoff
--batch-size <n>          default 50; bound transaction size
--audit-log <path>        write per-row before/after to a CSV for review
```

Behaviour:

- Without `--dry-run` and without either `--draft-id` or `--org-id`, refuse to run.
- Per row, default audit columns are non-sensitive:
  `(draft_id, ref_id, ref_type, old_uri=None, new_uri, score)` — where
  `ref_id` is the HMAC-truncated identifier from the resolver (see
  "Add diagnostic logging" above), **not** the raw `ref_text`.
- Raw `ref_text` is written to the audit log **only** when the same
  `SEADUSLOOME_RESOLVER_DEBUG_LOG_TEXT=1` env flag is set that gates the
  resolver's debug logging. Without it, the script writes the redacted
  shape. This keeps the rule consistent across logger and audit sink.
- Per draft touched: enqueue a re-analyse job so the impact report refreshes
  (idempotent — the job queue's `FOR UPDATE SKIP LOCKED` handles duplicates).
- Print a summary at exit: `N rows scanned, M updated, K errors`.

First production run: scope to **only** `--draft-id 218eea92-…` (the climate-law
upload from this testing pass) with `--dry-run`, review the audit log, then re-run
without `--dry-run`. Only after that, widen to the rest of the test-draft set.

### Step 4 — EU coverage / GDPR (#805, ½ day)

**Important framing fix** — GDPR is a **regulation** (`R` in CELEX `32016R0679`),
**not** a directive. Regulations have direct effect and *no* normal national
transposition deadline. The "EL ülevõtt" workflow is by definition about transposition,
which makes GDPR a category mismatch as an example.

**Three real work items, not two:**

1. **Decide what `?sisend=32016R0679` should do.** Two acceptable answers:
   - **(a)** The workflow recognises the act as a regulation and renders a
     "Määrus — ülevõtmist ei nõuta, kohaldatakse vahetult; vt harmoneerimine"
     panel, optionally listing harmonisation links (`estleg:harmonisedWith`) if
     present in the data. This is the right product behaviour.
   - **(b)** Keep the workflow directive-only and **change the dashboard
     example** from `32016R0679` to a real directive CELEX that exists in
     prod (`32016L0011` is one confirmed example from the prod CELEX
     scan — verify there are better candidates with non-trivial transposition
     data before picking).

   I'd ship (a) — it's the honest answer and the same data shape probably
   covers other regulations users will paste in.

2. **Data coverage.** The user's local source audit showed `32016R0679` is **absent**
   from `krr_outputs/eurlex/eurlex_combined.jsonld` and from the deployed Jena
   (a SPARQL `FILTER(REGEX(?celex, "32016"))` against prod returned only
   `32016L0011`). Decide whether to re-ingest with GDPR + other canonical
   CELEXes (`32019L0790`, `32018R1725`, `32016L0680`…) — independent of (1)
   because users will paste these CELEX numbers regardless.

3. **Resolver normalisation.** The current `_CELEX_RE` at
   `app/docs/reference_resolver.py:53` is
   `re.compile(r"\b(\d{5}[A-Z]\d{1,4})\b")` — strictly uppercase. A user
   pasting `32016r0679` (lowercase `r`) fails at the regex step *before* we
   ever get to SPARQL, so uppercasing before injection alone wouldn't help.

   Two acceptable fixes, pick one:
   - Compile the regex with `re.IGNORECASE`, then uppercase the captured
     group before binding. Keeps strict 1-char-sector validation.
   - Uppercase the **input string** before `_CELEX_RE.search()`, then run
     the unchanged regex. Slightly cheaper; same outcome.

   Either way also strip whitespace. Covered by tests in
   `tests/test_docs_reference_resolver.py` with case-variant inputs
   (`32016r0679`, ` 32016R0679 `, `32016R0679`).

**Acceptance:**

- `/analyysikeskus/el-ulevott?sisend=32016R0679` either (i) shows the
  "regulation — no transposition required" panel with the GDPR record
  found in Jena, or (ii) the dashboard example is changed and the new
  example resolves cleanly.
- Regression test in `tests/test_docs_reference_resolver.py` for case-
  variant inputs (`32016r0679`, ` 32016R0679 `).

### Step 5 — Downstream sourceAct / partOf audit (1 day) — **LOAD-BEARING**

Fixing the resolver makes `entity_uri` non-null for the first time. But the
downstream SPARQL that consumes those URIs has been silently coping with empty
inputs and is itself making assumptions about predicate shape that the Step 1
spike has now disproven. As soon as URIs flow, the next layer of bugs surfaces.

This was originally tagged "½ day, do it for hygiene." The spike confirmed
**`estleg:partOf` and `estleg:partOfAct` do not exist in prod** (zero rows for
both). That means every SPARQL fragment in the app that says
`?provision estleg:partOf ?act` is currently producing **zero rows** for the
entire corpus — silently. Step 5 is the difference between "resolver works,
impact reports still empty" and "resolver works, impact reports populated." Do
not skip it.

1. **Impact compliance (`app/docs/impact/queries.py`).** The "EU compliance"
   branch around line 295–330 unions `estleg:sourceAct` and `estleg:partOf`.
   Both branches are currently dead in prod:
   - The `sourceAct` branch binds `?_parentAct` to a string literal and then
     joins it against `?_parentAct estleg:transposesDirective ?euAct` — literals
     can't be subjects of those triples, zero rows.
   - The `partOf` branch references a predicate with zero triples in the corpus
     — zero rows.

   Fix: rewrite so the compliance check uses the literal `sourceAct` title as a
   join key. Two acceptable shapes — pick whichever is cleanest:
   - (a) Reverse-lookup the act URI from the title literal first
     (`?actUri rdfs:label ?title . VALUES ?title { "<lit>" }`), then continue
     the existing chain.
   - (b) Skip the act-URI hop entirely: query for directives whose own
     `estleg:transposedIntoTitle` (or equivalent literal predicate, if one
     exists — verify with the spike script) matches the act title.

   Run a small additional spike to enumerate transposition predicates by
   datatype before picking (a) vs (b):

   ```sparql
   SELECT ?p (DATATYPE(?o) AS ?dt) (COUNT(*) AS ?n)
   WHERE { ?s ?p ?o . FILTER(?p IN (estleg:transposesDirective,
                                    estleg:transposedBy,
                                    estleg:harmonisedWith)) }
   GROUP BY ?p (DATATYPE(?o))
   ```

   Add a regression test that walks the climate-law upload's resolved provisions
   through this query and asserts at least one non-zero compliance row.

2. **Analüüsikeskus workflows.** Grep the workstreams to enumerate which use
   `partOf`, `partOfAct`, or `sourceAct`:

   ```bash
   grep -rn "estleg:partOf\|estleg:partOfAct\|estleg:sourceAct" \
     app/analyysikeskus app/docs app/explorer
   ```

   At time of writing:
   - `burden.py`, `competency.py`, `sanctions.py`, `court_practice.py`,
     `history.py`, `impact/queries.py` → use `estleg:partOf`.
   - Resolver was the only place using `estleg:sourceAct` as a URI predicate.

   Walk each file and either confirm the predicate is correct for its corpus or
   widen to a UNION based on Step 1's findings. Where multiple predicates are
   accepted, factor the common fragment into a helper (e.g.
   `app/ontology/relations.py:provision_to_act_pattern()`) so future drift is
   one-place. See follow-up #785 which already tracks this refactor.

3. **Re-run the climate-law impact pipeline** end-to-end after the audit and
   confirm: resolver returns URIs, impact engine surfaces real rows, EU compliance
   panel produces non-empty results (or an honest "no compliance links" when the
   data really has none — but verified, not silenced).

### Step 6 — AvTS coverage gap (#803, ¼ day diagnosis)

The user flagged that AvTS in the source data is **thematic, not section-level**
(`AVTS_Par_JuurdepaasuYldpohimotted`, not `AvTS § 35`). Until that's ingested, the
"AvTS § 35" example *cannot* fully resolve to a provision even with a correct
resolver — it'll resolve to the AvTS act URI and a "see act, no exact §" response.

**Decision required** (raise this in a stand-up or in #803):
- Re-ingest AvTS with section-level granularity?
- Switch the dashboard example to a law that *does* have §-level data (e.g.
  `karistusseadustiku § 211`)?

For the immediate fix, change the dashboard cards to use an example we know resolves
(verify against prod first). File a follow-up issue for full section-level coverage.

---

## Wave 3 — chat hang (#802)

### Diagnostic outcome — 2026-05-18, verdict (d): FastHTML signature resolution

The Wave 3 Step 1 diagnostic completed with a definitive root cause that
**supersedes** every "watchdog/timeout" hypothesis in earlier drafts of this plan:

- Deployed app SHA matches `main` HEAD (`fbff868`) — no drift.
- `messages` table: **0 rows in all of prod**. Conversation
  `b034e693-9250-4c14-b8c9-1e3580508feb` has zero persisted messages. So does every
  other conversation.
- `llm_usage`: **0 rows with any `chat%` feature, ever.**
- Container logs at 15:26:49 (the test conversation) and at two other live chat
  sessions on 2026-05-18 all show the same ASGI exception:

  ```
  File "/app/.venv/lib/python3.13/site-packages/fasthtml/core.py", line 236, in _find_p
      raise ValueError(f"Missing required field: {arg}")
  ValueError: Missing required field: send
  ```
  cascading into `RuntimeError: Unexpected ASGI message 'websocket.send', after
  sending 'websocket.close'` (the client never sees a useful error because the
  next write tries to send on the already-closed socket).

**Root cause.** `app/chat/websocket.py:478` declares:

```python
async def _ws_handler(msg: str, send: Any, scope: dict[str, Any] | None = None) -> None:
```

FastHTML 0.13.3's `_find_p` (`fasthtml/core.py:195`) only honours the special-name
`send` → `partial(_send_ws, conn)` resolution inside the `if anno is empty:` branch
(line 211). With `send: Any`, that branch is skipped, FastHTML falls through to
generic data/path/cookies/headers/query lookup, finds nothing, and raises before
the handler body executes. Direct simulation inside the running container confirmed
this: handler with annotations → `FAIL ValueError`; same handler with annotations
stripped → `OK ['msg', 'send', 'scope']`.

**Consequence.** Every `send_message` over `/ws/chat` has been failing pre-
orchestrator since this signature shape was deployed. `_TURN_DEADLINE_SECONDS`,
the #684 user-message-persist work, and the proposed Wave 3 Step 2 watchdogs all
sit downstream of code that never runs. The 214 s "hang" was the client UI sitting
on a WS the server had already crashed.

### Wave 3 fixes — in priority order

1. **Drop the `send`/`scope` annotations** at `app/chat/websocket.py:478`. The
   minimal fix:

   ```python
   async def _ws_handler(msg, send, scope=None) -> None:
   ```

   (Keep `msg: str` if desired; the FastHTML special-name dispatch only inspects
   `send` and `scope`.)

   Same audit needed for any other WS handler — explorer WS, draft-status WS —
   per the memory note that all three follow the same shape. Verify each
   handler's signature; explorer WS works in practice (we tested it), so it's
   probably differently annotated, but confirm with a one-line grep:

   ```bash
   grep -nE "async def _ws_handler|app\.ws\(" app/chat/websocket.py \
     app/explorer/websocket.py app/docs/routes/_status_tracker.py
   ```

2. **Regression test.** Existing chat WS tests in `tests/test_chat_websocket*.py`
   pass because they call `ws_chat` directly, bypassing FastHTML's param resolver.
   Add an integration-shaped test that exercises the resolver path — instantiate
   the FastHTML app, register the WS route, simulate a connect+send, and assert
   the handler body actually runs (e.g. by checking that a row is written to
   `messages` or that `_send_ws` is reachable). The exact mock shape will need a
   minimal FastHTML test fixture; pattern after how other WS tests in the repo
   wire that up (likely via `starlette.testclient.TestClient` against the app
   instance).

3. **THEN, and only then, the layered progress/content watchdogs.** They're
   still the right shape for a second-line defence against *real* orchestrator
   hangs after this signature fix lands. But they cannot be implemented
   meaningfully until the orchestrator entrypoint runs at all in prod. Treat
   them as a follow-up PR after the signature fix is verified live.

### Historical "investigation steps" — kept for reference

The diagnostic steps below were correct for the question "is the orchestrator
hanging?" but the answer turned out to be "the orchestrator never starts." Future
investigations should still follow this pattern when the symptom is genuinely a
mid-stream hang.

**Diagnostic steps (do these in this order):**

1. **Confirm deployed SHA matches `main`:**

   ```bash
   ssh root@89.116.22.4 \
     "docker inspect ck92lybr2cqykzlg9vpiyy76 --format='{{.Config.Labels}}' \
      | tr ',' '\n' | grep -Ei 'sha|commit'"
   ```

   (Use `grep -Ei 'sha|commit'` — not `grep -i sha\|commit`, which the shell
   escapes to a literal `sha|commit` search, not an alternation.)

   If the SHA is behind `main` and missing the #652 commit, this is purely a deploy
   problem — redeploy and retest.

2. **Inspect what was actually persisted for the conversation.** Note: the chat
   plaintext columns (`content`, `tool_input`, `tool_output`, `rag_context`) were
   **dropped by migration 026**. The current source of truth is `content_encrypted`
   (`BYTEA`); see `app/chat/models.py:118-128`. A SQL query that references `content`
   directly will error. Don't try to read the message body in SQL — read row existence,
   role, timestamps, and the encrypted-blob size. If you actually need the plaintext
   for triage, use the app-side `_decode_encrypted_text` helper from
   `app/chat/models.py` from a Python shell with the master key available.

   Also note `llm_usage` does **not** carry `conversation_id`, `latency_ms`,
   `prompt_tokens`, `completion_tokens`, or `error_message` — only `user_id`,
   `org_id`, `provider`, `model`, `feature`, `tokens_input`, `tokens_output`,
   `cost_usd`, `created_at`. So you can't directly join llm_usage to a conversation.

   ```sql
   -- Did the orchestrator persist an assistant turn for that conversation at all?
   SELECT id, role, created_at,
          (content_encrypted IS NULL) AS empty_body,
          octet_length(content_encrypted) AS body_bytes
   FROM messages
   WHERE conversation_id = 'b034e693-9250-4c14-b8c9-1e3580508feb'
   ORDER BY created_at;

   -- Coarse cost/usage by the user around the test window (no per-conversation join).
   SELECT created_at, provider, model, feature, tokens_input, tokens_output, cost_usd
   FROM llm_usage
   WHERE user_id = '6c363beb-416f-4226-a337-6617f7322435'  -- henrik.aavik+koostaja
     AND created_at >= '2026-05-18 15:25:00+00'
     AND created_at <  '2026-05-18 15:35:00+00'
   ORDER BY created_at;
   ```

   Interpret:
   - **No assistant message row + no llm_usage row in the window** → orchestrator
     never called the LLM. Stuck in pre-LLM phase (RAG retrieve, tool dispatch).
   - **llm_usage rows present, no assistant message** → LLM was called, but the
     orchestrator either crashed or never persisted the result.
   - **Assistant message exists** → response generated server-side but never reached
     the client; look at WS frame logs.

   If you find this investigation needs per-conversation cost/latency routinely, file
   a follow-up migration to add `conversation_id`, `latency_ms`, and `error_message`
   to `llm_usage` (it's an obvious gap), but do it as its own PR — don't roll it into
   the chat-hang fix.

3. **Tail container logs for the conversation id:**

   ```bash
   ssh root@89.116.22.4 "docker logs --since=30m ck92lybr2cqykzlg9vpiyy76 2>&1 \
     | grep -F b034e693-9250-4c14-b8c9-1e3580508feb"
   ```

   Look for: turn-deadline log (`Chat turn deadline (120.0s) exceeded…`),
   RAG-retrieve-timeout log, tool-loop iteration count, any orchestrator exception.

**Then, and only then, code changes:**

- **Use two layered watchdogs, not a blanket turn timeout.** Wrapping
  `_drive_orchestrator()` with `asyncio.wait_for(..., 60)` would kill legitimate
  long streaming answers — exactly the kind of complex tool-using response we
  want to preserve. Instead, layer two narrow guards:

  1. **First-progress watchdog (~10 s).** Fires if the orchestrator hasn't
     produced *any* non-heartbeat event yet. The simplest correct definition is
     "any event sent through `send_event`" — heartbeats go through the raw
     `send` path (`app/chat/websocket.py:48-91`) and never touch
     `send_event`, so this rule naturally excludes them without needing an
     enumerated allowlist. Catches "stuck before RAG even fires".

  2. **First-content watchdog (~30–45 s).** Fires if no answer-text event has
     been streamed yet. The app's WS protocol emits content via
     `{"type": "content_delta"}` (see `app/chat/orchestrator.py:1256` and
     `:1378`) — **not** the Anthropic SDK's internal `content_block_delta` /
     `text_delta` names. The watchdog must match on the app's event names.
     Catches "RAG starts, tool loop iterates forever, model never produces
     a token".

  For reference, the full set of non-heartbeat event types the orchestrator
  currently emits (grep over `app/chat/orchestrator.py`):
  `retrieval_started`, `retrieval_done`, `warning`, `content_delta`,
  `tool_use`, `tool_result`, `stopped`, `error`. Any of these satisfies the
  first-progress watchdog; only `content_delta` satisfies the first-content
  watchdog.

  Wire both inside the existing `send_event` wrapper at
  `app/chat/websocket.py:260`, **not** around the raw `send()`. The heartbeat
  task at `app/chat/websocket.py:48-91` calls the underlying send directly to
  emit pings; hooking the watchdog at the raw layer would let a heartbeat
  silently satisfy the watchdog while the orchestrator is fully stuck.

  Pseudocode (note: the existing `send_event` is
  `await send(json.dumps(event, default=str))` — keep that shape, don't
  invent a `send_json` API):

  ```python
  # app/chat/websocket.py — augment the existing send_event wrapper.
  import contextlib

  first_progress = asyncio.Event()  # any event flowing through send_event
  first_content  = asyncio.Event()  # only the app's content_delta event

  _CONTENT_EVENT_TYPES = {"content_delta"}

  async def send_event(event: dict[str, Any]) -> None:
      await send(json.dumps(event, default=str))   # existing behaviour
      first_progress.set()                          # signal progress
      if event.get("type") in _CONTENT_EVENT_TYPES:
          first_content.set()                       # signal real tokens

  async def _watch(evt, seconds, message):
      try:
          await asyncio.wait_for(evt.wait(), seconds)
      except asyncio.TimeoutError:
          await send_event({"type": "error", "message": message})
          orchestrator_task.cancel()

  progress_watch = asyncio.create_task(_watch(
      first_progress, 10.0,
      "Server ei ole 10s jooksul vastust alustanud — vestlus on "
      "salvestatud, proovi uuesti.",
  ))
  content_watch = asyncio.create_task(_watch(
      first_content, 45.0,
      "Server tegeleb vastusega, kuid ei ole jõudnud teksti tootmiseni — "
      "vestlus on salvestatud, proovi uuesti.",
  ))

  try:
      await orchestrator_task
  finally:
      # Critical: cancel the watchdogs once the turn has resolved so they
      # don't fire late on a normal completion that ended without
      # content (e.g. tool-only "stopped" / "done" / "error" turns).
      progress_watch.cancel()
      content_watch.cancel()
      with contextlib.suppress(asyncio.CancelledError):
          await progress_watch
      with contextlib.suppress(asyncio.CancelledError):
          await content_watch
  ```

  That way:
  - genuine pre-RAG hangs surface a graceful error in ~10 s,
  - tool-loop-without-content hangs surface in ~45 s,
  - legitimate slow streams that produce regular content deltas run until
    `_TURN_DEADLINE_SECONDS` (the existing 120 s ceiling),
  - heartbeat pings can't falsely satisfy either watchdog.

  If we ever decide to cap entire turns, do it as a *separate* outer watchdog at
  `_TURN_DEADLINE_SECONDS + prephase_budget + margin` so the layered behaviour is
  explicit.

- If RAG is the bottleneck, lower `_RAG_RETRIEVE_TIMEOUT_SECONDS` from 15s to 5s and
  fall back to non-RAG mode (with a citation: "RAG retrieve aegus, vastus tugineb
  ainult ontoloogiale").

**Acceptance:**

- 30 sample chat requests with mixed complexity: p95 first-content < 30 s;
  p99 < 60 s.
- A simulated stall *before any progress event* surfaces a "no progress in 10 s"
  client error within ≤ 15 s.
- A simulated stall *after `retrieval_started` but before any content delta*
  (the actual failure mode observed in #802) surfaces a "no content in 45 s"
  client error within ≤ 50 s.
- A simulated 300 s mid-stream Claude API call that **emits regular content
  deltas** is NOT cancelled by either watchdog — only capped by the existing
  `_TURN_DEADLINE_SECONDS`.
- Heartbeat-only traffic (mock orchestrator that never calls `send_event`) does
  not satisfy either watchdog.
- A turn that ends normally with **no** `content_delta` (e.g. an `error` turn or
  a tool-only `stopped` turn after the first-progress watchdog has already been
  satisfied) does **not** surface a late "no content in 45 s" error — i.e. the
  watchdogs are cancelled in a `finally` once the orchestrator task resolves.
  Add an explicit regression test for this: a mock orchestrator that emits
  `retrieval_started` then `error` 200 ms later → assert the client sees the
  `error` event and **not** a watchdog-triggered timeout 45 s later.
- No request takes longer than `_TURN_DEADLINE_SECONDS + 5 s` to surface either a
  full response or a graceful timeout error.

---

## Cross-cutting follow-ups (out of scope here, file as separate issues)

- The 50+ pre-1997 EU directives flagged as "Ülevõtt puudub" — this is data hygiene,
  not the same fix as the year-1001 leak. File a separate ticket once the year-floor
  is in.
- A status-page (`/admin/diag` or similar) that runs the three probe queries from
  Step 1 above and shows red/green for resolver-critical predicates. Saves the next
  bug-hunt an hour. Out of scope here but easy.
- Add a CI test that loads the production ontology shape (or a frozen snapshot) and
  asserts the resolver finds URIs for a curated list of high-traffic refs. This is
  the regression guard against the next "predicate name drift" — the
  `estleg:shortName` triples-don't-exist surprise should not happen twice.
- **Migration to enrich `llm_usage`** with `conversation_id` (nullable FK),
  `latency_ms`, `request_id`, and `error_message`. Without these we can't do
  per-conversation cost analysis or quickly triage hung chats. Surfaced by #802
  investigation; ship as its own small PR after #802 lands.
<!-- abbreviation-map follow-up removed: the resolver PR already ships an
     ontology-derived map (see Wave 2 Step 2, `_resolve_law` step 2). -->


---

## Estimated total

| Wave | Items                                                  | Effort     |
| ---- | ------------------------------------------------------ | ---------- |
| 1    | #804, #806, #807 retest, #800, #808                    | ~1 day     |
| 2    | #801 + #803 resolver, downstream audit, reverse-fill, #805 | 3–5 days   |
| 3    | #802 diagnose + layered progress/content watchdogs     | 1–2 days   |

Wave 1 can ship as one PR per item or one combined PR (the changes are independent and
small). Wave 2 splits naturally into three PRs: (1) resolver rewrite + tests, (2)
downstream sourceAct/partOf audit fixes, (3) scoped reverse-fill script. The data
work for #805 is tracked separately. Wave 3 is gated on the diagnostic outputs and
may not need code changes at all — but if it does, the layered progress/content
watchdogs (hooked at `send_event`, not the raw send) are the right shape, not a
blanket turn timeout.
