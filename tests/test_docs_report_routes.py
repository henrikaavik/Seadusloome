"""Integration tests for the Phase 2 Batch 4 impact-report routes.

Same patching strategy as ``tests/test_docs_routes.py``: stub the
auth provider via ``_get_provider`` and the DB lookups via the route
module's helper imports. The job queue is patched via the
``app.docs.report_routes.JobQueue`` symbol.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch

from starlette.testclient import TestClient

from app.docs.draft_model import Draft
from app.jobs.queue import Job

_ORG_ID = "11111111-1111-1111-1111-111111111111"
_OTHER_ORG_ID = "22222222-2222-2222-2222-222222222222"
_USER_ID = "33333333-3333-3333-3333-333333333333"

_DRAFT_ID = uuid.UUID("44444444-4444-4444-4444-444444444444")
_REPORT_ID = uuid.UUID("55555555-5555-5555-5555-555555555555")


def _authed_user() -> dict[str, Any]:
    return {
        "id": _USER_ID,
        "email": "koostaja@seadusloome.ee",
        "full_name": "Test Koostaja",
        "role": "drafter",
        "org_id": _ORG_ID,
    }


def _make_draft(
    *,
    org_id: str = _ORG_ID,
    title: str = "Test eelnõu",
    status: str = "ready",
) -> Draft:
    now = datetime.now(UTC)
    return Draft(
        id=_DRAFT_ID,
        user_id=uuid.UUID(_USER_ID),
        org_id=uuid.UUID(org_id),
        title=title,
        filename="eelnou.docx",
        content_type=("application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
        file_size=2048,
        storage_path="/tmp/cipher.enc",
        graph_uri=f"https://data.riik.ee/ontology/estleg/drafts/{_DRAFT_ID}",
        status=status,
        parsed_text_encrypted=None,
        entity_count=None,
        error_message=None,
        created_at=now,
        updated_at=now,
    )


def _make_report_row(
    *,
    affected: int = 2,
    conflicts: int = 1,
    gaps: int = 0,
    score: int = 42,
    findings: dict | None = None,
) -> tuple:
    findings = findings or {
        "affected_entities": [
            {
                "uri": "urn:x:1",
                "label": "Märkimisväärne säte",
                "type": "https://data.riik.ee/ontology/estleg#EnactedLaw",
            }
            for _ in range(affected)
        ],
        "conflicts": [
            {
                "draft_ref": "Eelnõu § 1",
                "conflicting_entity": "urn:x:c1",
                "conflicting_label": "Vana säte",
                "reason": "Vastuolu",
            }
            for _ in range(conflicts)
        ],
        "eu_compliance": [],
        "gaps": [
            {
                "topic_cluster": "urn:cluster:1",
                "topic_cluster_label": "Andmekaitse",
                "total_provisions": "10",
                "referenced_provisions": "2",
                "description": "Vähene kaetus",
            }
            for _ in range(gaps)
        ],
    }
    return (
        _REPORT_ID,
        _DRAFT_ID,
        affected,
        conflicts,
        gaps,
        score,
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


def _make_job(
    *,
    job_id: int = 7,
    status: str = "running",
    payload: dict | None = None,
    result: dict | None = None,
    error_message: str | None = None,
) -> Job:
    now = datetime.now(UTC)
    return Job(
        id=job_id,
        job_type="export_report",
        payload=payload or {"draft_id": str(_DRAFT_ID), "report_id": str(_REPORT_ID)},
        status=status,
        priority=10,
        attempts=0,
        max_attempts=3,
        claimed_by=None,
        claimed_at=None,
        started_at=now,
        finished_at=None,
        error_message=error_message,
        result=result,
        scheduled_for=now,
        created_at=now,
    )


# ---------------------------------------------------------------------------
# Auth required
# ---------------------------------------------------------------------------


class TestAuthRequired:
    def test_report_page_redirects_unauthenticated(self):
        from app.main import app

        client = TestClient(app, follow_redirects=False)
        resp = client.get(f"/drafts/{_DRAFT_ID}/report")
        assert resp.status_code == 303
        assert resp.headers["location"] == "/auth/login"

    def test_export_post_redirects_unauthenticated(self):
        from app.main import app

        client = TestClient(app, follow_redirects=False)
        resp = client.post(f"/drafts/{_DRAFT_ID}/export")
        assert resp.status_code == 303
        assert resp.headers["location"] == "/auth/login"


# ---------------------------------------------------------------------------
# GET /drafts/{id}/report
# ---------------------------------------------------------------------------


class TestDraftReportPage:
    @patch("app.docs.report_routes._fetch_latest_report")
    @patch("app.docs.report_routes.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_own_org_report_renders_summary(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_fetch_report: MagicMock,
    ):
        mock_get_provider.return_value = _stub_provider()
        mock_fetch.return_value = _make_draft()
        mock_fetch_report.return_value = _make_report_row(score=72)

        client = _authed_client()
        resp = client.get(f"/drafts/{_DRAFT_ID}/report")

        assert resp.status_code == 200
        assert "Test eelnõu" in resp.text
        # Summary card contents
        assert "Mõjuskoor" in resp.text
        assert "72/100" in resp.text
        assert "Mõjutatud üksused" in resp.text
        assert "Konfliktid" in resp.text
        assert "EL-i õigusaktide vastavus" in resp.text
        assert "Lüngad" in resp.text
        # Back link to draft
        assert f"/drafts/{_DRAFT_ID}" in resp.text
        # Open-in-explorer link with draft overlay param
        assert f"/explorer?draft={_DRAFT_ID}" in resp.text
        # #724: cross-links into Analüüsikeskus + Nõustaja from the header.
        assert f"/analyysikeskus/normi-mojuahel?sisend={_DRAFT_ID}" in resp.text
        assert "Ava analüüsikeskuses" in resp.text
        assert f"/chat/new?draft={_DRAFT_ID}" in resp.text
        assert "Küsi nõustajalt selle eelnõu kohta" in resp.text
        # Export action
        assert "Laadi alla .docx" in resp.text

    @patch("app.docs.report_routes._fetch_latest_report")
    @patch("app.docs.report_routes.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_no_conflicts_shows_success_alert(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_fetch_report: MagicMock,
    ):
        mock_get_provider.return_value = _stub_provider()
        mock_fetch.return_value = _make_draft()
        mock_fetch_report.return_value = _make_report_row(conflicts=0)

        client = _authed_client()
        resp = client.get(f"/drafts/{_DRAFT_ID}/report")

        assert resp.status_code == 200
        assert "Konflikte ei tuvastatud." in resp.text

    @patch("app.docs.report_routes.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_cross_org_returns_404_page(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
    ):
        mock_get_provider.return_value = _stub_provider()
        mock_fetch.return_value = _make_draft(org_id=_OTHER_ORG_ID)

        client = _authed_client()
        resp = client.get(f"/drafts/{_DRAFT_ID}/report")

        assert resp.status_code == 404
        assert "Eelnõu ei leitud" in resp.text

    @patch("app.docs.report_routes._fetch_latest_report")
    @patch("app.docs.report_routes.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_missing_report_returns_404_page(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_fetch_report: MagicMock,
    ):
        mock_get_provider.return_value = _stub_provider()
        mock_fetch.return_value = _make_draft()
        mock_fetch_report.return_value = None

        client = _authed_client()
        resp = client.get(f"/drafts/{_DRAFT_ID}/report")

        assert resp.status_code == 404
        assert "Eelnõu ei leitud" in resp.text

    @patch("app.docs.report_routes.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_invalid_uuid_returns_404_page(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
    ):
        mock_get_provider.return_value = _stub_provider()

        client = _authed_client()
        resp = client.get("/drafts/not-a-uuid/report")

        assert resp.status_code == 404
        assert "Eelnõu ei leitud" in resp.text
        # fetch_draft must NOT be called when UUID parsing fails.
        mock_fetch.assert_not_called()

    @patch("app.docs.report_routes._fetch_latest_report")
    @patch("app.docs.report_routes.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_partial_match_row_renders_as_plain_text_with_act_phrase(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_fetch_report: MagicMock,
    ):
        """Wave 2 Step 5A (P2 review follow-up,
        docs/2026-05-18-bugfix-plan.md): an act-level partial match
        (``estleg:referencesAct "<title>"``) must surface in the
        "Mõjutatud üksused" table alongside any full URI matches. The
        partial row must:

          * carry the Estonian "Akt (sätet ei leitud)" phrasing in the
            Tüüp column,
          * render the act title in the Nimetus + URI columns as plain
            text (NOT as an ``<a href=/explorer?focus=…>`` anchor),
          * show the dedicated ``referencesAct`` Estonian legal phrase
            in the Seose liik column.
        """
        mock_get_provider.return_value = _stub_provider()
        mock_fetch.return_value = _make_draft()
        # Two rows: one full URI match (existing path) + one
        # literal-edge partial match (new path).
        partial_findings = {
            "affected_entities": [
                {
                    "uri": "https://data.riik.ee/ontology/estleg#KarS_Par_133",
                    "label": "KarS § 133",
                    "type": "https://data.riik.ee/ontology/estleg#LegalProvision",
                    "relation": "https://data.riik.ee/ontology/estleg#references",
                },
                {
                    # Literal-edge partial match. uri carries the act
                    # title, label echoes it, type is empty.
                    "uri": "Riigieelarve seadus",
                    "label": "Riigieelarve seadus",
                    "type": "",
                    "relation": "https://data.riik.ee/ontology/estleg#referencesAct",
                },
            ],
            "conflicts": [],
            "eu_compliance": [],
            "gaps": [],
        }
        mock_fetch_report.return_value = _make_report_row(
            affected=2, conflicts=0, gaps=0, findings=partial_findings
        )

        client = _authed_client()
        resp = client.get(f"/drafts/{_DRAFT_ID}/report")

        assert resp.status_code == 200
        body = resp.text
        # Both rows present.
        assert "KarS § 133" in body
        assert "Riigieelarve seadus" in body
        # Partial-match-specific UI strings:
        # - "Akt (sätet ei leitud)" phrase in the Tüüp column.
        assert "Akt (sätet ei leitud)" in body, (
            "Partial-match row must show 'Akt (sätet ei leitud)' in "
            "the Tüüp column — see Wave 2 Step 5A of "
            "docs/2026-05-18-bugfix-plan.md."
        )
        # - "viitab aktile (sätet ei leitud)" phrase from
        #   :mod:`app.ontology.relations.LEGAL_PHRASES` in Seose liik.
        assert "viitab aktile (sätet ei leitud)" in body, (
            "Partial-match row must show the Estonian legal phrase "
            "for referencesAct — see LEGAL_PHRASES[REFERENCES_ACT]."
        )
        # The URI column for the URI-shaped row must still be an
        # anchor pointing to the explorer.
        assert "/explorer?focus=" in body
        # The act title must NOT be wrapped in an /explorer?focus link
        # — there's no URI to focus on. The cheap proof is "no anchor
        # whose visible text equals the literal title". We assert this
        # by looking for ``>Riigieelarve seadus</a>`` and confirming
        # absence.
        assert ">Riigieelarve seadus</a>" not in body, (
            "Partial-match act title must NOT render as an anchor "
            "(no URI to focus on) — see Wave 2 Step 5A."
        )


# ---------------------------------------------------------------------------
# #815 — unresolved EU references warning block
# ---------------------------------------------------------------------------
#
# The analyze_handler persists ``ref_type='eu_act'`` rows that the
# resolver couldn't map into ``report_data["unresolved_eu_refs"]``.
# When non-empty, the report page surfaces a warning alert near the
# EL-i õigusaktide section so the user knows the analysis is missing
# coverage rather than treating "no EU findings" as "no EU impact".


class TestUnresolvedEuRefsSection:
    @patch("app.docs.report_routes._fetch_latest_report")
    @patch("app.docs.report_routes.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_warning_shown_when_unresolved_refs_present(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_fetch_report: MagicMock,
    ):
        """A draft mentioning GDPR + Working Conditions whose CELEXes
        weren't mapped must show a "kaardistamata viited" warning
        listing both CELEX numbers as inline <code>.
        """
        mock_get_provider.return_value = _stub_provider()
        mock_fetch.return_value = _make_draft()
        findings = {
            "affected_entities": [],
            "conflicts": [],
            "eu_compliance": [],
            "gaps": [],
            "unresolved_eu_refs": [
                {"ref_text": "32016R0679", "confidence": 0.95},
                {"ref_text": "32019L1152", "confidence": 0.88},
            ],
        }
        mock_fetch_report.return_value = _make_report_row(
            affected=0, conflicts=0, gaps=0, findings=findings
        )

        client = _authed_client()
        resp = client.get(f"/drafts/{_DRAFT_ID}/report")

        assert resp.status_code == 200
        body = resp.text
        # Estonian alert title.
        assert "EL-i kaardistamata viited" in body
        # The summary line mentions the count using the inclusive
        # "EL viidet" wording (extractor's eu_act ref_type covers both
        # CELEX numbers AND title/acronym mentions like GDPR — see #821
        # P2 follow-up).
        assert "2 EL viidet" in body
        assert "CELEX-numbrit" not in body, (
            "The summary line must not assert all refs are CELEX-shaped — "
            "GDPR/title/acronym mentions are also persisted to "
            "unresolved_eu_refs."
        )
        assert "kaardistada" in body
        # Both CELEX values are rendered inside <code> tags so they're
        # visually distinct + copy-paste friendly.
        assert "<code>32016R0679</code>" in body
        assert "<code>32019L1152</code>" in body
        # Action prompt.
        assert "Kontrollige käsitsi" in body

    @patch("app.docs.report_routes._fetch_latest_report")
    @patch("app.docs.report_routes.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_warning_hidden_when_unresolved_refs_empty(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_fetch_report: MagicMock,
    ):
        """No warning when every EU ref resolved cleanly (or there
        were no EU refs at all). The existing "EL-i õigusaktide seoseid
        ei tuvastatud" copy remains in the regular section.
        """
        mock_get_provider.return_value = _stub_provider()
        mock_fetch.return_value = _make_draft()
        findings = {
            "affected_entities": [],
            "conflicts": [],
            "eu_compliance": [],
            "gaps": [],
            "unresolved_eu_refs": [],
        }
        mock_fetch_report.return_value = _make_report_row(
            affected=0, conflicts=0, gaps=0, findings=findings
        )

        client = _authed_client()
        resp = client.get(f"/drafts/{_DRAFT_ID}/report")

        assert resp.status_code == 200
        body = resp.text
        # The warning title must NOT appear.
        assert "EL-i kaardistamata viited" not in body
        # The regular "no EU findings" copy still shows in the
        # eu_compliance section.
        assert "EL-i õigusaktide seoseid ei tuvastatud" in body

    @patch("app.docs.report_routes._fetch_latest_report")
    @patch("app.docs.report_routes.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_legacy_report_without_unresolved_key_renders_cleanly(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_fetch_report: MagicMock,
    ):
        """Reports written BEFORE #815 don't carry the
        ``unresolved_eu_refs`` key. The renderer must treat this as
        "nothing to warn about" rather than crashing or showing the
        warning.
        """
        mock_get_provider.return_value = _stub_provider()
        mock_fetch.return_value = _make_draft()
        # Legacy findings: no unresolved_eu_refs key at all.
        legacy_findings = {
            "affected_entities": [],
            "conflicts": [],
            "eu_compliance": [],
            "gaps": [],
        }
        mock_fetch_report.return_value = _make_report_row(
            affected=0, conflicts=0, gaps=0, findings=legacy_findings
        )

        client = _authed_client()
        resp = client.get(f"/drafts/{_DRAFT_ID}/report")

        assert resp.status_code == 200
        body = resp.text
        assert "EL-i kaardistamata viited" not in body

    @patch("app.docs.report_routes._fetch_latest_report")
    @patch("app.docs.report_routes.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_duplicate_celex_refs_are_deduplicated(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_fetch_report: MagicMock,
    ):
        """A long draft can mention the same CELEX many times. The
        warning lists each CELEX once, not N times.
        """
        mock_get_provider.return_value = _stub_provider()
        mock_fetch.return_value = _make_draft()
        findings = {
            "affected_entities": [],
            "conflicts": [],
            "eu_compliance": [],
            "gaps": [],
            "unresolved_eu_refs": [
                {"ref_text": "32016R0679", "confidence": 0.95},
                {"ref_text": "32016R0679", "confidence": 0.85},
                {"ref_text": "32016R0679", "confidence": 0.75},
            ],
        }
        mock_fetch_report.return_value = _make_report_row(
            affected=0, conflicts=0, gaps=0, findings=findings
        )

        client = _authed_client()
        resp = client.get(f"/drafts/{_DRAFT_ID}/report")

        assert resp.status_code == 200
        body = resp.text
        # Count reflects unique refs, not row count.
        assert "1 EL viidet" in body
        # The ref is rendered exactly once.
        assert body.count("<code>32016R0679</code>") == 1

    @patch("app.docs.report_routes._fetch_latest_report")
    @patch("app.docs.report_routes.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_title_acronym_refs_rendered_without_celex_claim(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_fetch_report: MagicMock,
    ):
        """#821 P2 regression: the extractor's ``eu_act`` ref_type covers
        BOTH canonical CELEX numbers and title/acronym mentions like
        ``GDPR``. The unresolved-EU section must NOT label acronym
        mentions as "CELEX-numbrit" in the copy. The inclusive "EL
        viidet" wording covers both shapes truthfully.
        """
        mock_get_provider.return_value = _stub_provider()
        mock_fetch.return_value = _make_draft()
        findings = {
            "affected_entities": [],
            "conflicts": [],
            "eu_compliance": [],
            "gaps": [],
            "unresolved_eu_refs": [
                {"ref_text": "GDPR", "confidence": 0.92},
                {"ref_text": "Working Conditions Directive", "confidence": 0.71},
            ],
        }
        mock_fetch_report.return_value = _make_report_row(
            affected=0, conflicts=0, gaps=0, findings=findings
        )

        client = _authed_client()
        resp = client.get(f"/drafts/{_DRAFT_ID}/report")

        assert resp.status_code == 200
        body = resp.text
        assert "2 EL viidet" in body
        # The acronym/title mentions must NOT be labelled "CELEX".
        assert "CELEX-numbrit" not in body
        # Both refs render inside <code> tags regardless of shape.
        assert "<code>GDPR</code>" in body
        assert "<code>Working Conditions Directive</code>" in body


# ---------------------------------------------------------------------------
# POST /drafts/{id}/export
# ---------------------------------------------------------------------------


class TestExportDraftReportHandler:
    @patch("app.docs.report_routes.JobQueue")
    @patch("app.docs.report_routes._fetch_latest_report")
    @patch("app.docs.report_routes.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_export_enqueues_job_and_returns_status(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_fetch_report: MagicMock,
        mock_queue_cls: MagicMock,
    ):
        mock_get_provider.return_value = _stub_provider()
        mock_fetch.return_value = _make_draft()
        mock_fetch_report.return_value = _make_report_row()
        queue_instance = MagicMock()
        queue_instance.enqueue.return_value = 99
        mock_queue_cls.return_value = queue_instance

        client = _authed_client()
        resp = client.post(
            f"/drafts/{_DRAFT_ID}/export",
            headers={"HX-Request": "true"},
        )

        assert resp.status_code == 200
        # Spinner copy + polling target are present.
        assert "Eksport käimas" in resp.text
        assert f"/drafts/{_DRAFT_ID}/export-status/99" in resp.text
        # Job was enqueued with the right type and payload.
        # #613: payload now carries an explicit format (defaults to "docx"
        # when the client doesn't post one).
        queue_instance.enqueue.assert_called_once()
        args, kwargs = queue_instance.enqueue.call_args
        assert args[0] == "export_report"
        assert args[1] == {
            "draft_id": str(_DRAFT_ID),
            "report_id": str(_REPORT_ID),
            "format": "docx",
        }
        assert kwargs.get("priority") == 10

    @patch("app.docs.report_routes.JobQueue")
    @patch("app.docs.report_routes._find_active_export_job")
    @patch("app.docs.report_routes._fetch_latest_report")
    @patch("app.docs.report_routes.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_export_dedupes_active_job(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_fetch_report: MagicMock,
        mock_find_active: MagicMock,
        mock_queue_cls: MagicMock,
    ):
        """#627: when an active export job already exists, reuse its id."""
        mock_get_provider.return_value = _stub_provider()
        mock_fetch.return_value = _make_draft()
        mock_fetch_report.return_value = _make_report_row()
        mock_find_active.return_value = 42
        queue_instance = MagicMock()
        mock_queue_cls.return_value = queue_instance

        client = _authed_client()
        resp = client.post(
            f"/drafts/{_DRAFT_ID}/export",
            headers={"HX-Request": "true"},
        )

        assert resp.status_code == 200
        # No fresh enqueue — the existing job is reused.
        queue_instance.enqueue.assert_not_called()
        assert f"/drafts/{_DRAFT_ID}/export-status/42" in resp.text

    @patch("app.docs.report_routes.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_export_cross_org_returns_404(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
    ):
        mock_get_provider.return_value = _stub_provider()
        mock_fetch.return_value = _make_draft(org_id=_OTHER_ORG_ID)

        client = _authed_client()
        resp = client.post(f"/drafts/{_DRAFT_ID}/export")
        assert resp.status_code == 404
        assert "Eelnõu ei leitud" in resp.text


