"""Integration tests for explorer API routes."""

from __future__ import annotations

import json
import uuid
from unittest.mock import MagicMock, patch

from starlette.testclient import TestClient

from app.main import app

# Mock data for SparqlClient responses
_OVERVIEW_DATA = [
    {"type": "http://www.w3.org/2002/07/owl#Class", "count": "150"},
    {"type": "http://www.w3.org/2002/07/owl#NamedIndividual", "count": "5000"},
    {"type": "https://data.riik.ee/ontology/estleg#TopicCluster", "count": "42"},
]

_ENTITIES_DATA = [
    {
        "entity": "https://data.riik.ee/ontology/estleg#Act_1",
        "label": "Asjaõigusseadus",
        "type": "https://data.riik.ee/ontology/estleg#Act",
    },
    {
        "entity": "https://data.riik.ee/ontology/estleg#Act_2",
        "label": "Töölepingu seadus",
        "type": "https://data.riik.ee/ontology/estleg#Act",
    },
]

_SEARCH_DATA = [
    {
        "entity": "https://data.riik.ee/ontology/estleg#Act_TLS",
        "label": "Töölepingu seadus",
        "type": "https://data.riik.ee/ontology/estleg#Act",
    },
]

_ENTITY_METADATA = [
    {
        "predicate": "http://www.w3.org/2000/01/rdf-schema#label",
        "value": "Asjaõigusseadus",
    },
    {
        "predicate": "https://data.riik.ee/ontology/estleg#paragrahv",
        "value": "§ 1",
    },
]

_ENTITY_OUTGOING = [
    {
        "predicate": "http://www.w3.org/1999/02/22-rdf-syntax-ns#type",
        "object": "https://data.riik.ee/ontology/estleg#Act",
        "objectLabel": "Seadus",
    },
]

_ENTITY_INCOMING = [
    {
        "subject": "https://data.riik.ee/ontology/estleg#Provision_1",
        "subjectLabel": "§ 1 lg 1",
        "predicate": "https://data.riik.ee/ontology/estleg#sourceAct",
    },
]

_TIMELINE_DATA = [
    {
        "entity": "https://data.riik.ee/ontology/estleg#Act_1",
        "label": "Asjaõigusseadus",
        "type": "https://data.riik.ee/ontology/estleg#Act",
        "validFrom": "1993-12-01",
        "validUntil": "",
    },
]


def _mock_query(sparql: str, bindings: dict[str, str] | None = None) -> list[dict[str, str]]:
    """Route mock SPARQL queries to appropriate test data."""
    sparql_upper = sparql.upper()
    if "COUNT" in sparql_upper and "GROUP BY" not in sparql_upper:
        # Count queries — return a count depending on context
        if "validFrom" in sparql:
            return [{"count": "1"}]
        return [{"count": "2"}]
    if "GROUP BY ?type" in sparql:
        return _OVERVIEW_DATA
    if "?categoryType" in sparql:
        return _ENTITIES_DATA
    if "REGEX" in sparql_upper:
        return _SEARCH_DATA
    if "isLiteral" in sparql:
        return _ENTITY_METADATA
    if "validFrom" in sparql:
        return _TIMELINE_DATA
    # Outgoing or incoming
    if "?predicate ?object" in sparql:
        return _ENTITY_OUTGOING
    if "?subject" in sparql:
        return _ENTITY_INCOMING
    return []


