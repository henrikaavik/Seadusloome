"""Tests for the current-law temporal scope across Analüüsikeskus engines (C5, #850).

Covers:

1. :mod:`app.ontology.temporal_scope` — the scope model
   (:class:`TemporalScope`, :func:`scope_from_param`), the clause
   builders, and the **silent-no-op guard**: the emitted ``FILTER NOT
   EXISTS`` references *only* the positively-populated predicates from the
   audit (``temporalStatus`` / ``repealDate``), never the deferred
   sample-only version-chain predicates.

2. The filter **bites real rdflib data** — a hand-built graph with a
   current provision, a provision whose owning act is positively repealed
   (both the literal-title prod shape and the URI fixture shape), a
   provision marked repealed on itself, and a provision with **no**
   temporal data. Default (current) scope drops only the positively-marked
   ones; the no-data provision survives (positive-knowledge exclusion);
   ``TemporalScope.ALL`` keeps everything.

3. Per-engine regression — burden / sanctions / competency / court-practice
   each honour the scope end-to-end against the same fixture graph through
   the real :meth:`SparqlClient.query` VALUES-injection path.

4. Scope-form round-trip — ``?oigus=`` is reflected in the rendered select
   and rides through the workflow links.

The rdflib ``SparqlClient`` fake mirrors the production
:meth:`SparqlClient.query` (VALUES / URI-VALUES injection then execute
against an in-memory :class:`rdflib.Graph`), the same shape used by
``test_analyysikeskus_competency.py``.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

from rdflib import Graph

from app.ontology.queries import PREFIXES
from app.ontology.sparql_client import SparqlClient
from app.ontology.temporal_scope import (
    DEFAULT_SCOPE,
    POSITIVE_REPEAL_PREDICATES,
    REPEALED_STATUS_VALUE,
    TemporalScope,
    current_law_filter,
    scope_from_param,
    temporal_scope_clause,
)

_NS = "https://data.riik.ee/ontology/estleg#"


# ---------------------------------------------------------------------------
# A graph that exercises every repeal-marker shape the filter must catch.
# ---------------------------------------------------------------------------
#
# Acts:
#   ActLive   — temporalStatus "in_force"            → NOT repealed
#   ActDead   — temporalStatus "repealed"            → repealed (literal-title hop)
#   ActDateRepealed — repealDate present              → repealed (literal-title hop)
#   ActUriDead — temporalStatus "repealed", reached via a URI sourceAct
#
# Provisions (all carry the engine predicates so one graph drives all four):
#   P_live      — in ActLive                          → kept under CURRENT
#   P_statusRep — in ActDead (literal title)          → dropped under CURRENT
#   P_dateRep   — in ActDateRepealed (literal title)  → dropped under CURRENT
#   P_uriRep    — sourceAct → ActUriDead (URI)        → dropped under CURRENT
#   P_selfRep   — temporalStatus "repealed" on itself → dropped under CURRENT
#   P_nodata    — sourceAct "Unknown act", no markers → kept under CURRENT
_GRAPH_TTL = f"""
@prefix estleg: <{_NS}> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .

# --- Acts (carry the temporal markers) ---
estleg:ActLive a estleg:Act ; rdfs:label "Live act" ;
    estleg:temporalStatus "in_force" .
estleg:ActDead a estleg:Act ; rdfs:label "Dead act" ;
    estleg:temporalStatus "repealed" .
estleg:ActDateRepealed a estleg:Act ; rdfs:label "Date-repealed act" ;
    estleg:repealDate "2019-12-31"^^xsd:date .
estleg:ActUriDead a estleg:Act ; rdfs:label "Uri-dead act" ;
    estleg:temporalStatus "repealed" .

# --- Provisions ---
# Current provision (live act, literal-title shape)
estleg:P_live a estleg:LegalProvision ; rdfs:label "Live provision" ;
    estleg:sourceAct "Live act" ;
    estleg:normativeType estleg:NormType_Obligation ;
    estleg:dutyHolder "Tööandja" ;
    estleg:competentAuthority estleg:Inst_A ;
    estleg:hasSanction estleg:San_live ;
    estleg:interpretedBy estleg:Dec_live .