# ---------------------------------------------------------------------------
# GET /drafts/{id}/export-status/{job_id}
# ---------------------------------------------------------------------------


class TestExportStatusFragment:
    @patch("app.docs.report_routes.JobQueue")
    @patch("app.docs.report_routes.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_status_running_keeps_polling(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_queue_cls: MagicMock,
    ):
        mock_get_provider.return_value = _stub_provider()
        mock_fetch.return_value = _make_draft()
        queue_instance = MagicMock()
        queue_instance.get.return_value = _make_job(status="running")
        mock_queue_cls.return_value = queue_instance

        client = _authed_client()
        resp = client.get(
            f"/drafts/{_DRAFT_ID}/export-status/7",
            headers={"HX-Request": "true"},
        )

        assert resp.status_code == 200
        assert "Eksport käimas" in resp.text
        assert f"/drafts/{_DRAFT_ID}/export-status/7" in resp.text
        assert "every 2s" in resp.text

    @patch("app.docs.report_routes.JobQueue")
    @patch("app.docs.report_routes.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_status_pending_keeps_polling(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_queue_cls: MagicMock,
    ):
        mock_get_provider.return_value = _stub_provider()
        mock_fetch.return_value = _make_draft()
        queue_instance = MagicMock()
        queue_instance.get.return_value = _make_job(status="pending")
        mock_queue_cls.return_value = queue_instance

        client = _authed_client()
        resp = client.get(f"/drafts/{_DRAFT_ID}/export-status/7")

        assert resp.status_code == 200
        assert "Eksport käimas" in resp.text

    @patch("app.docs.report_routes.JobQueue")
    @patch("app.docs.report_routes.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_status_success_returns_download_link(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_queue_cls: MagicMock,
    ):
        mock_get_provider.return_value = _stub_provider()
        mock_fetch.return_value = _make_draft()
        queue_instance = MagicMock()
        queue_instance.get.return_value = _make_job(
            status="success",
            result={
                "draft_id": str(_DRAFT_ID),
                "report_id": str(_REPORT_ID),
                "docx_path": "/tmp/exports/output.docx",
            },
        )
        mock_queue_cls.return_value = queue_instance

        client = _authed_client()
        resp = client.get(f"/drafts/{_DRAFT_ID}/export-status/7")

        assert resp.status_code == 200
        assert "Laadi alla .docx" in resp.text
        assert f"/drafts/{_DRAFT_ID}/export/7/download" in resp.text
        # No polling attribute in success state.
        assert "every 2s" not in resp.text

    @patch("app.docs.report_routes.JobQueue")
    @patch("app.docs.report_routes.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_status_success_pdf_job_labels_link_as_pdf(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_queue_cls: MagicMock,
    ):
        """#741: a completed PDF export must show a ``.pdf`` download
        label (and not ``.docx``), matching what the download endpoint
        actually serves from ``payload["format"]``. PDF jobs still write
        a ``docx_path`` (the .docx is the content source of truth) plus
        a ``pdf_path`` — the fragment must key on the format, not on the
        mere presence of ``docx_path``."""
        mock_get_provider.return_value = _stub_provider()
        mock_fetch.return_value = _make_draft()
        queue_instance = MagicMock()
        queue_instance.get.return_value = _make_job(
            status="success",
            payload={
                "draft_id": str(_DRAFT_ID),
                "report_id": str(_REPORT_ID),
                "format": "pdf",
            },
            result={
                "draft_id": str(_DRAFT_ID),
                "report_id": str(_REPORT_ID),
                "format": "pdf",
                "docx_path": "/tmp/exports/output.docx",
                "pdf_path": "/tmp/exports/output.pdf",
            },
        )
        mock_queue_cls.return_value = queue_instance

        client = _authed_client()
        resp = client.get(f"/drafts/{_DRAFT_ID}/export-status/7")

        assert resp.status_code == 200
        assert "Laadi alla .pdf" in resp.text
        assert "Laadi alla .docx" not in resp.text
        assert f"/drafts/{_DRAFT_ID}/export/7/download" in resp.text
        assert "every 2s" not in resp.text

    @patch("app.docs.report_routes.JobQueue")
    @patch("app.docs.report_routes.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_status_success_pdf_job_without_pdf_path_shows_error(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_queue_cls: MagicMock,
    ):
        """A PDF job whose ``pdf_path`` is missing surfaces the
        file-not-found alert rather than silently linking the .docx
        (#741)."""
        mock_get_provider.return_value = _stub_provider()
        mock_fetch.return_value = _make_draft()
        queue_instance = MagicMock()
        queue_instance.get.return_value = _make_job(
            status="success",
            payload={
                "draft_id": str(_DRAFT_ID),
                "report_id": str(_REPORT_ID),
                "format": "pdf",
            },
            result={
                "draft_id": str(_DRAFT_ID),
                "report_id": str(_REPORT_ID),
                "format": "pdf",
                "docx_path": "/tmp/exports/output.docx",
            },
        )
        mock_queue_cls.return_value = queue_instance

        client = _authed_client()
        resp = client.get(f"/drafts/{_DRAFT_ID}/export-status/7")

        assert resp.status_code == 200
        assert "Eksport valmis, kuid faili ei leitud." in resp.text
        assert "Laadi alla" not in resp.text

    @patch("app.docs.report_routes.JobQueue")
    @patch("app.docs.report_routes.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_status_failed_shows_error_alert(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_queue_cls: MagicMock,
    ):
        mock_get_provider.return_value = _stub_provider()
        mock_fetch.return_value = _make_draft()
        queue_instance = MagicMock()
        queue_instance.get.return_value = _make_job(
            status="failed",
            error_message="Disk full",
        )
        mock_queue_cls.return_value = queue_instance

        client = _authed_client()
        resp = client.get(f"/drafts/{_DRAFT_ID}/export-status/7")

        assert resp.status_code == 200
        assert "Disk full" in resp.text
        assert "Eksport ebaõnnestus" in resp.text


