# Prod verification checklist — 17 usability-test issues

Date: 2026-05-19
Prod SHA: `4183cde` (verified deployed via Coolify container `n123uq9cx84ml1gcujbkcr3u:4183cde4fa4d8ea38a135873cf48e9c3dabbc506`)
Site: <https://seadusloome.sixtyfour.ee>

## How to use

1. Log in as **drafter** QA account first → walk Section A (12 issues, ~50 min).
2. Log out, log in as **reviewer** QA account → walk Section B (1 issue, ~5 min).
3. Section C (4 issues) requires no account — public-shape browser checks or simple smoke.
4. For each issue, paste the pre-written `gh issue close` command at the end of the row. Replace `<your-evidence>` with the screenshot path / one-line note before running.
5. If anything FAILS, comment with the actual behavior instead of closing — paste the unrendered command for reference.

Order is optimized to **minimize account switching** and reuse uploaded test data.

---

## Section A — Drafter QA account

Log in as `koostaja@seadusloome.ee` (or whichever drafter account is seeded).

### Pre-flight setup (one-time, ~5 min)

Need one realistic test draft uploaded and analyzed to drive several checks below. Upload a small `.docx` with **explicit GDPR and AvTS § 35 references** in the body text. Suggested copy-paste body:

```
Eelnõu pealkiri: Andmekaitse näidiseelnõu

Käesolev eelnõu täiendab AvTS § 35 nõudeid isikuandmete töötlemise osas
ja kohaldub kooskõlas EL isikuandmete kaitse üldmäärusega (CELEX 32016R0679).
Asutus järgib KarS § 121 sätestatud andmetöötluse piiranguid.
```

Save as `qa-eelnou-20260519.docx`. Upload via `/drafts/upload` as **doc_type=Eelnõu**, link to no VTK. Wait for status `Valmis` (~30-90s).

Note the draft ID once Valmis (visible in URL `/drafts/<UUID>`).

---

### #801 — Entity URI resolver returns non-null

- **URL**: `/drafts/<UUID-from-pre-flight>` then click "Vaata mõjuaruannet"
- **Expected**: affected count > 0 (NOT `0/0/0/0`). At least 2 entities listed under "Mõjutatud üksused" (AvTS § 35 and/or KarS § 121).
- **PASS**: ✅ ❌
- **gh command**:
  ```bash
  gh issue close 801 -c "Verified on prod 4183cde with QA drafter account on 2026-05-19. Uploaded test draft with AvTS § 35 + KarS § 121 + GDPR refs. Impact report shows >0 affected entities. Fix from commits 2d5d2ec, 9ce24b6, 3ae05d6 confirmed working."
  ```

### #803 — Normi mõjuahel resolves "AvTS § 35"

- **URL**: `/analyysikeskus/normi-mojuahel?sisend=AvTS%20%C2%A7%2035`
- **Expected**: Result shell renders with affected provisions table — NOT "Ei tuvastatud" warning.
- **PASS**: ✅ ❌
- **gh command**:
  ```bash
  gh issue close 803 -c "Verified on prod 4183cde with QA drafter on 2026-05-19. /analyysikeskus/normi-mojuahel?sisend=AvTS § 35 renders impact table, not 'ei tuvastatud'. Fix from 2d5d2ec + fd1156f confirmed."
  ```

### #805 — EL ülevõtt: CELEX 32016R0679 shows canonical-not-mapped warning

- **URL**: `/analyysikeskus/el-ulevott?sisend=32016R0679`
- **Expected**: warning reads "EL õigusakt CELEX-numbriga 32016R0679 ei ole veel ontoloogias kaardistatud — kontrollige käsitsi…" — NOT the generic "Ei tuvastanud EL õigusakti."
- **Bonus check (P3)**: `/analyysikeskus/el-ulevott?sisend=32016r0679` (lowercase) — same canonical warning.
- **Bonus check (shape)**: `/analyysikeskus/el-ulevott?sisend=12abc34` — generic "Ei tuvastanud" (NOT the canonical warning).
- **PASS**: ✅ ❌
- **gh command**:
  ```bash
  gh issue close 805 -c "Verified on prod 4183cde with QA drafter on 2026-05-19. Canonical CELEX 32016R0679 (and lowercase variant) shows specific 'ei ole veel ontoloogias kaardistatud' warning. Garbage 12abc34 falls through to generic copy. Fix from PR #821 confirmed."
  ```