# Provision whose act is positively repealed via temporalStatus (literal title)
estleg:P_statusRep a estleg:LegalProvision ; rdfs:label "Status-repealed provision" ;
    estleg:sourceAct "Dead act" ;
    estleg:normativeType estleg:NormType_Prohibition ;
    estleg:competentAuthority estleg:Inst_A ;
    estleg:hasSanction estleg:San_statusRep ;
    estleg:interpretedBy estleg:Dec_statusRep .

# Provision whose act is positively repealed via repealDate (literal title)
estleg:P_dateRep a estleg:LegalProvision ; rdfs:label "Date-repealed provision" ;
    estleg:sourceAct "Date-repealed act" ;
    estleg:normativeType estleg:NormType_Obligation ;
    estleg:competentAuthority estleg:Inst_A ;
    estleg:hasSanction estleg:San_dateRep ;
    estleg:interpretedBy estleg:Dec_dateRep .

# Provision whose act is repealed, reached through a URI sourceAct (fixture shape)
estleg:P_uriRep a estleg:LegalProvision ; rdfs:label "Uri-repealed provision" ;
    estleg:sourceAct estleg:ActUriDead ;
    estleg:normativeType estleg:NormType_Right ;
    estleg:competentAuthority estleg:Inst_A ;
    estleg:hasSanction estleg:San_uriRep ;
    estleg:interpretedBy estleg:Dec_uriRep .

# Provision marked repealed directly on itself
estleg:P_selfRep a estleg:LegalProvision ; rdfs:label "Self-repealed provision" ;
    estleg:sourceAct "Live act" ;
    estleg:temporalStatus "repealed" ;
    estleg:normativeType estleg:NormType_Obligation ;
    estleg:competentAuthority estleg:Inst_A ;
    estleg:hasSanction estleg:San_selfRep ;
    estleg:interpretedBy estleg:Dec_selfRep .

# Provision with NO temporal data at all (positive-knowledge ⇒ kept)
estleg:P_nodata a estleg:LegalProvision ; rdfs:label "No-data provision" ;
    estleg:sourceAct "Unknown act" ;
    estleg:normativeType estleg:NormType_Permission ;
    estleg:competentAuthority estleg:Inst_A ;
    estleg:hasSanction estleg:San_nodata ;
    estleg:interpretedBy estleg:Dec_nodata .

# --- NormativeType individuals ---
estleg:NormType_Obligation a estleg:NormativeType ; rdfs:label "Kohustus" .
estleg:NormType_Right a estleg:NormativeType ; rdfs:label "Õigus" .
estleg:NormType_Permission a estleg:NormativeType ; rdfs:label "Luba" .
estleg:NormType_Prohibition a estleg:NormativeType ; rdfs:label "Keeld" .

# --- Sanctions (one per provision; all type "fine" with an overlapping range) ---
estleg:San_live a estleg:Sanction ; estleg:sanctionType "fine" ;
    estleg:minPenaltyAmount "100"^^xsd:decimal ; estleg:maxPenaltyAmount "500"^^xsd:decimal .
estleg:San_statusRep a estleg:Sanction ; estleg:sanctionType "fine" ;
    estleg:minPenaltyAmount "100"^^xsd:decimal ; estleg:maxPenaltyAmount "500"^^xsd:decimal .
estleg:San_dateRep a estleg:Sanction ; estleg:sanctionType "fine" ;
    estleg:minPenaltyAmount "100"^^xsd:decimal ; estleg:maxPenaltyAmount "500"^^xsd:decimal .
estleg:San_uriRep a estleg:Sanction ; estleg:sanctionType "fine" ;
    estleg:minPenaltyAmount "100"^^xsd:decimal ; estleg:maxPenaltyAmount "500"^^xsd:decimal .
estleg:San_selfRep a estleg:Sanction ; estleg:sanctionType "fine" ;
    estleg:minPenaltyAmount "100"^^xsd:decimal ; estleg:maxPenaltyAmount "500"^^xsd:decimal .
estleg:San_nodata a estleg:Sanction ; estleg:sanctionType "fine" ;
    estleg:minPenaltyAmount "100"^^xsd:decimal ; estleg:maxPenaltyAmount "500"^^xsd:decimal .

# --- Institutions (competency) ---
estleg:Inst_A a estleg:Institution ; rdfs:label "Asutus A" .