# ---------------------------------------------------------------------------
# GET /drafts/{id}/export/{job_id}/download
# ---------------------------------------------------------------------------


class TestDownloadExportHandler:
    @patch("app.docs.report_routes.JobQueue")
    @patch("app.docs.report_routes.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_download_returns_file_response(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_queue_cls: MagicMock,
        tmp_path: Any,
    ):
        mock_get_provider.return_value = _stub_provider()
        mock_fetch.return_value = _make_draft(title="Tööõiguse muudatus")
        # Create a real file so FileResponse can stat it.
        docx_file = tmp_path / "report.docx"
        docx_file.write_bytes(b"PK\x03\x04 fake docx content")
        queue_instance = MagicMock()
        queue_instance.get.return_value = _make_job(
            status="success",
            result={
                "draft_id": str(_DRAFT_ID),
                "report_id": str(_REPORT_ID),
                "docx_path": str(docx_file),
            },
        )
        mock_queue_cls.return_value = queue_instance

        client = _authed_client()
        resp = client.get(f"/drafts/{_DRAFT_ID}/export/7/download")

        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith(
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        )
        # Slugified filename in Content-Disposition.
        cd = resp.headers.get("content-disposition", "")
        assert "impact_report_" in cd
        # Estonian diacritics are stripped via NFKD + ASCII fold; the
        # title contained ö, ö and õ which all become bare 'o'.
        assert ".docx" in cd
        assert "muudatus" in cd
        # No diacritics survived.
        for ch in ("ö", "õ", "ä", "ü"):
            assert ch not in cd

    @patch("app.docs.report_routes.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_download_cross_org_returns_404(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
    ):
        mock_get_provider.return_value = _stub_provider()
        mock_fetch.return_value = _make_draft(org_id=_OTHER_ORG_ID)

        client = _authed_client()
        resp = client.get(f"/drafts/{_DRAFT_ID}/export/7/download")
        assert resp.status_code == 404
        assert "Eelnõu ei leitud" in resp.text

    @patch("app.docs.report_routes.JobQueue")
    @patch("app.docs.report_routes.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_download_missing_file_returns_404(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_queue_cls: MagicMock,
    ):
        mock_get_provider.return_value = _stub_provider()
        mock_fetch.return_value = _make_draft()
        queue_instance = MagicMock()
        queue_instance.get.return_value = _make_job(
            status="success",
            result={
                "draft_id": str(_DRAFT_ID),
                "report_id": str(_REPORT_ID),
                "docx_path": "/tmp/does-not-exist-12345.docx",
            },
        )
        mock_queue_cls.return_value = queue_instance

        client = _authed_client()
        resp = client.get(f"/drafts/{_DRAFT_ID}/export/7/download")
        assert resp.status_code == 404
        assert "Eelnõu ei leitud" in resp.text

    @patch("app.docs.report_routes.JobQueue")
    @patch("app.docs.report_routes.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_download_pending_job_returns_404(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_queue_cls: MagicMock,
    ):
        """A still-running job must not yield a 200 download stream."""
        mock_get_provider.return_value = _stub_provider()
        mock_fetch.return_value = _make_draft()
        queue_instance = MagicMock()
        queue_instance.get.return_value = _make_job(status="running")
        mock_queue_cls.return_value = queue_instance

        client = _authed_client()
        resp = client.get(f"/drafts/{_DRAFT_ID}/export/7/download")
        assert resp.status_code == 404
        assert "Eelnõu ei leitud" in resp.text


