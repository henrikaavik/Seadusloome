"""Tests for the JSON-LD to RDF converter."""

import json
from pathlib import Path

import pytest

from app.sync.converter import (
    UnresolvedLfsPointerError,
    _assert_not_lfs_pointer,
    convert_ontology,
    load_index,
    parse_jsonld_file,
    serialize_to_turtle,
)

FIXTURES = Path(__file__).parent / "fixtures"


def test_parse_jsonld_file():
    g = parse_jsonld_file(FIXTURES / "sample_peep.json")
    assert len(g) > 0


def test_parse_jsonld_real_fixture_passes_lfs_check():
    # The pointer check is a no-op for real content; parsing still works.
    g = parse_jsonld_file(FIXTURES / "sample_peep.json")
    assert len(g) > 0


def test_parse_jsonld_lfs_pointer_raises(tmp_path: Path):
    pointer = tmp_path / "combined_ontology.jsonld"
    pointer.write_bytes(
        b"version https://git-lfs.github.com/spec/v1\noid sha256:abc123\nsize 187819360\n"
    )
    with pytest.raises(UnresolvedLfsPointerError, match="Git LFS pointer") as exc_info:
        parse_jsonld_file(pointer)
    assert "combined_ontology.jsonld" in str(exc_info.value)


def test_convert_ontology_aborts_on_lfs_pointer_domain_file(tmp_path: Path):
    # Regression (PR #840): a domain subdirectory file that is an unresolved LFS
    # pointer must abort the whole sync, not be swallowed by the guarded loop's
    # broad `except Exception`. Otherwise a partial graph (combined only, no
    # curia/eurlex) could still be published.
    krr = tmp_path / "krr_outputs"
    krr.mkdir()

    # Valid combined file (reuse the real JSON-LD fixture) so the unguarded
    # combined parse succeeds and contributes triples *before* the domain loop.
    (krr / "combined_ontology.jsonld").write_bytes((FIXTURES / "sample_peep.json").read_bytes())

    # curia domain file is an unresolved Git LFS pointer.
    curia = krr / "curia"
    curia.mkdir()
    (curia / "curia_combined.jsonld").write_bytes(
        b"version https://git-lfs.github.com/spec/v1\noid sha256:abc123\nsize 27756872\n"
    )

    with pytest.raises(UnresolvedLfsPointerError, match="Git LFS pointer") as exc_info:
        convert_ontology(tmp_path)
    # Aborted on the domain pointer (after the valid combined file parsed) —
    # confirms no partial publish path was taken.
    assert "curia_combined.jsonld" in str(exc_info.value)


def test_assert_not_lfs_pointer_allows_normal_json(tmp_path: Path):
    normal = tmp_path / "real.jsonld"
    normal.write_text('{"@context": {}, "@graph": []}', encoding="utf-8")
    # Should not raise for a normal JSON document starting with '{'.
    _assert_not_lfs_pointer(normal)


def test_parse_jsonld_contains_expected_subjects():
    g = parse_jsonld_file(FIXTURES / "sample_peep.json")
    subjects = {str(s) for s in g.subjects()}
    assert "https://data.riik.ee/ontology/estleg#TEST_Par_1" in subjects
    assert "https://data.riik.ee/ontology/estleg#TEST_Par_2" in subjects


def test_parse_jsonld_preserves_estonian_chars():
    g = parse_jsonld_file(FIXTURES / "sample_peep.json")
    turtle = serialize_to_turtle(g)
    assert "Üldsätted" in turtle


def test_serialize_to_turtle():
    g = parse_jsonld_file(FIXTURES / "sample_peep.json")
    turtle = serialize_to_turtle(g)
    assert "@prefix" in turtle
    assert "estleg:" in turtle


def test_load_index(tmp_path: Path):
    krr = tmp_path / "krr_outputs"
    krr.mkdir()
    index = {
        "generated": "2026-01-01",
        "total_files": 2,
        "laws": [
            {"name": "test_law", "files": ["test_law_peep.json"]},
        ],
    }
    (krr / "INDEX.json").write_text(json.dumps(index))

    laws = load_index(tmp_path)
    assert len(laws) == 1
    assert laws[0]["name"] == "test_law"


def test_load_index_missing(tmp_path: Path):
    import pytest

    with pytest.raises(FileNotFoundError):
        load_index(tmp_path)
