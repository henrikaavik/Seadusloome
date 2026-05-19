"""Tests for the Analüüsikeskus routes (#714 — #720 directory, #721 result shell,
#722 Normi mõjuahel).

Follows the auth-mocking pattern from ``tests/test_chat_routes.py``: the
``app.main.app`` is exercised end-to-end via ``TestClient`` so the
FastHTML wiring + the ``auth_before`` Beforeware are validated; the
DB-touching ``_get_recent_analyses`` helper is patched out.

The #722 tests mock the SPARQL/Jena layer entirely: ``put_named_graph`` /
``delete_named_graph`` (the ephemeral-graph transport),
``ImpactAnalyzer.analyze`` (canned :class:`ImpactFindings`),
``ReferenceResolver.resolve`` (canned :class:`ResolvedRef`), the
owned-draft-report lookup, and the RAG retriever — all patched
*where used* (the patch-path contract).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch


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


def _authed_client(*, raise_server_exceptions: bool = True):
    from starlette.testclient import TestClient

    client = TestClient(
        __import__("app.main", fromlist=["app"]).app,
        follow_redirects=False,
        raise_server_exceptions=raise_server_exceptions,
    )
    client.cookies.set("access_token", "stub-token")
    return client


# ---------------------------------------------------------------------------
# #722 — shared fixtures: canned ImpactFindings + ResolvedRef
# ---------------------------------------------------------------------------

# The ``estleg:`` URI deliberately contains a ``#`` so the test can
# assert ``explorer_focus_url`` percent-encodes it to ``%23`` in the
# result-page links.
_AVTS_URI = "https://data.riik.ee/ontology/estleg#AvTS-p35"
_OTHER_DRAFT_URI = (
    "https://data.riik.ee/ontology/estleg/drafts/abcdef01-2345-6789-abcd-ef0123456789"
)
_COURT_URI = "https://data.riik.ee/ontology/estleg#RKHKo-3-1-1-63-15"
_EU_ACT_URI = "https://data.riik.ee/ontology/estleg#EU-32016R0679"


def _canned_findings():
    from app.docs.impact.analyzer import ImpactFindings

    affected = [
        {
            "uri": _AVTS_URI,
            "label": "AvTS § 35",
            "type": "https://data.riik.ee/ontology/estleg#Provision",
        },
        {
            "uri": _OTHER_DRAFT_URI,
            "label": "Teine eelnõu",
            "type": "https://data.riik.ee/ontology/estleg#DraftLegislation",
        },
        {
            "uri": _COURT_URI,
            "label": "RKHKo 3-1-1-63-15",
            "type": "https://data.riik.ee/ontology/estleg#CourtDecision",
        },
    ]
    conflicts = [
        {
            "draft_ref": _AVTS_URI,
            "conflicting_entity": _OTHER_DRAFT_URI,
            "conflicting_label": "Teine eelnõu",
            "reason": "Teine eelnõu viitab juba sellele sättele",
        },
    ]
    eu_compliance = [
        {
            "eu_act": _EU_ACT_URI,
            "eu_label": "GDPR",
            "estonian_provision": _AVTS_URI,
            "provision_label": "AvTS § 35",
            "transposition_status": "linked",
        },
    ]
    return ImpactFindings(
        affected_entities=affected,
        conflicts=conflicts,
        gaps=[],
        eu_compliance=eu_compliance,
        affected_count=len(affected),
        conflict_count=len(conflicts),
        gap_count=0,
    )


def _canned_resolved_ref(entity_uri: str = _AVTS_URI):
    from app.docs.entity_extractor import ExtractedRef
    from app.docs.reference_resolver import ResolvedRef

    return ResolvedRef(
        extracted=ExtractedRef(
            ref_text="AvTS § 35",
            ref_type="provision",
            confidence=1.0,
            location={"source": "analyysikeskus_input"},
        ),
        entity_uri=entity_uri,
        matched_label="AvTS § 35 — Avaliku teabe seadus",
        match_score=1.0,
    )


# ---------------------------------------------------------------------------
# Unauthenticated requests redirect to login
# ---------------------------------------------------------------------------


def test_analyysikeskus_redirects_unauthenticated():
    from starlette.testclient import TestClient

    from app.main import app

    client = TestClient(app, follow_redirects=False)
    resp = client.get("/analyysikeskus")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/auth/login"


def test_workflow_routes_redirect_unauthenticated():
    from starlette.testclient import TestClient

    from app.main import app

    client = TestClient(app, follow_redirects=False)
    for path in ("/analyysikeskus/normi-mojuahel", "/analyysikeskus/el-ulevott"):
        resp = client.get(path)
        assert resp.status_code == 303, path
        assert resp.headers["location"] == "/auth/login", path


# ---------------------------------------------------------------------------
# #720 — directory page
# ---------------------------------------------------------------------------


@patch("app.analyysikeskus.routes._get_recent_analyses", return_value=[])
@patch("app.auth.middleware._get_provider")
def test_analyysikeskus_directory_renders(mock_provider: MagicMock, mock_recent: MagicMock):
    mock_provider.return_value = _stub_provider()
    client = _authed_client()
    resp = client.get("/analyysikeskus")
    assert resp.status_code == 200
    body = resp.text
    assert "Analüüsikeskus" in body
    # Both in-scope workflow titles.
    assert "Normi mõjuahel" in body
    assert "EL ülevõtt" in body
    # The primary action button on each workflow card.
    assert "Alusta analüüsi" in body
    # The recent-analyses section + its empty state.
    assert "Hiljutised analüüsid" in body
    assert "Veel pole analüüse." in body
    # Sidebar marks the new nav item active.
    assert 'aria-current="page"' in body


# ---------------------------------------------------------------------------
# #722 — Normi mõjuahel: resolved-reference happy path
# ---------------------------------------------------------------------------


@patch("app.analyysikeskus.routes._get_recent_analyses", return_value=[])
@patch("app.analyysikeskus.adhoc_analysis.delete_named_graph", return_value=True)
@patch("app.analyysikeskus.adhoc_analysis.put_named_graph", return_value=True)
@patch("app.analyysikeskus.adhoc_analysis.ImpactAnalyzer")
@patch("app.docs.reference_resolver.ReferenceResolver.resolve")
@patch("app.auth.middleware._get_provider")
def test_normi_mojuahel_resolved_renders_full_result(
    mock_provider: MagicMock,
    mock_resolve: MagicMock,
    mock_analyzer_cls: MagicMock,
    mock_put: MagicMock,
    mock_delete: MagicMock,
    mock_recent: MagicMock,
):
    mock_provider.return_value = _stub_provider()
    mock_resolve.return_value = [_canned_resolved_ref()]
    mock_analyzer_cls.return_value.analyze.return_value = _canned_findings()

    client = _authed_client()
    # "AvTS § 35" url-encoded.
    resp = client.get("/analyysikeskus/normi-mojuahel?sisend=AvTS+%C2%A7+35")
    assert resp.status_code == 200
    body = resp.text

    assert "Normi mõjuahel" in body
    # All five Core-UI-Pattern block headings (the result shell).
    for heading in ("Sisend", "Ulatus", "Tulemused", "Tõendid", "Soovitatud tegevused"):
        assert heading in body, heading
    # All five Tulemused sub-headings.
    for sub in (
        "Peamised mõjud",
        "Kõrge riskiga seosed",
        "Seotud eelnõud",
        "Riigikohtu praktika",
        "EL seosed",
    ):
        assert sub in body, sub
    # The resolved entity's label in the Sisend block.
    assert "AvTS § 35 — Avaliku teabe seadus" in body
    # A Tõendid row references the analysed entity.
    assert "Tõendid" in body
    assert "on seotud üksusega" in body
    # "Ava õiguskaardil" link with the focus param %23-encoded (the
    # estleg: URI's "#" must survive into the query string).
    assert "/explorer?focus=" in body
    assert "%23" in body
    # "Küsi nõustajalt" → /chat/new.
    assert "Küsi nõustajalt" in body
    assert "/chat/new" in body
    # The enabled scope form — its submit must NOT be disabled (the
    # result shell's default disabled "Tulekul" stub button is replaced
    # here by the workflow's own enabled scope form).
    assert 'class="btn btn-secondary btn-sm">Uuenda ulatust</button>' in body
    # The synthetic graph was minted and torn down.
    mock_put.assert_called_once()
    mock_delete.assert_called_once()
    # The PUT and DELETE target the SAME ephemeral adhoc graph URI.
    put_uri = mock_put.call_args.args[0]
    del_uri = mock_delete.call_args.args[0]
    assert put_uri == del_uri
    assert "/estleg/adhoc/" in put_uri
    # An ad-hoc result must NOT offer "Lisa märkus" (no draft-version flow).
    assert "Lisa märkus" not in body


# ---------------------------------------------------------------------------
# #722 — the ephemeral graph is deleted even when rendering raises mid-way
# ---------------------------------------------------------------------------


@patch("app.analyysikeskus.routes._get_recent_analyses", return_value=[])
@patch("app.analyysikeskus.adhoc_analysis.delete_named_graph", return_value=True)
@patch("app.analyysikeskus.adhoc_analysis.put_named_graph", return_value=True)
@patch("app.analyysikeskus.adhoc_analysis.ImpactAnalyzer")
@patch("app.docs.reference_resolver.ReferenceResolver.resolve")
@patch("app.auth.middleware._get_provider")
def test_normi_mojuahel_deletes_graph_even_on_render_error(
    mock_provider: MagicMock,
    mock_resolve: MagicMock,
    mock_analyzer_cls: MagicMock,
    mock_put: MagicMock,
    mock_delete: MagicMock,
    mock_recent: MagicMock,
):
    mock_provider.return_value = _stub_provider()
    mock_resolve.return_value = [_canned_resolved_ref()]
    mock_analyzer_cls.return_value.analyze.return_value = _canned_findings()

    # Make the result-page assembly blow up *after* the analysis (and
    # thus after run_adhoc_impact_analysis's finally deleted the graph).
    with patch(
        "app.analyysikeskus.routes._build_results_block",
        side_effect=RuntimeError("boom while rendering"),
    ):
        client = _authed_client(raise_server_exceptions=False)
        resp = client.get("/analyysikeskus/normi-mojuahel?sisend=AvTS+%C2%A7+35")
        assert resp.status_code == 500

    # The ephemeral graph must still have been deleted.
    mock_delete.assert_called_once()
    put_uri = mock_put.call_args.args[0]
    del_uri = mock_delete.call_args.args[0]
    assert put_uri == del_uri
    assert "/estleg/adhoc/" in del_uri


# ---------------------------------------------------------------------------
# #722 — no structured reference recognised
# ---------------------------------------------------------------------------


@patch("app.analyysikeskus.routes._get_recent_analyses", return_value=[])
@patch("app.analyysikeskus.routes._rag_candidates", return_value=[])
@patch("app.analyysikeskus.adhoc_analysis.put_named_graph", return_value=True)
@patch("app.auth.middleware._get_provider")
def test_normi_mojuahel_unresolved_input_shows_warning(
    mock_provider: MagicMock,
    mock_put: MagicMock,
    mock_rag: MagicMock,
    mock_recent: MagicMock,
):
    mock_provider.return_value = _stub_provider()
    client = _authed_client()
    resp = client.get("/analyysikeskus/normi-mojuahel?sisend=mingi+suvaline+jutt")
    assert resp.status_code == 200
    body = resp.text
    # The friendly "no structured reference" warning.
    assert "Ei tuvastanud õiguslikku viidet" in body
    # Still a full result shell.
    for heading in ("Sisend", "Ulatus", "Tulemused", "Tõendid", "Soovitatud tegevused"):
        assert heading in body, heading
    # No synthetic graph was minted for an unrecognised input.
    mock_put.assert_not_called()
    # The scope form is still rendered (enabled).
    assert "Uuenda ulatust" in body


# ---------------------------------------------------------------------------
# #722 — owned-draft UUID short-circuits to the persisted impact_reports row
# ---------------------------------------------------------------------------


@patch("app.analyysikeskus.routes._get_recent_analyses", return_value=[])
@patch("app.analyysikeskus.adhoc_analysis.put_named_graph", return_value=True)
@patch("app.analyysikeskus.routes._load_owned_draft_report")
@patch("app.auth.middleware._get_provider")
def test_normi_mojuahel_draft_uuid_reuses_impact_report(
    mock_provider: MagicMock,
    mock_load_report: MagicMock,
    mock_put: MagicMock,
    mock_recent: MagicMock,
):
    import json

    mock_provider.return_value = _stub_provider()
    draft_id = "11111111-2222-3333-4444-555555555555"
    findings = _canned_findings()
    report_data = {
        "affected_entities": findings.affected_entities,
        "conflicts": findings.conflicts,
        "gaps": [],
        "eu_compliance": findings.eu_compliance,
        "affected_count": findings.affected_count,
        "conflict_count": findings.conflict_count,
        "gap_count": 0,
    }
    # (draft_id, draft_title, draft_version_id, report_data, impact_score)
    mock_load_report.return_value = (
        draft_id,
        "Minu eelnõu pealkiri",
        "ver-1",
        json.dumps(report_data),
        42,
    )

    client = _authed_client()
    resp = client.get(f"/analyysikeskus/normi-mojuahel?sisend={draft_id}")
    assert resp.status_code == 200
    body = resp.text
    # The draft title shows in the Sisend block.
    assert "Minu eelnõu pealkiri" in body
    # The draft-backed path enables "Lisa märkus" → /drafts/{id}/report.
    assert "Lisa märkus" in body
    assert f"/drafts/{draft_id}/report" in body
    # All five Tulemused sub-headings still render.
    for sub in (
        "Peamised mõjud",
        "Kõrge riskiga seosed",
        "Seotud eelnõud",
        "Riigikohtu praktika",
        "EL seosed",
    ):
        assert sub in body, sub
    # No ephemeral synthetic graph for the draft-backed path.
    mock_put.assert_not_called()
    # #724: the draft-backed Tõendid rows carry a per-row "Küsi nõustajalt"
    # form posting to /chat/seed, with the draft_id threaded into a hidden
    # input so the chat picks up the draft context.
    assert 'action="/chat/seed"' in body
    assert 'name="seed_text"' in body
    assert "Küsi nõustajalt" in body
    assert 'name="draft_id"' in body
    assert f'value="{draft_id}"' in body


# ---------------------------------------------------------------------------
# #724 — per-row "Küsi nõustajalt" affordance on the Normi Tõendid rows
# ---------------------------------------------------------------------------


@patch("app.analyysikeskus.routes._get_recent_analyses", return_value=[])
@patch("app.analyysikeskus.adhoc_analysis.delete_named_graph", return_value=True)
@patch("app.analyysikeskus.adhoc_analysis.put_named_graph", return_value=True)
@patch("app.analyysikeskus.adhoc_analysis.ImpactAnalyzer")
@patch("app.docs.reference_resolver.ReferenceResolver.resolve")
@patch("app.auth.middleware._get_provider")
def test_normi_mojuahel_evidence_rows_have_ask_advisor_form(
    mock_provider: MagicMock,
    mock_resolve: MagicMock,
    mock_analyzer_cls: MagicMock,
    mock_put: MagicMock,
    mock_delete: MagicMock,
    mock_recent: MagicMock,
):
    mock_provider.return_value = _stub_provider()
    mock_resolve.return_value = [_canned_resolved_ref()]
    mock_analyzer_cls.return_value.analyze.return_value = _canned_findings()

    client = _authed_client()
    resp = client.get("/analyysikeskus/normi-mojuahel?sisend=AvTS+%C2%A7+35")
    assert resp.status_code == 200
    body = resp.text
    # Each Tõendid row has a "Küsi nõustajalt" form posting to /chat/seed.
    assert 'action="/chat/seed"' in body
    assert 'method="post"' in body
    assert 'name="seed_text"' in body
    assert "Küsi nõustajalt" in body
    # The seed text references the finding (the analysed entity's label).
    assert "Selgita seda mõjuanalüüsi leidu" in body
    # An ad-hoc analysis has no draft, so no draft_id hidden input on the
    # per-row forms (there's no `name="draft_id"` anywhere on the page).
    assert 'name="draft_id"' not in body


def test_normi_mojuahel_blank_input_redirects():
    with patch("app.auth.middleware._get_provider") as mock_provider:
        mock_provider.return_value = _stub_provider()
        client = _authed_client()
        resp = client.get("/analyysikeskus/normi-mojuahel")
        assert resp.status_code == 303
        assert resp.headers["location"] == "/analyysikeskus"


# ---------------------------------------------------------------------------
# #722 — run_adhoc_impact_analysis: lifecycle of the ephemeral graph
# ---------------------------------------------------------------------------


@patch("app.analyysikeskus.adhoc_analysis.delete_named_graph", return_value=True)
@patch("app.analyysikeskus.adhoc_analysis.put_named_graph", return_value=True)
@patch("app.analyysikeskus.adhoc_analysis.ImpactAnalyzer")
def test_run_adhoc_impact_analysis_happy_path(
    mock_analyzer_cls: MagicMock, mock_put: MagicMock, mock_delete: MagicMock
):
    from app.analyysikeskus.adhoc_analysis import run_adhoc_impact_analysis

    mock_analyzer_cls.return_value.analyze.return_value = _canned_findings()
    result = run_adhoc_impact_analysis(_AVTS_URI)

    # Findings + a score came back.
    assert result.findings.affected_count == 3
    assert result.score > 0
    # The graph was minted, PUT, analysed, and deleted — same URI throughout.
    mock_put.assert_called_once()
    mock_delete.assert_called_once()
    put_uri = mock_put.call_args.args[0]
    assert put_uri == result.graph_uri == mock_delete.call_args.args[0]
    assert "/estleg/adhoc/" in put_uri
    # The analyzer ran against the minted graph.
    mock_analyzer_cls.return_value.analyze.assert_called_once_with(put_uri)


@patch("app.analyysikeskus.adhoc_analysis.delete_named_graph", return_value=True)
@patch("app.analyysikeskus.adhoc_analysis.put_named_graph", return_value=True)
@patch("app.analyysikeskus.adhoc_analysis.ImpactAnalyzer")
def test_run_adhoc_impact_analysis_deletes_graph_when_analyze_raises(
    mock_analyzer_cls: MagicMock, mock_put: MagicMock, mock_delete: MagicMock
):
    from app.analyysikeskus.adhoc_analysis import run_adhoc_impact_analysis

    mock_analyzer_cls.return_value.analyze.side_effect = RuntimeError("jena exploded")
    result = run_adhoc_impact_analysis(_AVTS_URI)

    # Degrades to empty findings (no 500), and the graph is still deleted.
    assert result.findings.affected_count == 0
    assert result.score == 0
    mock_delete.assert_called_once()
    assert mock_delete.call_args.args[0] == mock_put.call_args.args[0]


@patch("app.analyysikeskus.adhoc_analysis.delete_named_graph", return_value=True)
@patch("app.analyysikeskus.adhoc_analysis.put_named_graph", return_value=False)
@patch("app.analyysikeskus.adhoc_analysis.ImpactAnalyzer")
def test_run_adhoc_impact_analysis_deletes_graph_when_put_fails(
    mock_analyzer_cls: MagicMock, mock_put: MagicMock, mock_delete: MagicMock
):
    from app.analyysikeskus.adhoc_analysis import run_adhoc_impact_analysis

    result = run_adhoc_impact_analysis(_AVTS_URI)

    # PUT failed → empty findings, analyzer never ran, graph still deleted.
    assert result.findings.affected_count == 0
    mock_analyzer_cls.return_value.analyze.assert_not_called()
    mock_delete.assert_called_once()


def test_run_adhoc_impact_analysis_blank_uri_short_circuits():
    from app.analyysikeskus.adhoc_analysis import run_adhoc_impact_analysis

    # An empty entity URI must not touch Jena at all.
    with (
        patch("app.analyysikeskus.adhoc_analysis.put_named_graph") as mock_put,
        patch("app.analyysikeskus.adhoc_analysis.delete_named_graph") as mock_delete,
    ):
        result = run_adhoc_impact_analysis("")
        assert result.findings.affected_count == 0
        assert result.graph_uri == ""
        mock_put.assert_not_called()
        mock_delete.assert_not_called()


# ---------------------------------------------------------------------------
# #723 — EL ülevõtt ja harmoneerimine
# ---------------------------------------------------------------------------

_GDPR_URI = "https://data.riik.ee/ontology/estleg#EU-32016R0679"
_AVTS_ACT_URI = "https://data.riik.ee/ontology/estleg#avaliku-teabe-seadus"
_AVTS_P35_URI = "https://data.riik.ee/ontology/estleg#AvTS-p35"
_AML_ACT_URI = "https://data.riik.ee/ontology/estleg#rahapesu-tokestamise-seadus"


def _canned_eu_transposition_rows():
    """A canned ``run_eu_transposition`` result — covers covered/partial + a ``puudub`` row."""
    return [
        {
            "eu_act": _GDPR_URI,
            "eu_label": "Isikuandmete kaitse üldmäärus",
            "celex": "32016R0679",
            "ee_act": _AVTS_ACT_URI,
            "ee_act_label": "Avaliku teabe seadus",
            "ee_provision": _AVTS_P35_URI,
            "ee_provision_label": "AvTS § 35",
            "status": "kaetud",
            "authority": None,
            "authority_label": None,
        },
        {
            "eu_act": _GDPR_URI,
            "eu_label": "Isikuandmete kaitse üldmäärus",
            "celex": "32016R0679",
            "ee_act": _AML_ACT_URI,
            "ee_act_label": "Rahapesu tõkestamise seadus",
            "ee_provision": None,
            "ee_provision_label": None,
            "status": "osaline",
            "authority": None,
            "authority_label": None,
        },
        {
            "eu_act": _GDPR_URI,
            "eu_label": "Isikuandmete kaitse üldmäärus",
            "celex": "32016R0679",
            "ee_act": None,
            "ee_act_label": None,
            "ee_provision": None,
            "ee_provision_label": None,
            "status": "puudub",
            "authority": None,
            "authority_label": None,
        },
    ]


def _canned_eu_resolved_ref(entity_uri: str = _GDPR_URI):
    from app.docs.entity_extractor import ExtractedRef
    from app.docs.reference_resolver import ResolvedRef

    return ResolvedRef(
        extracted=ExtractedRef(
            ref_text="32016R0679",
            ref_type="eu_act",
            confidence=1.0,
            location={"source": "analyysikeskus_input"},
        ),
        entity_uri=entity_uri,
        matched_label="Isikuandmete kaitse üldmäärus",
        match_score=1.0,
    )


@patch("app.analyysikeskus.routes._get_recent_analyses", return_value=[])
@patch("app.analyysikeskus.routes.run_eu_transposition")
@patch("app.docs.reference_resolver.ReferenceResolver.resolve")
@patch("app.auth.middleware._get_provider")
def test_el_ulevott_celex_renders_transposition_table(
    mock_provider: MagicMock,
    mock_resolve: MagicMock,
    mock_run: MagicMock,
    mock_recent: MagicMock,
):
    mock_provider.return_value = _stub_provider()
    mock_resolve.return_value = [_canned_eu_resolved_ref()]
    mock_run.return_value = _canned_eu_transposition_rows()

    client = _authed_client()
    resp = client.get("/analyysikeskus/el-ulevott?sisend=32016R0679")
    assert resp.status_code == 200
    body = resp.text

    assert "EL ülevõtt" in body
    # All five Core-UI-Pattern block headings.
    for heading in ("Sisend", "Ulatus", "Tulemused", "Tõendid", "Soovitatud tegevused"):
        assert heading in body, heading
    # The transposition table headers.
    for header in ("EL õigusakt", "Eesti õigusakt", "Staatus", "Soovitatud tegevus"):
        assert header in body, header
    # A status Badge (the covered row → success variant).
    assert "badge badge-success" in body
    # The one-line summary line, templated from the row counts.
    assert "ülevõte:" in body
    assert "Eesti õigusakti seotud" in body
    # The resolved EU act's label shows in the Sisend block + CELEX.
    assert "Isikuandmete kaitse üldmäärus" in body
    assert "32016R0679" in body
    # "Ava õiguskaardil" link with the focus param %23-encoded (the
    # estleg: URI's "#" must survive into the query string).
    assert "/explorer?focus=" in body
    assert "%23" in body
    # "Küsi nõustajalt" → /chat/new.
    assert "Küsi nõustajalt" in body
    assert "/chat/new" in body
    # The enabled scope form's submit must NOT be disabled (the result
    # shell's default disabled "Tulekul" stub button is replaced here).
    assert 'class="btn btn-secondary btn-sm">Uuenda ulatust</button>' in body
    # A "puudub" row → the danger-band recommendation + the "Lisa puuduv säte" action.
    assert "Lisa puuduv säte" in body
    assert "Kõrge risk" in body
    # No "Vastutav asutus" column — no authority predicate is wired.
    assert "Vastutav asutus" not in body
    # run_eu_transposition was called with the resolved EU act URI.
    mock_run.assert_called_once()
    assert mock_run.call_args.args[0] == _GDPR_URI


@patch("app.analyysikeskus.routes._get_recent_analyses", return_value=[])
@patch("app.analyysikeskus.routes.search_eu_acts_by_label", return_value=[])
@patch("app.docs.reference_resolver.ReferenceResolver.resolve", return_value=[])
@patch("app.auth.middleware._get_provider")
def test_el_ulevott_unrecognised_input_shows_warning(
    mock_provider: MagicMock,
    mock_resolve: MagicMock,
    mock_search: MagicMock,
    mock_recent: MagicMock,
):
    mock_provider.return_value = _stub_provider()
    client = _authed_client()
    resp = client.get("/analyysikeskus/el-ulevott?sisend=mingi+suvaline+jutt")
    assert resp.status_code == 200
    body = resp.text
    # The friendly "no EU act recognised" warning.
    assert "Ei tuvastanud EL õigusakti" in body
    # Still a full result shell.
    for heading in ("Sisend", "Ulatus", "Tulemused", "Tõendid", "Soovitatud tegevused"):
        assert heading in body, heading
    # The scope form is still rendered (enabled).
    assert "Uuenda ulatust" in body


# ---------------------------------------------------------------------------
# #805 — canonical-shaped CELEX missing from the ontology gets its own copy
# ---------------------------------------------------------------------------
#
# Distinguishes "user typed a real CELEX (GDPR / Working Conditions / …)
# that just isn't in our snapshot yet" from "user typed prose / garbage".
# The route reaches ``_render_eu_unresolved`` when:
#   * resolver returns nothing (no entity_uri match), AND
#   * label search returns nothing (CELEX is unknown, label is empty).
# Both paths are mocked here so the test pins the BRANCH inside
# ``_render_eu_unresolved`` (canonical-shape vs. generic) rather than
# the upstream wiring.


@patch("app.analyysikeskus.routes._get_recent_analyses", return_value=[])
@patch("app.analyysikeskus.routes.search_eu_acts_by_label", return_value=[])
@patch("app.docs.reference_resolver.ReferenceResolver.resolve", return_value=[])
@patch("app.auth.middleware._get_provider")
def test_el_ulevott_canonical_celex_missing_from_data_shows_specific_warning(
    mock_provider: MagicMock,
    mock_resolve: MagicMock,
    mock_search: MagicMock,
    mock_recent: MagicMock,
):
    """The user typed GDPR's CELEX, which is well-formed but missing
    from the ontology — the warning must name the CELEX so they know
    to look it up manually rather than wonder if they mistyped.
    """
    mock_provider.return_value = _stub_provider()
    client = _authed_client()
    resp = client.get("/analyysikeskus/el-ulevott?sisend=32016R0679")
    assert resp.status_code == 200
    body = resp.text
    # The canonical-CELEX message names the input + tells the user to
    # check manually.
    assert "32016R0679" in body
    assert "ei ole veel" in body and "ontoloogias kaardistatud" in body
    assert "Kontrollige käsitsi" in body
    # The generic copy must NOT appear (avoids confusing double messaging).
    assert "Ei tuvastanud EL õigusakti" not in body
    # Still a full result shell with the standard headings + scope form.
    for heading in ("Sisend", "Ulatus", "Tulemused", "Tõendid", "Soovitatud tegevused"):
        assert heading in body, heading
    assert "Uuenda ulatust" in body


@patch("app.analyysikeskus.routes._get_recent_analyses", return_value=[])
@patch("app.analyysikeskus.routes.search_eu_acts_by_label", return_value=[])
@patch("app.docs.reference_resolver.ReferenceResolver.resolve", return_value=[])
@patch("app.auth.middleware._get_provider")
def test_el_ulevott_near_celex_garbage_shows_generic_warning(
    mock_provider: MagicMock,
    mock_resolve: MagicMock,
    mock_search: MagicMock,
    mock_recent: MagicMock,
):
    """An alphanumeric string that ISN'T a canonical CELEX (``12abc34``)
    keeps the generic "Ei tuvastatud" copy with the CELEX example as
    a hint — we don't want to imply ``12abc34`` is a real CELEX.
    """
    mock_provider.return_value = _stub_provider()
    client = _authed_client()
    resp = client.get("/analyysikeskus/el-ulevott?sisend=12abc34")
    assert resp.status_code == 200
    body = resp.text
    # Generic copy + the example CELEX hint.
    assert "Ei tuvastanud EL õigusakti" in body
    assert "32016R0679" in body  # example hint
    # The canonical-CELEX-specific copy must NOT fire.
    assert "ei ole veel" not in body or "ontoloogias kaardistatud" not in body


@patch("app.analyysikeskus.routes._get_recent_analyses", return_value=[])
@patch("app.analyysikeskus.routes.search_eu_acts_by_label", return_value=[])
@patch("app.docs.reference_resolver.ReferenceResolver.resolve", return_value=[])
@patch("app.auth.middleware._get_provider")
def test_el_ulevott_directive_title_shows_generic_warning(
    mock_provider: MagicMock,
    mock_resolve: MagicMock,
    mock_search: MagicMock,
    mock_recent: MagicMock,
):
    """A free-text directive name with no label-search match keeps
    the generic "Ei tuvastatud" copy — we don't have anything specific
    to tell the user about a string that doesn't even look like a
    CELEX.
    """
    mock_provider.return_value = _stub_provider()
    client = _authed_client()
    resp = client.get("/analyysikeskus/el-ulevott?sisend=just+some+directive+name")
    assert resp.status_code == 200
    body = resp.text
    assert "Ei tuvastanud EL õigusakti" in body
    # No canonical-shape copy.
    assert "ontoloogias kaardistatud" not in body


@patch("app.analyysikeskus.routes._get_recent_analyses", return_value=[])
@patch("app.analyysikeskus.routes.search_eu_acts_by_label")
@patch("app.docs.reference_resolver.ReferenceResolver.resolve", return_value=[])
@patch("app.auth.middleware._get_provider")
def test_el_ulevott_label_search_multiple_candidates(
    mock_provider: MagicMock,
    mock_resolve: MagicMock,
    mock_search: MagicMock,
    mock_recent: MagicMock,
):
    mock_provider.return_value = _stub_provider()
    mock_search.return_value = [
        {"uri": _GDPR_URI, "label": "Isikuandmete kaitse üldmäärus", "celex": "32016R0679"},
        {
            "uri": "https://data.riik.ee/ontology/estleg#EU-32018L1972",
            "label": "Isikuandmete kaitse direktiiv",
            "celex": "32018L1972",
        },
    ]
    client = _authed_client()
    resp = client.get("/analyysikeskus/el-ulevott?sisend=isikuandmete+kaitse")
    assert resp.status_code == 200
    body = resp.text
    # The disambiguation prompt + both candidate links (re-running the
    # workflow with each candidate's CELEX).
    assert "Mitu vastet — valige üks:" in body
    assert "sisend=32016R0679" in body
    assert "sisend=32018L1972" in body
    # Still a full result shell with an enabled scope form.
    for heading in ("Sisend", "Ulatus", "Tulemused", "Tõendid", "Soovitatud tegevused"):
        assert heading in body, heading
    assert "Uuenda ulatust" in body


def test_el_ulevott_blank_input_redirects():
    with patch("app.auth.middleware._get_provider") as mock_provider:
        mock_provider.return_value = _stub_provider()
        client = _authed_client()
        resp = client.get("/analyysikeskus/el-ulevott")
        assert resp.status_code == 303
        assert resp.headers["location"] == "/analyysikeskus"


# ---------------------------------------------------------------------------
# Result-shell _block_body: empty list → fallback, non-empty list → wrapped
# ---------------------------------------------------------------------------


def test_result_shell_empty_list_block_renders_fallback():
    from fasthtml.common import P, to_xml

    from app.analyysikeskus.result_shell import analysis_result_shell

    page = analysis_result_shell(
        workflow_title="Normi mõjuahel",
        input_summary=P("Sisestasite: «AvTS § 35»"),
        results_block=[],  # an empty findings list must NOT render as "[]"
        evidence_block=[],
        actions=[{"label": "Tagasi", "href": "/analyysikeskus"}],
        user=None,
    )
    html = to_xml(page)
    assert "Tulemusi ei leitud." in html
    assert "Tõendeid ei leitud." in html
    assert "[]" not in html


def test_result_shell_nonempty_list_block_renders_all_items():
    from fasthtml.common import P, to_xml

    from app.analyysikeskus.result_shell import analysis_result_shell

    page = analysis_result_shell(
        workflow_title="Normi mõjuahel",
        input_summary=P("Sisestasite: «AvTS § 35»"),
        results_block=[P("Leid üks"), P("Leid kaks")],
        evidence_block=P("Tõend"),
        actions=[{"label": "Tagasi", "href": "/analyysikeskus"}],
        user=None,
    )
    html = to_xml(page)
    assert "Leid üks" in html
    assert "Leid kaks" in html
    assert "Tulemusi ei leitud." not in html