# ---------------------------------------------------------------------------
# GET /drafts/{id}/report/full.{docx,pdf} — Safari-friendly synchronous
# download (#811). The user-facing "Laadi alla .docx/.pdf" buttons hit
# these routes via plain ``<a href download>`` anchors so the browser
# treats the response (``Content-Disposition: attachment``) as a native
# download. The prior HTMX form-POST + async-job pipeline silently
# failed in Safari WebKit because the JS-driven swap never visibly
# surfaced a download link before users gave up.
# ---------------------------------------------------------------------------


class TestReportFullDownloadHandler:
    @patch("app.docs.report_routes._fetch_latest_report")
    @patch("app.docs.report_routes.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_report_page_renders_safari_friendly_anchor_for_docx(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_fetch_report: MagicMock,
    ):
        """#811: the "Laadi alla .docx" control must be a plain
        ``<a href="…/report/full.docx" download="…">`` anchor — NOT a
        form-POST with ``hx-post``. Anchors with ``download`` trigger
        native downloads in Safari; the previous HTMX-form shape did
        not."""
        mock_get_provider.return_value = _stub_provider()
        mock_fetch.return_value = _make_draft()
        mock_fetch_report.return_value = _make_report_row()

        client = _authed_client()
        resp = client.get(f"/drafts/{_DRAFT_ID}/report")

        assert resp.status_code == 200
        body = resp.text
        # The direct GET anchor for both formats is present...
        assert f"/drafts/{_DRAFT_ID}/report/full.docx" in body
        assert f"/drafts/{_DRAFT_ID}/report/full.pdf" in body
        # ...and they carry the ``download`` attribute so Safari treats
        # the click as a file download (mirrors the working summary link).
        assert 'download="impact_report_' in body
        # The old HTMX-form pipeline must NOT be wired to these buttons:
        # no ``hx-post`` targeting the legacy /export endpoint inside the
        # Eksport card. We check for the precise legacy shape (the
        # async POST handler is still mounted for back-compat but the
        # user-facing controls no longer use it).
        assert f'hx-post="/drafts/{_DRAFT_ID}/export"' not in body
        # Belt-and-braces: no ``method="post" action="…/export"`` form
        # for the inline export card either.
        assert f'action="/drafts/{_DRAFT_ID}/export"' not in body

    @patch("app.docs.report_routes._fetch_latest_report")
    @patch("app.docs.report_routes.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    @patch("app.docs.report_routes.build_impact_report_docx", create=True)
    def test_full_docx_streams_with_attachment_disposition(
        self,
        mock_build: MagicMock,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_fetch_report: MagicMock,
        tmp_path: Any,
    ):
        """#811: the ``/report/full.docx`` route must respond with a
        ``Content-Disposition: attachment`` so the browser saves the
        file rather than rendering it inline."""
        mock_get_provider.return_value = _stub_provider()
        mock_fetch.return_value = _make_draft(title="Tööõiguse muudatus")
        mock_fetch_report.return_value = _make_report_row()
        # Create a real file so FileResponse can stat + stream it.
        docx_file = tmp_path / "report.docx"
        docx_file.write_bytes(b"PK\x03\x04 fake docx content")
        # The handler does a lazy ``from app.docs.docx_export import
        # build_impact_report_docx`` so we patch the import target.
        import app.docs.docx_export as _docx_export

        original = _docx_export.build_impact_report_docx
        _docx_export.build_impact_report_docx = lambda *a, **kw: docx_file
        try:
            client = _authed_client()
            resp = client.get(f"/drafts/{_DRAFT_ID}/report/full.docx")
        finally:
            _docx_export.build_impact_report_docx = original

        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith(
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        )
        cd = resp.headers.get("content-disposition", "")
        # Must be ``attachment`` (not ``inline``) so Safari downloads it.
        assert cd.startswith("attachment"), (
            f"Content-Disposition must be 'attachment' for Safari downloads, got: {cd!r}"
        )
        assert "impact_report_" in cd
        assert ".docx" in cd
        # Estonian diacritics are stripped via NFKD + ASCII fold.
        for ch in ("ö", "õ", "ä", "ü"):
            assert ch not in cd

    @patch("app.docs.report_routes._fetch_latest_report")
    @patch("app.docs.report_routes.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_full_docx_cross_org_returns_404(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_fetch_report: MagicMock,
    ):
        mock_get_provider.return_value = _stub_provider()
        mock_fetch.return_value = _make_draft(org_id=_OTHER_ORG_ID)
        mock_fetch_report.return_value = _make_report_row()

        client = _authed_client()
        resp = client.get(f"/drafts/{_DRAFT_ID}/report/full.docx")
        assert resp.status_code == 404
        assert "Eelnõu ei leitud" in resp.text

    @patch("app.docs.report_routes._fetch_latest_report")
    @patch("app.docs.report_routes.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_full_docx_missing_report_returns_404(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_fetch_report: MagicMock,
    ):
        mock_get_provider.return_value = _stub_provider()
        mock_fetch.return_value = _make_draft()
        mock_fetch_report.return_value = None

        client = _authed_client()
        resp = client.get(f"/drafts/{_DRAFT_ID}/report/full.docx")
        assert resp.status_code == 404
        assert "Eelnõu ei leitud" in resp.text

    @patch("app.docs.report_routes._fetch_latest_report")
    @patch("app.docs.report_routes.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_full_pdf_streams_with_attachment_disposition(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_fetch_report: MagicMock,
        tmp_path: Any,
    ):
        """#811: the ``/report/full.pdf`` route must respond with a
        ``Content-Disposition: attachment`` so Safari treats it as a
        download."""
        mock_get_provider.return_value = _stub_provider()
        mock_fetch.return_value = _make_draft(title="QA Safari tooö")
        mock_fetch_report.return_value = _make_report_row()
        docx_file = tmp_path / "report.docx"
        docx_file.write_bytes(b"PK\x03\x04 fake docx content")
        pdf_file = tmp_path / "report.pdf"
        pdf_file.write_bytes(b"%PDF-1.4 fake pdf content")

        import app.docs.docx_export as _docx_export

        original_build = _docx_export.build_impact_report_docx
        original_convert = _docx_export.convert_docx_to_pdf
        _docx_export.build_impact_report_docx = lambda *a, **kw: docx_file
        _docx_export.convert_docx_to_pdf = lambda *a, **kw: pdf_file
        try:
            client = _authed_client()
            resp = client.get(f"/drafts/{_DRAFT_ID}/report/full.pdf")
        finally:
            _docx_export.build_impact_report_docx = original_build
            _docx_export.convert_docx_to_pdf = original_convert

        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("application/pdf")
        cd = resp.headers.get("content-disposition", "")
        assert cd.startswith("attachment"), (
            f"Content-Disposition must be 'attachment' for Safari downloads, got: {cd!r}"
        )
        assert "impact_report_" in cd
        assert ".pdf" in cd

    @patch("app.docs.report_routes.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_full_download_unknown_format_returns_404(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
    ):
        """A bogus extension must not leak path traversal or land in the
        DOCX/PDF branches — the handler returns the 404 page."""
        mock_get_provider.return_value = _stub_provider()
        mock_fetch.return_value = _make_draft()

        # The route only matches /report/full.docx and /report/full.pdf
        # at registration time, so a bogus suffix never reaches our
        # handler — the framework 404s it instead.
        client = _authed_client()
        resp = client.get(f"/drafts/{_DRAFT_ID}/report/full.xml")
        assert resp.status_code == 404

    def test_full_docx_requires_auth(self):
        """Unauthenticated users get redirected to /auth/login."""
        from app.main import app

        client = TestClient(app, follow_redirects=False)
        resp = client.get(f"/drafts/{_DRAFT_ID}/report/full.docx")
        assert resp.status_code == 303
        assert resp.headers["location"] == "/auth/login"


# ---------------------------------------------------------------------------
# GET /drafts/{id}/report/section/{section} — pagination (#611)
# ---------------------------------------------------------------------------


class TestReportSectionPagination:
    @patch("app.docs.report_routes._fetch_latest_report")
    @patch("app.docs.report_routes.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_report_page_shows_pager_when_more_than_50_rows(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_fetch_report: MagicMock,
    ):
        mock_get_provider.return_value = _stub_provider()
        mock_fetch.return_value = _make_draft()
        many = [
            {
                "uri": f"urn:x:{i}",
                "label": f"Säte {i}",
                "type": "https://data.riik.ee/ontology/estleg#EnactedLaw",
            }
            for i in range(75)
        ]
        mock_fetch_report.return_value = _make_report_row(
            affected=75,
            findings={
                "affected_entities": many,
                "conflicts": [],
                "eu_compliance": [],
                "gaps": [],
            },
        )

        client = _authed_client()
        resp = client.get(f"/drafts/{_DRAFT_ID}/report")
        assert resp.status_code == 200
        # Pager footer + "Näita rohkem" button are both present.
        assert "Kuvatud 50 / 75" in resp.text
        assert "Näita rohkem" in resp.text
        # And the HTMX target for the next batch is wired up.
        assert "/report/section/affected?offset=50" in resp.text

    @patch("app.docs.report_routes._fetch_latest_report")
    @patch("app.docs.report_routes.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_section_fragment_returns_next_batch(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_fetch_report: MagicMock,
    ):
        mock_get_provider.return_value = _stub_provider()
        mock_fetch.return_value = _make_draft()
        many = [
            {
                "uri": f"urn:x:{i}",
                "label": f"Säte {i}",
                "type": "https://data.riik.ee/ontology/estleg#EnactedLaw",
            }
            for i in range(120)
        ]
        mock_fetch_report.return_value = _make_report_row(
            affected=120,
            findings={
                "affected_entities": many,
                "conflicts": [],
                "eu_compliance": [],
                "gaps": [],
            },
        )

        client = _authed_client()
        resp = client.get(f"/drafts/{_DRAFT_ID}/report/section/affected?offset=50&limit=50")
        assert resp.status_code == 200
        # The 51st row (index 50) is in the fragment.
        assert "Säte 50" in resp.text
        # The 101st row (index 100) is NOT yet — the next click loads it.
        assert "Säte 100" not in resp.text
        # Follow-up pager is wired for the remaining rows (20 left).
        assert "Kuvatud 100 / 120" in resp.text
        assert "offset=100" in resp.text

    @patch("app.docs.report_routes._fetch_latest_report")
    @patch("app.docs.report_routes.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_section_fragment_unknown_section_returns_404(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_fetch_report: MagicMock,
    ):
        mock_get_provider.return_value = _stub_provider()
        mock_fetch.return_value = _make_draft()
        mock_fetch_report.return_value = _make_report_row()

        client = _authed_client()
        resp = client.get(f"/drafts/{_DRAFT_ID}/report/section/bogus")
        assert resp.status_code == 404
        assert "Eelnõu ei leitud" in resp.text


# ---------------------------------------------------------------------------
# #612: ontology-version drift banner + /report/reanalyze
# ---------------------------------------------------------------------------


class TestOntologyDriftBanner:
    @patch("app.docs.report_routes._current_ontology_version")
    @patch("app.docs.report_routes._fetch_latest_report")
    @patch("app.docs.report_routes.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_drift_banner_renders_when_versions_differ(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_fetch_report: MagicMock,
        mock_current: MagicMock,
    ):
        mock_get_provider.return_value = _stub_provider()
        mock_fetch.return_value = _make_draft()
        mock_fetch_report.return_value = _make_report_row()  # version tag v2.1-ish
        mock_current.return_value = "2026-04-15T00:00+00:00@1061500"

        client = _authed_client()
        resp = client.get(f"/drafts/{_DRAFT_ID}/report")
        assert resp.status_code == 200
        assert "Ontoloogia on uuenenud" in resp.text
        assert "Analüüsi uuesti" in resp.text

    @patch("app.docs.report_routes._current_ontology_version")
    @patch("app.docs.report_routes._fetch_latest_report")
    @patch("app.docs.report_routes.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_drift_banner_absent_when_versions_match(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_fetch_report: MagicMock,
        mock_current: MagicMock,
    ):
        mock_get_provider.return_value = _stub_provider()
        mock_fetch.return_value = _make_draft()
        report_row = _make_report_row()
        mock_fetch_report.return_value = report_row
        mock_current.return_value = str(report_row[7])

        client = _authed_client()
        resp = client.get(f"/drafts/{_DRAFT_ID}/report")
        assert resp.status_code == 200
        assert "Ontoloogia on uuenenud" not in resp.text

    @patch("app.docs.report_routes.JobQueue")
    @patch("app.docs.report_routes.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_reanalyze_enqueues_analyze_job(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_queue_cls: MagicMock,
    ):
        mock_get_provider.return_value = _stub_provider()
        mock_fetch.return_value = _make_draft()
        queue_instance = MagicMock()
        queue_instance.enqueue.return_value = 321
        mock_queue_cls.return_value = queue_instance

        client = _authed_client()
        resp = client.post(
            f"/drafts/{_DRAFT_ID}/report/reanalyze",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        queue_instance.enqueue.assert_called_once()
        args, _ = queue_instance.enqueue.call_args
        assert args[0] == "analyze_impact"
        assert args[1] == {"draft_id": str(_DRAFT_ID)}
        # HTMX fragment replaces the banner with a success alert.
        assert "Analüüs alustati" in resp.text

    @patch("app.docs.report_routes.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_reanalyze_cross_org_returns_404(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
    ):
        mock_get_provider.return_value = _stub_provider()
        mock_fetch.return_value = _make_draft(org_id=_OTHER_ORG_ID)
        client = _authed_client()
        resp = client.post(f"/drafts/{_DRAFT_ID}/report/reanalyze")
        assert resp.status_code == 404
        assert "Eelnõu ei leitud" in resp.text


# ---------------------------------------------------------------------------
# explorer_focus_url — URL-encoding of estleg URIs in ?focus= links (#719)
# ---------------------------------------------------------------------------


class TestExplorerFocusUrl:
    def test_encodes_hash_so_uri_is_not_truncated(self):
        from app.docs.report_routes import explorer_focus_url

        uri = "https://data.riik.ee/ontology/estleg#KarS_par_133"
        url = explorer_focus_url(uri)
        # The fragment-marker must be percent-encoded, otherwise the
        # browser treats "#KarS_par_133" as a fragment and ``focus`` is
        # silently truncated to ".../estleg".
        assert "#" not in url
        assert url.startswith("/explorer?focus=")
        assert "%23" in url  # encoded '#'
        # Round-trips back to the original URI.
        from urllib.parse import parse_qs, urlsplit

        q = parse_qs(urlsplit(url).query)
        assert q["focus"] == [uri]

    def test_encodes_slashes_and_colons(self):
        from app.docs.report_routes import explorer_focus_url

        url = explorer_focus_url("https://example.org/a/b")
        assert "%2F" in url or "%2f" in url
        assert "%3A" in url or "%3a" in url

    def test_appends_draft_id_when_given(self):
        from app.docs.report_routes import explorer_focus_url

        url = explorer_focus_url("https://data.riik.ee/ontology/estleg#X", draft_id="abc-123")
        assert "&draft=abc-123" in url
        assert url.index("focus=") < url.index("draft=")


# ---------------------------------------------------------------------------
# explorer_draft_url — /explorer?draft=<id> deep-link helper (#759)
# ---------------------------------------------------------------------------


class TestExplorerDraftUrl:
    def test_builds_explorer_draft_query(self):
        from app.docs.report_routes import explorer_draft_url

        draft_id = "11111111-2222-3333-4444-555555555555"
        url = explorer_draft_url(draft_id)
        assert url == f"/explorer?draft={draft_id}"

    def test_url_encodes_non_uuid_input(self):
        from urllib.parse import parse_qs, urlsplit

        from app.docs.report_routes import explorer_draft_url

        url = explorer_draft_url("a b/c#d")
        # The space / slash / hash must be percent-encoded so the whole
        # value survives the round-trip through the query string.
        assert " " not in url
        assert "#" not in url
        assert url.startswith("/explorer?draft=")
        q = parse_qs(urlsplit(url).query)
        assert q["draft"] == ["a b/c#d"]

    def test_accepts_uuid_object(self):
        import uuid

        from app.docs.report_routes import explorer_draft_url

        u = uuid.uuid4()
        url = explorer_draft_url(u)  # type: ignore[arg-type]
        assert url == f"/explorer?draft={u}"
