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


class TestBareLawReference:
    """Bare law-name recognition — the post-Wave-2-Step-5 fix.

    A bare law abbreviation like ``KarS`` / ``KMS`` / ``AvTS`` (matched
    via the resolver's curated :data:`_HUMAN_ABBREV_ALIASES`) or a
    full law title ending in an Estonian legal-reference suffix
    (``Töölepingu seadus``, ``Karistusseadustik``) emits one ``law``
    ref. The downstream resolver returns a ``partial_match`` payload
    carrying the canonical act title, and the analüüsikeskus routes
    pick up that payload to call the ``list_*_for_act`` helpers.
    """

    def test_curated_abbreviation_kars(self):
        refs = parse_user_reference("KarS")
        assert _types(refs) == ["law"]
        assert refs[0].ref_text == "KarS"
        # Curated alias hits are high-confidence (1.0).
        assert refs[0].confidence == 1.0

    def test_curated_abbreviation_avts(self):
        # AvTS is in the curated alias map. Case-insensitive match.
        refs = parse_user_reference("AvTS")
        assert _types(refs) == ["law"]
        assert refs[0].ref_text == "AvTS"
        assert refs[0].confidence == 1.0

    def test_abbreviation_shape_kms(self):
        # KMS is not in the curated alias map (the resolver's
        # _HUMAN_ABBREV_ALIASES intentionally omits abbreviations
        # whose corpus TOKEN hasn't been confirmed). The parser still
        # emits a law ref via the abbreviation-shape heuristic so the
        # resolver gets a chance to TOKEN-match or fuzzy-match it.
        # Confidence is the lower suffix-tier (0.8).
        refs = parse_user_reference("KMS")
        assert _types(refs) == ["law"]
        assert refs[0].ref_text == "KMS"
        assert refs[0].confidence == 0.8

    def test_abbreviation_shape_tls(self):
        # TLS is the analogous case for the burden test fixtures.
        refs = parse_user_reference("TLS")
        assert _types(refs) == ["law"]
        assert refs[0].ref_text == "TLS"
        assert refs[0].confidence == 0.8

    def test_curated_alias_avts_asciified(self):
        # AOKS is asciified AõKS — the asciified key is curated in
        # _HUMAN_ABBREV_ALIASES so this hits the 1.0 branch.
        refs = parse_user_reference("AOKS")
        assert _types(refs) == ["law"]
        assert refs[0].ref_text == "AOKS"
        assert refs[0].confidence == 1.0

    def test_full_title_with_seadus_suffix(self):
        refs = parse_user_reference("Töölepingu seadus")
        assert _types(refs) == ["law"]
        assert refs[0].ref_text == "Töölepingu seadus"
        # Suffix-only matches get 0.8 confidence.
        assert refs[0].confidence == 0.8

    def test_full_title_with_seadustik_suffix(self):
        refs = parse_user_reference("karistusseadustik")
        assert _types(refs) == ["law"]
        assert refs[0].ref_text == "karistusseadustik"
        assert refs[0].confidence == 0.8

    def test_inflected_seadus_form(self):
        # The suffix list covers Estonian inflected forms.
        refs = parse_user_reference("töölepingu seaduses")
        assert _types(refs) == ["law"]
        assert refs[0].confidence == 0.8

    def test_freetext_question_with_seadus_word_returns_empty(self):
        # A question shape that happens to contain the word "tööleping"
        # but no curated alias or suffix should fall through to free-text.
        assert parse_user_reference("mida tähendab tööleping?") == []

    def test_sentence_shaped_input_above_length_cap_returns_empty(self):
        # Long sentence-shaped input — even if it ends in "seaduses" we
        # cap recognition at 80 chars to avoid swallowing free-text.
        long_sentence = (
            "Mind huvitab, milline on praegune regulatsioon riigieelarve "
            "ülevaate koostamise seaduses ja kuidas seda kohaldatakse "
            "kohalikule omavalitsusele"
        )
        assert len(long_sentence) > 80
        assert parse_user_reference(long_sentence) == []

    def test_section_reference_unchanged_by_bare_law_branch(self):
        # Regression: existing §-reference behaviour must not change.
        refs = parse_user_reference("KarS § 211")
        # provision first, then the law short name (from the §-branch).
        assert _types(refs) == ["provision", "law"]
        prov = _by_type(refs, "provision")
        assert prov.ref_text == "KarS § 211"
        assert _by_type(refs, "law").ref_text == "KarS"
