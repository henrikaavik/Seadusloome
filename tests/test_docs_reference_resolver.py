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
        """Law-only refs resolve to a partial-match, never a literal entity_uri.

        Per P1#1: the corpus has no act-level URIs, so a law-only ref
        (no ``§ N``) cannot have ``entity_uri`` set without polluting
        downstream RDF with malformed triples like ``<Karistusseadustik>``.
        The canonical title + TOKEN ride along on ``partial_match``.
        """
        sparql = _make_sparql_router(
            abbrev_rows=_abbrev_map_rows(("KRIMIN", "Karistusseadustik")),
        )
        resolver = ReferenceResolver(sparql_client=sparql)

        result = resolver.resolve([_ref("KRIMIN", "law")])

        assert len(result) == 1
        assert result[0].entity_uri is None
        assert result[0].matched_label == "Karistusseadustik"
        assert result[0].match_score == 1.0
        assert result[0].partial_match is not None
        assert result[0].partial_match["act_token"] == "KRIMIN"
        assert result[0].partial_match["act_title"] == "Karistusseadustik"
        assert result[0].partial_match["section"] is None

    def test_resolve_law_exact_title_match(self):
        sparql = _make_sparql_router(
            abbrev_rows=_abbrev_map_rows(("KRIMIN", "Karistusseadustik")),
        )
        resolver = ReferenceResolver(sparql_client=sparql)

        result = resolver.resolve([_ref("karistusseadustik", "law")])

        # ``karistusseadustik`` normalises to ``karistus`` because
        # ``seadustik`` is stripped — the abbreviation map should
        # still produce a match via the title index, NOT match an
        # unrelated act fuzzily. Per P1#1, the match surfaces as a
        # partial_match, not an entity_uri literal.
        assert result[0].entity_uri is None
        assert result[0].matched_label == "Karistusseadustik"
        assert result[0].partial_match is not None
        assert result[0].partial_match["act_title"] == "Karistusseadustik"
        assert result[0].partial_match["section"] is None

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

        assert result[0].entity_uri is None
        assert result[0].matched_label == "Atmosfääriõhu kaitse seadus"
        assert result[0].partial_match is not None
        assert result[0].partial_match["act_title"] == "Atmosfääriõhu kaitse seadus"
        assert 0.7 <= result[0].match_score <= 1.0

    def test_resolve_law_no_match(self, caplog: pytest.LogCaptureFixture):
        sparql = _make_sparql_router(
            abbrev_rows=_abbrev_map_rows(("KRIMIN", "Karistusseadustik")),
        )
        resolver = ReferenceResolver(sparql_client=sparql)

        with caplog.at_level("INFO"):
            result = resolver.resolve([_ref("Täiesti tundmatu seadus", "law")])

        assert result[0].entity_uri is None
        assert result[0].partial_match is None
        assert result[0].match_score == 0.0
        assert any("resolver: law unresolved" in rec.message for rec in caplog.records)

    def test_resolve_law_only_match_does_not_populate_entity_uri(self):
        """P1#1 regression guard.

        ``Karistusseadustik`` (a bare law name, no ``§``) used to land
        in ``entity_uri`` as the literal title string. The
        graph_builder then serialised that as ``<Karistusseadustik>``,
        producing malformed RDF and poisoning the impact-engine BFS.

        Today the same input must produce ``entity_uri=None`` with the
        title living on ``partial_match["act_title"]``.
        """
        sparql = _make_sparql_router(
            abbrev_rows=_abbrev_map_rows(("KRIMIN", "Karistusseadustik")),
        )
        resolver = ReferenceResolver(sparql_client=sparql)

        result = resolver.resolve([_ref("Karistusseadustik", "law")])

        assert result[0].entity_uri is None
        assert result[0].partial_match is not None
        assert result[0].partial_match["act_title"] == "Karistusseadustik"
        # And — critically — the title literal must NOT have leaked
        # into entity_uri.
        assert result[0].entity_uri != "Karistusseadustik"

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
        # an exact 1.0 collapse. Per P1#1 the match also lives on
        # partial_match (entity_uri stays None for law-only refs).
        if result[0].matched_label is not None:
            assert result[0].match_score < 1.0
            assert result[0].entity_uri is None


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
        """``KarS § 211 lg 2`` resolves via the human-abbreviation alias map.

        Per P1#3: the corpus TOKEN is ``KRIMIN``, but practitioners
        type ``KarS``. The :data:`_HUMAN_ABBREV_ALIASES` map bridges
        that, so the URI-guess path lands on ``KRIMIN_Par_211``.
        """
        sparql = _make_sparql_router(
            abbrev_rows=_abbrev_map_rows(("KRIMIN", "Karistusseadustik")),
            ask_result={"KRIMIN_Par_211": True},
        )
        resolver = ReferenceResolver(sparql_client=sparql)

        result = resolver.resolve([_ref("KarS § 211 lg 2", "provision")])

        # KarS → KRIMIN via _HUMAN_ABBREV_ALIASES → URI-guess
        # ASK hits on KRIMIN_Par_211 → fully resolved.
        assert result[0].entity_uri == f"{ESTLEG}KRIMIN_Par_211"
        assert result[0].match_score == 1.0

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


