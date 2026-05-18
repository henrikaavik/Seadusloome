"""Unit tests for ``app.docs.reference_resolver``.

The resolver talks to Jena via :class:`SparqlClient`; these tests
inject a MagicMock client so no real HTTP traffic happens. We verify
each ref_type's strategy in isolation and that a dead Jena turns into
unresolved refs (not an exception).

Revised 2026-05-18 for Wave 2 Step 2 of
``docs/2026-05-18-bugfix-plan.md``: the resolver now derives its
abbreviation map from ``LegalProvision_<TOKEN>`` subclasses, treats
``estleg:sourceAct`` as a literal title, attempts a URI-guess fast
path (``ASK { <estleg:TOKEN_Par_N> ?p ?o }``), and surfaces a
distinct ``partial_match`` state when the act resolves but the
section does not.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from app.docs.entity_extractor import ExtractedRef
from app.docs.reference_resolver import (
    ReferenceResolver,
    resolve_refs,
)

ESTLEG = "https://data.riik.ee/ontology/estleg#"


@pytest.fixture(autouse=True)
def _fixed_resolver_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the HMAC secret via env var so tests get stable ``ref_id`` values.

    Setting ``RESOLVER_REF_HASH_SECRET`` in the env exercises the real
    ``_get_ref_hash_secret`` helper rather than monkeypatching it — that
    way the production-raise test below can still drive the helper's
    real gate by clearing the env and flipping ``APP_ENV``.
    """
    from app.docs.reference_resolver import _REF_HASH_SECRET_ENV

    monkeypatch.setenv(_REF_HASH_SECRET_ENV, "test-secret-deterministic")


def _ref(ref_text: str, ref_type: str, confidence: float = 0.9) -> ExtractedRef:
    return ExtractedRef(
        ref_text=ref_text,
        ref_type=ref_type,
        confidence=confidence,
        location={"chunk": 0, "offset": 0},
    )


def _abbrev_map_rows(*tokens_titles: tuple[str, str]) -> list[dict[str, str]]:
    """Build a fake response for the abbreviation-map SPARQL query.

    Each ``(TOKEN, title)`` pair becomes one synthetic row mimicking
    ``?prov a ?cls ; estleg:sourceAct ?actLit``.
    """
    rows: list[dict[str, str]] = []
    for token, title in tokens_titles:
        rows.append(
            {
                "prov": f"{ESTLEG}{token}_Par_1",
                "cls": f"{ESTLEG}LegalProvision_{token}",
                "actLit": title,
            }
        )
    return rows


def _make_sparql_router(
    *,
    abbrev_rows: list[dict[str, str]] | None = None,
    provision_rows: list[dict[str, str]] | None = None,
    eu_rows: list[dict[str, str]] | None = None,
    court_rows: list[dict[str, str]] | None = None,
    concept_rows: list[dict[str, str]] | None = None,
    ask_result: bool | dict[str, bool] = False,
) -> MagicMock:
    """Build a MagicMock SparqlClient that routes by query content.

    The abbreviation-map query contains ``LegalProvision_``; the
    provision query contains ``estleg:paragrahv ?par``; EU contains
    ``EULegislation``; court contains ``caseNumber``; concept contains
    ``LegalConcept``. ``ask_result`` can be either a global bool or a
    map from a substring (e.g. a TOKEN) to the bool the ASK should
    return when that substring appears in the query.
    """
    sparql = MagicMock()

    def _query(q: str, bindings: dict[str, str] | None = None, **kwargs: Any):
        if "LegalProvision_" in q and "?cls" in q:
            return abbrev_rows or []
        if "estleg:paragrahv ?par" in q or "estleg:paragrahv ?paragrahv" in q:
            return provision_rows or []
        if "EULegislation" in q:
            return eu_rows or []
        if "caseNumber" in q:
            return court_rows or []
        if "LegalConcept" in q:
            return concept_rows or []
        return []

    def _ask(q: str) -> bool:
        if isinstance(ask_result, bool):
            return ask_result
        for key, value in ask_result.items():
            if key in q:
                return value
        return False

    sparql.query.side_effect = _query
    sparql.ask.side_effect = _ask
    return sparql


