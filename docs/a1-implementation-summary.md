# A1 — Sanktsioonide indeks v1 standalone — implementation summary

## What landed

* New SPARQL helper module `app/analyysikeskus/sanctions.py` with three
  public functions (`list_sanctions_for_provision`,
  `list_sanctions_for_act`, `find_similar_sanctions`), a typed
  `SanctionRow` dataclass, and Estonian display-label helpers
  (`sanction_type_label`, `sanction_unit_label`). Every SPARQL call is
  guarded — a dead Jena yields `[]`, never a 500.
* New route `/analyysikeskus/sanktsioonid` appended at the end of
  `app/analyysikeskus/routes.py` (the directory page in the same file
  is untouched — that's B3's territory). The route reuses the
  established 5-card result shell (`Sisend → Ulatus → Tulemused →
  Tõendid → Soovitatud tegevused`) so the visual rhythm matches
  Normi mõjuahel and EL ülevõtt.
* The result page renders a single-line summary
  (`X sanktsiooni — N rahatrahv, M vangistus…`), a sanctions table
  with provision/act/type/penalty-range/enforcement/default-rule
  columns, Tõendid rows with the Õiguskaart deep link plus the
  per-row "Küsi nõustajalt" chat-seed form (the #724 pattern), and a
  static "Soovitatud tegevused" set (no LLM advice yet, per the
  design doc) including a "Võrdle sarnaste aktide sanktsioonidega"
  toggle that re-runs the workflow with `find_similar_sanctions`.
* Disambiguation / unresolved-input branches mirror Normi mõjuahel's
  flow: a friendly warning + RAG candidates when nothing matches,
  clickable candidate links when several plausible URIs resolve.
* Unit test `tests/test_analyysikeskus_sanctions.py` — 31 tests cover
  the label helpers, the three SPARQL helpers (happy path, empty URI
  short-circuit, dead-Jena fallback, range-overlap binding, seed-act
  exclusion, limit honouring), the input-parser pinning for §-refs /
  CELEX / plain prose, and route smoke tests (auth gate, landing
  form, resolved-provision render, comparison toggle, act-level
  branch, unresolved warning, disambiguation, empty-result render,
  and "existing routes still register" regression).

## What's deferred

C6 will fold A1 into the impact report's "sanctions delta" section
once C0 lands. The Õiguskaart "Sanction" focus icon and a richer
provision-vs-act picker for the comparison seed are out of scope for
v1. Branch pushed as `a1-sanctions-v1-standalone`; no PR opened per
task instructions.
