"""Unit tests for ``app.analyysikeskus.input_parser`` (#722).

``parse_user_reference`` is the adapter between the Analüüsikeskus
"Normi mõjuahel" search box and :class:`ReferenceResolver`: it turns a
free-text line into ``list[ExtractedRef]``. These tests pin the
recognition rules — CELEX, §-references (with/without ``lg`` / ``p``),
court case numbers, and "plain prose → empty".
"""

from __future__ import annotations

from app.analyysikeskus.input_parser import parse_user_reference
from app.docs.entity_extractor import ExtractedRef


def _types(refs: list[ExtractedRef]) -> list[str]:
    return [r.ref_type for r in refs]


def _by_type(refs: list[ExtractedRef], ref_type: str) -> ExtractedRef:
    matches = [r for r in refs if r.ref_type == ref_type]
    assert matches, f"no {ref_type!r} ref in {refs!r}"
    return matches[0]


class TestCelex:
    def test_bare_celex_regulation(self):
        refs = parse_user_reference("32016R0679")
        assert _types(refs) == ["eu_act"]
        assert refs[0].ref_text == "32016R0679"
        assert refs[0].confidence == 1.0

    def test_bare_celex_directive(self):
        refs = parse_user_reference("32019L0790")
        assert _types(refs) == ["eu_act"]
        assert refs[0].ref_text == "32019L0790"

    def test_celex_embedded_in_phrase(self):
        refs = parse_user_reference("GDPR (32016R0679)")
        assert _types(refs) == ["eu_act"]
        assert refs[0].ref_text == "32016R0679"

    def test_celex_is_case_insensitive(self):
        refs = parse_user_reference("32016r0679")
        assert _types(refs) == ["eu_act"]


class TestSectionReference:
    def test_simple_section(self):
        refs = parse_user_reference("AvTS § 35")
        # provision first, then the law short name.
        assert _types(refs) == ["provision", "law"]
        prov = _by_type(refs, "provision")
        assert "AvTS" in prov.ref_text and "§ 35" in prov.ref_text
        law = _by_type(refs, "law")
        assert law.ref_text == "AvTS"

    def test_section_with_lg_and_p(self):
        refs = parse_user_reference("KarS § 133 lg 2 p 1")
        assert _types(refs) == ["provision", "law"]
        prov = _by_type(refs, "provision")
        assert prov.ref_text == "KarS § 133 lg 2 p 1"
        assert _by_type(refs, "law").ref_text == "KarS"

    def test_section_with_only_lg(self):
        refs = parse_user_reference("TsÜS § 12 lg 3")
        prov = _by_type(refs, "provision")
        assert prov.ref_text == "TsÜS § 12 lg 3"
        assert _by_type(refs, "law").ref_text == "TsÜS"

    def test_section_tight_spacing_is_normalised(self):
        refs = parse_user_reference("AvTS §35 lg2 p1")
        prov = _by_type(refs, "provision")
        # Re-spelled to the canonical "Law § N lg N p N" form.
        assert prov.ref_text == "AvTS § 35 lg 2 p 1"

    def test_section_keyword_case_insensitive(self):
        refs = parse_user_reference("KOKS § 6 LG 1 P 2")
        prov = _by_type(refs, "provision")
        assert prov.ref_text == "KOKS § 6 lg 1 p 2"
        assert _by_type(refs, "law").ref_text == "KOKS"


class TestCourtCaseNumber:
    def test_estonian_supreme_court_number(self):
        refs = parse_user_reference("3-1-1-63-15")
        assert _types(refs) == ["court_decision"]
        assert refs[0].ref_text == "3-1-1-63-15"

    def test_estonian_three_group_number(self):
        refs = parse_user_reference("5-19-1-2")
        assert _types(refs) == ["court_decision"]

    def test_cjeu_case_number(self):
        refs = parse_user_reference("C-131/12")
        assert _types(refs) == ["court_decision"]
        assert refs[0].ref_text == "C-131/12"

    def test_general_court_case_number(self):
        refs = parse_user_reference("T-99/04")
        assert _types(refs) == ["court_decision"]


class TestPlainProse:
    def test_plain_prose_returns_empty(self):
        assert parse_user_reference("mingi suvaline jutt") == []

    def test_descriptive_intent_returns_empty(self):
        assert parse_user_reference("Soovin muuta avaliku teabe kättesaadavust") == []

    def test_blank_input_returns_empty(self):
        assert parse_user_reference("") == []
        assert parse_user_reference("   ") == []
        assert parse_user_reference("\t\n") == []

    def test_law_name_alone_without_section_returns_empty(self):
        # A bare law abbreviation with no "§" is too ambiguous — the
        # parser is conservative and returns nothing (the route then
        # surfaces the "no structured ref" branch).
        assert parse_user_reference("karistusseadustik") == []