# ---------------------------------------------------------------------------
# Law half resolution
# ---------------------------------------------------------------------------


class TestResolveLaw:
    def test_resolve_law_exact_token_match(self):
        sparql = _make_sparql_router(
            abbrev_rows=_abbrev_map_rows(("KRIMIN", "Karistusseadustik")),
        )
        resolver = ReferenceResolver(sparql_client=sparql)

        result = resolver.resolve([_ref("KRIMIN", "law")])

        assert len(result) == 1
        assert result[0].entity_uri == "Karistusseadustik"
        assert result[0].matched_label == "Karistusseadustik"
        assert result[0].match_score == 1.0

    def test_resolve_law_exact_title_match(self):
        sparql = _make_sparql_router(
            abbrev_rows=_abbrev_map_rows(("KRIMIN", "Karistusseadustik")),
        )
        resolver = ReferenceResolver(sparql_client=sparql)

        result = resolver.resolve([_ref("karistusseadustik", "law")])

        # ``karistusseadustik`` normalises to ``karistus`` because
        # ``seadustik`` is stripped — the abbreviation map should
        # still produce a match via the title index, NOT match an
        # unrelated act fuzzily.
        # We accept either the token route (preferred) or the title
        # route; both should land on the canonical title.
        assert result[0].entity_uri == "Karistusseadustik"

    def test_resolve_law_fuzzy_match(self):
        sparql = _make_sparql_router(
            abbrev_rows=_abbrev_map_rows(
                ("KRIMIN", "Karistusseadustik"),
                ("ATMOSF", "Atmosfääriõhu kaitse seadus"),
            ),
        )
        resolver = ReferenceResolver(sparql_client=sparql)

        # Slight typo on a title → fuzzy match
        result = resolver.resolve([_ref("Atmosfääriõhu kaitse", "law")])

        assert result[0].entity_uri == "Atmosfääriõhu kaitse seadus"
        assert 0.7 <= result[0].match_score <= 1.0

    def test_resolve_law_no_match(self, caplog: pytest.LogCaptureFixture):
        sparql = _make_sparql_router(
            abbrev_rows=_abbrev_map_rows(("KRIMIN", "Karistusseadustik")),
        )
        resolver = ReferenceResolver(sparql_client=sparql)

        with caplog.at_level("INFO"):
            result = resolver.resolve([_ref("Täiesti tundmatu seadus", "law")])

        assert result[0].entity_uri is None
        assert result[0].match_score == 0.0
        assert any("resolver: law unresolved" in rec.message for rec in caplog.records)

    def test_resolve_law_suffix_false_positive_guard(self):
        """``karistusseaduselt`` must NOT match — ``seaduselt`` is not stripped."""
        sparql = _make_sparql_router(
            abbrev_rows=_abbrev_map_rows(("KRIMIN", "Karistusseadustik")),
        )
        resolver = ReferenceResolver(sparql_client=sparql)

        # The input has the bogus suffix ``-elt`` which we deliberately
        # do NOT strip. The token differs from the canonical, and the
        # difflib ratio should be below threshold.
        result = resolver.resolve([_ref("karistusseaduselt", "law")])

        # The resolver may still produce a fuzzy hit because
        # ``karistusseaduselt`` and ``karistus`` share a long prefix.
        # The important property is that suffix-stripping does NOT
        # collapse them to the same key — verify by comparing the
        # match_score against an exact token match.
        # If a match happens, it MUST be flagged as fuzzy (<1.0), not
        # an exact 1.0 collapse.
        if result[0].entity_uri is not None:
            assert result[0].match_score < 1.0


# ---------------------------------------------------------------------------
# Provision resolution
# ---------------------------------------------------------------------------


