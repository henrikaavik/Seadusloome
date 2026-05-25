# `tests/fixtures/drafts/` — sample `.docx` draft files

Deterministic `.docx` fixtures generated via `python-docx` and committed to
the repo. Used by `tests/test_phase2_edge_cases.py` (and future Sprint 3
unit-tests of the parse / extract / analyze pipeline) so we exercise the
document-processing code path against realistic legislative-style input
without re-uploading files through the live `/drafts` route.

## Regenerating the fixtures

The files are pinned bytes-on-disk so tests can rely on their content,
but if you need to update them run:

```bash
uv run python tests/fixtures/drafts/__generate__.py
```

The script overwrites every file with the same canonical content. Commit
the regenerated `.docx` blobs in the same PR that updates the script.

## Catalogue

| File | Body | Covered cases |
| --- | --- | --- |
| `normal_legal_text.docx` | Title + 2 paragraphs. 3 `§`-refs (`§ 5`, `§ 12 lg 2`, `§ 47`) + 1 CELEX (`32016R0679`). | Happy path. Reference extractor recall on a typical short draft. |
| `very_short.docx` | Title + one short paragraph, no legal references. | Empty-extraction path. Extractor must return `[]` without crashing. |
| `many_references.docx` | Title + 4 paragraphs. 15+ `§`-refs (mix of bare and `lg`/`p` forms) + 3 CELEX (`32016R0679`, `32019L0790`, `32020D0001`) + 2 court cases (`3-2-1-100-23`, `3-1-1-50-22`). | Extractor recall + dedupe. Confirms the LLM-stub / mocked-provider path returns the expected counts after dedupe. |
| `empty_body.docx` | Title only, no body paragraphs. | Parser edge case — confirms python-docx accepts the file and downstream code handles a body-less document gracefully. |
| `malformed_refs.docx` | Title + 2 paragraphs containing typo-ridden references (`§ X.Y.Z`, broken CELEX `320XX0679`, partial case number `3-2-1-`) alongside one valid `§ 5`. | Graceful degradation — the pipeline must not raise and must still extract the valid reference. |

## Why are these committed?

`python-docx` output is deterministic for the same input, but `.docx` is a
ZIP archive of XML and re-generating the files at test-run time would (a)
slow every test session, and (b) couple the test fixture to whatever
`python-docx` version is installed in the dev env. Checking in the
generated blobs keeps the fixture stable across machines and CI runs.

The script lives next to the files so a future contributor can extend the
fixture set with one PR (script edit + regenerate + commit blobs +
update this README).
