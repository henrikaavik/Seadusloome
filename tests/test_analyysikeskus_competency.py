"""Tests for the Pädevuste kaardistus workflow (A3 v1 — issue #797).

Covers:

1. The SPARQL helper layer in :mod:`app.analyysikeskus.competency` —
   row → :class:`CompetenceRow` / :class:`OverlapRow` conversion,
   empty / dead-Jena paths, the institution-name fuzzy search, the
   per-institution competence query, the overlap query and the
   :func:`gather_institution_competences` aggregator.
2. An end-to-end SPARQL exercise against the in-memory rdflib
   :class:`Graph` loaded from ``tests/fixtures/ontology_canonical.ttl`` —
   proves the templates actually parse + match the populated data.
3. The route layer in :mod:`app.analyysikeskus.routes` — the
   ``/analyysikeskus/padevused`` endpoint: the auth gate, the landing
   page (no ``sisend``), the single-match happy path, the
   disambiguation branch, the unresolved branch and the deep-link URI
   branch.
4. The capability + analyysikeskus inputs registration — A3 must
   surface as a live workflow card on the directory page.

Tests follow the same shape as ``test_analyysikeskus_sanctions.py`` —
the SPARQL client / institution lookup is patched *where used* (the
patch-path contract).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from rdflib import Graph

from app.ontology.sparql_client import SparqlClient

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "ontology_canonical.ttl"


# ---------------------------------------------------------------------------
# Test fixtures — URIs and row stubs
# ---------------------------------------------------------------------------

_INST_2_URI = "https://data.riik.ee/ontology/estleg#Institution_2"
_INST_3_URI = "https://data.riik.ee/ontology/estleg#Institution_3"
_PROV_4_URI = "https://data.riik.ee/ontology/estleg#Provision_4"
_PROV_5_URI = "https://data.riik.ee/ontology/estleg#Provision_5"
_ACT_1_URI = "https://data.riik.ee/ontology/estleg#Act_1"
_ACT_2_URI = "https://data.riik.ee/ontology/estleg#Act_2"


def _make_sparql_against_fixture() -> SparqlClient:
    """Return a SparqlClient whose ``query`` runs SPARQL against the canonical fixture.

    Bypasses :meth:`SparqlClient.__init__` (no HTTP / no Fuseki) and
    replaces ``query`` with a function that mirrors
    ``SparqlClient.query`` by invoking the VALUES / URI VALUES injectors
    and delegating execution to a freshly-loaded rdflib :class:`Graph`.
    """
    graph = Graph()
    graph.parse(FIXTURE_PATH, format="turtle")

    client = SparqlClient.__new__(SparqlClient)
    client.jena_url = "http://localhost:3030"  # type: ignore[attr-defined]
    client.dataset = "ontology"  # type: ignore[attr-defined]
    client.timeout = 5.0  # type: ignore[attr-defined]

    def _query(
        sparql: str,
        bindings: dict[str, str] | None = None,
        uri_bindings: dict[str, str] | None = None,
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


# ---------------------------------------------------------------------------
# 1. search_institutions_by_label
# ---------------------------------------------------------------------------


class TestSearchInstitutionsByLabel:
    def test_short_query_returns_empty_without_hitting_jena(self):
        from app.analyysikeskus.competency import search_institutions_by_label

        stub = MagicMock()
        rows = search_institutions_by_label("a", sparql_client=stub)
        assert rows == []
        stub.query.assert_not_called()

    def test_empty_query_returns_empty(self):
        from app.analyysikeskus.competency import search_institutions_by_label

        stub = MagicMock()
        assert search_institutions_by_label("", sparql_client=stub) == []
        assert search_institutions_by_label("   ", sparql_client=stub) == []
        stub.query.assert_not_called()

    def test_returns_parsed_candidates(self):
        from app.analyysikeskus.competency import (
            InstitutionCandidate,
            search_institutions_by_label,
        )

        stub = MagicMock()
        stub.query.return_value = [
            {"institution": _INST_2_URI, "label": "Andmekaitse Inspektsioon"},
            {"institution": _INST_3_URI, "label": "Tarbijakaitse"},
        ]
        rows = search_institutions_by_label("kaitse", sparql_client=stub)
        assert len(rows) == 2
        assert all(isinstance(r, InstitutionCandidate) for r in rows)
        assert rows[0].uri == _INST_2_URI
        assert rows[0].label == "Andmekaitse Inspektsioon"

    def test_sparql_error_returns_empty(self):
        from app.analyysikeskus.competency import search_institutions_by_label

        stub = MagicMock()
        stub.query.side_effect = RuntimeError("jena down")
        assert search_institutions_by_label("Andmekaitse", sparql_client=stub) == []

    def test_dedupes_by_uri(self):
        from app.analyysikeskus.competency import search_institutions_by_label

        stub = MagicMock()
        # Same URI twice (a stray reasoner round-trip could yield this).
        stub.query.return_value = [
            {"institution": _INST_2_URI, "label": "Andmekaitse Inspektsioon"},
            {"institution": _INST_2_URI, "label": "Andmekaitse Inspektsioon"},
        ]
        rows = search_institutions_by_label("kaitse", sparql_client=stub)
        assert len(rows) == 1

    def test_against_fixture_label_search(self):
        """End-to-end SPARQL against the canonical fixture — proves the template parses."""
        from app.analyysikeskus.competency import search_institutions_by_label

        client = _make_sparql_against_fixture()
        rows = search_institutions_by_label("andmekaitse", sparql_client=client)
        assert any(r.uri == _INST_2_URI for r in rows)


# ---------------------------------------------------------------------------
# 2. get_institution_label
# ---------------------------------------------------------------------------


class TestGetInstitutionLabel:
    def test_blank_uri_returns_empty_without_hitting_jena(self):
        from app.analyysikeskus.competency import get_institution_label

        stub = MagicMock()
        assert get_institution_label("", sparql_client=stub) == ""
        stub.query.assert_not_called()

    def test_returns_label(self):
        from app.analyysikeskus.competency import get_institution_label

        stub = MagicMock()
        stub.query.return_value = [{"label": "Andmekaitse Inspektsioon"}]
        assert get_institution_label(_INST_2_URI, sparql_client=stub) == "Andmekaitse Inspektsioon"

    def test_sparql_error_returns_empty(self):
        from app.analyysikeskus.competency import get_institution_label

        stub = MagicMock()
        stub.query.side_effect = RuntimeError("jena down")
        assert get_institution_label(_INST_2_URI, sparql_client=stub) == ""


# ---------------------------------------------------------------------------
# 3. list_competences_for_institution
# ---------------------------------------------------------------------------


class TestListCompetencesForInstitution:
    def test_blank_uri_returns_empty(self):
        from app.analyysikeskus.competency import list_competences_for_institution

        stub = MagicMock()
        assert list_competences_for_institution("", sparql_client=stub) == []
        stub.query.assert_not_called()

    def test_returns_parsed_rows(self):
        from app.analyysikeskus.competency import (
            CompetenceRow,
            list_competences_for_institution,
        )

        stub = MagicMock()
        stub.query.return_value = [
            {
                "provision": _PROV_4_URI,
                "provisionLabel": "Provision 4",
                "act": _ACT_1_URI,
                "actLabel": "Act 1",
            },
        ]
        rows = list_competences_for_institution(_INST_2_URI, sparql_client=stub)
        assert len(rows) == 1
        assert isinstance(rows[0], CompetenceRow)
        assert rows[0].provision_uri == _PROV_4_URI
        assert rows[0].provision_label == "Provision 4"
        assert rows[0].act_uri == _ACT_1_URI
        assert rows[0].act_label == "Act 1"

    def test_drops_rows_without_provision_uri(self):
        from app.analyysikeskus.competency import list_competences_for_institution

        stub = MagicMock()
        stub.query.return_value = [
            {"provision": "", "provisionLabel": "no uri"},
            {
                "provision": _PROV_4_URI,
                "provisionLabel": "Provision 4",
                "act": "",
                "actLabel": "",
            },
        ]
        rows = list_competences_for_institution(_INST_2_URI, sparql_client=stub)
        assert len(rows) == 1
        assert rows[0].provision_uri == _PROV_4_URI

    def test_sparql_error_returns_empty(self):
        from app.analyysikeskus.competency import list_competences_for_institution

        stub = MagicMock()
        stub.query.side_effect = RuntimeError("jena down")
        assert list_competences_for_institution(_INST_2_URI, sparql_client=stub) == []

    def test_binds_institution_as_uri(self):
        from app.analyysikeskus.competency import list_competences_for_institution

        stub = MagicMock()
        stub.query.return_value = []
        list_competences_for_institution(_INST_2_URI, sparql_client=stub)
        kwargs = stub.query.call_args.kwargs
        assert "uri_bindings" in kwargs
        assert kwargs["uri_bindings"] == {"institution": _INST_2_URI}

    def test_against_fixture(self):
        """End-to-end SPARQL: Institution_2 holds powers on Provision_4 + Provision_5."""
        from app.analyysikeskus.competency import list_competences_for_institution

        client = _make_sparql_against_fixture()
        rows = list_competences_for_institution(_INST_2_URI, sparql_client=client)
        provisions = {r.provision_uri for r in rows}
        assert _PROV_4_URI in provisions
        assert _PROV_5_URI in provisions


# ---------------------------------------------------------------------------
# 4. list_competence_overlaps
# ---------------------------------------------------------------------------


class TestListCompetenceOverlaps:
    def test_blank_uri_returns_empty(self):
        from app.analyysikeskus.competency import list_competence_overlaps

        stub = MagicMock()
        assert list_competence_overlaps("", sparql_client=stub) == []
        stub.query.assert_not_called()

    def test_returns_parsed_rows(self):
        from app.analyysikeskus.competency import (
            OverlapRow,
            list_competence_overlaps,
        )

        stub = MagicMock()
        stub.query.return_value = [
            {
                "provision": _PROV_5_URI,
                "provisionLabel": "Provision 5",
                "act": _ACT_2_URI,
                "actLabel": "Act 2",
                "other": _INST_3_URI,
                "otherLabel": "Institution 3",
            },
        ]
        rows = list_competence_overlaps(_INST_2_URI, sparql_client=stub)
        assert len(rows) == 1
        assert isinstance(rows[0], OverlapRow)
        assert rows[0].provision_uri == _PROV_5_URI
        assert rows[0].other_institution_uri == _INST_3_URI

    def test_defence_in_depth_drops_self_pair(self):
        """Even if SPARQL leaks a self-pair, the Python filter drops it."""
        from app.analyysikeskus.competency import list_competence_overlaps

        stub = MagicMock()
        stub.query.return_value = [
            {
                "provision": _PROV_5_URI,
                "other": _INST_2_URI,  # same as seed — must drop
            },
            {
                "provision": _PROV_5_URI,
                "other": _INST_3_URI,  # genuine overlap
            },
        ]
        rows = list_competence_overlaps(_INST_2_URI, sparql_client=stub)
        assert len(rows) == 1
        assert rows[0].other_institution_uri == _INST_3_URI

    def test_drops_rows_without_other_uri(self):
        from app.analyysikeskus.competency import list_competence_overlaps

        stub = MagicMock()
        stub.query.return_value = [
            {"provision": _PROV_5_URI, "other": ""},
            {"provision": _PROV_5_URI, "other": _INST_3_URI},
        ]
        rows = list_competence_overlaps(_INST_2_URI, sparql_client=stub)
        assert len(rows) == 1

    def test_sparql_error_returns_empty(self):
        from app.analyysikeskus.competency import list_competence_overlaps

        stub = MagicMock()
        stub.query.side_effect = RuntimeError("jena down")
        assert list_competence_overlaps(_INST_2_URI, sparql_client=stub) == []

    def test_against_fixture(self):
        """End-to-end SPARQL: Institution_2 overlaps with Institution_3 on Provision_5."""
        from app.analyysikeskus.competency import list_competence_overlaps

        client = _make_sparql_against_fixture()
        rows = list_competence_overlaps(_INST_2_URI, sparql_client=client)
        # At least one overlap, all pointing at Institution_3.
        assert rows
        other_uris = {r.other_institution_uri for r in rows}
        assert _INST_3_URI in other_uris
        # Self-pair must never appear.
        assert _INST_2_URI not in other_uris


# ---------------------------------------------------------------------------
# 5. gather_institution_competences
# ---------------------------------------------------------------------------


class TestGatherInstitutionCompetences:
    def test_blank_uri_returns_empty_view(self):
        from app.analyysikeskus.competency import (
            InstitutionCompetences,
            gather_institution_competences,
        )

        view = gather_institution_competences("")
        assert isinstance(view, InstitutionCompetences)
        assert view.institution_uri == ""
        assert view.total_count == 0
        assert view.by_act == {}
        assert view.overlaps == []

    @patch("app.analyysikeskus.competency.list_competence_overlaps")
    @patch("app.analyysikeskus.competency.list_competences_for_institution")
    @patch("app.analyysikeskus.competency.get_institution_label")
    def test_aggregates_by_act(
        self,
        mock_label: MagicMock,
        mock_list: MagicMock,
        mock_overlaps: MagicMock,
    ):
        from app.analyysikeskus.competency import (
            CompetenceRow,
            OverlapRow,
            gather_institution_competences,
        )

        mock_label.return_value = "Andmekaitse Inspektsioon"
        mock_list.return_value = [
            CompetenceRow(
                provision_uri=_PROV_4_URI,
                provision_label="P4",
                act_uri=_ACT_1_URI,
                act_label="A1",
            ),
            CompetenceRow(
                provision_uri=_PROV_5_URI,
                provision_label="P5",
                act_uri=_ACT_2_URI,
                act_label="A2",
            ),
        ]
        mock_overlaps.return_value = [
            OverlapRow(
                provision_uri=_PROV_5_URI,
                provision_label="P5",
                act_uri=_ACT_2_URI,
                act_label="A2",
                other_institution_uri=_INST_3_URI,
                other_institution_label="I3",
            )
        ]

        view = gather_institution_competences(_INST_2_URI)
        assert view.institution_uri == _INST_2_URI
        assert view.institution_label == "Andmekaitse Inspektsioon"
        assert view.total_count == 2
        assert set(view.by_act.keys()) == {_ACT_1_URI, _ACT_2_URI}
        assert view.by_act[_ACT_1_URI][0].provision_uri == _PROV_4_URI
        assert len(view.overlaps) == 1
        assert view.truncated is False

    @patch("app.analyysikeskus.competency.list_competence_overlaps", return_value=[])
    @patch("app.analyysikeskus.competency.list_competences_for_institution")
    @patch("app.analyysikeskus.competency.get_institution_label", return_value="Inst")
    def test_truncation_detected(
        self,
        _mock_label: MagicMock,
        mock_list: MagicMock,
        _mock_overlaps: MagicMock,
    ):
        from app.analyysikeskus.competency import (
            _MAX_COMPETENCES_PER_INSTITUTION,
            CompetenceRow,
            gather_institution_competences,
        )

        # SPARQL LIMIT is _MAX+1 — the gatherer treats len > _MAX as
        # "truncated" and trims back. Simulate by returning _MAX+1 rows.
        mock_list.return_value = [
            CompetenceRow(
                provision_uri=f"https://example.org/p{i}",
                provision_label=f"P{i}",
                act_uri=_ACT_1_URI,
                act_label="A1",
            )
            for i in range(_MAX_COMPETENCES_PER_INSTITUTION + 1)
        ]
        view = gather_institution_competences(_INST_2_URI)
        assert view.truncated is True
        assert view.total_count == _MAX_COMPETENCES_PER_INSTITUTION

    @patch("app.analyysikeskus.competency.list_competence_overlaps", return_value=[])
    @patch("app.analyysikeskus.competency.list_competences_for_institution", return_value=[])
    @patch("app.analyysikeskus.competency.get_institution_label", return_value="")
    def test_label_falls_back_to_uri_tail(
        self,
        _mock_label: MagicMock,
        _mock_list: MagicMock,
        _mock_overlaps: MagicMock,
    ):
        from app.analyysikeskus.competency import gather_institution_competences

        view = gather_institution_competences(_INST_2_URI)
        # Falls back to the local-name fragment so the page heading
        # never reads blank.
        assert view.institution_label == "Institution_2"


# ---------------------------------------------------------------------------
# 6. Route smoke tests (test client end-to-end)
# ---------------------------------------------------------------------------


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


def _canned_view():
    from app.analyysikeskus.competency import (
        CompetenceRow,
        InstitutionCompetences,
        OverlapRow,
    )

    return InstitutionCompetences(
        institution_uri=_INST_2_URI,
        institution_label="Andmekaitse Inspektsioon",
        by_act={
            _ACT_1_URI: [
                CompetenceRow(
                    provision_uri=_PROV_4_URI,
                    provision_label="Provision 4",
                    act_uri=_ACT_1_URI,
                    act_label="Act 1",
                )
            ],
            _ACT_2_URI: [
                CompetenceRow(
                    provision_uri=_PROV_5_URI,
                    provision_label="Provision 5",
                    act_uri=_ACT_2_URI,
                    act_label="Act 2",
                )
            ],
        },
        overlaps=[
            OverlapRow(
                provision_uri=_PROV_5_URI,
                provision_label="Provision 5",
                act_uri=_ACT_2_URI,
                act_label="Act 2",
                other_institution_uri=_INST_3_URI,
                other_institution_label="Tarbijakaitse ja Tehnilise Järelevalve Amet",
            )
        ],
        total_count=2,
        truncated=False,
    )


def test_padevused_redirects_unauthenticated():
    from starlette.testclient import TestClient

    from app.main import app

    client = TestClient(app, follow_redirects=False)
    resp = client.get("/analyysikeskus/padevused")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/auth/login"


@patch("app.auth.middleware._get_provider")
def test_padevused_landing_renders_input_form(mock_provider: MagicMock):
    """GET /analyysikeskus/padevused with no sisend renders the landing shell."""
    mock_provider.return_value = _stub_provider()
    client = _authed_client()
    resp = client.get("/analyysikeskus/padevused")
    assert resp.status_code == 200
    body = resp.text
    assert "Pädevuste kaardistus" in body
    for heading in ("Sisend", "Ulatus", "Tulemused", "Tõendid", "Soovitatud tegevused"):
        assert heading in body, heading
    assert 'action="/analyysikeskus/padevused"' in body
    assert "Otsi pädevusi" in body
    assert "Sisestage asutuse nimi" in body


@patch("app.analyysikeskus.routes.gather_institution_competences")
@patch("app.analyysikeskus.routes.search_institutions_by_label")
@patch("app.auth.middleware._get_provider")
def test_padevused_single_match_renders_result(
    mock_provider: MagicMock,
    mock_search: MagicMock,
    mock_gather: MagicMock,
):
    """A single institution match renders the per-act sections + overlaps."""
    from app.analyysikeskus.competency import InstitutionCandidate

    mock_provider.return_value = _stub_provider()
    mock_search.return_value = [
        InstitutionCandidate(uri=_INST_2_URI, label="Andmekaitse Inspektsioon"),
    ]
    mock_gather.return_value = _canned_view()

    client = _authed_client()
    resp = client.get("/analyysikeskus/padevused?sisend=Andmekaitse+Inspektsioon")
    assert resp.status_code == 200
    body = resp.text

    # Page title + the 5-card shell.
    assert "Pädevuste kaardistus" in body
    for heading in ("Sisend", "Ulatus", "Tulemused", "Tõendid", "Soovitatud tegevused"):
        assert heading in body, heading

    # The resolved institution label appears in Sisend.
    assert "Andmekaitse Inspektsioon" in body

    # The v2 limitation banner is present.
    assert "Näitan pädevusi asutuse tasandil" in body

    # Summary line with the count.
    assert "2 pädevust" in body

    # Per-act section headings (label-driven).
    assert "Act 1" in body
    assert "Act 2" in body

    # Kattuvad pädevused section appears with the overlap row.
    assert "Kattuvad pädevused" in body
    assert "Tarbijakaitse ja Tehnilise Järelevalve Amet" in body

    # Tõendid rows include the canonical phrase.
    assert "on pädev asutus sättes" in body

    # "Küsi nõustajalt" per-row form is present (pattern from #724).
    assert 'action="/chat/seed"' in body
    assert 'name="seed_text"' in body

    # Static "Soovitatud tegevused".
    assert "Ava õiguskaardil" in body
    assert "Tagasi analüüsikeskusesse" in body


@patch("app.analyysikeskus.routes.search_institutions_by_label")
@patch("app.auth.middleware._get_provider")
def test_padevused_disambiguation_when_multiple_candidates(
    mock_provider: MagicMock,
    mock_search: MagicMock,
):
    """Multiple matches ⇒ a disambiguation card with clickable candidates."""
    from app.analyysikeskus.competency import InstitutionCandidate

    mock_provider.return_value = _stub_provider()
    mock_search.return_value = [
        InstitutionCandidate(uri=_INST_2_URI, label="Andmekaitse Inspektsioon"),
        InstitutionCandidate(
            uri=_INST_3_URI,
            label="Tarbijakaitse ja Tehnilise Järelevalve Amet",
        ),
    ]

    client = _authed_client()
    resp = client.get("/analyysikeskus/padevused?sisend=kaitse")
    assert resp.status_code == 200
    body = resp.text
    assert "Sisend võib viidata mitmele asutusele" in body
    assert "Andmekaitse Inspektsioon" in body
    assert "Tarbijakaitse" in body


@patch("app.analyysikeskus.routes.search_institutions_by_label", return_value=[])
@patch("app.auth.middleware._get_provider")
def test_padevused_unresolved_shows_warning(
    mock_provider: MagicMock,
    mock_search: MagicMock,
):
    """An unrecognised input renders the friendly warning + the result shell."""
    mock_provider.return_value = _stub_provider()
    client = _authed_client()
    resp = client.get("/analyysikeskus/padevused?sisend=foobar")
    assert resp.status_code == 200
    body = resp.text
    assert "Ei tuvastanud asutust" in body
    for heading in ("Sisend", "Ulatus", "Tulemused", "Tõendid", "Soovitatud tegevused"):
        assert heading in body, heading


@patch("app.analyysikeskus.routes.gather_institution_competences")
@patch("app.analyysikeskus.routes.search_institutions_by_label")
@patch("app.auth.middleware._get_provider")
def test_padevused_exact_match_short_circuits_to_result(
    mock_provider: MagicMock,
    mock_search: MagicMock,
    mock_gather: MagicMock,
):
    """When the input exactly matches a candidate label, the result page renders."""
    from app.analyysikeskus.competency import InstitutionCandidate

    mock_provider.return_value = _stub_provider()
    # Two candidates — but the input matches the first exactly.
    mock_search.return_value = [
        InstitutionCandidate(uri=_INST_2_URI, label="Andmekaitse Inspektsioon"),
        InstitutionCandidate(uri=_INST_3_URI, label="Tarbijakaitse ja Tehnilise Järelevalve Amet"),
    ]
    mock_gather.return_value = _canned_view()

    client = _authed_client()
    resp = client.get("/analyysikeskus/padevused?sisend=andmekaitse+inspektsioon")
    assert resp.status_code == 200
    body = resp.text
    # We should NOT be on the disambiguation page.
    assert "Sisend võib viidata mitmele asutusele" not in body
    assert "Näitan pädevusi asutuse tasandil" in body


@patch("app.analyysikeskus.routes.gather_institution_competences")
@patch("app.analyysikeskus.routes.get_institution_label")
@patch("app.analyysikeskus.routes.search_institutions_by_label")
@patch("app.auth.middleware._get_provider")
def test_padevused_uri_deep_link_resolves_directly(
    mock_provider: MagicMock,
    mock_search: MagicMock,
    mock_get_label: MagicMock,
    mock_gather: MagicMock,
):
    """A URI-shaped sisend (deep-link from explorer) skips the label search."""
    mock_provider.return_value = _stub_provider()
    mock_get_label.return_value = "Andmekaitse Inspektsioon"
    mock_gather.return_value = _canned_view()

    from urllib.parse import quote

    client = _authed_client()
    # URL-encode the URI so the ``#`` fragment is preserved through the
    # query-string parser (otherwise Starlette strips the fragment).
    resp = client.get(f"/analyysikeskus/padevused?sisend={quote(_INST_2_URI, safe='')}")
    assert resp.status_code == 200
    # The label-search path must NOT be hit on a URI-shaped input.
    mock_search.assert_not_called()
    mock_get_label.assert_called_once_with(_INST_2_URI)


@patch("app.analyysikeskus.routes.gather_institution_competences")
@patch("app.analyysikeskus.routes.search_institutions_by_label")
@patch("app.auth.middleware._get_provider")
def test_padevused_empty_result_renders_friendly_message(
    mock_provider: MagicMock,
    mock_search: MagicMock,
    mock_gather: MagicMock,
):
    """No competences returned ⇒ a friendly "ei leitud" line, not a 500."""
    from app.analyysikeskus.competency import (
        InstitutionCandidate,
        InstitutionCompetences,
    )

    mock_provider.return_value = _stub_provider()
    mock_search.return_value = [
        InstitutionCandidate(uri=_INST_2_URI, label="Andmekaitse Inspektsioon")
    ]
    mock_gather.return_value = InstitutionCompetences(
        institution_uri=_INST_2_URI,
        institution_label="Andmekaitse Inspektsioon",
    )

    client = _authed_client()
    resp = client.get("/analyysikeskus/padevused?sisend=Andmekaitse+Inspektsioon")
    assert resp.status_code == 200
    body = resp.text
    assert "Pädevusi ei leitud" in body
    # Banner still present so the v1 scope note rides through.
    assert "Näitan pädevusi asutuse tasandil" in body


# ---------------------------------------------------------------------------
# 7. Existing routes still register — no breakage
# ---------------------------------------------------------------------------


def test_existing_workflows_still_registered():
    """A3 only appends — the previous workflow routes must still respond."""
    from starlette.testclient import TestClient

    from app.main import app

    client = TestClient(app, follow_redirects=False)
    for path in (
        "/analyysikeskus",
        "/analyysikeskus/normi-mojuahel",
        "/analyysikeskus/el-ulevott",
        "/analyysikeskus/sanktsioonid",
        "/analyysikeskus/padevused",
    ):
        resp = client.get(path)
        assert resp.status_code == 303, path
        assert resp.headers["location"] == "/auth/login", path


# ---------------------------------------------------------------------------
# 8. Capability + analyysikeskus inputs registration
# ---------------------------------------------------------------------------


class TestPadevusedCapabilityRegistration:
    def test_capability_is_live(self):
        """The padevused capability must be ``status="live"`` post-#797."""
        from app.ui.capabilities import get_capability

        cap = get_capability("padevused")
        assert cap is not None
        assert cap.status == "live"
        assert cap.target_url == "/analyysikeskus/padevused"

    def test_inputs_entry_present(self):
        """The analyysikeskus directory needs an _ANALYYSIKESKUS_INPUTS row."""
        from app.analyysikeskus.routes import _ANALYYSIKESKUS_INPUTS

        assert "padevused" in _ANALYYSIKESKUS_INPUTS
        entry = _ANALYYSIKESKUS_INPUTS["padevused"]
        for key in ("placeholder", "aria_label", "examples"):
            assert key in entry
            assert entry[key]
