"""Round-2 #844: legacy stored-report conflict masking at every consumer.

A report persisted *before* tenant scoping landed still carries FOREIGN
org draft URIs/labels (and possibly a stale adhoc-probe row) in its
``conflicts`` list. This module proves all four render/return surfaces
scrub them:

1. The full report section render (``GET /drafts/{id}/report``).
2. The "Näita rohkem" pagination fragment
   (``GET /drafts/{id}/report/section/conflicts``).
3. The explorer draft-subgraph JSON
   (``GET /explorer/draft-subgraph/{id}``).
4. The chat ``get_draft_impact`` tool result (what goes back to the LLM).

Same-org conflict rows must pass through unmasked at every surface.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch

from starlette.testclient import TestClient

from app.chat.tools import execute_tool
from app.docs.draft_model import Draft
from app.docs.impact.masking import _MASKED_CONFLICT_LABEL
from app.ontology.sparql_client import SparqlClient

# ---------------------------------------------------------------------------
# Shared identities
# ---------------------------------------------------------------------------

_ORG_ID = "11111111-1111-1111-1111-111111111111"
_USER_ID = "33333333-3333-3333-3333-333333333333"
_DRAFT_ID = uuid.UUID("44444444-4444-4444-4444-444444444444")
_REPORT_ID = uuid.UUID("55555555-5555-5555-5555-555555555555")

# A draft the viewer's org owns (the conflict points at it → keep).
_OWN_OTHER_DRAFT = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
_OWN_OTHER_SELF = f"https://data.riik.ee/ontology/estleg/drafts/{_OWN_OTHER_DRAFT}#self"
# A draft another org owns (the conflict points at it → mask).
_FOREIGN_DRAFT = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
_FOREIGN_SELF = f"https://data.riik.ee/ontology/estleg/drafts/{_FOREIGN_DRAFT}#self"
_FOREIGN_LABEL = "TEISE ASUTUSE SALAJANE EELNÕU"

# The owned-draft set the viewer's org actually owns.
_OWNED_IDS = {str(_DRAFT_ID), _OWN_OTHER_DRAFT}


def _legacy_findings() -> dict[str, Any]:
    """A persisted report mixing an own-org and a foreign-org conflict row."""
    return {
        "affected_entities": [
            {"uri": "urn:x:1", "label": "§ 1", "type": "estleg#LegalProvision"},
        ],
        "conflicts": [
            {
                "draft_ref": "§ 5",
                "conflicting_entity": _OWN_OTHER_SELF,
                "conflicting_label": "Meie teine eelnõu",
                "reason": "Teine eelnõu viitab juba sellele sättele",
                "relation": "https://data.riik.ee/ontology/estleg#references",
            },
            {
                "draft_ref": "§ 5",
                "conflicting_entity": _FOREIGN_SELF,
                "conflicting_label": _FOREIGN_LABEL,
                "reason": "Teine eelnõu viitab juba sellele sättele",
                "relation": "https://data.riik.ee/ontology/estleg#references",
            },
        ],
        "eu_compliance": [],
        "gaps": [],
    }


# ===========================================================================
# Sites 1 + 2 — report page + pagination fragment
# ===========================================================================


def _authed_user() -> dict[str, Any]:
    return {
        "id": _USER_ID,
        "email": "koostaja@seadusloome.ee",
        "full_name": "Test Koostaja",
        "role": "drafter",
        "org_id": _ORG_ID,
    }


def _make_draft() -> Draft:
    now = datetime.now(UTC)
    return Draft(
        id=_DRAFT_ID,
        user_id=uuid.UUID(_USER_ID),
        org_id=uuid.UUID(_ORG_ID),
        title="Test eelnõu",
        filename="eelnou.docx",
        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        file_size=2048,
        storage_path="/tmp/cipher.enc",
        graph_uri=f"https://data.riik.ee/ontology/estleg/drafts/{_DRAFT_ID}",
        status="ready",
        parsed_text_encrypted=None,
        entity_count=None,
        error_message=None,
        created_at=now,
        updated_at=now,
    )


def _make_report_row(findings: dict[str, Any]) -> tuple:
    return (
        _REPORT_ID,
        _DRAFT_ID,
        1,
        2,
        0,
        50,
        findings,
        "2026-04-09T12:00+00:00@1061123",
        datetime(2026, 4, 9, 12, 0, tzinfo=UTC),
    )


def _stub_provider() -> MagicMock:
    provider = MagicMock()
    provider.get_current_user.return_value = _authed_user()
    return provider


def _authed_client() -> TestClient:
    client = TestClient(__import__("app.main", fromlist=["app"]).app, follow_redirects=False)
    client.cookies.set("access_token", "stub-token")
    return client


def _owned_conn_cm() -> MagicMock:
    """A ``get_connection()`` context-manager mock whose conn returns the
    viewer's owned draft ids from ``fetch_owned_draft_ids``.

    Used to stand in for the connection ``mask_stored_conflict_rows``
    opens on the report-page path (which holds no connection of its own).
    """
    conn = MagicMock()
    cursor = MagicMock()
    cursor.fetchall.return_value = [(i,) for i in _OWNED_IDS]
    conn.execute.return_value = cursor
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=conn)
    cm.__exit__ = MagicMock(return_value=False)
    return cm


class TestReportPageMasking:
    @patch("app.db.get_connection")
    @patch("app.docs.report_routes._fetch_latest_report_version_id", return_value="")
    @patch("app.docs.report_routes._fetch_latest_report")
    @patch("app.docs.report_routes.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_full_report_render_masks_foreign_keeps_own(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_fetch_report: MagicMock,
        mock_version: MagicMock,
        mock_db_conn: MagicMock,
    ):
        mock_get_provider.return_value = _stub_provider()
        mock_fetch.return_value = _make_draft()
        mock_fetch_report.return_value = _make_report_row(_legacy_findings())
        mock_db_conn.return_value = _owned_conn_cm()

        resp = _authed_client().get(f"/drafts/{_DRAFT_ID}/report")
        assert resp.status_code == 200
        # Foreign-org identity scrubbed.
        assert _FOREIGN_LABEL not in resp.text
        assert _FOREIGN_DRAFT not in resp.text
        # Masked placeholder shown instead.
        assert _MASKED_CONFLICT_LABEL in resp.text
        # Own-org conflict row preserved.
        assert "Meie teine eelnõu" in resp.text

    @patch("app.db.get_connection")
    @patch("app.docs.report_routes._fetch_latest_report_version_id", return_value="")
    @patch("app.docs.report_routes._fetch_latest_report")
    @patch("app.docs.report_routes.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_pagination_fragment_masks_foreign(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_fetch_report: MagicMock,
        mock_version: MagicMock,
        mock_db_conn: MagicMock,
    ):
        mock_get_provider.return_value = _stub_provider()
        mock_fetch.return_value = _make_draft()
        mock_fetch_report.return_value = _make_report_row(_legacy_findings())
        mock_db_conn.return_value = _owned_conn_cm()

        resp = _authed_client().get(
            f"/drafts/{_DRAFT_ID}/report/section/conflicts?offset=0&limit=50"
        )
        assert resp.status_code == 200
        assert _FOREIGN_LABEL not in resp.text
        assert _FOREIGN_DRAFT not in resp.text
        assert "Meie teine eelnõu" in resp.text


# ===========================================================================
# Site 3 — explorer draft-subgraph JSON
# ===========================================================================


def _explorer_user() -> dict[str, Any]:
    return {
        "id": _USER_ID,
        "email": "k@x.ee",
        "full_name": "K",
        "role": "drafter",
        "org_id": _ORG_ID,
    }


def _explorer_provider() -> MagicMock:
    p = MagicMock()
    p.get_current_user.return_value = _explorer_user()
    return p


def _explorer_client() -> TestClient:
    c = TestClient(__import__("app.main", fromlist=["app"]).app, follow_redirects=False)
    c.cookies.set("access_token", "stub-token")
    return c


class _ConnCM:
    def __init__(self, conn: Any) -> None:
        self._conn = conn

    def __enter__(self) -> Any:
        return self._conn

    def __exit__(self, *a: Any) -> bool:
        return False


def _subgraph_conn(findings: dict[str, Any]) -> MagicMock:
    """Mock for explorer_draft_subgraph: SELECT draft, SELECT report, then
    the #844 owned-draft lookup. The owned-draft query is patched
    separately, so only the first two cursors matter here."""
    conn = MagicMock()
    cur_draft = MagicMock()
    cur_draft.fetchone.return_value = (_ORG_ID, "Test eelnõu")
    cur_report = MagicMock()
    cur_report.fetchone.return_value = (findings,)
    cur_owned = MagicMock()
    cur_owned.fetchall.return_value = [(i,) for i in _OWNED_IDS]
    conn.execute.side_effect = [cur_draft, cur_report, cur_owned]
    return conn


class TestExplorerSubgraphMasking:
    @patch("app.explorer.routes._connect")
    @patch("app.auth.middleware._get_provider")
    def test_subgraph_masks_foreign_keeps_own(
        self,
        mock_get_provider: MagicMock,
        mock_connect: MagicMock,
    ):
        mock_get_provider.return_value = _explorer_provider()
        mock_connect.return_value = _ConnCM(_subgraph_conn(_legacy_findings()))

        resp = _explorer_client().get(f"/explorer/draft-subgraph/{_DRAFT_ID}")
        assert resp.status_code == 200
        raw = resp.text
        body = resp.json()
        node_ids = {n["id"] for n in body["data"]["nodes"]}
        # Foreign draft URI must not appear anywhere in the JSON.
        assert _FOREIGN_SELF not in raw
        assert _FOREIGN_DRAFT not in raw
        assert _FOREIGN_LABEL not in raw
        assert _FOREIGN_SELF not in node_ids
        # Own-org conflict node preserved (its URI is an owned draft).
        assert _OWN_OTHER_SELF in node_ids


# ===========================================================================
# Site 4 — chat get_draft_impact tool result (LLM context)
# ===========================================================================


def _make_sparql() -> SparqlClient:
    client = SparqlClient.__new__(SparqlClient)
    client.jena_url = "http://localhost:3030"
    client.dataset = "ontology"
    client.timeout = 5.0
    client.query = MagicMock(return_value=[])  # type: ignore[assignment]
    return client


_CHAT_AUTH = {"id": _USER_ID, "org_id": _ORG_ID}


class TestChatGetDraftImpactMasking:
    @patch("app.docs.impact.masking.fetch_owned_draft_ids")
    @patch("app.chat.tools.get_connection")
    def test_tool_result_masks_foreign_conflict(
        self,
        mock_get_conn: MagicMock,
        mock_owned: MagicMock,
    ):
        mock_conn = MagicMock()
        mock_get_conn.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_get_conn.return_value.__exit__ = MagicMock(return_value=False)
        mock_conn.execute.return_value.fetchone.return_value = (_legacy_findings(),)
        mock_owned.return_value = set(_OWNED_IDS)

        result = asyncio.run(
            execute_tool(
                "get_draft_impact",
                {"draft_id": str(_DRAFT_ID)},
                _make_sparql(),
                auth=_CHAT_AUTH,
            )
        )
        assert "report" in result
        report = result["report"]
        blob = repr(report)
        # The foreign draft identity must not reach the model context.
        assert _FOREIGN_LABEL not in blob
        assert _FOREIGN_DRAFT not in blob
        # Conflict count preserved (both rows kept, one masked).
        assert report["conflict_count"] == 2
        labels = {str(r.get("conflicting_label") or "") for r in report["conflicts"]}
        assert _MASKED_CONFLICT_LABEL in labels
        assert "Meie teine eelnõu" in labels

    @patch("app.docs.impact.masking.fetch_owned_draft_ids")
    @patch("app.chat.tools.get_connection")
    def test_same_org_conflicts_pass_through(
        self,
        mock_get_conn: MagicMock,
        mock_owned: MagicMock,
    ):
        mock_conn = MagicMock()
        mock_get_conn.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_get_conn.return_value.__exit__ = MagicMock(return_value=False)
        # Both conflicts point at owned drafts → nothing masked.
        findings = {
            "conflicts": [
                {
                    "draft_ref": "§ 5",
                    "conflicting_entity": _OWN_OTHER_SELF,
                    "conflicting_label": "Meie teine eelnõu",
                    "reason": "r",
                }
            ],
            "gaps": [],
        }
        mock_conn.execute.return_value.fetchone.return_value = (findings,)
        mock_owned.return_value = set(_OWNED_IDS)

        result = asyncio.run(
            execute_tool(
                "get_draft_impact",
                {"draft_id": str(_DRAFT_ID)},
                _make_sparql(),
                auth=_CHAT_AUTH,
            )
        )
        report = result["report"]
        assert _MASKED_CONFLICT_LABEL not in repr(report)
        assert report["conflicts"][0]["conflicting_label"] == "Meie teine eelnõu"