def _mock_count(sparql: str) -> int:
    if "validFrom" in sparql:
        return 1
    return 2


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestExplorerOverview:
    def test_returns_json(self):
        with patch("app.explorer.routes._get_client") as mock_get:
            mock_client = mock_get.return_value
            mock_client.query.return_value = _OVERVIEW_DATA
            client = TestClient(app)
            resp = client.get("/api/explorer/overview")

        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("application/json")
        data = resp.json()
        assert "data" in data
        assert "meta" in data
        assert isinstance(data["data"], list)
        # Each row should carry name + count after the route's transform.
        for row in data["data"]:
            assert "name" in row
            assert "count" in row

    def test_returns_category_names(self):
        with patch("app.explorer.routes._get_client") as mock_get:
            mock_client = mock_get.return_value
            mock_client.query.return_value = _OVERVIEW_DATA
            client = TestClient(app)
            resp = client.get("/api/explorer/overview")

        assert resp.status_code == 200
        categories = resp.json()["data"]
        names = [c["name"] for c in categories]
        assert "Class" in names
        assert "TopicCluster" in names


class TestExplorerCategory:
    def test_returns_paginated_entities(self):
        with patch("app.explorer.routes._get_client") as mock_get:
            mock_client = mock_get.return_value
            mock_client.query.return_value = _ENTITIES_DATA
            mock_client.count.return_value = 2
            client = TestClient(app)
            resp = client.get(
                "/api/explorer/category/https%3A%2F%2Fdata.riik.ee%2Fontology%2Festleg%23Act"
            )

        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("application/json")
        data = resp.json()
        assert data["meta"]["page"] == 1
        assert data["meta"]["total"] == 2
        assert len(data["data"]) == 2
        # Each entity row carries the fields the D3 client expects.
        for row in data["data"]:
            assert "uri" in row
            assert "label" in row

    def test_pagination_params(self):
        with patch("app.explorer.routes._get_client") as mock_get:
            mock_client = mock_get.return_value
            mock_client.query.return_value = []
            mock_client.count.return_value = 100
            client = TestClient(app)
            resp = client.get(
                "/api/explorer/category/https%3A%2F%2Fdata.riik.ee%2Fontology%2Festleg%23Act"
                "?page=3&size=10"
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["meta"]["page"] == 3
        assert data["meta"]["size"] == 10
        assert data["meta"]["total"] == 100

    def test_invalid_category_returns_400(self):
        client = TestClient(app)
        resp = client.get("/api/explorer/category/not-a-uri")
        assert resp.status_code == 400
        body = resp.json()
        assert "error" in body
        assert isinstance(body["error"], str)
        assert len(body["error"]) > 0


class TestExplorerEntity:
    def test_returns_entity_detail(self):
        with patch("app.explorer.routes._get_client") as mock_get:
            mock_client = mock_get.return_value
            mock_client.query.side_effect = _mock_query
            client = TestClient(app)
            resp = client.get(
                "/api/explorer/entity/https%3A%2F%2Fdata.riik.ee%2Fontology%2Festleg%23Act_1"
            )

        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["uri"] == "https://data.riik.ee/ontology/estleg#Act_1"
        assert "metadata" in data
        assert "outgoing" in data
        assert "incoming" in data

    def test_invalid_entity_uri_returns_400(self):
        client = TestClient(app)
        resp = client.get("/api/explorer/entity/not-a-uri")
        assert resp.status_code == 400


class TestExplorerSearch:
    def test_search_returns_results(self):
        with patch("app.explorer.routes._get_client") as mock_get:
            mock_client = mock_get.return_value
            mock_client.query.return_value = _SEARCH_DATA
            client = TestClient(app)
            resp = client.get("/api/explorer/search?q=Töölepingu")

        assert resp.status_code == 200
        data = resp.json()
        assert len(data["data"]) == 1
        assert data["data"][0]["label"] == "Töölepingu seadus"
        assert data["meta"]["query"] == "Töölepingu"

    def test_search_with_estonian_chars(self):
        """Ensure Estonian characters (ä, ö, ü, õ, š, ž) work in searches."""
        with patch("app.explorer.routes._get_client") as mock_get:
            mock_client = mock_get.return_value
            mock_client.query.return_value = [
                {
                    "entity": "https://data.riik.ee/ontology/estleg#Act_Äri",
                    "label": "Äriseadustik",
                    "type": "https://data.riik.ee/ontology/estleg#Act",
                },
            ]
            client = TestClient(app)
            resp = client.get("/api/explorer/search?q=Äriseadustik")

        assert resp.status_code == 200
        assert resp.json()["data"][0]["label"] == "Äriseadustik"
        assert resp.json()["meta"]["query"] == "Äriseadustik"

    def test_empty_query_returns_empty(self):
        client = TestClient(app)
        resp = client.get("/api/explorer/search?q=")
        assert resp.status_code == 200
        assert resp.json()["data"] == []

    def test_missing_query_returns_empty(self):
        client = TestClient(app)
        resp = client.get("/api/explorer/search")
        assert resp.status_code == 200
        assert resp.json()["data"] == []

    def test_search_limit_param(self):
        with patch("app.explorer.routes._get_client") as mock_get:
            mock_client = mock_get.return_value
            mock_client.query.return_value = _SEARCH_DATA
            client = TestClient(app)
            resp = client.get("/api/explorer/search?q=test&limit=5")

        assert resp.status_code == 200
        # Check that the query was called (we just verify the endpoint works)
        mock_client.query.assert_called_once()


class TestExplorerTimeline:
    def test_timeline_returns_entities(self):
        with patch("app.explorer.routes._get_client") as mock_get:
            mock_client = mock_get.return_value
            mock_client.query.return_value = _TIMELINE_DATA
            mock_client.count.return_value = 1
            client = TestClient(app)
            resp = client.get("/api/explorer/timeline?date=2024-01-01")

        assert resp.status_code == 200
        data = resp.json()
        assert len(data["data"]) == 1
        assert data["meta"]["date"] == "2024-01-01"
        assert data["meta"]["total"] == 1

    def test_missing_date_returns_400(self):
        client = TestClient(app)
        resp = client.get("/api/explorer/timeline")
        assert resp.status_code == 400
        assert "error" in resp.json()

    def test_invalid_date_format_returns_400(self):
        client = TestClient(app)
        resp = client.get("/api/explorer/timeline?date=not-a-date")
        assert resp.status_code == 400

    def test_timeline_pagination(self):
        with patch("app.explorer.routes._get_client") as mock_get:
            mock_client = mock_get.return_value
            mock_client.query.return_value = []
            mock_client.count.return_value = 50
            client = TestClient(app)
            resp = client.get("/api/explorer/timeline?date=2024-01-01&page=2&size=10")

        data = resp.json()
        assert data["meta"]["page"] == 2
        assert data["meta"]["size"] == 10


class TestExplorerAuthSkip:
    """Verify that explorer endpoints do not require authentication."""

    def test_overview_no_auth_required(self):
        with patch("app.explorer.routes._get_client") as mock_get:
            mock_client = mock_get.return_value
            mock_client.query.return_value = []
            client = TestClient(app, follow_redirects=False)
            resp = client.get("/api/explorer/overview")

        # Should NOT redirect to login (303)
        assert resp.status_code == 200

    def test_search_no_auth_required(self):
        client = TestClient(app, follow_redirects=False)
        resp = client.get("/api/explorer/search?q=test")
        # Even without mock, empty query returns 200
        # But with mock we get 200 for sure
        # Without Jena running the query returns empty due to error handling
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Phase 2 Batch 4 — Explorer draft overlay
# ---------------------------------------------------------------------------


_OVERLAY_ORG_ID = "11111111-1111-1111-1111-111111111111"
_OVERLAY_OTHER_ORG_ID = "22222222-2222-2222-2222-222222222222"
_OVERLAY_USER_ID = "33333333-3333-3333-3333-333333333333"
_OVERLAY_DRAFT_ID = uuid.UUID("44444444-4444-4444-4444-444444444444")


def _overlay_user(org_id: str = _OVERLAY_ORG_ID) -> dict:
    return {
        "id": _OVERLAY_USER_ID,
        "email": "drafter@seadusloome.ee",
        "full_name": "Test Drafter",
        "role": "drafter",
        "org_id": org_id,
    }


def _overlay_provider(org_id: str = _OVERLAY_ORG_ID) -> MagicMock:
    provider = MagicMock()
    provider.get_current_user.return_value = _overlay_user(org_id)
    return provider


def _overlay_authed_client() -> TestClient:
    client = TestClient(app, follow_redirects=False)
    client.cookies.set("access_token", "stub-token")
    return client


class _ConnectCM:
    """Context-manager wrapper around the explorer overlay DB mock."""

    def __init__(self, conn: MagicMock):
        self.conn = conn

    def __enter__(self) -> MagicMock:
        return self.conn

    def __exit__(self, *_):
        return False


def _make_overlay_conn(
    *,
    draft_org_id: str | None,
    findings: dict | None,
) -> MagicMock:
    """Build a connection mock matching the two SELECTs in the overlay path.

    The first SELECT returns ``(org_id,)``; the second returns
    ``(report_data_jsonb,)``. ``draft_org_id=None`` simulates a missing
    draft row; ``findings=None`` simulates a missing report row.
    """
    conn = MagicMock()
    cursor1 = MagicMock()
    cursor1.fetchone.return_value = (draft_org_id,) if draft_org_id else None
    cursor2 = MagicMock()
    cursor2.fetchone.return_value = (findings,) if findings is not None else None
    conn.execute.side_effect = [cursor1, cursor2]
    return conn


class TestExplorerDraftOverlay:
    """End-to-end overlay tests using a real authenticated session.

    Rewritten for #442: previously these tests stubbed
    ``_fetch_draft_overlay`` directly, which masked the bug where
    ``/explorer`` was in ``SKIP_PATHS`` and ``req.scope['auth']`` was
    therefore always missing. Now we go through the auth middleware
    by stubbing ``_get_provider`` (matching the
    ``tests/test_docs_routes.py`` pattern) and stubbing the underlying
    DB connection used by ``_fetch_draft_overlay``. Any future
    regression that bypasses the middleware would surface as a 303
    redirect to ``/auth/login``.
    """

    @patch("app.explorer.pages._connect")
    @patch("app.auth.middleware._get_provider")
    def test_own_org_draft_embeds_overlay_data(
        self,
        mock_get_provider: MagicMock,
        mock_connect: MagicMock,
    ):
        mock_get_provider.return_value = _overlay_provider()
        # First SELECT: draft.org_id matches our user; second SELECT:
        # impact_reports.report_data carrying two affected entities.
        report_data = {
            "affected_entities": [
                {"uri": "urn:x:1"},
                {"uri": "urn:x:2"},
            ]
        }
        conn = _make_overlay_conn(
            draft_org_id=_OVERLAY_ORG_ID,
            findings=report_data,
        )
        mock_connect.return_value = _ConnectCM(conn)

        client = _overlay_authed_client()
        resp = client.get(f"/explorer?draft={_OVERLAY_DRAFT_ID}")

        assert resp.status_code == 200, (
            f"explorer page must require auth via cookie (#442); got {resp.status_code}"
        )
        # The JSON blob is embedded in a <script id="draft-overlay-data"> tag.
        assert 'id="draft-overlay-data"' in resp.text
        assert "urn:x:1" in resp.text
        assert "urn:x:2" in resp.text

        # Validate the embedded JSON parses cleanly.
        start = resp.text.find('id="draft-overlay-data"')
        assert start != -1
        script_open = resp.text.find(">", start) + 1
        script_close = resp.text.find("</script>", script_open)
        payload = resp.text[script_open:script_close]
        # The XSS-escape (#464) writes ``<\/`` for ``</`` so we have to
        # un-escape before json.loads. JSON allows ``\/`` natively.
        parsed = json.loads(payload)
        assert "uris" in parsed
        assert "urn:x:1" in parsed["uris"]
        assert "urn:x:2" in parsed["uris"]

    @patch("app.explorer.pages._connect")
    @patch("app.auth.middleware._get_provider")
    def test_cross_org_draft_drops_overlay_silently(
        self,
        mock_get_provider: MagicMock,
        mock_connect: MagicMock,
    ):
        mock_get_provider.return_value = _overlay_provider()
        # Draft belongs to a different org — _fetch_draft_overlay
        # short-circuits on the org check and returns an empty list.
        conn = _make_overlay_conn(
            draft_org_id=_OVERLAY_OTHER_ORG_ID,
            findings=None,
        )
        mock_connect.return_value = _ConnectCM(conn)

        client = _overlay_authed_client()
        resp = client.get(f"/explorer?draft={_OVERLAY_DRAFT_ID}")

        # Page still renders normally — no overlay tag, no error UI.
        assert resp.status_code == 200
        assert 'id="draft-overlay-data"' not in resp.text
        # The explorer page still works (Otsi search button as a smoke check).
        assert "Otsi" in resp.text

    @patch("app.auth.middleware._get_provider")
    def test_malformed_draft_param_drops_overlay_silently(
        self,
        mock_get_provider: MagicMock,
    ):
        mock_get_provider.return_value = _overlay_provider()
        # _fetch_draft_overlay short-circuits before any DB lookup when
        # the UUID is malformed, so no _connect mock is needed.
        client = _overlay_authed_client()
        resp = client.get("/explorer?draft=not-a-uuid")

        assert resp.status_code == 200
        assert 'id="draft-overlay-data"' not in resp.text
        # Standard explorer chrome is still present.
        assert "Otsi" in resp.text

    def test_unauthenticated_explorer_redirects_to_login(self):
        """Regression for #442: /explorer is no longer in SKIP_PATHS."""
        from app.main import app

        client = TestClient(app, follow_redirects=False)
        resp = client.get("/explorer")
        assert resp.status_code == 303
        assert resp.headers["location"] == "/auth/login"

    @patch("app.explorer.pages._connect")
    @patch("app.auth.middleware._get_provider")
    def test_xss_escape_in_overlay_payload(
        self,
        mock_get_provider: MagicMock,
        mock_connect: MagicMock,
    ):
        """Regression for #464: closing-tag sequences in URIs must be escaped.

        An attacker who can plant an entity URI containing
        ``</script>`` should not be able to break out of the JSON
        ``<script>`` tag and inject HTML into the page.
        """
        mock_get_provider.return_value = _overlay_provider()
        report_data = {
            "affected_entities": [
                {"uri": "urn:x</script><script>alert(1)</script>"},
            ]
        }
        conn = _make_overlay_conn(
            draft_org_id=_OVERLAY_ORG_ID,
            findings=report_data,
        )
        mock_connect.return_value = _ConnectCM(conn)

        client = _overlay_authed_client()
        resp = client.get(f"/explorer?draft={_OVERLAY_DRAFT_ID}")

        assert resp.status_code == 200
        # The JSON tag must contain the escaped form ``<\/script>``.
        assert "<\\/script>" in resp.text
        # The injected literal sequence must NOT appear unescaped within
        # the draft-overlay-data tag (we look between the tag's opening
        # ``>`` and the next ``</script>`` close).
        start = resp.text.find('id="draft-overlay-data"')
        assert start != -1
        script_open = resp.text.find(">", start) + 1
        script_close = resp.text.find("</script>", script_open)
        payload = resp.text[script_open:script_close]
        # The closing-tag sequence inside the JSON payload must have
        # been rewritten so that the script tag is not prematurely
        # terminated.
        assert "</script>" not in payload
        assert "<\\/script>" in payload
        # And the JSON should still round-trip cleanly.
        parsed = json.loads(payload)
        assert "uris" in parsed
        assert any("</script>" in uri for uri in parsed["uris"]), (
            "the original payload should still decode back to the literal "
            "</script> sequence after JSON unescaping"
        )
