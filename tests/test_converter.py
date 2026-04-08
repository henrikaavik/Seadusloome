"""Tests for the JSON-LD to RDF converter."""

import json
from pathlib import Path

from app.sync.converter import (
    load_index,
    parse_jsonld_file,
    serialize_to_turtle,
)

FIXTURES = Path(__file__).parent / "fixtures"


def test_parse_jsonld_file():
    g = parse_jsonld_file(FIXTURES / "sample_peep.json")
    assert len(g) > 0


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
    index = {"generated": "2026-01-01", "total_files": 2, "laws": [
        {"name": "test_law", "files": ["test_law_peep.json"]},
    ]}
    (krr / "INDEX.json").write_text(json.dumps(index))

    laws = load_index(tmp_path)
    assert len(laws) == 1
    assert laws[0]["name"] == "test_law"


def test_load_index_missing(tmp_path: Path):
    import pytest
    with pytest.raises(FileNotFoundError):
        load_index(tmp_path)
