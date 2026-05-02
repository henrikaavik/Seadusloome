# Code Review

Date: 2026-04-16

Scope: PR #651 (`feat/643-vtk-detail-additions`) covering the new `Tüüp` column on `/drafts`, the VTK-side child-eelnõu card on draft detail pages, `list_eelnous_for_vtk()`, and the related route/model tests.

## Findings

No blocking correctness, security, or regression findings identified in the PR diff.

## Residual Risks / Notes

- The VTK children card resolves uploader names with one `get_user()` call per child row. That is acceptable for the current feature, but it is an N+1 render path worth flattening later if a single VTK can accumulate many follow-on eelnõud.
- The defence-in-depth org filter for child eelnõud currently lives in `draft_detail_page()` rather than inside `list_eelnous_for_vtk()`. The current caller is safe; if the helper is reused elsewhere, that caller needs to preserve the same org check.

## Verification

- `uv run pytest tests/test_docs_routes.py tests/test_docs_draft_model.py -q` -> 117 passed
- `uv run pytest tests/test_docs_*.py -q` -> 332 passed
- `ruff check app/docs/routes.py app/docs/draft_model.py tests/test_docs_routes.py tests/test_docs_draft_model.py` -> all checks passed
- `uv run pyright app/docs/routes.py app/docs/draft_model.py tests/test_docs_routes.py tests/test_docs_draft_model.py` -> 0 errors, 0 warnings