### #815 — Uploaded draft with GDPR shows unresolved-EU section

- **URL**: `/drafts/<UUID-from-pre-flight>/report` (the test draft uploaded in pre-flight)
- **Expected**: section titled **"EL-i kaardistamata viited"** present, body reads "Tuvastasime dokumendis viiteid EL õigusele (N EL viidet), mida ei õnnestunud ontoloogias kaardistada:" followed by `<code>32016R0679</code>` or similar.
- **PASS**: ✅ ❌
- **gh command**:
  ```bash
  gh issue close 815 -c "Verified on prod 4183cde with QA drafter on 2026-05-19. Uploaded draft with explicit GDPR/CELEX text shows the 'EL-i kaardistamata viited' section with the CELEX listed. Fix from PR #821 confirmed."
  ```

### #809 — Chat from a Valmis draft inherits real impact metrics

- **URL**: `/drafts/<UUID-from-pre-flight>` → click "Küsi nõustajalt" (or `/chat?draft=<UUID>`)
- **Action**: Send the first message "Mis on selle eelnõu peamised mõjud?"
- **Expected**: response references real impact numbers (e.g., "X mõjutatud sätet, Y konflikti"), NOT a fallback like "mõjuaruanne pole saadaval."
- **PASS**: ✅ ❌
- **gh command**:
  ```bash
  gh issue close 809 -c "Verified on prod 4183cde with QA drafter on 2026-05-19. Chat seeded from draft includes real impact metrics in the system context. Fix from 9ce32b0 confirmed."
  ```

### #810 — Version history shows "Esitatud" for Eelnõu v1

- **URL**: `/drafts/<UUID-from-pre-flight>` then check the **Versioonide ajalugu** sidebar
- **Expected**: v1 row shows stage **"Esitatud"** (NOT "VTK"). The pre-flight draft was uploaded as doc_type=Eelnõu.
- **PASS**: ✅ ❌
- **gh command**:
  ```bash
  gh issue close 810 -c "Verified on prod 4183cde with QA drafter on 2026-05-19. Versioonide ajalugu shows 'Esitatud' for Eelnõu v1 (not 'VTK'). Fix from fb2528c confirmed."
  ```

### #811 — Safari report DOCX/PDF download

