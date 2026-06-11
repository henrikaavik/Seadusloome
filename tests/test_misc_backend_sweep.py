"""Backend review-sweep tests for issue #861.

Covers five independent findings, one section each:

* **A** — ``app.email.templates`` HTML-escapes interpolated user values
  (recipient + admin names, reset URL) so a crafted name can't inject
  markup into the password-reset emails.
* **B** — ``app.docs.reference_resolver`` provision regex accepts dotted
  subsection abbreviations (``lg. 2`` / ``p. 1``) and superscript section
  numbers (Unicode ``§ 113¹`` and ASCII ``§ 113^1`` / ``§ 113.1``), with
  regression coverage for the plain forms.
* **C** — ``app.analyysikeskus.input_parser`` no longer mis-parses an ISO
  date (``2026-06-10``) as an Estonian court case number, while real case
  formats (``3-1-1-63-15``, ``3-20-1044``, ``5-19-1-2``) still match.
* **E** — ``app.analyysikeskus.history.list_impact_reports`` matches via
  indexed JSONB containment instead of a ``report_data::text ILIKE``
  full-table scan, with bound ``jsonb`` parameters (no wildcard surface).

(Finding **D**, the SPARQL client's shared pooled ``httpx.Client`` and
``count(on_error=...)`` passthrough, is covered in
``tests/test_sparql_client.py``.)
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# A. Email templates HTML-escape interpolated values
# ---------------------------------------------------------------------------


class TestEmailTemplateEscaping:
    def test_password_reset_escapes_name_markup(self):
        from app.email.templates import password_reset

        _subject, html, text = password_reset(
            full_name="<script>alert(1)</script> & Co",
            reset_url="https://example.com/auth/reset/abc",
        )
        # The raw tag must not survive into the HTML body.
        assert "<script>alert(1)</script>" not in html
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
        assert "&amp; Co" in html
        # The plain-text part is not HTML and is intentionally left raw.
        assert "<script>alert(1)</script> & Co" in text

    def test_password_reset_escapes_url_in_attribute(self):
        from app.email.templates import password_reset

        # A double-quote in the URL must not break out of the href="" attr.
        _subject, html, _text = password_reset(
            full_name="Mari",
            reset_url='https://example.com/r"onmouseover=alert(1)',
        )
        assert 'href="https://example.com/r"onmouseover=alert(1)"' not in html
        assert "&quot;onmouseover=alert(1)" in html

    def test_password_reset_admin_escapes_admin_name(self):
        from app.email.templates import password_reset_admin

        _subject, html, _text = password_reset_admin(
            full_name="Mari",
            reset_url="https://example.com/auth/reset/xyz",
            admin_name="<b>Boss</b>",
        )
        assert "<b>Boss</b>" not in html
        assert "&lt;b&gt;Boss&lt;/b&gt;" in html
        # The legitimate surrounding <strong> markup is still present.
        assert "<strong>" in html

    def test_plain_names_unchanged(self):
        """Escaping is a no-op for ordinary Estonian names."""
        from app.email.templates import password_reset, password_reset_admin

        _s1, html1, _t1 = password_reset(
            full_name="Mari Maasikas",
            reset_url="https://example.com/auth/reset/abc",
        )
        assert "Mari Maasikas" in html1
        _s2, html2, _t2 = password_reset_admin(
            full_name="Mari",
            reset_url="https://example.com/auth/reset/abc",
            admin_name="Henrik Aavik",
        )
        assert "Henrik Aavik" in html2


# ---------------------------------------------------------------------------
# B. Reference-resolver provision regex — dotted abbrevs + superscripts
# ---------------------------------------------------------------------------

ESTLEG = "https://data.riik.ee/ontology/estleg#"


@pytest.fixture(autouse=True)
def _fixed_resolver_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the resolver's HMAC secret so miss-log ref_ids are stable."""
    from app.docs.reference_resolver import _REF_HASH_SECRET_ENV

    monkeypatch.setenv(_REF_HASH_SECRET_ENV, "test-secret-deterministic")


