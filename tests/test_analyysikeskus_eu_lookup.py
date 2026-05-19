"""Unit tests for :mod:`app.analyysikeskus.eu_lookup`.

Pins the shape recognition + label-search degradation rules. The
SPARQL-backed branch of :func:`search_eu_acts_by_label` is covered
indirectly through ``test_analyysikeskus_routes.py`` — these tests
focus on the pure shape helper introduced for #805/#815 and the
defensive fallbacks around label search.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from app.analyysikeskus.eu_lookup import is_canonical_celex_shape, search_eu_acts_by_label


class TestIsCanonicalCelexShape:
    """``is_canonical_celex_shape`` — strict canonical-CELEX recogniser.

    The helper exists to discriminate "user typed a real CELEX that
    just isn't in our ontology snapshot" (warning copy names the
    CELEX) from "user typed prose or garbage" (generic hint copy).
    The acceptance contract for #805 is:

    * True for canonical CELEXes (sector 1-9, 4-digit year, uppercase
      form letter from the documented whitelist, 4-digit running #)
    * False for everything else
    """

    def test_gdpr_celex(self):
        # The bug-report CELEX from #805 (GDPR).
        assert is_canonical_celex_shape("32016R0679") is True

    def test_working_conditions_celex(self):
        # A directive — different form letter from the regulation case.
        assert is_canonical_celex_shape("32019L1152") is True

    def test_decision_celex(self):
        assert is_canonical_celex_shape("32020D0001") is True

    def test_recommendation_celex(self):
        assert is_canonical_celex_shape("32023H0001") is True

    def test_with_leading_and_trailing_whitespace(self):
        # The helper must strip — the route hands ``sisend`` straight in.
        assert is_canonical_celex_shape("  32016R0679  ") is True

    def test_rejects_short_alphanumeric_garbage(self):
        # Acceptance criterion from #805 plan.
        assert is_canonical_celex_shape("12abc34") is False

    def test_rejects_word_acronym(self):
        # Users type "GDPR" — that's NOT a canonical CELEX.
        assert is_canonical_celex_shape("GDPR") is False

    def test_rejects_empty(self):
        assert is_canonical_celex_shape("") is False

    def test_rejects_blank(self):
        assert is_canonical_celex_shape("   ") is False

    def test_rejects_none_safely(self):
        # The route passes ``(req.query_params.get(...) or "").strip()``
        # so the helper never sees None in production, but defensive
        # behaviour is cheap and prevents a TypeError if a caller forgets.
        assert is_canonical_celex_shape(None) is False  # type: ignore[arg-type]

    def test_rejects_lowercase_form_letter(self):
        # Real CELEX form letters are always uppercase.
        assert is_canonical_celex_shape("32016r0679") is False

    def test_rejects_invalid_form_letter_x(self):
        # Acceptance criterion from #805 plan: X is outside the
        # binding-instrument whitelist.
        assert is_canonical_celex_shape("32016X0679") is False

    def test_rejects_zero_sector(self):
        # Sector 0 doesn't exist in EurLex's CELEX scheme.
        assert is_canonical_celex_shape("02016R0679") is False

    def test_rejects_too_short_year(self):
        assert is_canonical_celex_shape("3216R0679") is False

    def test_rejects_too_short_running_number(self):
        # input_parser accepts 1-4 digits to be lenient with paste-noise;
        # this canonical helper requires exactly 4.
        assert is_canonical_celex_shape("32016R067") is False
        assert is_canonical_celex_shape("32016R67") is False
        assert is_canonical_celex_shape("32016R6") is False

    def test_rejects_too_long_running_number(self):
        assert is_canonical_celex_shape("32016R06799") is False

    def test_rejects_celex_embedded_in_phrase(self):
        # ``parse_user_reference`` *does* lift an embedded CELEX out, but
        # this helper is intentionally exact-shape only — the route
        # already calls ``parse_user_reference`` first; what falls
        # through to ``_render_eu_unresolved`` is whatever the user
        # typed verbatim.
        assert is_canonical_celex_shape("GDPR (32016R0679)") is False

    def test_rejects_with_internal_whitespace(self):
        assert is_canonical_celex_shape("32016 R 0679") is False


class TestSearchEuActsByLabelDegradation:
    """Defensive fallbacks — short queries + SPARQL failures yield ``[]``.

    The happy path (a successful Jena query returning labelled
    candidates) is covered by route-level integration tests.
    """

    def test_short_query_returns_empty(self):
        # The helper short-circuits before hitting Jena when the query
        # is too narrow to be useful — prevents a stray 1-char input
        # from dumping the entire EU corpus.
        assert search_eu_acts_by_label("") == []
        assert search_eu_acts_by_label(" ") == []
        assert search_eu_acts_by_label("A") == []

    def test_sparql_exception_degrades_to_empty(self):
        # Jena dead / unreachable → ``[]`` so the route falls through to
        # the "ei tuvastatud" warning rather than 500-ing.
        client = MagicMock()
        client.query.side_effect = RuntimeError("boom")
        with patch(
            "app.analyysikeskus.eu_lookup.SparqlClient",
            return_value=client,
        ):
            assert search_eu_acts_by_label("isikuandmete kaitse") == []

    def test_empty_rows_returns_empty(self):
        client = MagicMock()
        client.query.return_value = []
        with patch(
            "app.analyysikeskus.eu_lookup.SparqlClient",
            return_value=client,
        ):
            assert search_eu_acts_by_label("isikuandmete kaitse") == []