- **URL**: `/drafts/<UUID-from-pre-flight>/report`
- **Browser**: **Safari** (open a Safari window if you're in Chrome)
- **Action**: Click "Lae alla DOCX" and "Lae alla PDF"
- **Expected**: file downloads directly (no spinner, no HTMX roundtrip).
- **PASS**: ✅ ❌
- **gh command**:
  ```bash
  gh issue close 811 -c "Verified on prod 4183cde with Safari + QA drafter on 2026-05-19. Both DOCX and PDF report exports download immediately via plain GET. Fix from 36565df confirmed."
  ```

### #812 — Safari drafter workflow submit

- **URL**: `/drafter/new`
- **Browser**: **Safari**
- **Action**: Select **"Täielik seadus"** workflow → click **"Alusta"**.
- **Expected**: form submits and the page advances to step 2 (intent capture). No silent failure.
- **PASS**: ✅ ❌
- **gh command**:
  ```bash
  gh issue close 812 -c "Verified on prod 4183cde with Safari + QA drafter on 2026-05-19. Drafter workflow form submits; user lands on step 2 intent capture. Fix from 754b9af confirmed."
  ```

### #816 — Koostaja Jätka button after 3 answers

- **URL**: `/drafter/new` (Chrome or Safari)
- **Action**: Pick any workflow → enter a short intent → submit → answer **3** clarification questions (skip the rest). Scroll down.
- **Expected**: "Jätka uurimisega" button visible + enabled. Hint text says you can continue after 3.
- **PASS**: ✅ ❌
- **gh command**:
  ```bash
  gh issue close 816 -c "Verified on prod 4183cde with QA drafter on 2026-05-19. After 3 answered clarifications the Jätka button is rendered. State-machine guard also unified. Fix from PR #818 confirmed."
  ```

### #814 — Plain-language intent entry point

- **URL**: `/dashboard` (Töölaud)
- **Expected**: "Analüüsi poliitikamõttest" capability card visible with example text. NO "Tulekul" badge.
- **Action**: Click the card. Should land on `/analyysikeskus/moju-poliitikamottest?sisend=Soovin%20lihtsustada%20puudega...`. Confirm textarea is **pre-filled** with the example.
- **Action**: Submit form. Confirmation panel renders with candidate refs + scope chips (chips labeled "kuvatakse tulemustes — ei mõjuta kandidaatide otsingut").
- **PASS**: ✅ ❌
- **gh command**:
  ```bash
  gh issue close 814 -c "Verified on prod 4183cde with QA drafter on 2026-05-19. Capability card live, ?sisend= prefills textarea, extract panel renders with chip scope-metadata disclaimer. Fix from PR #819 + #822 (incl. review-round-2 manual-refs-win merge) confirmed."
  ```

### #808 — VTK dropdown empty-state UX

- **URL**: `/drafts/upload`
- **Expected**: if your QA org has 0 VTKs, the "Seotud VTK" dropdown is **disabled** with text "Selles töötsoonis pole veel VTK-sid" — NOT a lone `— vali —` option. If org has VTKs, dropdown lists them.
- **Bonus check**: toggle doc_type Eelnõu ↔ VTK 3× — dropdown state persists correctly.
- **PASS**: ✅ ❌
- **gh command**:
  ```bash
  gh issue close 808 -c "Verified on prod 4183cde with QA drafter on 2026-05-19. VTK dropdown shows the documented empty-state copy when org has 0 VTKs (or lists VTKs when present). disabled='disabled' string-form survives the FastHTML HTTP renderer. Fix from 4b65dce + da51a5d + 222dab4 confirmed."
  ```

### #813 — bool-True HTML attrs survive serialization

- **Action**: open Chrome DevTools on `/auth/login` (logout first to see the login form), inspect the email input
- **Expected**: input has `required="required"` (or `required` rendered) AND form submission rejects empty input. No silent regression.
- **Faster check**: just verify ANY form on prod (login, upload, drafter step 2) submits successfully — if #812 + #816 both pass above, this is already covered indirectly.
- **PASS**: ✅ ❌
- **gh command**:
  ```bash
  gh issue close 813 -c "Verified on prod 4183cde on 2026-05-19. Primitives layer + Pattern A/B sites converted to string-form; the existing form-submission paths (drafter #812, upload #808, login) all work. Fix from PR #813 (4 commits) confirmed."
  ```

---

## Section B — Reviewer QA account

Log in as `ulevaataja@seadusloome.ee` (or whichever reviewer account is seeded).

### #817 — Reviewer outcome actions on a draft

- **URL**: `/drafts/<UUID-from-pre-flight>` (the test draft from Section A, accessible because it's in the same org)
- **Expected**:
  - Three buttons visible in the action row: **"Puuduvad probleemid"** / **"Leitud probleem"** / **"Vajab arutelu"**
  - Optional comment textarea above the buttons (placeholder "Lisa märkus (valikuline)")
- **Action**: Pick "Vajab arutelu" + leave a short comment → click submit.
- **Expected after submit**: HTMX swap updates the review history list inline; the new entry shows reviewer name + outcome chip + comment + timestamp.
- **Action**: visit `/dashboard` as reviewer.
- **Expected**: "Ülevaatuse järgi ootavad" widget — the draft you just reviewed should NOT be in it (you've reviewed it).
- **PASS**: ✅ ❌
- **gh command**:
  ```bash
  gh issue close 817 -c "Verified on prod 4183cde with reviewer QA account on 2026-05-19. Three outcome buttons render, HTMX swap updates the review history with the new entry, reviewer Töölaud widget reflects the review. Migration 035 + UI + audit log all working. Fix from PR #820 confirmed."
  ```

---

## Section C — No login needed (or any logged-in user)

### #804 — Õiguskaart TDZ ReferenceError on load

- **URL**: `/explorer` (any logged-in user)
- **Action**: Open Chrome DevTools console BEFORE the page loads (Cmd+Opt+J). Reload.
- **Expected**: **no** `ReferenceError: Cannot access 'minimapViewportRect' before initialization` in console.
- **PASS**: ✅ ❌
- **gh command**:
  ```bash
  gh issue close 804 -c "Verified on prod 4183cde on 2026-05-19. /explorer cold load shows no TDZ ReferenceError in DevTools console. Fix from 8c2d606 confirmed."
  ```

### #806 — `/explorer?search=...` deep link

- **URL**: `/explorer?search=AvTS%20%C2%A7%2035`
- **Expected**: toolbar search input pre-populated with "AvTS § 35"; results panel populated OR explicit "Tulemusi ei leitud" empty state.
- **PASS**: ✅ ❌
- **gh command**:
  ```bash
  gh issue close 806 -c "Verified on prod 4183cde on 2026-05-19. /explorer?search=... pre-populates the toolbar input and fires the search bootstrap (no longer blocked by the TDZ error #804). Fix from 8c2d606 confirmed."
  ```

### #807 — `/explorer?vaade=koik` renders categories

- **URL**: `/explorer?vaade=koik`
- **Expected**: canvas populated with 5 category-level nodes (kehtiv seadus / eelnõu / kohtulahend / EL õigusakt / EL kohtulahend). Not an empty canvas.
- **PASS**: ✅ ❌
- **gh command**:
  ```bash
  gh issue close 807 -c "Verified on prod 4183cde on 2026-05-19. /explorer?vaade=koik renders 5 category-level nodes (no longer blocked by the TDZ error #804). Fix from 8c2d606 confirmed."
  ```

### #802 — Nõustaja chat doesn't hang

- **URL**: `/chat` (drafter or reviewer, any logged-in)
- **Action**: send "Tere" with no draft context
- **Expected**: response within ~30s (NOT an infinite hang)
- **PASS**: ✅ ❌
- **gh command**:
  ```bash
  gh issue close 802 -c "Verified on prod 4183cde on 2026-05-19. /chat responds within ~30s on a no-context message. WS param-resolver trap fix from b901455 confirmed."
  ```

---

## Section D — bulk-close if Sections A-C all pass

If every checkbox above is ✅, you can run all close commands in one batch:

```bash
# Stale-open (11)
gh issue close 801 -c "Verified on prod 4183cde on 2026-05-19 — see qa run notes." &&
gh issue close 802 -c "Verified on prod 4183cde on 2026-05-19 — see qa run notes." &&
gh issue close 803 -c "Verified on prod 4183cde on 2026-05-19 — see qa run notes." &&
gh issue close 804 -c "Verified on prod 4183cde on 2026-05-19 — see qa run notes." &&
gh issue close 806 -c "Verified on prod 4183cde on 2026-05-19 — see qa run notes." &&
gh issue close 807 -c "Verified on prod 4183cde on 2026-05-19 — see qa run notes." &&
gh issue close 808 -c "Verified on prod 4183cde on 2026-05-19 — see qa run notes." &&
gh issue close 809 -c "Verified on prod 4183cde on 2026-05-19 — see qa run notes." &&
gh issue close 810 -c "Verified on prod 4183cde on 2026-05-19 — see qa run notes." &&
gh issue close 811 -c "Verified on prod 4183cde with Safari on 2026-05-19 — see qa run notes." &&
gh issue close 812 -c "Verified on prod 4183cde with Safari on 2026-05-19 — see qa run notes." &&

# Phase 2 fixes (6)
gh issue close 805 -c "Verified on prod 4183cde on 2026-05-19 — see qa run notes." &&
gh issue close 813 -c "Verified on prod 4183cde on 2026-05-19 — see qa run notes." &&
gh issue close 814 -c "Verified on prod 4183cde on 2026-05-19 — see qa run notes." &&
gh issue close 815 -c "Verified on prod 4183cde on 2026-05-19 — see qa run notes." &&
gh issue close 816 -c "Verified on prod 4183cde on 2026-05-19 — see qa run notes." &&
gh issue close 817 -c "Verified on prod 4183cde with reviewer account on 2026-05-19 — see qa run notes."
```

But the per-row commands above are more honest (each cites the actual fix commit / PR), so use those unless you're rushing.

---

## After Section D — what's left

- I update `docs/2026-05-19-usability-fixes-plan.md` to mark Phase 1 + Phase 2 closed.
- I update memory `project_progress.md` with the shipped state.
- We decide whether to start the ontology data PR for GDPR + Working Conditions CELEXes (separate `estonian-legal-ontology` repo).
- Epic #784 (12 deferred ontology tasks) and older issues (#349-#362, #622, #680) — separate sessions.
