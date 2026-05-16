# C0 implementation summary

Branch: `c0-fix-sparql-predicate-names` (pushed to origin; no PR opened
per instructions).

## Files changed

New:

- `app/ontology/relations.py` — canonical relation vocabulary (PREDICATES URI
  constants, INVERSES, LEGAL_PHRASES Estonian labels, RELATION_GROUPS, plus
  `legal_phrase` / `inverse_of` / `group_of` / `predicate_for_label` /
  `is_{amendment,interpretation,transposition}_relation` helpers). Legacy
  local-name aliases keep cached UI payloads resolving during transition.
- `tests/fixtures/ontology_canonical.ttl` — small Turtle graph exercising
  every canonical predicate once.
- `tests/test_ontology_relations.py` — 43 unit cases (PREDICATES, INVERSES,
  LEGAL_PHRASES, RELATION_GROUPS, helpers, fixture-graph SPARQL).
- `tests/test_impact_queries_canonical.py` — 20 SPARQL integration tests
  running impact queries against the seeded fixture, plus "no legacy
  predicate strings" regression guards.
- `tests/smoke/test_canonical_predicates_corpus.py` — 12 `pytest.mark.smoke`
  tests that skip cleanly when Jena is unreachable; otherwise assert each
  canonical predicate has >=1 row in the corpus.

Modified:

- `app/docs/impact/queries.py` — replaced dead UNION branches
  (`interpretsProvision`, `amendsProvision`, `hasTopic`, `implementsEU`)
  with canonical predicates; every query now projects `?relation`.
- `app/docs/impact/analyzer.py` — surface the `relation` field on every
  returned row so C5 can render the relation type.
- `app/chat/tools.py` — `get_provision_details` now searches `references`
  UNION `semanticallySimilarTo` UNION `harmonisedWith` instead of the
  non-existent `relatedTo`.
- `app/drafter/handlers.py` — audit comment confirming the four research
  queries are canonical (no rename needed).
- `app/explorer/routes.py` — `relation_legal_phrase` delegates to
  `app.ontology.relations.legal_phrase`; the 50-entry inline dict was
  migrated verbatim into the new module.
- `tests/test_docs_impact_analyzer.py` — updated shape assertion to include
  the new `relation` field.
- `pyproject.toml` — added `smoke` pytest marker.

## Test results

Full suite (excluding smoke): **2517 passed, 25 skipped, 0 failed.** Smoke
tests skip cleanly when Jena is offline (12 skipped). `ruff format`, `ruff
check`, and `pyright` all green on touched files.

## Deviations from the plan

- Drafter handlers (lines 53-112) audited and found to be already canonical;
  added an audit comment instead of renaming since no predicate matched the
  rename list.

## TODOs / follow-ups

- C5 will consume the new `relation` projection on impact-query rows to
  render "Seose liik" columns in the impact report.
- A future cleanup can shrink `_LEGACY_ALIASES` in `relations.py` once C5
  ships and no cached payloads carry old predicate names.