# --- Court decisions (one per provision via interpretedBy above) ---
estleg:Dec_live a estleg:CourtDecision ; rdfs:label "Dec live" ;
    estleg:caseNumber "3-1-1-1-20" ; estleg:decisionDate "2020-01-01" .
estleg:Dec_statusRep a estleg:CourtDecision ; rdfs:label "Dec status-rep" ;
    estleg:caseNumber "3-1-1-2-20" ; estleg:decisionDate "2020-02-01" .
estleg:Dec_dateRep a estleg:CourtDecision ; rdfs:label "Dec date-rep" ;
    estleg:caseNumber "3-1-1-3-20" ; estleg:decisionDate "2020-03-01" .
estleg:Dec_uriRep a estleg:CourtDecision ; rdfs:label "Dec uri-rep" ;
    estleg:caseNumber "3-1-1-4-20" ; estleg:decisionDate "2020-04-01" .
estleg:Dec_selfRep a estleg:CourtDecision ; rdfs:label "Dec self-rep" ;
    estleg:caseNumber "3-1-1-5-20" ; estleg:decisionDate "2020-05-01" .
estleg:Dec_nodata a estleg:CourtDecision ; rdfs:label "Dec nodata" ;
    estleg:caseNumber "3-1-1-6-20" ; estleg:decisionDate "2020-06-01" .
