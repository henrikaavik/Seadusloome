# B3 — Capability dictionary (implementation summary)

Branch: `b3-capability-dictionary`. Created `app/ui/capabilities.py` as the single
source of truth for every "what can I do" entry in Seadusloome and refactored
the two main consumers to read from it.

## Files changed

* **`app/ui/capabilities.py`** (new, 303 lines) — `@dataclass(frozen=True) Capability`
  with the 10 required fields, `CAPABILITIES: list[Capability]` with all 13
  entries from the spec (5 live, 8 planned), and the five helpers
  `get_capability`, `live_capabilities`, `planned_capabilities`,
  `capabilities_for_use_case`, `mobile_capabilities`. Lucide-style icon slugs;
  Estonian user-facing fields; ASCII-clean slugs.
* **`app/chat/routes.py`** — `/chat` InfoBox refactored. The hardcoded prose
  enumeration in the middle paragraph is replaced with a generated `<ul>`
  pulled from `live_capabilities()` filtered to use cases 1-5 (excluding the
  chat itself). The framing prose is preserved. A new `<P>` introduces the
  list ("Lisaks Nõustajale saate Seadusloomes:").
* **`app/analyysikeskus/routes.py`** — `analyysikeskus_page` now iterates over
  `CAPABILITIES` filtered to `target_url.startswith("/analyysikeskus")`. Live
  capabilities render with the full `_workflow_card` input form (placeholder
  / aria / examples metadata kept in a small per-slug overlay dict). Planned
  capabilities render via a new `_planned_workflow_card` helper that adds a
  `Badge("Tulekul", variant="warning")` to the header and shows the
  description without an input form. Order matches the dict.
* **`tests/test_capabilities.py`** (new, 171 lines) — 14 invariant + helper
  tests covering slug uniqueness, every section-2 use case has at least one
  capability, status whitelist, ASCII-clean slugs, required-field population,
  frozen-dataclass contract, and a smoke test per helper.

## Files NOT touched
Per the spec, `app/explorer/start_panel.py` was audited and left alone — it
is a pure data layer (bookmarks/high-risk/recent-drafts queries), no
capability lists to refactor. The "Sirvi liikide kaupa" shortcut lives in
`app/explorer/pages.py` and is a single operational button, not a B3-style
capability list.

## Test coverage
14 new tests pass. The existing `test_analyysikeskus_directory_renders` and
all 55 chat tests still pass — the canonical names "Normi mõjuahel" and "EL
ülevõtt" remain substrings of the refactored capability names. Full suite:
2491 passed, 25 skipped.

`ruff check`, `ruff format`, and `pyright` are all green on the four touched
files.

## Deviations
None. The capability "Küsi Nõustajalt" slug was normalised to `noustaja` so
slugs stay diacritic-clean ASCII (the spec's `nőustaja` used a Hungarian
character that isn't Estonian and isn't ASCII).