class TestSectionNumberNormalisation:
    def test_plain_digits(self):
        from app.docs.reference_resolver import _normalise_section_number

        assert _normalise_section_number("143") == "143"

    def test_unicode_superscript(self):
        from app.docs.reference_resolver import _normalise_section_number

        assert _normalise_section_number("113¹") == "113^1"
        assert _normalise_section_number("113²³") == "113^23"

    def test_caret_and_dot_forms(self):
        from app.docs.reference_resolver import _normalise_section_number

        assert _normalise_section_number("113^1") == "113^1"
        assert _normalise_section_number("113.1") == "113^1"

    def test_rejects_non_digit(self):
        from app.docs.reference_resolver import _normalise_section_number

        assert _normalise_section_number("") is None
        assert _normalise_section_number("12a") is None
        assert _normalise_section_number("a12") is None

    def test_paragrahv_literals_plain(self):
        from app.docs.reference_resolver import _paragrahv_literals

        assert _paragrahv_literals("143") == ["§ 143.", "§ 143"]

    def test_paragrahv_literals_superscript_widened(self):
        from app.docs.reference_resolver import _paragrahv_literals

        lits = _paragrahv_literals("113^1")
        # Caret, concatenated, and Unicode forms, each with/without period.
        assert "§ 113^1." in lits
        assert "§ 1131" in lits
        assert "§ 113¹" in lits

    def test_section_display_superscript(self):
        from app.docs.reference_resolver import _section_display

        assert _section_display("113^1") == "113¹"
        assert _section_display("143") == "143"


class TestProvisionRegexDottedAndSuperscript:
    def _router(self, ask: bool = False, provision_rows=None):
        sparql = MagicMock()

        def _query(q, bindings=None, **kwargs):
            if "LegalProvision_" in q and "?cls" in q:
                return [
                    {
                        "prov": f"{ESTLEG}KRIMIN_Par_1",
                        "cls": f"{ESTLEG}LegalProvision_KRIMIN",
                        "actLit": "Karistusseadustik",
                    }
                ]
            if "estleg:paragrahv ?par" in q:
                return provision_rows or []
            return []

        sparql.query.side_effect = _query
        sparql.ask.return_value = ask
        return sparql

    def _ref(self, text):
        from app.docs.entity_extractor import ExtractedRef

        return ExtractedRef(ref_text=text, ref_type="provision", confidence=0.9, location={})

    def test_dotted_lg_abbreviation_parses(self):
        """``KarS § 211 lg. 2`` resolves the section (ASK hit) — the dotted
        ``lg.`` must not break the parse."""
        from app.docs.reference_resolver import ReferenceResolver

        resolver = ReferenceResolver(sparql_client=self._router(ask=True))
        result = resolver.resolve([self._ref("KarS § 211 lg. 2")])
        assert result[0].entity_uri == f"{ESTLEG}KRIMIN_Par_211"

    def test_dotted_punkt_abbreviation_parses(self):
        from app.docs.reference_resolver import ReferenceResolver

        resolver = ReferenceResolver(sparql_client=self._router(ask=True))
        result = resolver.resolve([self._ref("KarS § 211 lg. 2 p. 1")])
        assert result[0].entity_uri == f"{ESTLEG}KRIMIN_Par_211"

    def test_superscript_section_unicode_partial_match(self):
        """``KarS § 113¹`` with no matching section yields a partial match
        carrying the canonical caret section — not a regex miss."""
        from app.docs.reference_resolver import ReferenceResolver

        resolver = ReferenceResolver(sparql_client=self._router(ask=False, provision_rows=[]))
        result = resolver.resolve([self._ref("KarS § 113¹")])
        assert result[0].partial_match is not None
        assert result[0].partial_match["section"] == "113^1"
        assert result[0].partial_match["act_token"] == "KRIMIN"
        # The user-facing label renders the superscript back.
        assert "113¹" in (result[0].matched_label or "")

    def test_superscript_section_ascii_caret(self):
        from app.docs.reference_resolver import ReferenceResolver

        resolver = ReferenceResolver(sparql_client=self._router(ask=False, provision_rows=[]))
        result = resolver.resolve([self._ref("KarS § 113^1")])
        assert result[0].partial_match is not None
        assert result[0].partial_match["section"] == "113^1"

    def test_superscript_section_probes_widened_literals(self):
        """The structural SPARQL for a superscript section must probe the
        caret / concatenated / Unicode literal spellings."""
        from app.docs.reference_resolver import ReferenceResolver

        captured: list[str] = []

        sparql = MagicMock()

        def _query(q, bindings=None, **kwargs):
            captured.append(q)
            if "LegalProvision_" in q and "?cls" in q:
                return [
                    {
                        "prov": f"{ESTLEG}KRIMIN_Par_1",
                        "cls": f"{ESTLEG}LegalProvision_KRIMIN",
                        "actLit": "Karistusseadustik",
                    }
                ]
            if "estleg:paragrahv ?par" in q:
                return [{"p": f"{ESTLEG}KRIMIN_Par_113_1"}]
            return []

        sparql.query.side_effect = _query
        sparql.ask.return_value = False
        resolver = ReferenceResolver(sparql_client=sparql)

        result = resolver.resolve([self._ref("KarS § 113¹")])
        structural = [q for q in captured if "estleg:paragrahv ?par" in q]
        assert structural, "structural query should run for superscript section"
        assert "§ 113^1" in structural[0]
        assert "§ 1131" in structural[0]
        assert "§ 113¹" in structural[0]
        # The row hit means we get a resolved URI, not a partial match.
        assert result[0].entity_uri == f"{ESTLEG}KRIMIN_Par_113_1"

    def test_plain_section_regression_uri_guess(self):
        """Regression: a plain ``KarS § 211`` still takes the URI-guess path."""
        from app.docs.reference_resolver import ReferenceResolver

        sparql = self._router(ask=True)
        resolver = ReferenceResolver(sparql_client=sparql)
        result = resolver.resolve([self._ref("KarS § 211")])
        assert result[0].entity_uri == f"{ESTLEG}KRIMIN_Par_211"
        assert result[0].match_score == 1.0
        # URI guess only fires for plain sections.
        assert sparql.ask.call_count == 1

    def test_superscript_skips_uri_guess(self):
        """A superscript section must NOT attempt the (unproven) URI guess."""
        from app.docs.reference_resolver import ReferenceResolver

        sparql = self._router(ask=True, provision_rows=[])
        resolver = ReferenceResolver(sparql_client=sparql)
        resolver.resolve([self._ref("KarS § 113¹")])
        assert sparql.ask.call_count == 0