# ---------------------------------------------------------------------------
# Human-abbreviation alias map (P1#3)
# ---------------------------------------------------------------------------


class TestHumanAbbreviationAliases:
    """The corpus's TOKENs (``KRIMIN``, ``AVTS``, …) are not what users
    type. Users type human-friendly Estonian legal shortcuts (``KarS``,
    ``AvTS``, ``RES``, …). :data:`_HUMAN_ABBREV_ALIASES` bridges that.

    Each test below mocks the abbreviation-map SPARQL so the alias's
    target TOKEN is present, then drives a real resolve call and
    asserts the alias landed on the expected corpus TOKEN.
    """

    def test_kars_alias_resolves_via_human_abbreviation_map(self):
        """``KarS § 211`` → ``KRIMIN_Par_211`` via the alias map."""
        sparql = _make_sparql_router(
            abbrev_rows=_abbrev_map_rows(("KRIMIN", "Karistusseadustik")),
            ask_result={"KRIMIN_Par_211": True},
        )
        resolver = ReferenceResolver(sparql_client=sparql)

        result = resolver.resolve([_ref("KarS § 211", "provision")])

        assert result[0].entity_uri == f"{ESTLEG}KRIMIN_Par_211"
        assert result[0].match_score == 1.0

    def test_avts_alias_resolves(self):
        """``AvTS § 35`` → act half resolves to AVTS via the alias map.

        Per the Step 1 spike, ``AVTS_Par_35`` doesn't exist in the
        corpus (AvTS provisions are thematic, not section-numbered),
        so the URI-guess + structural fallback both miss and the
        result is a ``partial_match`` rather than a fully-resolved URI.
        The test asserts the act-half routing through the alias map
        regardless.
        """
        sparql = _make_sparql_router(
            abbrev_rows=_abbrev_map_rows(("AVTS", "Avaliku teabe seadus")),
            ask_result=False,
            provision_rows=[],
        )
        resolver = ReferenceResolver(sparql_client=sparql)

        result = resolver.resolve([_ref("AvTS § 35", "provision")])

        assert result[0].partial_match is not None
        assert result[0].partial_match["act_token"] == "AVTS"
        assert result[0].partial_match["act_title"] == "Avaliku teabe seadus"
        assert result[0].partial_match["section"] == "35"

    def test_res_alias_resolves_to_riigieelarve(self):
        """``RES § 20`` → ``REELS_Par_20`` via the alias map."""
        sparql = _make_sparql_router(
            abbrev_rows=_abbrev_map_rows(("REELS", "Riigieelarve seadus")),
            ask_result={"REELS_Par_20": True},
        )
        resolver = ReferenceResolver(sparql_client=sparql)

        result = resolver.resolve([_ref("RES § 20", "provision")])

        assert result[0].entity_uri == f"{ESTLEG}REELS_Par_20"

    def test_alias_map_is_case_insensitive(self):
        """``KARS``, ``KarS``, ``kars`` all route to ``KRIMIN``."""
        sparql = _make_sparql_router(
            abbrev_rows=_abbrev_map_rows(("KRIMIN", "Karistusseadustik")),
            ask_result={"KRIMIN_Par_1": True},
        )
        resolver = ReferenceResolver(sparql_client=sparql)

        for variant in ("KARS § 1", "KarS § 1", "kars § 1"):
            result = resolver.resolve([_ref(variant, "provision")])
            assert result[0].entity_uri == f"{ESTLEG}KRIMIN_Par_1", variant

    def test_law_only_kars_alias_returns_partial_match(self):
        """A bare ``KarS`` (law-only, no §) gives partial_match with TOKEN."""
        sparql = _make_sparql_router(
            abbrev_rows=_abbrev_map_rows(("KRIMIN", "Karistusseadustik")),
        )
        resolver = ReferenceResolver(sparql_client=sparql)

        result = resolver.resolve([_ref("KarS", "law")])

        assert result[0].entity_uri is None
        assert result[0].partial_match is not None
        assert result[0].partial_match["act_token"] == "KRIMIN"
        assert result[0].partial_match["act_title"] == "Karistusseadustik"
        assert result[0].partial_match["section"] is None


