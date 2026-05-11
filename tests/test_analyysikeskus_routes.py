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
# #721 / #723 — EL ülevõtt stub result shell
# ---------------------------------------------------------------------------


@patch("app.analyysikeskus.routes._get_recent_analyses", return_value=[])
@patch("app.auth.middleware._get_provider")
def test_el_ulevott_stub_renders(mock_provider: MagicMock, mock_recent: MagicMock):
    mock_provider.return_value = _stub_provider()
    client = _authed_client()
    resp = client.get("/analyysikeskus/el-ulevott?sisend=32016R0679")
    assert resp.status_code == 200
    body = resp.text
    assert "EL ülevõtt" in body
    for heading in ("Sisend", "Ulatus", "Tulemused", "Tõendid", "Soovitatud tegevused"):
        assert heading in body, heading
    assert "32016R0679" in body


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