# ---------------------------------------------------------------------------
# C. input_parser — ISO dates are not court case numbers
# ---------------------------------------------------------------------------


class TestIsoDateNotCaseNumber:
    @pytest.mark.parametrize(
        "iso_date",
        ["2026-06-10", "2024-01-01", "1999-12-31", "2026-6-1"],
    )
    def test_iso_date_does_not_parse_as_case(self, iso_date: str):
        from app.analyysikeskus.input_parser import parse_user_reference

        refs = parse_user_reference(iso_date)
        types = [r.ref_type for r in refs]
        assert "court_decision" not in types

    @pytest.mark.parametrize(
        "case_number",
        ["3-1-1-63-15", "3-2-1-4-13", "5-19-1-2", "3-20-1044"],
    )
    def test_real_case_numbers_still_match(self, case_number: str):
        from app.analyysikeskus.input_parser import parse_user_reference

        refs = parse_user_reference(case_number)
        assert [r.ref_type for r in refs] == ["court_decision"]
        assert refs[0].ref_text == case_number

    def test_cjeu_case_still_matches(self):
        from app.analyysikeskus.input_parser import parse_user_reference

        refs = parse_user_reference("C-131/12")
        assert [r.ref_type for r in refs] == ["court_decision"]

    def test_invalid_month_day_is_not_a_date_but_also_not_a_case(self):
        """``2026-13-99`` is neither a valid ISO date nor (4-digit-prefix) a
        case number, so it falls through to the empty result."""
        from app.analyysikeskus.input_parser import parse_user_reference

        refs = parse_user_reference("2026-13-99")
        assert [r.ref_type for r in refs] == []

    def test_section_superscript_via_input_parser(self):
        """Finding B lockstep: ``KarS § 113¹`` parses into provision+law with
        the canonical caret section in the provision text."""
        from app.analyysikeskus.input_parser import parse_user_reference

        refs = parse_user_reference("KarS § 113¹")
        types = [r.ref_type for r in refs]
        assert types == ["provision", "law"]
        provision = next(r for r in refs if r.ref_type == "provision")
        assert "§ 113^1" in provision.ref_text

    def test_dotted_lg_via_input_parser(self):
        from app.analyysikeskus.input_parser import parse_user_reference

        refs = parse_user_reference("KarS § 211 lg. 2")
        provision = next(r for r in refs if r.ref_type == "provision")
        assert "lg 2" in provision.ref_text


