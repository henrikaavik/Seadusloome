"""Integration tests for #619 PR-C: row-annotations wired into the report.

Covers three deliverables from the sprint plan §6 Days 3-4:

    1. ``app/annotations/row_keys`` — deterministic row_key formulas
       (entity / eu / conflict / gap) per the §9.4 contract locked in
       PR-A.
    2. ``app/docs/report_routes`` — AnnotationButton injection into
       every impact-report row, side-panel container rendered once,
       version-scoped /annotations/version/... HTMX wiring.
    3. ``app/docs/analyze_handler`` — best-effort stale-flag automation
       at analyze tail (annotation row vanished → stale=true; row
       reappeared → stale=false).

Patterns follow ``tests/test_docs_report_routes.py`` (TestClient with a
patched auth provider + mocked DB lookups) and
``tests/test_docs_analyze_handler.py`` (mock the get_connection chain).
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from app.annotations.models import (
    count_unresolved_for_version_row,
    update_stale_flags_for_version,
)
from app.annotations.row_keys import (
    collect_row_specs,
    row_key_for_conflict,
    row_key_for_entity,
    row_key_for_eu,
    row_key_for_gap,
    stable_hash,
)
from app.docs.draft_model import Draft
from app.docs.impact.analyzer import ImpactFindings
from app.docs.version_model import DraftVersion

# ---------------------------------------------------------------------------
# Constants + fixtures
# ---------------------------------------------------------------------------

_ORG_ID = "11111111-1111-1111-1111-111111111111"
_OTHER_ORG_ID = "22222222-2222-2222-2222-222222222222"
_USER_ID = "33333333-3333-3333-3333-333333333333"
_DRAFT_ID = uuid.UUID("44444444-4444-4444-4444-444444444444")
_REPORT_ID = uuid.UUID("55555555-5555-5555-5555-555555555555")
_VERSION_ID = uuid.UUID("66666666-6666-6666-6666-666666666666")


def _authed_user() -> dict[str, Any]:
    return {
        "id": _USER_ID,
        "email": "koostaja@seadusloome.ee",
        "full_name": "Test Koostaja",
        "role": "drafter",
        "org_id": _ORG_ID,
    }


def _stub_provider() -> MagicMock:
    provider = MagicMock()
    provider.get_current_user.return_value = _authed_user()
    return provider


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
        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
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


def _make_findings(
    *,
    affected: int = 2,
    conflicts: int = 1,
    eu: int = 1,
    gaps: int = 1,
) -> dict[str, Any]:
    """Build an in-memory ``report_data`` dict matching the JSONB shape."""
    return {
        "affected_entities": [
            {
                "uri": f"urn:entity:{i}",
                "label": f"Säte {i}",
                "type": "https://data.riik.ee/ontology/estleg#EnactedLaw",
            }
            for i in range(affected)
        ],
        "conflicts": [
            {
                "draft_ref": f"Eelnõu § {i}",
                "conflicting_entity": f"urn:conflict:{i}",
                "conflicting_label": f"Vana säte {i}",
                "reason": f"Vastuolu {i}",
            }
            for i in range(conflicts)
        ],
        "eu_compliance": [
            {
                "eu_act": f"urn:eu:act:{i}",
                "eu_label": f"Direktiiv {i}",
                "estonian_provision": f"urn:ee:{i}",
                "provision_label": f"§ {i}",
                "transposition_status": "linked",
            }
            for i in range(eu)
        ],
        "gaps": [
            {
                "topic_cluster": f"urn:cluster:{i}",
                "topic_cluster_label": f"Klaster {i}",
                "total_provisions": "10",
                "referenced_provisions": "2",
                "description": f"Lünk {i}",
            }
            for i in range(gaps)
        ],
    }


def _make_report_row(findings: dict[str, Any] | None = None) -> tuple:
    """9-tuple matching ``_REPORT_SELECT_COLUMNS`` order in report_routes."""
    return (
        _REPORT_ID,
        _DRAFT_ID,
        2,  # affected_count
        1,  # conflict_count
        1,  # gap_count
        42,  # impact_score
        findings if findings is not None else _make_findings(),
        "2026-04-09T12:00+00:00@1061123",
        datetime(2026, 4, 9, 12, 0, tzinfo=UTC),
    )


def _authed_client():
    """Return a TestClient with a stub session cookie."""
    from starlette.testclient import TestClient

    client = TestClient(__import__("app.main", fromlist=["app"]).app, follow_redirects=False)
    client.cookies.set("access_token", "stub-token")
    return client


# ===========================================================================
# 1. Row-key formulas — determinism + stability
# ===========================================================================


class TestRowKeyDeterminism:
    """The §9.4 contract requires stable hashes across runs and machines."""

    def test_entity_returns_uri(self):
        row = {"uri": "urn:entity:42", "label": "X", "type": "T"}
        assert row_key_for_entity(row) == "urn:entity:42"

    def test_entity_empty_uri_yields_empty_string(self):
        # Empty key signals "no annotation affordance"; the renderer
        # gracefully drops the button rather than rendering one with a
        # zero-length target_id.
        assert row_key_for_entity({}) == ""
        assert row_key_for_entity({"uri": ""}) == ""

    def test_eu_returns_eu_act_uri(self):
        row = {"eu_act": "https://eur-lex.europa.eu/eli/dir/2016/679"}
        assert row_key_for_eu(row) == "https://eur-lex.europa.eu/eli/dir/2016/679"

    def test_eu_empty_yields_empty_string(self):
        assert row_key_for_eu({}) == ""

    def test_conflict_is_deterministic_across_calls(self):
        """Same inputs MUST produce the same hash on every call."""
        row = {
            "draft_ref": "Eelnõu § 5",
            "conflicting_entity": "urn:law/123",
            "reason": "Vastuolu KarS § 133-ga",
        }
        first = row_key_for_conflict(row)
        second = row_key_for_conflict(row)
        third = row_key_for_conflict(dict(row))  # fresh dict, same data
        assert first == second == third
        # 32 hex chars matches the spec length.
        assert len(first) == 32
        assert all(c in "0123456789abcdef" for c in first)

    def test_conflict_sort_normalises_subject_object_order(self):
        """sorted([draft_ref, conflicting_entity]) means swapping them
        does NOT change the hash — the key is identity-of-edge, not
        directionality."""
        row_a = {
            "draft_ref": "AAA",
            "conflicting_entity": "BBB",
            "reason": "r",
        }
        row_b = {
            "draft_ref": "BBB",
            "conflicting_entity": "AAA",
            "reason": "r",
        }
        assert row_key_for_conflict(row_a) == row_key_for_conflict(row_b)

    def test_conflict_reason_is_truncated_to_64_chars(self):
        """A novel reason longer than 64 chars in the prefix shifts the
        hash; identical 64-char prefixes with different suffixes do NOT.
        """
        base_reason = "x" * 64
        long_a = {"draft_ref": "d", "conflicting_entity": "e", "reason": base_reason + "AAA"}
        long_b = {"draft_ref": "d", "conflicting_entity": "e", "reason": base_reason + "BBB"}
        # Truncated at 64 → identical truncated tie-breaker → identical hash.
        assert row_key_for_conflict(long_a) == row_key_for_conflict(long_b)

    def test_conflict_different_reasons_give_different_keys(self):
        """Different short reasons must NOT collide."""
        row_a = {"draft_ref": "d", "conflicting_entity": "e", "reason": "alpha"}
        row_b = {"draft_ref": "d", "conflicting_entity": "e", "reason": "beta"}
        assert row_key_for_conflict(row_a) != row_key_for_conflict(row_b)

    def test_gap_is_deterministic_across_calls(self):
        row = {"topic_cluster": "urn:cluster:andmekaitse"}
        assert row_key_for_gap(row) == row_key_for_gap(row)
        assert len(row_key_for_gap(row)) == 32

    def test_gap_different_clusters_give_different_keys(self):
        a = {"topic_cluster": "urn:cluster:1"}
        b = {"topic_cluster": "urn:cluster:2"}
        assert row_key_for_gap(a) != row_key_for_gap(b)

    def test_stable_hash_is_unicode_safe(self):
        """Estonian-letter inputs MUST produce the same hash on every
        run (canonical_json with ensure_ascii=False)."""
        a = stable_hash(["Aäoõu", "Šžütõ"])
        b = stable_hash(["Aäoõu", "Šžütõ"])
        assert a == b
        assert len(a) == 32


class TestCollectRowSpecs:
    def test_walks_every_section_in_order(self):
        findings = _make_findings(affected=2, conflicts=1, eu=1, gaps=1)
        specs = collect_row_specs(findings)
        # All five expected: 2 entity + 1 conflict + 1 eu + 1 gap = 5.
        assert len(specs) == 5
        kinds = [k for k, _ in specs]
        # Order: entity → conflict → eu → gap (matches the section render order).
        assert kinds == ["entity", "entity", "conflict", "eu", "gap"]

    def test_empty_findings_yields_empty_list(self):
        assert collect_row_specs({}) == []
        assert collect_row_specs({"affected_entities": [], "conflicts": []}) == []

    def test_skips_rows_with_empty_keys(self):
        findings = {
            "affected_entities": [{"uri": ""}, {"uri": "urn:x"}],
            "conflicts": [],
            "eu_compliance": [],
            "gaps": [],
        }
        specs = collect_row_specs(findings)
        # The empty-uri row was dropped; only the urn:x row survives.
        assert len(specs) == 1
        assert specs[0] == ("entity", "urn:x")


# ===========================================================================
# 2. Report-page rendering — AnnotationButton injection + side panel
# ===========================================================================


class TestReportPageRendersAnnotationButtons:
    """The report HTML must carry a per-row AnnotationButton for every
    section, plus a single side-panel container."""

    @patch("app.docs.report_routes._load_unresolved_counts")
    @patch("app.docs.report_routes._fetch_latest_report_version_id")
    @patch("app.docs.report_routes._fetch_latest_report")
    @patch("app.docs.report_routes.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_renders_side_panel_container_once(
        self,
        mock_provider,
        mock_fetch_draft,
        mock_fetch_report,
        mock_fetch_version,
        mock_load_counts,
    ):
        mock_provider.return_value = _stub_provider()
        mock_fetch_draft.return_value = _make_draft()
        mock_fetch_report.return_value = _make_report_row()
        mock_fetch_version.return_value = str(_VERSION_ID)
        mock_load_counts.return_value = {}

        client = _authed_client()
        resp = client.get(f"/drafts/{_DRAFT_ID}/report")

        assert resp.status_code == 200
        # The side-panel container id matches PR-B's _SIDE_PANEL_ID so
        # the version-scoped routes can swap into it.
        assert resp.text.count('id="annotation-side-panel"') == 1
        # The complementary ARIA role keeps it a valid landmark even
        # when empty.
        assert "annotation-side-panel" in resp.text

    @patch("app.docs.report_routes._load_unresolved_counts")
    @patch("app.docs.report_routes._fetch_latest_report_version_id")
    @patch("app.docs.report_routes._fetch_latest_report")
    @patch("app.docs.report_routes.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_each_row_carries_version_scoped_hx_get(
        self,
        mock_provider,
        mock_fetch_draft,
        mock_fetch_report,
        mock_fetch_version,
        mock_load_counts,
    ):
        """Every row's button MUST point at the §9.4 PR-B route, not
        the legacy /api/annotations endpoint."""
        mock_provider.return_value = _stub_provider()
        mock_fetch_draft.return_value = _make_draft()
        mock_fetch_report.return_value = _make_report_row(_make_findings(affected=1))
        mock_fetch_version.return_value = str(_VERSION_ID)
        mock_load_counts.return_value = {}

        client = _authed_client()
        resp = client.get(f"/drafts/{_DRAFT_ID}/report")

        assert resp.status_code == 200
        # Per-row buttons must use the version-scoped path; the
        # affected-entities row_key for "urn:entity:0" is the URI itself.
        # #773 / #781 follow-up: URI chars (``:``, ``/``, ``#``, and any
        # literal ``%XX``) go through opaque base64url encoding so the
        # path segment is transport-safe end to end.
        from app.annotations.row_keys import safe_row_key

        encoded_key = safe_row_key("urn:entity:0")
        expected = f"/annotations/version/{_VERSION_ID}/entity/{encoded_key}"
        assert expected in resp.text

    @patch("app.docs.report_routes._load_unresolved_counts")
    @patch("app.docs.report_routes._fetch_latest_report_version_id")
    @patch("app.docs.report_routes._fetch_latest_report")
    @patch("app.docs.report_routes.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_conflict_row_uses_hashed_row_key(
        self,
        mock_provider,
        mock_fetch_draft,
        mock_fetch_report,
        mock_fetch_version,
        mock_load_counts,
    ):
        mock_provider.return_value = _stub_provider()
        mock_fetch_draft.return_value = _make_draft()
        findings = _make_findings(affected=0, conflicts=1, eu=0, gaps=0)
        mock_fetch_report.return_value = _make_report_row(findings)
        mock_fetch_version.return_value = str(_VERSION_ID)
        mock_load_counts.return_value = {}

        client = _authed_client()
        resp = client.get(f"/drafts/{_DRAFT_ID}/report")

        assert resp.status_code == 200
        # The conflict row's row_key is a sha256-32 hex digest; the URL
        # carries its base64url-encoded form because the encoder is
        # applied uniformly to every row kind.
        from app.annotations.row_keys import safe_row_key

        expected_key = safe_row_key(row_key_for_conflict(findings["conflicts"][0]))
        assert f"/annotations/version/{_VERSION_ID}/conflict/{expected_key}" in resp.text

    @patch("app.docs.report_routes._load_unresolved_counts")
    @patch("app.docs.report_routes._fetch_latest_report_version_id")
    @patch("app.docs.report_routes._fetch_latest_report")
    @patch("app.docs.report_routes.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_eu_row_uses_eu_act_uri(
        self,
        mock_provider,
        mock_fetch_draft,
        mock_fetch_report,
        mock_fetch_version,
        mock_load_counts,
    ):
        mock_provider.return_value = _stub_provider()
        mock_fetch_draft.return_value = _make_draft()
        findings = _make_findings(affected=0, conflicts=0, eu=1, gaps=0)
        mock_fetch_report.return_value = _make_report_row(findings)
        mock_fetch_version.return_value = str(_VERSION_ID)
        mock_load_counts.return_value = {}

        client = _authed_client()
        resp = client.get(f"/drafts/{_DRAFT_ID}/report")

        assert resp.status_code == 200
        # #773 / #781 follow-up: URI chars (``:``) go through opaque
        # base64url so the path segment is transport-safe.
        from app.annotations.row_keys import safe_row_key

        encoded_key = safe_row_key("urn:eu:act:0")
        assert f"/annotations/version/{_VERSION_ID}/eu/{encoded_key}" in resp.text

    @patch("app.docs.report_routes._load_unresolved_counts")
    @patch("app.docs.report_routes._fetch_latest_report_version_id")
    @patch("app.docs.report_routes._fetch_latest_report")
    @patch("app.docs.report_routes.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_gap_row_uses_hashed_row_key(
        self,
        mock_provider,
        mock_fetch_draft,
        mock_fetch_report,
        mock_fetch_version,
        mock_load_counts,
    ):
        mock_provider.return_value = _stub_provider()
        mock_fetch_draft.return_value = _make_draft()
        findings = _make_findings(affected=0, conflicts=0, eu=0, gaps=1)
        mock_fetch_report.return_value = _make_report_row(findings)
        mock_fetch_version.return_value = str(_VERSION_ID)
        mock_load_counts.return_value = {}

        client = _authed_client()
        resp = client.get(f"/drafts/{_DRAFT_ID}/report")

        assert resp.status_code == 200
        from app.annotations.row_keys import safe_row_key

        expected_key = safe_row_key(row_key_for_gap(findings["gaps"][0]))
        assert f"/annotations/version/{_VERSION_ID}/gap/{expected_key}" in resp.text

    @patch("app.docs.report_routes._load_unresolved_counts")
    @patch("app.docs.report_routes._fetch_latest_report_version_id")
    @patch("app.docs.report_routes._fetch_latest_report")
    @patch("app.docs.report_routes.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_unresolved_count_renders_badge(
        self,
        mock_provider,
        mock_fetch_draft,
        mock_fetch_report,
        mock_fetch_version,
        mock_load_counts,
    ):
        """When the bulk count helper reports unresolved messages, the
        badge must show the number."""
        mock_provider.return_value = _stub_provider()
        mock_fetch_draft.return_value = _make_draft()
        findings = _make_findings(affected=1, conflicts=0, eu=0, gaps=0)
        mock_fetch_report.return_value = _make_report_row(findings)
        mock_fetch_version.return_value = str(_VERSION_ID)
        # 7 unresolved messages on the lone affected-entity row.
        mock_load_counts.return_value = {("entity", "urn:entity:0"): 7}

        client = _authed_client()
        resp = client.get(f"/drafts/{_DRAFT_ID}/report")

        assert resp.status_code == 200
        assert "annotation-count-badge" in resp.text
        # The badge text should literally say "7".
        assert ">7<" in resp.text or '">7<' in resp.text

    @patch("app.docs.report_routes._load_unresolved_counts")
    @patch("app.docs.report_routes._fetch_latest_report_version_id")
    @patch("app.docs.report_routes._fetch_latest_report")
    @patch("app.docs.report_routes.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_no_version_id_skips_per_row_buttons(
        self,
        mock_provider,
        mock_fetch_draft,
        mock_fetch_report,
        mock_fetch_version,
        mock_load_counts,
    ):
        """Legacy reports without a draft_version_id FK MUST NOT crash;
        the page renders without the per-row buttons (graceful degrade).
        """
        mock_provider.return_value = _stub_provider()
        mock_fetch_draft.return_value = _make_draft()
        mock_fetch_report.return_value = _make_report_row()
        mock_fetch_version.return_value = None  # no version FK
        mock_load_counts.return_value = {}

        client = _authed_client()
        resp = client.get(f"/drafts/{_DRAFT_ID}/report")

        assert resp.status_code == 200
        # No per-row buttons because version_id is missing.
        assert "/annotations/version/" not in resp.text
        # ...but the side panel still renders (so PR-D / future code can
        # still hook into it).
        assert 'id="annotation-side-panel"' in resp.text


# ===========================================================================
# 3. count_unresolved_for_version_row + bulk loader
# ===========================================================================


class TestCountUnresolvedForVersionRow:
    def test_returns_count_from_db(self):
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = (5,)

        result = count_unresolved_for_version_row(conn, _VERSION_ID, "conflict", "abc-123")

        assert result == 5
        # The query must filter by all four §9.4 dimensions.
        sql = conn.execute.call_args.args[0].lower()
        assert "draft_version_id" in sql
        assert "target_type" in sql
        assert "target_id" in sql
        assert "resolved" in sql
        # Bound params: version_id, "conflict:abc-123"
        params = conn.execute.call_args.args[1]
        assert params == (str(_VERSION_ID), "conflict:abc-123")

    def test_zero_when_no_rows(self):
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = None
        assert count_unresolved_for_version_row(conn, _VERSION_ID, "entity", "k") == 0

    def test_zero_on_db_error(self):
        conn = MagicMock()
        conn.execute.side_effect = RuntimeError("boom")
        # MUST NOT raise — would crash the report page.
        assert count_unresolved_for_version_row(conn, _VERSION_ID, "entity", "k") == 0

    def test_invalid_row_kind_returns_zero_without_db_call(self):
        """A bad row_kind must not even hit the DB."""
        conn = MagicMock()
        result = count_unresolved_for_version_row(conn, _VERSION_ID, "INVALID", "k")
        assert result == 0
        conn.execute.assert_not_called()


# ===========================================================================
# 4. update_stale_flags_for_version — analyze-time automation
# ===========================================================================


class TestUpdateStaleFlagsForVersion:
    def test_flips_stale_true_when_row_vanished(self):
        """Annotation present in DB but NOT in the new analyze → stale=true."""
        conn = MagicMock()
        ann_id = uuid.UUID("88888888-8888-8888-8888-888888888888")
        # One annotation on a conflict row that no longer exists.
        conn.execute.return_value.fetchall.return_value = [
            (ann_id, "conflict:vanished-key", False),
        ]
        # The new analyze produced ZERO matching rows.
        current = set()

        changed = update_stale_flags_for_version(conn, _VERSION_ID, current)

        assert changed == 1
        # SET stale = TRUE was issued.
        update_calls = [
            c for c in conn.execute.call_args_list if "stale = true" in c.args[0].lower()
        ]
        assert len(update_calls) == 1
        # The bound param must include our annotation id (cast to str).
        bound = update_calls[0].args[1]
        assert any(str(ann_id) in str(arg) for arg in bound)

    def test_flips_stale_false_when_row_reappeared(self):
        conn = MagicMock()
        ann_id = uuid.UUID("99999999-9999-9999-9999-999999999999")
        conn.execute.return_value.fetchall.return_value = [
            (ann_id, "entity:urn:e", True),  # currently stale
        ]
        # Re-analyze surfaced the same entity again.
        current = {("entity", "urn:e")}

        changed = update_stale_flags_for_version(conn, _VERSION_ID, current)

        assert changed == 1
        clear_calls = [
            c for c in conn.execute.call_args_list if "stale = false" in c.args[0].lower()
        ]
        assert len(clear_calls) == 1

    def test_no_op_when_state_already_matches(self):
        """Idempotency: rows whose stale flag already matches the new
        analyze must NOT be updated."""
        conn = MagicMock()
        a = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
        b = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
        # a: present in new set, currently NOT stale → no-op
        # b: absent from new set, currently stale → no-op
        conn.execute.return_value.fetchall.return_value = [
            (a, "entity:urn:present", False),
            (b, "entity:urn:absent", True),
        ]
        current = {("entity", "urn:present")}

        changed = update_stale_flags_for_version(conn, _VERSION_ID, current)

        assert changed == 0
        # No UPDATE calls beyond the SELECT.
        update_calls = [
            c for c in conn.execute.call_args_list if "update annotations" in c.args[0].lower()
        ]
        assert update_calls == []

    def test_running_twice_with_same_set_is_no_op_on_second_call(self):
        """Idempotent: after one reconciliation the stale flags match
        the current set, so a follow-up call with the same set is a no-op.
        """
        # Round 1: row vanished → stale=true
        conn1 = MagicMock()
        ann_id = uuid.UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
        conn1.execute.return_value.fetchall.return_value = [
            (ann_id, "conflict:k", False),
        ]
        update_stale_flags_for_version(conn1, _VERSION_ID, set())
        # Round 2: same set, but DB now reflects stale=true already.
        conn2 = MagicMock()
        conn2.execute.return_value.fetchall.return_value = [
            (ann_id, "conflict:k", True),  # already stale
        ]
        changed = update_stale_flags_for_version(conn2, _VERSION_ID, set())
        assert changed == 0

    def test_swallows_db_errors(self):
        """A DB error during the reconciliation must NOT raise; analyze
        keeps a clean exit path even when stale-flag bookkeeping breaks.
        """
        conn = MagicMock()
        conn.execute.side_effect = RuntimeError("connection reset")
        # MUST NOT raise.
        result = update_stale_flags_for_version(conn, _VERSION_ID, {("entity", "urn:x")})
        assert result == 0

    def test_no_annotations_short_circuits(self):
        """When the version has zero annotations the function returns 0
        without issuing any UPDATE."""
        conn = MagicMock()
        conn.execute.return_value.fetchall.return_value = []
        result = update_stale_flags_for_version(conn, _VERSION_ID, {("entity", "urn:x")})
        assert result == 0
        # Only the initial SELECT happened.
        assert conn.execute.call_count == 1

    def test_skips_rows_with_malformed_target_id(self):
        """target_id without a colon cannot be parsed → skipped silently."""
        conn = MagicMock()
        ann_id = uuid.UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")
        conn.execute.return_value.fetchall.return_value = [
            (ann_id, "no_colon_at_all", False),
        ]
        result = update_stale_flags_for_version(conn, _VERSION_ID, set())
        # No update because the row was unparseable.
        assert result == 0


# ===========================================================================
# 5. analyze_handler integration — stale-flag wiring
# ===========================================================================


class _ConnectCM:
    """Context-manager wrapper around a cursor-ish mock (mirrors the
    helper in tests/test_docs_analyze_handler.py)."""

    def __init__(self, conn: MagicMock):
        self.conn = conn

    def __enter__(self) -> MagicMock:
        return self.conn

    def __exit__(self, *_: Any) -> bool:
        return False


def _make_version() -> DraftVersion:
    return DraftVersion(
        id=_VERSION_ID,
        draft_id=_DRAFT_ID,
        version_number=1,
        reading_stage="vtk",
        parsed_text_encrypted=None,
        storage_path="/tmp/cipher.enc",
        graph_uri=f"https://data.riik.ee/ontology/estleg/drafts/{_DRAFT_ID}",
        status="analyzing",
        created_at=datetime.now(UTC),
        created_by=uuid.UUID(_USER_ID),
    )


def _findings_with_one_conflict() -> ImpactFindings:
    return ImpactFindings(
        affected_entities=[],
        conflicts=[
            {
                "draft_ref": "Eelnõu § 1",
                "conflicting_entity": "urn:c:1",
                "reason": "vastuolu",
            }
        ],
        gaps=[],
        eu_compliance=[],
        affected_count=0,
        conflict_count=1,
        gap_count=0,
    )


class TestAnalyzeHandlerStaleFlagWiring:
    def test_reconciles_stale_flags_after_successful_analyze(self):
        """The analyze handler MUST call update_stale_flags_for_version
        with the new (row_kind, row_key) set after the impact_reports
        insert commits."""
        from app.docs.analyze_handler import analyze_impact

        load_conn = MagicMock()
        load_conn.execute.return_value.fetchall.return_value = []
        sync_conn = MagicMock()
        sync_conn.execute.return_value.fetchone.return_value = (
            datetime(2026, 4, 9, 12, 0, tzinfo=UTC),
            42,
        )
        insert_conn = MagicMock()
        insert_conn.execute.return_value.rowcount = 1
        stale_conn = MagicMock()

        with (
            patch("app.docs.analyze_handler.get_connection") as mock_get_conn,
            patch("app.docs.analyze_handler.get_draft", return_value=_make_draft()),
            patch("app.docs.analyze_handler.get_latest_version", return_value=_make_version()),
            patch("app.docs.analyze_handler.build_draft_graph", return_value="# ttl"),
            patch("app.docs.analyze_handler.put_named_graph", return_value=True),
            patch("app.docs.analyze_handler.write_doc_lineage", return_value=None),
            patch("app.docs.analyze_handler.fetch_draft", return_value=None),
            patch(
                "app.docs.analyze_handler.ImpactAnalyzer",
                return_value=MagicMock(
                    analyze=MagicMock(return_value=_findings_with_one_conflict())
                ),
            ),
            patch("app.docs.analyze_handler.calculate_impact_score", return_value=42),
            patch("app.docs.analyze_handler.update_stale_flags_for_version") as mock_stale,
        ):
            mock_get_conn.side_effect = [
                _ConnectCM(load_conn),
                _ConnectCM(sync_conn),
                _ConnectCM(insert_conn),
                _ConnectCM(stale_conn),
            ]
            mock_stale.return_value = 0

            analyze_impact({"draft_id": str(_DRAFT_ID)})

        # update_stale_flags_for_version was called exactly once.
        assert mock_stale.call_count == 1
        call_args = mock_stale.call_args
        # version_id matches the latest version of the draft.
        assert call_args.args[1] == str(_VERSION_ID)
        # current_keys is a set with exactly one ("conflict", <hash>) tuple.
        current_keys = call_args.args[2]
        assert isinstance(current_keys, set)
        assert len(current_keys) == 1
        only = next(iter(current_keys))
        assert only[0] == "conflict"
        # row_key is the deterministic 32-hex-char hash.
        assert len(only[1]) == 32

    def test_stale_flag_failure_does_not_fail_analyze(self):
        """update_stale_flags_for_version raising MUST NOT bubble up; the
        analyze handler returns its happy-path result regardless."""
        from app.docs.analyze_handler import analyze_impact

        load_conn = MagicMock()
        load_conn.execute.return_value.fetchall.return_value = []
        sync_conn = MagicMock()
        sync_conn.execute.return_value.fetchone.return_value = None
        insert_conn = MagicMock()
        insert_conn.execute.return_value.rowcount = 1

        # The 4th connection raises during __enter__ to simulate a DB
        # outage on the stale-flag transaction. This is a realistic
        # failure shape (psycopg can fail at acquire-time).
        broken_cm = MagicMock()
        broken_cm.__enter__ = MagicMock(side_effect=RuntimeError("DB down"))
        broken_cm.__exit__ = MagicMock(return_value=False)

        with (
            patch("app.docs.analyze_handler.get_connection") as mock_get_conn,
            patch("app.docs.analyze_handler.get_draft", return_value=_make_draft()),
            patch("app.docs.analyze_handler.get_latest_version", return_value=_make_version()),
            patch("app.docs.analyze_handler.build_draft_graph", return_value="# ttl"),
            patch("app.docs.analyze_handler.put_named_graph", return_value=True),
            patch("app.docs.analyze_handler.write_doc_lineage", return_value=None),
            patch("app.docs.analyze_handler.fetch_draft", return_value=None),
            patch(
                "app.docs.analyze_handler.ImpactAnalyzer",
                return_value=MagicMock(
                    analyze=MagicMock(return_value=_findings_with_one_conflict())
                ),
            ),
            patch("app.docs.analyze_handler.calculate_impact_score", return_value=42),
        ):
            mock_get_conn.side_effect = [
                _ConnectCM(load_conn),
                _ConnectCM(sync_conn),
                _ConnectCM(insert_conn),
                broken_cm,
            ]

            # MUST NOT raise — best-effort contract.
            result = analyze_impact({"draft_id": str(_DRAFT_ID)})

        assert result["draft_id"] == str(_DRAFT_ID)
        assert result["impact_score"] == 42

    def test_skips_stale_flag_when_version_id_missing(self):
        """If the analyze run has no draft_version_id (legacy / pre-PR-B),
        we MUST skip the stale-flag update entirely so we don't issue a
        spurious WHERE draft_version_id = NULL query."""
        from app.docs.analyze_handler import analyze_impact

        load_conn = MagicMock()
        load_conn.execute.return_value.fetchall.return_value = []
        sync_conn = MagicMock()
        sync_conn.execute.return_value.fetchone.return_value = None
        insert_conn = MagicMock()
        insert_conn.execute.return_value.rowcount = 1

        with (
            patch("app.docs.analyze_handler.get_connection") as mock_get_conn,
            patch("app.docs.analyze_handler.get_draft", return_value=_make_draft()),
            patch("app.docs.analyze_handler.get_latest_version", return_value=None),  # no version
            patch("app.docs.analyze_handler.build_draft_graph", return_value="# ttl"),
            patch("app.docs.analyze_handler.put_named_graph", return_value=True),
            patch("app.docs.analyze_handler.write_doc_lineage", return_value=None),
            patch("app.docs.analyze_handler.fetch_draft", return_value=None),
            patch(
                "app.docs.analyze_handler.ImpactAnalyzer",
                return_value=MagicMock(
                    analyze=MagicMock(return_value=_findings_with_one_conflict())
                ),
            ),
            patch("app.docs.analyze_handler.calculate_impact_score", return_value=42),
            patch("app.docs.analyze_handler.update_stale_flags_for_version") as mock_stale,
        ):
            mock_get_conn.side_effect = [
                _ConnectCM(load_conn),
                _ConnectCM(sync_conn),
                _ConnectCM(insert_conn),
            ]

            analyze_impact({"draft_id": str(_DRAFT_ID)})

        # update_stale_flags_for_version was NOT called because the
        # draft_version_id is None.
        mock_stale.assert_not_called()


# ===========================================================================
# 6. Section-pagination fragment carries annotation buttons too
# ===========================================================================


class TestSectionPagerFragment:
    @patch("app.docs.report_routes._load_unresolved_counts")
    @patch("app.docs.report_routes._fetch_latest_report_version_id")
    @patch("app.docs.report_routes._fetch_latest_report")
    @patch("app.docs.report_routes.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_paginated_batch_renders_annotation_buttons(
        self,
        mock_provider,
        mock_fetch_draft,
        mock_fetch_report,
        mock_fetch_version,
        mock_load_counts,
    ):
        """The "Näita rohkem" fragment route must also wire the
        AnnotationButton column for the slice it returns."""
        mock_provider.return_value = _stub_provider()
        mock_fetch_draft.return_value = _make_draft()
        mock_fetch_report.return_value = _make_report_row(_make_findings(affected=3))
        mock_fetch_version.return_value = str(_VERSION_ID)
        mock_load_counts.return_value = {("entity", "urn:entity:1"): 2}

        client = _authed_client()
        resp = client.get(f"/drafts/{_DRAFT_ID}/report/section/affected?offset=1&limit=1")

        assert resp.status_code == 200
        # The fragment renders a per-row button targeting the version-scoped route.
        # #773 / #781 follow-up: URI chars go through opaque base64url encoding.
        from app.annotations.row_keys import safe_row_key

        encoded_key = safe_row_key("urn:entity:1")
        assert f"/annotations/version/{_VERSION_ID}/entity/{encoded_key}" in resp.text


# ===========================================================================
# Smoke: fixtures exercise (catches collection errors early)
# ===========================================================================


@pytest.mark.parametrize(
    "row,expected_kind",
    [
        ({"uri": "u"}, "entity"),
        ({"eu_act": "x"}, "eu"),
    ],
)
def test_row_key_helpers_smoke(row: dict[str, Any], expected_kind: str):
    """Sanity check that the row-key helpers can be called across kinds
    without raising on minimal inputs."""
    if expected_kind == "entity":
        assert row_key_for_entity(row) == "u"
    else:
        assert row_key_for_eu(row) == "x"


def test_findings_json_roundtrip_preserves_keys():
    """The report_data JSONB roundtrip must preserve the field names the
    row-key helpers depend on."""
    findings = _make_findings(affected=1, conflicts=1, eu=1, gaps=1)
    roundtripped = json.loads(json.dumps(findings))
    assert "affected_entities" in roundtripped
    assert "conflicts" in roundtripped
    assert "eu_compliance" in roundtripped
    assert "gaps" in roundtripped
    # row_key formulas must produce the same key before / after JSON.
    assert row_key_for_entity(findings["affected_entities"][0]) == row_key_for_entity(
        roundtripped["affected_entities"][0]
    )
    assert row_key_for_conflict(findings["conflicts"][0]) == row_key_for_conflict(
        roundtripped["conflicts"][0]
    )


# ===========================================================================
# #773: Report rows with URI row_keys (entity / eu) render safe URLs
# ===========================================================================


class TestReportRowWithUriRowKey:
    """Affected-entity and EU rows carry raw ontology URIs as row_keys.

    The rendered ``hx_get`` must base64url-encode the URI so the path
    segment is opaque end to end (URIs contain ``/``, ``:``, ``#``, and
    sometimes literal ``%XX`` — all routing-significant characters or
    transport-layer ambiguities).
    """

    _URI_ROW_KEY = "https://data.riik.ee/ontology/estleg#KarS"

    @patch("app.docs.report_routes._load_unresolved_counts")
    @patch("app.docs.report_routes._fetch_latest_report_version_id")
    @patch("app.docs.report_routes._fetch_latest_report")
    @patch("app.docs.report_routes.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_entity_row_with_uri_renders_encoded_path(
        self,
        mock_provider,
        mock_fetch_draft,
        mock_fetch_report,
        mock_fetch_version,
        mock_load_counts,
    ):
        """An entity row whose URI contains ``/`` and ``#`` must produce
        an HTMX URL with the URI base64url-encoded — the literal URI must
        NOT appear inside the ``hx-get`` path segment, since the browser
        would otherwise fragment on ``#`` before sending."""
        from app.annotations.row_keys import safe_row_key

        mock_provider.return_value = _stub_provider()
        mock_fetch_draft.return_value = _make_draft()
        findings = {
            "affected_entities": [
                {
                    "uri": self._URI_ROW_KEY,
                    "label": "KarS",
                    "type": "https://data.riik.ee/ontology/estleg#EnactedLaw",
                }
            ],
            "conflicts": [],
            "eu_compliance": [],
            "gaps": [],
        }
        mock_fetch_report.return_value = _make_report_row(findings)
        mock_fetch_version.return_value = str(_VERSION_ID)
        mock_load_counts.return_value = {}

        client = _authed_client()
        resp = client.get(f"/drafts/{_DRAFT_ID}/report")

        assert resp.status_code == 200
        # The base64url-encoded URI appears in the HTMX URL.
        encoded = safe_row_key(self._URI_ROW_KEY)
        expected = f"/annotations/version/{_VERSION_ID}/entity/{encoded}"
        assert expected in resp.text
        # And the raw URI must NOT appear inside any hx-get path — the
        # browser would treat ``#`` as a fragment marker.
        assert (
            f'hx-get="/annotations/version/{_VERSION_ID}/entity/{self._URI_ROW_KEY}'
            not in resp.text
        )

    @patch("app.docs.report_routes._load_unresolved_counts")
    @patch("app.docs.report_routes._fetch_latest_report_version_id")
    @patch("app.docs.report_routes._fetch_latest_report")
    @patch("app.docs.report_routes.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_eu_row_with_uri_renders_encoded_path(
        self,
        mock_provider,
        mock_fetch_draft,
        mock_fetch_report,
        mock_fetch_version,
        mock_load_counts,
    ):
        """EU compliance rows pass the EU directive URI as the row_key —
        same encoding contract as the entity rows."""
        from app.annotations.row_keys import safe_row_key

        eu_uri = "https://eur-lex.europa.eu/eli/dir/2016/679/oj#article-1"

        mock_provider.return_value = _stub_provider()
        mock_fetch_draft.return_value = _make_draft()
        findings = {
            "affected_entities": [],
            "conflicts": [],
            "eu_compliance": [
                {
                    "eu_act": eu_uri,
                    "eu_label": "GDPR",
                    "estonian_provision": "urn:ee:0",
                    "provision_label": "§ 1",
                    "transposition_status": "linked",
                }
            ],
            "gaps": [],
        }
        mock_fetch_report.return_value = _make_report_row(findings)
        mock_fetch_version.return_value = str(_VERSION_ID)
        mock_load_counts.return_value = {}

        client = _authed_client()
        resp = client.get(f"/drafts/{_DRAFT_ID}/report")

        assert resp.status_code == 200
        encoded = safe_row_key(eu_uri)
        expected = f"/annotations/version/{_VERSION_ID}/eu/{encoded}"
        assert expected in resp.text