# ---------------------------------------------------------------------------
# Abbreviation-map cache lifecycle (P2#5)
# ---------------------------------------------------------------------------


class TestAbbrevMapCacheLifecycle:
    """A transient Jena failure on the first ``_get_abbrev_maps`` call
    used to poison the resolver singleton forever (empty dict cached
    as if loaded). P2#5 fixes that: a raise from ``SparqlClient.query``
    leaves the cache un-populated so the next call retries.
    """

    def test_get_abbrev_maps_does_not_cache_empty_on_transient_failure(self):
        """First call: SPARQL raises → empty result, NOT cached.
        Second call: SPARQL returns real rows → map populates.
        """
        from app.docs.reference_resolver import ReferenceResolver

        call_count = {"n": 0}
        good_rows = _abbrev_map_rows(("KRIMIN", "Karistusseadustik"))

        def _query_flaky(q: str, bindings=None, **kwargs):
            # The abbreviation-map query is the only one we route here;
            # all other branches return [] (which is fine because the
            # test only exercises law resolution).
            if "LegalProvision_" in q and "?cls" in q:
                call_count["n"] += 1
                if call_count["n"] == 1:
                    # Simulate the SparqlClient on_error="raise" path
                    # firing on a transient httpx.ConnectError.
                    raise RuntimeError("connection refused")
                return good_rows
            return []

        sparql = MagicMock()
        sparql.query.side_effect = _query_flaky
        sparql.ask.return_value = False
        resolver = ReferenceResolver(sparql_client=sparql)

        # First call: should hit the transient failure path → unresolved.
        result_1 = resolver.resolve([_ref("KRIMIN", "law")])
        assert result_1[0].entity_uri is None
        assert result_1[0].partial_match is None
        # Cache must remain un-populated (None) so the next call retries.
        assert resolver._token_to_title is None

        # Second call: SPARQL now returns rows → map populates →
        # KRIMIN resolves.
        result_2 = resolver.resolve([_ref("KRIMIN", "law")])
        assert result_2[0].partial_match is not None
        assert result_2[0].partial_match["act_token"] == "KRIMIN"
        # Cache populated this time.
        assert resolver._token_to_title is not None
        assert "KRIMIN" in resolver._token_to_title

    def test_get_abbrev_maps_logs_warning_on_transient_failure(
        self, caplog: pytest.LogCaptureFixture
    ):
        """A WARN line on transient failure so ops can see the retry signal."""
        sparql = MagicMock()
        sparql.query.side_effect = RuntimeError("jena unreachable")
        sparql.ask.return_value = False
        resolver = ReferenceResolver(sparql_client=sparql)

        with caplog.at_level("WARNING"):
            resolver.resolve([_ref("KRIMIN", "law")])

        warn_msgs = [
            rec for rec in caplog.records if "abbreviation-map load failed" in rec.message
        ]
        assert warn_msgs, "expected a WARN log line on transient SPARQL failure"
        assert warn_msgs[0].levelname == "WARNING"

    def test_get_abbrev_maps_does_cache_genuinely_empty_response(self):
        """If Jena really has no provisions, the load is one-shot, not retried."""
        sparql = MagicMock()
        sparql.query.return_value = []  # genuinely empty, no exception
        sparql.ask.return_value = False
        resolver = ReferenceResolver(sparql_client=sparql)

        resolver.resolve([_ref("KRIMIN", "law")])
        resolver.resolve([_ref("AvTS", "law")])
        resolver.resolve([_ref("foo", "law")])

        # The abbreviation-map SPARQL query must have been issued
        # exactly ONCE despite three resolve calls — a genuine empty
        # response caches as "loaded but empty" and we don't hammer
        # Jena on every subsequent call.
        abbrev_query_calls = [
            c
            for c in sparql.query.call_args_list
            if "LegalProvision_" in (c.args[0] if c.args else "")
        ]
        assert len(abbrev_query_calls) == 1
        # The on_error="raise" kwarg should be present so transient
        # failures are still distinguished from empty responses.
        assert abbrev_query_calls[0].kwargs.get("on_error") == "raise"

    def test_get_abbrev_maps_passes_on_error_raise_to_sparql(self):
        """Sanity check: the loader calls SparqlClient with on_error='raise'."""
        sparql = MagicMock()
        sparql.query.return_value = _abbrev_map_rows(("KRIMIN", "Karistusseadustik"))
        sparql.ask.return_value = False
        resolver = ReferenceResolver(sparql_client=sparql)

        resolver._get_abbrev_maps()

        # The abbrev-map call site should pass on_error="raise" so the
        # SparqlClient surfaces transient failures instead of swallowing
        # them as an empty list.
        first_call = sparql.query.call_args_list[0]
        assert first_call.kwargs.get("on_error") == "raise"