# ---------------------------------------------------------------------------
# E. list_impact_reports — indexed JSONB containment, no ::text ILIKE
# ---------------------------------------------------------------------------


class TestListImpactReportsContainment:
    _URI = "https://data.riik.ee/ontology/estleg#Provision_1"

    def _conn_capturing(self, rows):
        captured: dict[str, Any] = {}
        stub_conn = MagicMock()
        stub_cur = MagicMock()
        stub_conn.cursor.return_value.__enter__.return_value = stub_cur
        stub_conn.cursor.return_value.__exit__.return_value = None

        def _execute(sql, params):
            captured["sql"] = sql
            captured["params"] = params

        stub_cur.execute.side_effect = _execute
        stub_cur.fetchall.return_value = rows
        return stub_conn, captured

    def test_uses_containment_not_text_ilike(self):
        from app.analyysikeskus.history import list_impact_reports

        conn, captured = self._conn_capturing([])
        list_impact_reports(self._URI, db_connection=conn)
        sql = captured["sql"]
        assert isinstance(sql, str)
        # The full-table-scan text cast + ILIKE must be gone.
        assert "::text" not in sql
        assert "ILIKE" not in sql.upper()
        # Replaced by JSONB containment.
        assert "@>" in sql

    def test_binds_jsonb_params_no_wildcards(self):
        from psycopg.types.json import Jsonb

        from app.analyysikeskus.history import list_impact_reports

        conn, captured = self._conn_capturing([])
        list_impact_reports(self._URI, db_connection=conn)
        params = captured["params"]
        assert isinstance(params, tuple)
        # Every containment param is a typed Jsonb wrapper carrying the URI,
        # never a ``%...%`` LIKE pattern string.
        jsonb_params = [p for p in params if isinstance(p, Jsonb)]
        assert len(jsonb_params) >= 5
        for p in params:
            assert not (isinstance(p, str) and "%" in p)

    def test_containment_covers_known_uri_paths(self):
        """The probes cover the documented URI-bearing report_data paths."""
        import json

        from psycopg.types.json import Jsonb

        from app.analyysikeskus.history import list_impact_reports

        conn, captured = self._conn_capturing([])
        list_impact_reports(self._URI, db_connection=conn)
        # Serialise each Jsonb's payload to a comparable string blob.
        blobs = [
            json.dumps(p.obj, sort_keys=True) for p in captured["params"] if isinstance(p, Jsonb)
        ]
        joined = " ".join(blobs)
        for key in (
            "affected_entities",
            "conflicting_entity",
            "eu_act",
            "estonian_provision",
            "topic_cluster",
        ):
            assert key in joined

    def test_returns_parsed_rows(self):
        from datetime import datetime

        from app.analyysikeskus.history import list_impact_reports

        gen_at = datetime(2024, 3, 1, 12, 30)
        conn, _ = self._conn_capturing(
            [
                (
                    "11111111-1111-1111-1111-111111111111",
                    "22222222-2222-2222-2222-222222222222",
                    "Eelnõu pealkiri",
                    gen_at,
                    3,
                )
            ]
        )
        rows = list_impact_reports(self._URI, db_connection=conn)
        assert len(rows) == 1
        assert rows[0].draft_title == "Eelnõu pealkiri"
        assert rows[0].version_number == 3

    def test_blank_uri_skips_query(self):
        from app.analyysikeskus.history import list_impact_reports

        conn = MagicMock()
        assert list_impact_reports("   ", db_connection=conn) == []
        conn.cursor.assert_not_called()


class TestMigration042:
    def _path(self):
        from pathlib import Path

        return (
            Path(__file__).parent.parent / "migrations" / "042_impact_reports_report_data_gin.sql"
        )

    def test_exists(self):
        assert self._path().exists()

    def test_is_idempotent(self):
        body = self._path().read_text().lower()
        assert "create index if not exists" in body

    def test_gin_index_on_report_data(self):
        body = self._path().read_text().lower()
        assert "using gin (report_data)" in body
        assert "idx_impact_reports_report_data" in body