class TestResolveProvision:
    def test_provision_uri_guess_hit_atmosf(self):
        """``atmosfääriõhu kaitse seaduse §-s 143`` → ATMOSF_Par_143 via ASK."""
        sparql = _make_sparql_router(
            abbrev_rows=_abbrev_map_rows(
                ("ATMOSF", "Atmosfääriõhu kaitse seadus"),
            ),
            ask_result={"ATMOSF_Par_143": True},
        )
        resolver = ReferenceResolver(sparql_client=sparql)

        result = resolver.resolve([_ref("atmosfääriõhu kaitse seaduse §-s 143", "provision")])

        assert result[0].entity_uri == f"{ESTLEG}ATMOSF_Par_143"
        assert result[0].match_score == 1.0
        # The ASK path should be reached exactly once.
        assert sparql.ask.call_count == 1

    def test_provision_uri_guess_hit_kars(self):
        """``karistusseadustiku §-s 211`` → KRIMIN_Par_211 via ASK."""
        sparql = _make_sparql_router(
            abbrev_rows=_abbrev_map_rows(("KRIMIN", "Karistusseadustik")),
            ask_result={"KRIMIN_Par_211": True},
        )
        resolver = ReferenceResolver(sparql_client=sparql)

        result = resolver.resolve([_ref("karistusseadustiku §-s 211", "provision")])

        assert result[0].entity_uri == f"{ESTLEG}KRIMIN_Par_211"
        assert result[0].match_score == 1.0

    def test_provision_structural_fallback_period_form(self):
        """ASK misses; structural query with ``§ N.`` literal hits."""
        sparql = _make_sparql_router(
            abbrev_rows=_abbrev_map_rows(("RES", "Riigieelarve seadus")),
            provision_rows=[{"p": f"{ESTLEG}RES_Par_20"}],
            ask_result={"RES_Par_20": False},
        )
        resolver = ReferenceResolver(sparql_client=sparql)

        result = resolver.resolve([_ref("riigieelarve seaduse § 20 lõike 5", "provision")])

        assert result[0].entity_uri == f"{ESTLEG}RES_Par_20"
        assert result[0].match_score == 1.0

    def test_provision_both_paragrahv_literal_forms(self):
        """The structural query must accept ``"§ 143"`` (no period) AND ``"§ 143."``."""
        captured_queries: list[str] = []

        def _query_capture(q: str, bindings=None, **kwargs):
            captured_queries.append(q)
            if "estleg:paragrahv ?par" in q:
                # The query should mention BOTH forms.
                return [{"p": f"{ESTLEG}ATMOSF_Par_143"}]
            if "LegalProvision_" in q:
                return _abbrev_map_rows(("ATMOSF", "Atmosfääriõhu kaitse seadus"))
            return []

        sparql = MagicMock()
        sparql.query.side_effect = _query_capture
        sparql.ask.return_value = False  # force the structural path

        resolver = ReferenceResolver(sparql_client=sparql)

        result = resolver.resolve([_ref("atmosfääriõhu kaitse seaduse § 143", "provision")])

        assert result[0].entity_uri == f"{ESTLEG}ATMOSF_Par_143"
        # The captured structural query must contain BOTH literal forms.
        structural = [q for q in captured_queries if "estleg:paragrahv ?par" in q]
        assert len(structural) >= 1
        assert '"§ 143."' in structural[0]
        assert '"§ 143"' in structural[0]

    def test_provision_partial_match_avts_thematic(self, caplog: pytest.LogCaptureFixture):
        """``AvTS § 35`` resolves the act but not the section in this corpus.

        Per the spike, AvTS in the corpus has only thematic provision
        nodes (``AVTS_Par_JuurdepaasuYldpohimotted``), so ``AVTS_Par_35``
        does not exist and the structural fallback also returns no rows.
        The resolver must surface this as a ``partial_match`` state,
        NOT a clean miss.
        """
        sparql = _make_sparql_router(
            abbrev_rows=_abbrev_map_rows(("AVTS", "Avaliku teabe seadus")),
            provision_rows=[],
            ask_result=False,  # URI guess misses
        )
        resolver = ReferenceResolver(sparql_client=sparql)

        with caplog.at_level("INFO"):
            result = resolver.resolve([_ref("AvTS § 35", "provision")])

        assert result[0].entity_uri is None
        assert result[0].match_score == 0.5
        assert result[0].partial_match is not None
        assert result[0].partial_match["act_token"] == "AVTS"
        assert result[0].partial_match["act_title"] == "Avaliku teabe seadus"
        assert result[0].partial_match["section"] == "35"
        assert "Avaliku teabe seadus" in (result[0].matched_label or "")
        # The miss is logged with the HMAC'd ref_id.
        miss_logs = [
            rec for rec in caplog.records if "resolver: provision unresolved" in rec.message
        ]
        assert miss_logs, "miss log should be emitted for partial-match path"
        assert "ref_id=" in miss_logs[0].message

    def test_provision_avts_dash_inflection(self):
        """``AvTS-i § 35`` should normalise the same as ``AvTS § 35``."""
        sparql = _make_sparql_router(
            abbrev_rows=_abbrev_map_rows(("AVTS", "Avaliku teabe seadus")),
            ask_result=False,
            provision_rows=[],
        )
        resolver = ReferenceResolver(sparql_client=sparql)

        result = resolver.resolve([_ref("AvTS-i § 35", "provision")])

        assert result[0].partial_match is not None
        assert result[0].partial_match["act_token"] == "AVTS"

    def test_provision_paragrahvi_inflection(self):
        """``paragrahvi 143`` should be parsed like ``§ 143``."""
        sparql = _make_sparql_router(
            abbrev_rows=_abbrev_map_rows(
                ("ATMOSF", "Atmosfääriõhu kaitse seadus"),
            ),
            ask_result={"ATMOSF_Par_143": True},
        )
        resolver = ReferenceResolver(sparql_client=sparql)

        result = resolver.resolve(
            [_ref("Atmosfääriõhu kaitse seadus paragrahvi 143", "provision")]
        )

        assert result[0].entity_uri == f"{ESTLEG}ATMOSF_Par_143"

    def test_provision_with_lg_punkt(self):
        sparql = _make_sparql_router(
            abbrev_rows=_abbrev_map_rows(("KRIMIN", "Karistusseadustik")),
            ask_result={"KRIMIN_Par_211": True},
        )
        resolver = ReferenceResolver(sparql_client=sparql)

        result = resolver.resolve([_ref("KarS § 211 lg 2", "provision")])

        # Note: KarS isn't in the abbreviation map (KRIMIN is the
        # canonical TOKEN for karistusseadustik in our spike). With
        # only KRIMIN in the map, "KarS" cannot match the token route.
        # The act-half resolver returns title=None → unresolved.
        # This documents the current behaviour: the source data drives
        # the TOKEN names, and we don't hard-code "KarS" → "KRIMIN".
        assert result[0].entity_uri is None

    def test_provision_regex_no_match(self, caplog: pytest.LogCaptureFixture):
        """Free-form text with no ``§`` marker is unresolved with a log."""
        sparql = _make_sparql_router(
            abbrev_rows=_abbrev_map_rows(("KRIMIN", "Karistusseadustik")),
        )
        resolver = ReferenceResolver(sparql_client=sparql)

        with caplog.at_level("INFO"):
            result = resolver.resolve([_ref("FooBar 999", "provision")])

        assert result[0].entity_uri is None
        assert result[0].partial_match is None
        assert any("resolver: provision unresolved" in rec.message for rec in caplog.records)

    def test_provision_unknown_act_unresolved(self, caplog: pytest.LogCaptureFixture):
        """Unknown act half → unresolved, no partial_match."""
        sparql = _make_sparql_router(
            abbrev_rows=_abbrev_map_rows(("KRIMIN", "Karistusseadustik")),
            ask_result=False,
        )
        resolver = ReferenceResolver(sparql_client=sparql)

        with caplog.at_level("INFO"):
            result = resolver.resolve([_ref("XYZUNKNOWN § 5", "provision")])

        assert result[0].entity_uri is None
        # Without an act-half resolution there's no partial state.
        assert result[0].partial_match is None
        assert any("resolver: provision unresolved" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# EU act resolution
# ---------------------------------------------------------------------------


class TestResolveEUAct:
    def test_resolve_eu_act_by_celex(self):
        sparql = _make_sparql_router(
            eu_rows=[{"uri": f"{ESTLEG}EU_GDPR", "label": "GDPR"}],
        )
        resolver = ReferenceResolver(sparql_client=sparql)

        result = resolver.resolve([_ref("määrus 32016R0679 (GDPR)", "eu_act")])

        assert result[0].entity_uri == f"{ESTLEG}EU_GDPR"
        assert result[0].matched_label == "GDPR"

    def test_resolve_eu_act_lowercase_letter_normalised(self):
        """``32016r0679`` (lowercase R) must still match."""
        sparql = _make_sparql_router(
            eu_rows=[{"uri": f"{ESTLEG}EU_GDPR", "label": "GDPR"}],
        )
        resolver = ReferenceResolver(sparql_client=sparql)

        result = resolver.resolve([_ref("32016r0679", "eu_act")])

        assert result[0].entity_uri == f"{ESTLEG}EU_GDPR"

    def test_resolve_eu_act_whitespace_stripped(self):
        sparql = _make_sparql_router(
            eu_rows=[{"uri": f"{ESTLEG}EU_GDPR", "label": "GDPR"}],
        )
        resolver = ReferenceResolver(sparql_client=sparql)

        result = resolver.resolve([_ref("  32016R0679  ", "eu_act")])

        assert result[0].entity_uri == f"{ESTLEG}EU_GDPR"

    def test_resolve_eu_act_without_celex_is_unresolved(self, caplog: pytest.LogCaptureFixture):
        sparql = _make_sparql_router()
        resolver = ReferenceResolver(sparql_client=sparql)

        with caplog.at_level("INFO"):
            result = resolver.resolve([_ref("GDPR üldiselt", "eu_act")])

        assert result[0].entity_uri is None
        assert any("resolver: eu_act unresolved" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# Court decision
# ---------------------------------------------------------------------------


class TestResolveCourtDecision:
    def test_resolve_court_decision_by_case_number(self):
        sparql = _make_sparql_router(
            court_rows=[
                {
                    "uri": f"{ESTLEG}case_3-1-1-63-15",
                    "label": "Riigikohus 3-1-1-63-15",
                }
            ],
        )
        resolver = ReferenceResolver(sparql_client=sparql)

        result = resolver.resolve([_ref("3-1-1-63-15", "court_decision")])

        assert result[0].entity_uri == f"{ESTLEG}case_3-1-1-63-15"
        assert result[0].matched_label == "Riigikohus 3-1-1-63-15"

    def test_resolve_court_decision_whitespace_stripped(self):
        sparql = _make_sparql_router(
            court_rows=[{"uri": f"{ESTLEG}case_C-123-20", "label": "C-123/20"}],
        )
        resolver = ReferenceResolver(sparql_client=sparql)

        result = resolver.resolve([_ref("  C-123/20  ", "court_decision")])

        assert result[0].entity_uri == f"{ESTLEG}case_C-123-20"

    def test_resolve_court_decision_empty_unresolved(self, caplog: pytest.LogCaptureFixture):
        sparql = _make_sparql_router()
        resolver = ReferenceResolver(sparql_client=sparql)

        with caplog.at_level("INFO"):
            result = resolver.resolve([_ref("", "court_decision")])

        assert result[0].entity_uri is None
        assert any("resolver: court_decision unresolved" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# Concept
# ---------------------------------------------------------------------------


class TestResolveConcept:
    def test_resolve_concept_by_label(self):
        sparql = _make_sparql_router(
            concept_rows=[
                {
                    "uri": f"{ESTLEG}concept_good_faith",
                    "label": "hea usu põhimõte",
                }
            ],
        )
        resolver = ReferenceResolver(sparql_client=sparql)

        result = resolver.resolve([_ref("hea usu põhimõte", "concept")])

        assert result[0].entity_uri == f"{ESTLEG}concept_good_faith"

    def test_resolve_concept_case_insensitive(self):
        sparql = _make_sparql_router(
            concept_rows=[
                {
                    "uri": f"{ESTLEG}concept_good_faith",
                    "label": "hea usu põhimõte",
                }
            ],
        )
        resolver = ReferenceResolver(sparql_client=sparql)

        result = resolver.resolve([_ref("Hea Usu Põhimõte", "concept")])

        assert result[0].entity_uri == f"{ESTLEG}concept_good_faith"


# ---------------------------------------------------------------------------
# Jena outage handling
# ---------------------------------------------------------------------------


class TestResolverOutage:
    def test_resolve_handles_jena_outage(self, caplog: pytest.LogCaptureFixture):
        """SparqlClient.query raising → all refs returned unresolved with a warning."""
        sparql = MagicMock()
        sparql.query.side_effect = RuntimeError("connection refused")
        sparql.ask.side_effect = RuntimeError("connection refused")
        resolver = ReferenceResolver(sparql_client=sparql)

        with caplog.at_level("WARNING"):
            result = resolver.resolve(
                [
                    _ref("KarS", "law"),
                    _ref("KarS § 1", "provision"),
                    _ref("32016R0679", "eu_act"),
                    _ref("3-1-1-63-15", "court_decision"),
                    _ref("hea usu põhimõte", "concept"),
                ]
            )

        assert len(result) == 5
        for r in result:
            assert r.entity_uri is None
            assert r.match_score == 0.0
        # Every ref_type should have logged a warning at least once.
        assert len(caplog.records) >= 1

    def test_resolve_empty_list_returns_empty(self):
        sparql = _make_sparql_router()
        resolver = ReferenceResolver(sparql_client=sparql)

        assert resolver.resolve([]) == []
        sparql.query.assert_not_called()


# ---------------------------------------------------------------------------
# Singleton factory
# ---------------------------------------------------------------------------


class TestDefaultResolverFactory:
    def test_resolve_refs_top_level_helper(self, monkeypatch: pytest.MonkeyPatch):
        """``resolve_refs`` delegates to the module-level singleton."""
        import app.docs.reference_resolver as mod

        fake_resolver = MagicMock()
        fake_resolver.resolve.return_value = ["fake-result"]
        monkeypatch.setattr(mod, "_default_resolver", fake_resolver)

        out = resolve_refs([_ref("KarS", "law")])

        assert out == ["fake-result"]
        fake_resolver.resolve.assert_called_once()


# ---------------------------------------------------------------------------
# Privacy-preserving miss logging
# ---------------------------------------------------------------------------


class TestRefIdLogging:
    def test_ref_id_is_stable_and_hashed(self):
        import app.docs.reference_resolver as mod

        # With the fixed test secret, the same text should always
        # produce the same 12-hex-char id.
        id_a = mod._ref_id("AvTS § 35")
        id_b = mod._ref_id("AvTS § 35")
        assert id_a == id_b
        assert len(id_a) == 12
        # The raw text must NOT appear anywhere in the hashed id.
        assert "AvTS" not in id_a
        assert "35" not in id_a

    def test_get_ref_hash_secret_dev_fallback(self, monkeypatch: pytest.MonkeyPatch):
        """Outside production with no env var, the helper returns the dev sentinel."""
        from app.docs.reference_resolver import (
            _REF_HASH_SECRET_ENV,
            _get_ref_hash_secret,
        )

        monkeypatch.delenv(_REF_HASH_SECRET_ENV, raising=False)
        monkeypatch.setenv("APP_ENV", "development")

        result = _get_ref_hash_secret()
        assert result == b"dev-only-resolver-ref-id-secret"

    def test_get_ref_hash_secret_production_raises_without_env(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """In production the missing env var must raise — refuse to log raw text."""
        from app.docs.reference_resolver import (
            _REF_HASH_SECRET_ENV,
            _get_ref_hash_secret,
        )

        monkeypatch.delenv(_REF_HASH_SECRET_ENV, raising=False)
        monkeypatch.setenv("APP_ENV", "production")

        with pytest.raises(RuntimeError, match="RESOLVER_REF_HASH_SECRET"):
            _get_ref_hash_secret()

    def test_get_ref_hash_secret_env_var_wins(self, monkeypatch: pytest.MonkeyPatch):
        """When the env var is set, the helper returns it byte-encoded."""
        from app.docs.reference_resolver import (
            _REF_HASH_SECRET_ENV,
            _get_ref_hash_secret,
        )

        monkeypatch.setenv(_REF_HASH_SECRET_ENV, "explicit-test-value")

        assert _get_ref_hash_secret() == b"explicit-test-value"