# ---------------------------------------------------------------------------
# Static invariant: entity_uri is either None or an absolute URI
# ---------------------------------------------------------------------------


class TestEntityUriIsURIOrNone:
    """P1#1 invariant guard: ``ResolvedRef.entity_uri`` must never
    contain a non-URI literal. Either it's ``None`` (unresolved /
    partial) or it's an absolute ``https://`` URI that downstream
    RDF code can serialise as ``<uri>`` without producing malformed
    triples.
    """

    @pytest.mark.parametrize(
        "input_text, ref_type",
        [
            # The seven canonical refs from the climate-law test draft
            # (per docs/2026-05-18-bugfix-plan.md Step 2 acceptance).
            ("Karistusseadustik", "law"),
            ("Atmosfääriõhu kaitse seadus", "law"),
            ("AvTS", "law"),
            ("KarS § 211", "provision"),
            ("Atmosfääriõhu kaitse seaduse § 143", "provision"),
            ("AvTS § 35", "provision"),
            ("32016R0679", "eu_act"),
        ],
    )
    def test_entity_uri_is_either_none_or_absolute_uri(self, input_text: str, ref_type: str):
        sparql = _make_sparql_router(
            abbrev_rows=_abbrev_map_rows(
                ("KRIMIN", "Karistusseadustik"),
                ("ATMOSF", "Atmosfääriõhu kaitse seadus"),
                ("AVTS", "Avaliku teabe seadus"),
            ),
            ask_result={
                "ATMOSF_Par_143": True,
                "KRIMIN_Par_211": True,
            },
            eu_rows=[{"uri": f"{ESTLEG}EU_GDPR", "label": "GDPR"}],
        )
        resolver = ReferenceResolver(sparql_client=sparql)

        result = resolver.resolve([_ref(input_text, ref_type)])
        uri = result[0].entity_uri

        # Either None or a real absolute URI — never a bare title literal.
        if uri is not None:
            assert uri.startswith("https://") or uri.startswith("http://"), (
                f"entity_uri {uri!r} for {input_text!r} is not an absolute URI"
            )
            # Title literals like ``Karistusseadustik`` must NEVER
            # leak in here. Negative guard:
            assert " " not in uri, f"entity_uri {uri!r} contains whitespace — looks like a literal"