"""

# The provisions that must be EXCLUDED under the default current-law scope.
_REPEALED_PROVISIONS = {
    f"{_NS}P_statusRep",
    f"{_NS}P_dateRep",
    f"{_NS}P_uriRep",
    f"{_NS}P_selfRep",
}
# The provisions that must SURVIVE under the default current-law scope.
_CURRENT_PROVISIONS = {
    f"{_NS}P_live",
    f"{_NS}P_nodata",
}
_ALL_PROVISIONS = _CURRENT_PROVISIONS | _REPEALED_PROVISIONS


def _make_sparql_against(ttl: str) -> SparqlClient:
    """Return a SparqlClient whose ``query`` runs SPARQL against *ttl* via rdflib.

    Mirrors :meth:`SparqlClient.query` — applies the VALUES / URI-VALUES
    injectors then executes against an in-memory graph — so the test
    exercises the real injection path (the nested-brace ``FILTER NOT
    EXISTS`` interaction with the VALUES splice).
    """
    graph = Graph()
    graph.parse(data=ttl, format="turtle")

    client = SparqlClient.__new__(SparqlClient)
    client.jena_url = "http://localhost:3030"  # type: ignore[attr-defined]
    client.dataset = "ontology"  # type: ignore[attr-defined]
    client.timeout = 5.0  # type: ignore[attr-defined]

    def _query(
        sparql: str,
        bindings: dict[str, str] | None = None,
        uri_bindings: dict[str, str] | None = None,
        **_kw: Any,
    ) -> list[dict[str, str]]:
        text = sparql
        if bindings:
            text = client._inject_bindings(text, bindings)
        if uri_bindings:
            text = client._inject_uri_bindings(text, uri_bindings)
        rows: list[dict[str, str]] = []
        for row in graph.query(text):
            d: dict[str, str] = {}
            for var in row.labels:  # type: ignore[attr-defined,union-attr]
                value = row[var]  # type: ignore[index]
                d[str(var)] = str(value) if value is not None else ""
            rows.append(d)
        return rows

    client.query = MagicMock(side_effect=_query)  # type: ignore[assignment]
    return client


def _select_provisions(graph: Graph, scope: TemporalScope) -> set[str]:
    """Run a bare provision SELECT with the scope clause; return the URIs kept."""
    q = (
        PREFIXES
        + "SELECT ?provision WHERE {\n  ?provision a estleg:LegalProvision .\n"
        + temporal_scope_clause(scope, "provision")
        + "\n}"
    )
    return {str(r[0]) for r in graph.query(q)}  # type: ignore[index]


# ===========================================================================
# 1. Scope model — TemporalScope + scope_from_param
# ===========================================================================


class TestScopeModel:
    def test_default_scope_is_current(self):
        assert DEFAULT_SCOPE is TemporalScope.CURRENT

    def test_scope_from_param_canonical(self):
        assert scope_from_param("current") is TemporalScope.CURRENT
        assert scope_from_param("all") is TemporalScope.ALL

    def test_scope_from_param_blank_and_unknown_default_to_current(self):
        assert scope_from_param(None) is TemporalScope.CURRENT
        assert scope_from_param("") is TemporalScope.CURRENT
        assert scope_from_param("   ") is TemporalScope.CURRENT
        assert scope_from_param("garbage") is TemporalScope.CURRENT

    def test_scope_from_param_history_aliases(self):
        for alias in ("kogu_ajalugu", "ajalugu", "current_plus_history", "all_history"):
            assert scope_from_param(alias) is TemporalScope.ALL, alias

    def test_scope_from_param_is_case_insensitive(self):
        assert scope_from_param("ALL") is TemporalScope.ALL
        assert scope_from_param("Current") is TemporalScope.CURRENT


# ===========================================================================
# 2. The silent-no-op guard — clause references ONLY audited predicates
# ===========================================================================


class TestClauseReferencesOnlyAuditedPredicates:
    """The core anti-pitfall test: prove the filter cannot silently no-op.

    The whole design hinges on building the exclusion over the
    *positively-populated* predicates (``temporalStatus`` / ``repealDate``)
    and NOT the deferred sample-only version-chain predicates. If a future
    edit swaps in ``versionValidFrom`` / ``supersededByVersion`` etc. the
    filter would pass everything in production while staying green on
    synthetic fixtures — exactly the trap #850's comment warns about.
    """

    def test_clause_contains_every_audited_predicate(self):
        clause = current_law_filter("provision")
        for pred in POSITIVE_REPEAL_PREDICATES:
            assert pred in clause, f"audited predicate {pred} missing from clause"

    def test_clause_references_no_deferred_version_chain_predicate(self):
        clause = current_law_filter("provision")
        for deferred in (
            "versionValidFrom",
            "versionValidTo",
            "supersededByVersion",
            "versionText",
            "previousVersion",
            "ProvisionVersion",
        ):
            assert deferred not in clause, f"deferred predicate {deferred} leaked into clause"

    def test_clause_only_estleg_predicates_are_the_audited_two(self):
        """No estleg predicate other than the audited two + structural joins.

        The clause is allowed to use ``estleg:sourceAct`` (the act
        membership hop) and ``rdfs:label`` (the literal-title → Act-node
        bridge), but the only ``estleg:`` *repeal-marker* predicates must
        be the two audited ones. We assert the full estleg URIs of the
        audited predicates are present and that the deferred ones are not
        (covered above) — together these pin the clause to the audit.
        """
        clause = current_law_filter("provision")
        assert f"{_NS}temporalStatus" in clause
        assert f"{_NS}repealDate" in clause
        # The repeal value is the explicit "repealed" status, never a
        # false-positive on "in_force" / "pending".
        assert f'"{REPEALED_STATUS_VALUE}"' in clause
        assert '"in_force"' not in clause
        assert '"pending"' not in clause

    def test_all_scope_emits_empty_clause(self):
        assert temporal_scope_clause(TemporalScope.ALL, "provision") == ""

    def test_current_scope_emits_filter_not_exists(self):
        clause = temporal_scope_clause(TemporalScope.CURRENT, "provision")
        assert "FILTER NOT EXISTS" in clause

    def test_clause_respects_custom_provision_var(self):
        clause = current_law_filter("prov")
        assert "?prov " in clause or "?prov\n" in clause

    def test_literal_title_join_is_str_coerced(self):
        """The label↔sourceAct literal join must compare lexical forms.

        Review follow-up (#850): the literal-title hop binds the
        ``rdfs:label`` and ``estleg:sourceAct`` literals to *distinct*
        variables and joins them with ``STR(...) = STR(...)`` rather than
        re-using one variable. A shared variable forces RDF-term equality
        (including the language tag), which would silently no-op the hop
        the moment labels become ``"…"@et`` while ``sourceAct`` stays a
        plain literal. This guards against a refactor reverting to that.
        """
        clause = current_law_filter("provision")
        # Both label and sourceAct are STR()-coerced in the join filter.
        assert "STR(?_tsLabel_provision) = STR(?_tsSourceAct_provision)" in clause
        assert "STR(?_tsLabel_provisionb) = STR(?_tsSourceAct_provisionb)" in clause


# ===========================================================================
# 3. The filter bites real rdflib data
# ===========================================================================


class TestFilterBitesRealData:
    def test_current_scope_excludes_only_positively_repealed(self):
        g = Graph()
        g.parse(data=_GRAPH_TTL, format="turtle")
        kept = _select_provisions(g, TemporalScope.CURRENT)
        assert kept == _CURRENT_PROVISIONS, (
            f"current scope must keep exactly the live + no-data provisions; got {sorted(kept)}"
        )

    def test_no_data_provision_is_included_under_current(self):
        """Positive-knowledge exclusion: absent temporal data ⇒ kept."""
        g = Graph()
        g.parse(data=_GRAPH_TTL, format="turtle")
        kept = _select_provisions(g, TemporalScope.CURRENT)
        assert f"{_NS}P_nodata" in kept

    def test_all_scope_includes_everything(self):
        g = Graph()
        g.parse(data=_GRAPH_TTL, format="turtle")
        kept = _select_provisions(g, TemporalScope.ALL)
        assert kept == _ALL_PROVISIONS

    def test_each_repeal_shape_is_excluded(self):
        """Every repeal-marker shape (status/date, literal/URI, self) bites."""
        g = Graph()
        g.parse(data=_GRAPH_TTL, format="turtle")
        kept = _select_provisions(g, TemporalScope.CURRENT)
        for repealed in _REPEALED_PROVISIONS:
            assert repealed not in kept, f"{repealed} should be excluded under current"

    def test_language_tagged_act_label_still_excludes(self):
        """Review follow-up (#850): a future ``"…"@et`` act label must not no-op.

        The literal-title hop joins the act's ``rdfs:label`` to the
        provision's plain ``estleg:sourceAct`` literal. If the join used a
        shared variable (RDF-term equality), an ``@et`` tag on the label
        would stop matching and a repealed act's provisions would silently
        leak back into the current-law scope. The ``STR()``-coerced join
        keeps comparing lexical forms, so the repealed act's provision is
        still excluded — and the live act's provision is still kept.
        """
        ttl = f"""
        @prefix estleg: <{_NS}> .
        @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
        @prefix xsd: <http://www.w3.org/2001/XMLSchema#> .

        # Repealed act with an @et-tagged label; provision sourceAct is PLAIN.
        estleg:ActDeadEt a estleg:Act ; rdfs:label "Kehtetu seadus"@et ;
            estleg:temporalStatus "repealed" .
        estleg:P_taggedRep a estleg:LegalProvision ; rdfs:label "prov rep" ;
            estleg:sourceAct "Kehtetu seadus" .

        # Repealed-by-date act, @et label, plain sourceAct.
        estleg:ActDeadDateEt a estleg:Act ; rdfs:label "Aegunud seadus"@et ;
            estleg:repealDate "2018-06-30"^^xsd:date .
        estleg:P_taggedDateRep a estleg:LegalProvision ; rdfs:label "prov date rep" ;
            estleg:sourceAct "Aegunud seadus" .

        # Live act with @et label, plain sourceAct (must stay included).
        estleg:ActLiveEt a estleg:Act ; rdfs:label "Elav seadus"@et ;
            estleg:temporalStatus "in_force" .
        estleg:P_taggedLive a estleg:LegalProvision ; rdfs:label "prov live" ;
            estleg:sourceAct "Elav seadus" .
        """
        g = Graph()
        g.parse(data=ttl, format="turtle")

        kept = _select_provisions(g, TemporalScope.CURRENT)
        assert f"{_NS}P_taggedRep" not in kept, (
            "repealed act with @et label must still be excluded under current scope"
        )
        assert f"{_NS}P_taggedDateRep" not in kept, (
            "date-repealed act with @et label must still be excluded under current scope"
        )
        assert f"{_NS}P_taggedLive" in kept, "live act with @et label must be kept"

        # Under all-history scope every provision survives.
        kept_all = _select_provisions(g, TemporalScope.ALL)
        assert kept_all == {
            f"{_NS}P_taggedRep",
            f"{_NS}P_taggedDateRep",
            f"{_NS}P_taggedLive",
        }


# ===========================================================================
# 4. Per-engine regression — all four engines honour the scope
# ===========================================================================


class TestBurdenEngineScope:
    def test_act_default_excludes_repealed_act_provisions(self):
        from app.analyysikeskus.burden import list_burden_for_act

        client = _make_sparql_against(_GRAPH_TTL)
        # "Dead act" is positively repealed → current scope yields nothing.
        cur = list_burden_for_act("Dead act", scope=TemporalScope.CURRENT, sparql_client=client)
        allh = list_burden_for_act("Dead act", scope=TemporalScope.ALL, sparql_client=client)
        assert cur.total == 0
        assert allh.total == 1

    def test_act_live_act_counted_under_both_scopes(self):
        from app.analyysikeskus.burden import list_burden_for_act

        client = _make_sparql_against(_GRAPH_TTL)
        # "Live act" hosts P_live (current) + P_selfRep (self-repealed).
        cur = list_burden_for_act("Live act", scope=TemporalScope.CURRENT, sparql_client=client)
        allh = list_burden_for_act("Live act", scope=TemporalScope.ALL, sparql_client=client)
        assert cur.total == 1  # only P_live; P_selfRep excluded
        assert allh.total == 2  # P_live + P_selfRep

    def test_provision_self_repealed_dropped_under_current(self):
        from app.analyysikeskus.burden import list_burden_for_provision

        client = _make_sparql_against(_GRAPH_TTL)
        cur = list_burden_for_provision(
            f"{_NS}P_selfRep", scope=TemporalScope.CURRENT, sparql_client=client
        )
        allh = list_burden_for_provision(
            f"{_NS}P_selfRep", scope=TemporalScope.ALL, sparql_client=client
        )
        assert cur.total == 0
        assert allh.total == 1


class TestSanctionsEngineScope:
    def test_act_default_excludes_repealed_act_sanctions(self):
        from app.analyysikeskus.sanctions import list_sanctions_for_act

        client = _make_sparql_against(_GRAPH_TTL)
        cur = list_sanctions_for_act(
            "Date-repealed act", scope=TemporalScope.CURRENT, sparql_client=client
        )
        allh = list_sanctions_for_act(
            "Date-repealed act", scope=TemporalScope.ALL, sparql_client=client
        )
        assert len(cur) == 0
        assert len(allh) == 1

    def test_provision_uri_repealed_act_dropped_under_current(self):
        from app.analyysikeskus.sanctions import list_sanctions_for_provision

        client = _make_sparql_against(_GRAPH_TTL)
        # P_uriRep's act (URI shape) is repealed.
        cur = list_sanctions_for_provision(
            f"{_NS}P_uriRep", scope=TemporalScope.CURRENT, sparql_client=client
        )
        allh = list_sanctions_for_provision(
            f"{_NS}P_uriRep", scope=TemporalScope.ALL, sparql_client=client
        )
        assert len(cur) == 0
        assert len(allh) == 1

    def test_similar_sanctions_excludes_repealed_acts_under_current(self):
        from app.analyysikeskus.sanctions import SanctionRow, find_similar_sanctions

        client = _make_sparql_against(_GRAPH_TTL)
        seed = SanctionRow(
            sanction_type="fine", act_label="Seed act", min_amount=100.0, max_amount=500.0
        )
        cur = find_similar_sanctions(
            seed, scope=TemporalScope.CURRENT, sparql_client=client, limit=50
        )
        allh = find_similar_sanctions(
            seed, scope=TemporalScope.ALL, sparql_client=client, limit=50
        )
        # Under current, only live + no-data sanctions are comparable (2);
        # under all, every "fine" sanction is comparable (6).
        assert len(cur) == 2
        assert len(allh) == 6


class TestCompetencyEngineScope:
    def test_competences_default_excludes_repealed_act_powers(self):
        from app.analyysikeskus.competency import list_competences_for_institution

        client = _make_sparql_against(_GRAPH_TTL)
        cur = list_competences_for_institution(
            f"{_NS}Inst_A", scope=TemporalScope.CURRENT, sparql_client=client
        )
        allh = list_competences_for_institution(
            f"{_NS}Inst_A", scope=TemporalScope.ALL, sparql_client=client
        )
        cur_uris = {r.provision_uri for r in cur}
        all_uris = {r.provision_uri for r in allh}
        assert cur_uris == _CURRENT_PROVISIONS
        assert all_uris == _ALL_PROVISIONS

    def test_gather_threads_scope(self):
        from app.analyysikeskus.competency import gather_institution_competences

        client = _make_sparql_against(_GRAPH_TTL)
        cur = gather_institution_competences(
            f"{_NS}Inst_A", scope=TemporalScope.CURRENT, sparql_client=client
        )
        allh = gather_institution_competences(
            f"{_NS}Inst_A", scope=TemporalScope.ALL, sparql_client=client
        )
        assert cur.total_count == len(_CURRENT_PROVISIONS)
        assert allh.total_count == len(_ALL_PROVISIONS)


class TestCourtPracticeEngineScope:
    def test_provision_default_excludes_repealed_act_practice(self):
        from app.analyysikeskus.court_practice import list_decisions_for_provision

        client = _make_sparql_against(_GRAPH_TTL)
        # P_dateRep's act is repealed → no current court practice.
        cur = list_decisions_for_provision(
            f"{_NS}P_dateRep", scope=TemporalScope.CURRENT, sparql_client=client
        )
        allh = list_decisions_for_provision(
            f"{_NS}P_dateRep", scope=TemporalScope.ALL, sparql_client=client
        )
        assert len(cur) == 0
        assert len(allh) == 1

    def test_provision_live_act_practice_kept_under_both(self):
        from app.analyysikeskus.court_practice import list_decisions_for_provision

        client = _make_sparql_against(_GRAPH_TTL)
        cur = list_decisions_for_provision(
            f"{_NS}P_live", scope=TemporalScope.CURRENT, sparql_client=client
        )
        allh = list_decisions_for_provision(
            f"{_NS}P_live", scope=TemporalScope.ALL, sparql_client=client
        )
        assert len(cur) == 1
        assert len(allh) == 1


# ===========================================================================
# 5. _Scope.temporal_scope mapping
# ===========================================================================


class TestRouteScopeMapping:
    def _scope(self, params: dict[str, str]):
        from app.analyysikeskus.routes import _Scope

        return _Scope(params)

    def test_default_scope_is_current(self):
        s = self._scope({})
        assert s.oigus == "current"
        assert s.temporal_scope is TemporalScope.CURRENT

    def test_oigus_all_maps_to_all(self):
        s = self._scope({"oigus": "all"})
        assert s.oigus == "all"
        assert s.temporal_scope is TemporalScope.ALL

    def test_legacy_alias_canonicalised(self):
        s = self._scope({"oigus": "kogu_ajalugu"})
        # Canonicalised to the "all" token for clean round-tripping.
        assert s.oigus == "all"
        assert s.temporal_scope is TemporalScope.ALL

    def test_all_scope_carried_in_query_pairs(self):
        s = self._scope({"oigus": "all"})
        pairs = dict(s.query_pairs("Karistusseadustik"))
        assert pairs.get("oigus") == "all"

    def test_current_scope_not_carried_in_query_pairs(self):
        """Default current is the implicit state — keep links clean."""
        s = self._scope({})
        pairs = dict(s.query_pairs("Karistusseadustik"))
        assert "oigus" not in pairs


# ===========================================================================
# 6. Scope-form round-trip through a live endpoint
# ===========================================================================


def _authed_user() -> dict[str, Any]:
    return {
        "id": "33333333-3333-3333-3333-333333333333",
        "email": "kasutaja@seadusloome.ee",
        "full_name": "Test Kasutaja",
        "role": "drafter",
        "org_id": "11111111-1111-1111-1111-111111111111",
    }


def _stub_provider() -> MagicMock:
    provider = MagicMock()
    provider.get_current_user.return_value = _authed_user()
    return provider


def _authed_client():
    from starlette.testclient import TestClient

    client = TestClient(
        __import__("app.main", fromlist=["app"]).app,
        follow_redirects=False,
        raise_server_exceptions=True,
    )
    client.cookies.set("access_token", "stub-token")
    return client


def _resolved_kars_law_ref():
    from app.docs.entity_extractor import ExtractedRef
    from app.docs.reference_resolver import ResolvedRef

    return ResolvedRef(
        extracted=ExtractedRef(
            ref_text="KarS",
            ref_type="law",
            confidence=1.0,
            location={"source": "analyysikeskus_input"},
        ),
        entity_uri=None,
        matched_label="Karistusseadustik",
        match_score=1.0,
        partial_match={"act_token": "KARS", "act_title": "Karistusseadustik", "section": None},
    )


class TestScopeFormRoundTrip:
    @patch("app.docs.reference_resolver.ReferenceResolver.resolve")
    @patch("app.auth.middleware._get_provider")
    def test_sanctions_result_form_offers_both_scopes_default_current(
        self,
        mock_provider: MagicMock,
        mock_resolve: MagicMock,
    ):
        """The result-page scope form offers both options; default = current."""
        mock_provider.return_value = _stub_provider()
        mock_resolve.return_value = [_resolved_kars_law_ref()]
        with patch(
            "app.analyysikeskus.routes._sanktsioonid.list_sanctions_for_act", return_value=[]
        ):
            client = _authed_client()
            resp = client.get("/analyysikeskus/sanktsioonid?sisend=KarS")
        assert resp.status_code == 200
        body = resp.text
        # Both scope options are offered in legal language.
        assert "Kehtiv õigus" in body
        assert "Kogu ajalugu" in body
        # Default reflects current (the select option for current is chosen).
        assert 'value="current" selected' in body or 'value="current"  selected' in body

    @patch("app.docs.reference_resolver.ReferenceResolver.resolve")
    @patch("app.auth.middleware._get_provider")
    def test_sanctions_result_form_reflects_oigus_all(
        self,
        mock_provider: MagicMock,
        mock_resolve: MagicMock,
    ):
        """A ``?oigus=all`` request renders the select with the all option chosen."""
        from app.docs.entity_extractor import ExtractedRef
        from app.docs.reference_resolver import ResolvedRef

        mock_provider.return_value = _stub_provider()
        mock_resolve.return_value = [
            ResolvedRef(
                extracted=ExtractedRef(
                    ref_text="KarS",
                    ref_type="law",
                    confidence=1.0,
                    location={"source": "analyysikeskus_input"},
                ),
                entity_uri=None,
                matched_label="Karistusseadustik",
                match_score=1.0,
                partial_match={
                    "act_token": "KARS",
                    "act_title": "Karistusseadustik",
                    "section": None,
                },
            )
        ]
        with patch(
            "app.analyysikeskus.routes._sanktsioonid.list_sanctions_for_act", return_value=[]
        ):
            client = _authed_client()
            resp = client.get(
                "/analyysikeskus/sanktsioonid?sisend=KarS&oigus=all&ulatus_submitted=1"
            )
        assert resp.status_code == 200
        body = resp.text
        # The select reflects the URL state: the "all" option is marked selected.
        assert 'value="all" selected' in body or 'value="all"  selected' in body

    @patch("app.docs.reference_resolver.ReferenceResolver.resolve")
    @patch("app.auth.middleware._get_provider")
    def test_sanctions_result_passes_all_scope_to_engine(
        self,
        mock_provider: MagicMock,
        mock_resolve: MagicMock,
    ):
        """``?oigus=all`` reaches the engine as TemporalScope.ALL."""
        from app.docs.entity_extractor import ExtractedRef
        from app.docs.reference_resolver import ResolvedRef

        mock_provider.return_value = _stub_provider()
        mock_resolve.return_value = [
            ResolvedRef(
                extracted=ExtractedRef(
                    ref_text="KarS",
                    ref_type="law",
                    confidence=1.0,
                    location={"source": "analyysikeskus_input"},
                ),
                entity_uri=None,
                matched_label="Karistusseadustik",
                match_score=1.0,
                partial_match={
                    "act_token": "KARS",
                    "act_title": "Karistusseadustik",
                    "section": None,
                },
            )
        ]
        with patch(
            "app.analyysikeskus.routes._sanktsioonid.list_sanctions_for_act", return_value=[]
        ) as mock_act:
            client = _authed_client()
            resp = client.get(
                "/analyysikeskus/sanktsioonid?sisend=KarS&oigus=all&ulatus_submitted=1"
            )
            assert resp.status_code == 200
        mock_act.assert_called_once_with("Karistusseadustik", scope=TemporalScope.ALL)
