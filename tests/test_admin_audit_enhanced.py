"""Tests for the enhanced audit log viewer (#187 / #542).

Covers: filter form rendering, query param extraction, filtered DB queries,
CSV export, date parsing, JSONB detail expander, filter persistence across
pages, empty state with filter context, and Estonian copy.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from unittest.mock import MagicMock, patch

from starlette.requests import Request
from starlette.testclient import TestClient


def _make_request(path: str = "/admin/audit", query_string: str = "") -> Request:
    """Build a minimal Starlette Request for unit testing."""
    scope = {
        "type": "http",
        "method": "GET",
        "path": path,
        "headers": [],
        "query_string": query_string.encode(),
        "scheme": "http",
        "server": ("testserver", 80),
        "client": ("127.0.0.1", 12345),
        "auth": {"role": "admin", "id": "admin-1", "email": "a@b.ee", "full_name": "Admin User"},
    }
    return Request(scope)


# ---------------------------------------------------------------------------
# Date parsing
# ---------------------------------------------------------------------------


class TestParseDateHelper:
    def test_valid_date(self):
        from app.admin.audit import _parse_date

        result = _parse_date("2025-03-15")
        assert result == date(2025, 3, 15)

    def test_invalid_date_returns_none(self):
        from app.admin.audit import _parse_date

        assert _parse_date("not-a-date") is None

    def test_empty_string_returns_none(self):
        from app.admin.audit import _parse_date

        assert _parse_date("") is None

    def test_none_returns_none(self):
        from app.admin.audit import _parse_date

        assert _parse_date(None) is None


# ---------------------------------------------------------------------------
# WHERE clause builder
# ---------------------------------------------------------------------------


class TestBuildAuditWhere:
    def test_no_filters_returns_true(self):
        from app.admin.audit import _build_audit_where

        where, params = _build_audit_where()
        assert where == "TRUE"
        assert params == []

    def test_action_filter(self):
        from app.admin.audit import _build_audit_where

        where, params = _build_audit_where(action="user.login")
        assert "a.action = %s" in where
        assert "user.login" in params

    def test_user_filter(self):
        from app.admin.audit import _build_audit_where

        where, params = _build_audit_where(user_id="user-42")
        assert "a.user_id = %s" in where
        assert "user-42" in params

    def test_date_range_filter(self):
        from app.admin.audit import _build_audit_where

        where, params = _build_audit_where(date_from=date(2025, 1, 1), date_to=date(2025, 1, 31))
        assert "a.created_at >= %s" in where
        assert "a.created_at < %s" in where
        assert len(params) == 2

    def test_query_filter(self):
        from app.admin.audit import _build_audit_where

        where, params = _build_audit_where(query="draft")
        assert "a.detail::text ILIKE %s" in where
        assert "%draft%" in params

    def test_query_filter_has_escape_clause(self):
        """#861-B: the ILIKE pattern must carry an explicit ESCAPE clause."""
        from app.admin.audit import _build_audit_where

        where, _params = _build_audit_where(query="draft")
        assert "ILIKE %s ESCAPE '\\'" in where

    def test_query_filter_escapes_like_wildcards(self):
        """#861-B: %, _ and \\ in a user query must be matched literally."""
        from app.admin.audit import _build_audit_where

        _where, params = _build_audit_where(query="100%_x\\y")
        # The bound pattern is wrapped in %...% but the user's own wildcards
        # are backslash-escaped so they cannot act as wildcards.
        assert params[-1] == "%100\\%\\_x\\\\y%"

    def test_combined_filters(self):
        from app.admin.audit import _build_audit_where

        where, params = _build_audit_where(action="doc.upload", user_id="u-1", query="test")
        assert "a.action = %s" in where
        assert "a.user_id = %s" in where
        assert "a.detail::text ILIKE %s" in where
        assert len(params) == 3

    def test_multiple_actions_emit_in_clause(self):
        from app.admin.audit import _build_audit_where

        where, params = _build_audit_where(actions=["user.login", "doc.upload"])
        assert "a.action IN (%s, %s)" in where
        assert params == ["user.login", "doc.upload"]

    def test_action_and_actions_merged_and_deduped(self):
        from app.admin.audit import _build_audit_where

        where, params = _build_audit_where(action="user.login", actions=["user.login", "x.y"])
        # Should de-dup to two unique actions
        assert params.count("user.login") == 1
        assert "x.y" in params

    def test_org_filter_joins_users(self):
        from app.admin.audit import _build_audit_where

        where, params = _build_audit_where(org_id="org-1")
        assert "u.org_id = %s" in where
        assert "org-1" in params

    def test_combined_action_user_daterange(self):
        from app.admin.audit import _build_audit_where

        where, params = _build_audit_where(
            actions=["user.login", "user.logout"],
            user_id="u-1",
            date_from=date(2025, 1, 1),
            date_to=date(2025, 1, 31),
        )
        assert "a.action IN (%s, %s)" in where
        assert "a.user_id = %s" in where
        assert "a.created_at >= %s" in where
        assert "a.created_at < %s" in where


# ---------------------------------------------------------------------------
# Filtered DB query
# ---------------------------------------------------------------------------


class TestGetAuditLogPageFiltered:
    @patch("app.admin.audit._connect")
    def test_filtered_query_includes_where(self, mock_connect: MagicMock):
        from app.admin.audit import _get_audit_log_page

        mock_conn = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)

        mock_conn.execute.side_effect = [
            MagicMock(fetchone=MagicMock(return_value=(0,))),
            MagicMock(fetchall=MagicMock(return_value=[])),
        ]

        entries, total = _get_audit_log_page(1, 25, action="user.login", query="test")

        # First call is COUNT
        count_sql = mock_conn.execute.call_args_list[0][0][0]
        assert "a.action = %s" in count_sql
        assert "a.detail::text ILIKE %s" in count_sql

    @patch("app.admin.audit._connect")
    def test_unfiltered_returns_entries(self, mock_connect: MagicMock):
        from app.admin.audit import _get_audit_log_page

        mock_conn = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)

        now = datetime(2025, 3, 15, 10, 30)
        # New tuple shape: includes u.org_id at position 6
        mock_conn.execute.side_effect = [
            MagicMock(fetchone=MagicMock(return_value=(1,))),
            MagicMock(
                fetchall=MagicMock(
                    return_value=[
                        (1, "user-1", "Test User", "user.login", None, now, "org-1"),
                    ]
                )
            ),
        ]

        entries, total = _get_audit_log_page(1, 25)
        assert total == 1
        assert len(entries) == 1
        assert entries[0]["action"] == "user.login"
        assert entries[0]["user_name"] == "Test User"
        assert entries[0]["org_id"] == "org-1"

    @patch("app.admin.audit._connect")
    def test_multi_action_passes_in_clause(self, mock_connect: MagicMock):
        from app.admin.audit import _get_audit_log_page

        mock_conn = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)
        mock_conn.execute.side_effect = [
            MagicMock(fetchone=MagicMock(return_value=(0,))),
            MagicMock(fetchall=MagicMock(return_value=[])),
        ]

        _get_audit_log_page(1, 25, actions=["user.login", "doc.upload"])

        count_sql = mock_conn.execute.call_args_list[0][0][0]
        assert "a.action IN (%s, %s)" in count_sql


# ---------------------------------------------------------------------------
# Distinct actions, users, orgs
# ---------------------------------------------------------------------------


class TestFilterOptions:
    @patch("app.admin.audit._connect")
    def test_get_distinct_actions(self, mock_connect: MagicMock):
        from app.admin.audit import _get_distinct_actions

        mock_conn = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)
        mock_conn.execute.return_value.fetchall.return_value = [
            ("doc.upload",),
            ("user.login",),
        ]

        actions = _get_distinct_actions()
        assert actions == ["doc.upload", "user.login"]

    @patch("app.admin.audit._connect")
    def test_get_distinct_actions_on_error(self, mock_connect: MagicMock):
        from app.admin.audit import _get_distinct_actions

        mock_connect.side_effect = Exception("DB down")
        actions = _get_distinct_actions()
        assert actions == []

    @patch("app.admin.audit._connect")
    def test_get_audit_users(self, mock_connect: MagicMock):
        from app.admin.audit import _get_audit_users

        mock_conn = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)
        mock_conn.execute.return_value.fetchall.return_value = [
            ("u-1", "Alice"),
            ("u-2", "Bob"),
        ]

        users = _get_audit_users()
        assert len(users) == 2
        assert users[0] == {"id": "u-1", "name": "Alice"}

    @patch("app.admin.audit._connect")
    def test_get_audit_users_on_error(self, mock_connect: MagicMock):
        from app.admin.audit import _get_audit_users

        mock_connect.side_effect = Exception("DB down")
        users = _get_audit_users()
        assert users == []

    @patch("app.admin.audit._connect")
    def test_get_audit_orgs(self, mock_connect: MagicMock):
        from app.admin.audit import _get_audit_orgs

        mock_conn = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)
        mock_conn.execute.return_value.fetchall.return_value = [
            ("org-1", "Justiitsministeerium"),
            ("org-2", "Riigikantselei"),
        ]

        orgs = _get_audit_orgs()
        assert orgs == [
            {"id": "org-1", "name": "Justiitsministeerium"},
            {"id": "org-2", "name": "Riigikantselei"},
        ]

    @patch("app.admin.audit._connect")
    def test_get_audit_orgs_on_error(self, mock_connect: MagicMock):
        from app.admin.audit import _get_audit_orgs

        mock_connect.side_effect = Exception("DB down")
        assert _get_audit_orgs() == []


# ---------------------------------------------------------------------------
# Filter extraction & querystring round-trip
# ---------------------------------------------------------------------------


class TestExtractFilters:
    def test_single_action(self):
        from app.admin.audit import _extract_filters

        req = _make_request("/admin/audit", "action=user.login&user=u-1&from=2025-01-01")
        f = _extract_filters(req)
        assert f["action"] == "user.login"
        assert f["user"] == "u-1"
        assert f["from"] == "2025-01-01"

    def test_multi_action_via_actions_param(self):
        from app.admin.audit import _extract_filters

        req = _make_request("/admin/audit", "actions=user.login&actions=doc.upload")
        f = _extract_filters(req)
        assert f["actions"] == ["user.login", "doc.upload"]

    def test_org_param(self):
        from app.admin.audit import _extract_filters

        req = _make_request("/admin/audit", "org=org-42")
        f = _extract_filters(req)
        assert f["org"] == "org-42"

    def test_org_present_flag_tracks_explicit_param(self):
        """#861-B: ``org_present`` distinguishes a blank ?org= from no ?org=."""
        from app.admin.audit import _extract_filters

        # Present but blank (explicit "all orgs" opt-in).
        f_blank = _extract_filters(_make_request("/admin/audit", "org="))
        assert f_blank["org_present"] is True
        # Absent entirely (default-to-own-org).
        f_absent = _extract_filters(_make_request("/admin/audit", "user=u-1"))
        assert f_absent["org_present"] is False


# ---------------------------------------------------------------------------
# Org-scoping policy + LIKE escaping (#861-B)
# ---------------------------------------------------------------------------


class TestEscapeLike:
    def test_escapes_percent_underscore_backslash(self):
        from app.admin.audit import _escape_like

        assert _escape_like("100%") == "100\\%"
        assert _escape_like("a_b") == "a\\_b"
        # Backslash escaped first so it is not double-processed.
        assert _escape_like("a\\b") == "a\\\\b"

    def test_plain_text_unchanged(self):
        from app.admin.audit import _escape_like

        assert _escape_like("draft.upload") == "draft.upload"


class TestResolveOrgScope:
    def test_defaults_to_own_org_when_no_param(self):
        from app.admin.audit import _resolve_org_scope

        filters = {"org": "", "org_present": False}
        auth = {"role": "admin", "org_id": "own-org"}
        assert _resolve_org_scope(filters, auth) == "own-org"

    def test_platform_admin_without_org_sees_all_by_default(self):
        from app.admin.audit import _resolve_org_scope

        filters = {"org": "", "org_present": False}
        auth = {"role": "admin"}  # no org_id
        assert _resolve_org_scope(filters, auth) is None

    def test_explicit_org_override_wins(self):
        from app.admin.audit import _resolve_org_scope

        filters = {"org": "picked-org", "org_present": True}
        auth = {"role": "admin", "org_id": "own-org"}
        assert _resolve_org_scope(filters, auth) == "picked-org"

    def test_explicit_all_token_returns_none(self):
        from app.admin.audit import _resolve_org_scope

        auth = {"role": "admin", "org_id": "own-org"}
        for token in ("all", "*", ""):
            filters = {"org": token, "org_present": True}
            assert _resolve_org_scope(filters, auth) is None

    def test_no_auth_returns_none(self):
        from app.admin.audit import _resolve_org_scope

        assert _resolve_org_scope({"org": "", "org_present": False}, None) is None


class TestHasNarrowingFilter:
    def test_all_org_is_not_narrowing(self):
        from app.admin.audit import _has_narrowing_filter

        base = {"action": "", "actions": [], "user": "", "from": "", "to": "", "query": ""}
        assert _has_narrowing_filter({**base, "org": "all", "org_present": True}) is False
        assert _has_narrowing_filter({**base, "org": "", "org_present": True}) is False

    def test_explicit_specific_org_is_narrowing(self):
        from app.admin.audit import _has_narrowing_filter

        base = {"action": "", "actions": [], "user": "", "from": "", "to": "", "query": ""}
        # An explicitly-chosen specific org narrows the result set.
        assert _has_narrowing_filter({**base, "org": "org-7", "org_present": True}) is True

    def test_resolved_default_org_is_not_narrowing(self):
        """#886 review: a specific org that came from the default-to-own-org
        resolution (org_present=False) must NOT count as a narrowing filter —
        otherwise an org admin with an empty log gets an "over-filtered" empty
        state and a clear-filters button that just reloads the same scope."""
        from app.admin.audit import _has_narrowing_filter

        base = {"action": "", "actions": [], "user": "", "from": "", "to": "", "query": ""}
        # Own-org UUID resolved as the default — org_present is False.
        assert _has_narrowing_filter({**base, "org": "own-org", "org_present": False}) is False

    def test_other_filters_are_narrowing(self):
        from app.admin.audit import _has_narrowing_filter

        base = {
            "action": "",
            "actions": [],
            "user": "",
            "from": "",
            "to": "",
            "query": "",
            "org": "all",
            "org_present": True,
        }
        assert _has_narrowing_filter({**base, "query": "x"}) is True
        assert _has_narrowing_filter({**base, "action": "user.login"}) is True


class TestAuditPageOrgScoping:
    """End-to-end: the page handler applies the default-to-own-org policy."""

    @patch("app.admin.audit._get_audit_orgs")
    @patch("app.admin.audit._get_audit_users")
    @patch("app.admin.audit._get_distinct_actions")
    @patch("app.admin.audit._get_audit_log_page")
    def test_default_scopes_query_to_own_org(
        self,
        mock_page: MagicMock,
        mock_actions: MagicMock,
        mock_users: MagicMock,
        mock_orgs: MagicMock,
    ):
        from app.admin.audit import admin_audit_page

        mock_page.return_value = ([], 0)
        mock_actions.return_value = []
        mock_users.return_value = []
        mock_orgs.return_value = []

        # Admin with an org_id, no ?org= override.
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/admin/audit",
            "headers": [],
            "query_string": b"",
            "scheme": "http",
            "server": ("testserver", 80),
            "client": ("127.0.0.1", 12345),
            "auth": {
                "role": "admin",
                "id": "a-1",
                "org_id": "home-org",
                "email": "a@b.ee",
                "full_name": "Admin User",
            },
        }
        admin_audit_page(Request(scope))
        # The DB query must have been scoped to the admin's own org.
        assert mock_page.call_args.kwargs.get("org_id") == "home-org"

    @patch("app.admin.audit._get_audit_orgs")
    @patch("app.admin.audit._get_audit_users")
    @patch("app.admin.audit._get_distinct_actions")
    @patch("app.admin.audit._get_audit_log_page")
    def test_explicit_all_widens_to_cross_org(
        self,
        mock_page: MagicMock,
        mock_actions: MagicMock,
        mock_users: MagicMock,
        mock_orgs: MagicMock,
    ):
        from app.admin.audit import admin_audit_page

        mock_page.return_value = ([], 0)
        mock_actions.return_value = []
        mock_users.return_value = []
        mock_orgs.return_value = []

        scope = {
            "type": "http",
            "method": "GET",
            "path": "/admin/audit",
            "headers": [],
            "query_string": b"org=all",
            "scheme": "http",
            "server": ("testserver", 80),
            "client": ("127.0.0.1", 12345),
            "auth": {
                "role": "admin",
                "id": "a-1",
                "org_id": "home-org",
                "email": "a@b.ee",
                "full_name": "Admin User",
            },
        }
        admin_audit_page(Request(scope))
        # Cross-org opt-in => no org predicate.
        assert mock_page.call_args.kwargs.get("org_id") is None


def _audit_scope(query_string: str = "", *, org_id: str | None = "home-org") -> Request:
    """Admin Request for the audit page; ``org_id=None`` => platform admin."""
    auth: dict[str, object] = {
        "role": "admin",
        "id": "a-1",
        "email": "a@b.ee",
        "full_name": "Admin User",
    }
    if org_id is not None:
        auth["org_id"] = org_id
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/admin/audit",
            "headers": [],
            "query_string": query_string.encode(),
            "scheme": "http",
            "server": ("testserver", 80),
            "client": ("127.0.0.1", 12345),
            "auth": auth,
        }
    )


class TestAuditEmptyStateOrgScoping:
    """#886 review: the empty-state copy must not treat the resolved default
    org scope as a user-applied filter (the no-op clear-filters loop)."""

    @patch("app.admin.audit._get_audit_orgs")
    @patch("app.admin.audit._get_audit_users")
    @patch("app.admin.audit._get_distinct_actions")
    @patch("app.admin.audit._get_audit_log_page")
    def test_default_scope_empty_log_shows_plain_empty_copy(
        self,
        mock_page: MagicMock,
        mock_actions: MagicMock,
        mock_users: MagicMock,
        mock_orgs: MagicMock,
    ):
        from fasthtml.common import to_xml

        from app.admin.audit import admin_audit_page

        mock_page.return_value = ([], 0)
        mock_actions.return_value = []
        mock_users.return_value = []
        mock_orgs.return_value = []

        # Org admin, no ?org= — scoped to own org by default, empty log.
        html = to_xml(admin_audit_page(_audit_scope("")))
        # Plain "log is empty" copy, NOT the over-filtered variant, and NO
        # clear-filters button (it would just reload the identical scope).
        assert "Auditilogis kirjeid ei leitud" in html
        assert "Praeguste filtritega" not in html
        assert "Tühjenda filtrid" not in html

    @patch("app.admin.audit._get_audit_orgs")
    @patch("app.admin.audit._get_audit_users")
    @patch("app.admin.audit._get_distinct_actions")
    @patch("app.admin.audit._get_audit_log_page")
    def test_explicit_org_empty_result_shows_filtered_empty_state(
        self,
        mock_page: MagicMock,
        mock_actions: MagicMock,
        mock_users: MagicMock,
        mock_orgs: MagicMock,
    ):
        from fasthtml.common import to_xml

        from app.admin.audit import admin_audit_page

        mock_page.return_value = ([], 0)
        mock_actions.return_value = []
        mock_users.return_value = []
        mock_orgs.return_value = []

        # Platform admin explicitly picks a specific org that has no rows.
        html = to_xml(admin_audit_page(_audit_scope("org=other-org", org_id=None)))
        assert "Praeguste filtritega ei leitud kirjeid" in html
        assert "Tühjenda filtrid" in html
        assert "Auditilogis kirjeid ei leitud" not in html


class TestAuditPaginationScopePreservation:
    """#886 review: pagination + CSV links must carry the *effective* org so
    page 2 / the export does not silently widen back to all orgs."""

    def _render(self, req, rows: int) -> str:
        from fasthtml.common import to_xml

        from app.admin.audit import admin_audit_page

        entries = [
            {
                "id": i,
                "user_id": "u-1",
                "user_name": "U",
                "action": "user.login",
                "detail": {"x": 1},
                "created_at": datetime(2026, 4, 8, 9, 0),
                "org_id": "home-org",
            }
            for i in range(rows)
        ]
        with (
            patch("app.admin.audit._get_audit_log_page", return_value=(entries, rows)),
            patch("app.admin.audit._get_distinct_actions", return_value=[]),
            patch("app.admin.audit._get_audit_users", return_value=[]),
            patch(
                "app.admin.audit._get_audit_orgs",
                return_value=[
                    {"id": "home-org", "name": "Home"},
                    {"id": "other-org", "name": "Other"},
                ],
            ),
        ):
            return to_xml(admin_audit_page(req))

    def test_default_own_org_pagination_keeps_scope(self):
        # 60 rows over 25/page => multiple pages, so pagination links render.
        html = self._render(_audit_scope(""), rows=60)
        # Page-2 link must re-resolve to the same own-org scope. We emit it by
        # omitting ?org= (page 2 re-defaults to own-org) — so the page-2 href
        # must NOT carry an explicit foreign org and must point back to the
        # audit page.
        assert "/admin/audit?" in html
        assert "org=other-org" not in html

    def test_explicit_all_pagination_preserves_cross_org(self):
        html = self._render(_audit_scope("org=all", org_id="home-org"), rows=60)
        # The cross-org opt-in must survive pagination as org=all.
        assert "org=all" in html

    def test_explicit_specific_org_csv_link_carries_org(self):
        html = self._render(_audit_scope("org=other-org", org_id=None), rows=5)
        # The CSV export href is scoped to the chosen org.
        assert "/admin/audit/export?" in html
        assert "org=other-org" in html


class TestFilterQuerystring:
    def test_round_trip_preserves_actions(self):
        from app.admin.audit import _filter_querystring

        qs = _filter_querystring(
            {
                "action": "",
                "actions": ["a", "b"],
                "user": "u-1",
                "org": "",
                "from": "",
                "to": "",
                "query": "x",
            }
        )
        # Order isn't strict — just check presence
        assert "actions=a" in qs
        assert "actions=b" in qs
        assert "user=u-1" in qs
        assert "query=x" in qs

    def test_empty_filters_yields_empty_string(self):
        from app.admin.audit import _filter_querystring

        qs = _filter_querystring(
            {"action": "", "actions": [], "user": "", "org": "", "from": "", "to": "", "query": ""}
        )
        assert qs == ""


# ---------------------------------------------------------------------------
# Detail summary + JSON formatter
# ---------------------------------------------------------------------------


class TestSummarizeDetail:
    def test_none_returns_emdash(self):
        from app.admin.audit import _summarize_detail

        assert _summarize_detail("user.login", None) == "—"

    def test_user_login_uses_email(self):
        from app.admin.audit import _summarize_detail

        s = _summarize_detail("user.login", {"email": "alice@example.ee"})
        assert "alice@example.ee" in s
        assert "Sisselogimine" in s

    def test_doc_upload_uses_filename(self):
        from app.admin.audit import _summarize_detail

        s = _summarize_detail("doc.upload", {"filename": "eelnõu.docx"})
        assert "eelnõu.docx" in s
        assert "Üleslaadimine" in s

    def test_long_message_truncates(self):
        from app.admin.audit import _summarize_detail

        long = "x" * 200
        s = _summarize_detail("unknown.action", {"message": long})
        assert s.endswith("…")
        assert len(s) <= 80

    def test_fallback_to_json_when_no_message(self):
        from app.admin.audit import _summarize_detail

        s = _summarize_detail("unknown.action", {"foo": "bar"})
        assert "foo" in s


class TestFormatDetailJson:
    def test_dict_pretty_prints(self):
        from app.admin.audit import _format_detail_json

        s = _format_detail_json({"a": 1, "b": [1, 2]})
        # Indented JSON with non-ASCII preserved
        assert '"a"' in s
        assert "\n" in s

    def test_string_is_reparsed(self):
        from app.admin.audit import _format_detail_json

        s = _format_detail_json('{"a":1}')
        assert '"a"' in s
        assert "\n" in s  # pretty-printed

    def test_invalid_string_returned_as_is(self):
        from app.admin.audit import _format_detail_json

        assert _format_detail_json("not json") == "not json"

    def test_none_returns_empty(self):
        from app.admin.audit import _format_detail_json

        assert _format_detail_json(None) == ""

    def test_non_ascii_preserved(self):
        from app.admin.audit import _format_detail_json

        s = _format_detail_json({"title": "Eelnõu"})
        assert "Eelnõu" in s


# ---------------------------------------------------------------------------
# Detail expander fragment endpoint
# ---------------------------------------------------------------------------


class TestAuditDetailFragment:
    @patch("app.admin.audit._get_audit_entry")
    def test_returns_formatted_json(self, mock_entry: MagicMock):
        from fasthtml.common import to_xml

        from app.admin.audit import admin_audit_detail

        mock_entry.return_value = {
            "id": 5,
            "user_id": "u-1",
            "user_name": "Alice",
            "action": "doc.upload",
            "detail": {"filename": "x.docx", "size": 1024},
            "created_at": datetime(2025, 6, 15, 12, 0),
        }
        req = _make_request("/admin/audit/detail/5")
        req.scope["path_params"] = {"id": "5"}

        result = admin_audit_detail(req)
        html = to_xml(result)
        assert "filename" in html
        assert "x.docx" in html
        assert "audit-detail-json" in html

    @patch("app.admin.audit._get_audit_entry")
    def test_missing_entry_shows_estonian_message(self, mock_entry: MagicMock):
        from fasthtml.common import to_xml

        from app.admin.audit import admin_audit_detail

        mock_entry.return_value = None
        req = _make_request("/admin/audit/detail/999")
        req.scope["path_params"] = {"id": "999"}

        result = admin_audit_detail(req)
        html = to_xml(result)
        assert "Kirjet ei leitud" in html

    @patch("app.admin.audit._get_audit_entry")
    def test_empty_detail_shows_estonian_message(self, mock_entry: MagicMock):
        from fasthtml.common import to_xml

        from app.admin.audit import admin_audit_detail

        mock_entry.return_value = {
            "id": 1,
            "user_id": None,
            "user_name": "Süsteem",
            "action": "system.boot",
            "detail": None,
            "created_at": datetime(2025, 1, 1),
        }
        req = _make_request("/admin/audit/detail/1")
        req.scope["path_params"] = {"id": "1"}

        result = admin_audit_detail(req)
        html = to_xml(result)
        assert "Lisadetaile pole" in html

    def test_invalid_id_returns_400(self):
        from app.admin.audit import admin_audit_detail

        req = _make_request("/admin/audit/detail/abc")
        req.scope["path_params"] = {"id": "abc"}
        result = admin_audit_detail(req)
        assert result.status_code == 400  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# Detail-endpoint org scoping (#886 review)
# ---------------------------------------------------------------------------


def _detail_request(entry_id: str, *, query_string: str = "", org_id: str | None) -> Request:
    """Build an /admin/audit/detail/{id} Request with a controllable scope.

    ``org_id=None`` models a platform admin (no home org); otherwise the
    admin is bound to that org.
    """
    auth: dict[str, object] = {"role": "admin", "id": "a-1", "email": "a@b.ee"}
    if org_id is not None:
        auth["org_id"] = org_id
    req = Request(
        {
            "type": "http",
            "method": "GET",
            "path": f"/admin/audit/detail/{entry_id}",
            "headers": [],
            "query_string": query_string.encode(),
            "scheme": "http",
            "server": ("testserver", 80),
            "client": ("127.0.0.1", 12345),
            "auth": auth,
            "path_params": {"id": entry_id},
        }
    )
    return req


class TestGetAuditEntryScoping:
    """#886: the single-row fetch must apply the SAME org predicate as the list."""

    @patch("app.admin.audit._connect")
    def test_no_org_id_has_no_org_predicate(self, mock_connect: MagicMock):
        from app.admin.audit import _get_audit_entry

        mock_conn = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)
        mock_conn.execute.return_value.fetchone.return_value = None

        _get_audit_entry(5)
        sql = mock_conn.execute.call_args[0][0]
        params = mock_conn.execute.call_args[0][1]
        assert "u.org_id" not in sql
        assert list(params) == [5]

    @patch("app.admin.audit._connect")
    def test_org_id_adds_predicate_and_param(self, mock_connect: MagicMock):
        from app.admin.audit import _get_audit_entry

        mock_conn = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)
        mock_conn.execute.return_value.fetchone.return_value = None

        _get_audit_entry(5, org_id="home-org")
        sql = mock_conn.execute.call_args[0][0]
        params = mock_conn.execute.call_args[0][1]
        # Same join + predicate the list path uses.
        assert "LEFT JOIN users u ON u.id = a.user_id" in sql
        assert "u.org_id = %s" in sql
        assert list(params) == [5, "home-org"]


class TestAuditDetailOrgScoping:
    """#886: the HTMX detail endpoint enforces the list's org scope and treats
    an out-of-scope id exactly like a missing row (no existence leak)."""

    @patch("app.admin.audit._get_audit_entry")
    def test_own_org_admin_can_expand_own_org_entry(self, mock_entry: MagicMock):
        from fasthtml.common import to_xml

        from app.admin.audit import admin_audit_detail

        mock_entry.return_value = {
            "id": 5,
            "user_id": "u-1",
            "user_name": "Alice",
            "action": "doc.upload",
            "detail": {"filename": "x.docx"},
            "created_at": datetime(2025, 6, 15, 12, 0),
        }
        result = admin_audit_detail(_detail_request("5", org_id="home-org"))
        html = to_xml(result)
        assert "x.docx" in html
        # The fetch was scoped to the admin's own org.
        assert mock_entry.call_args.kwargs.get("org_id") == "home-org"

    @patch("app.admin.audit._get_audit_entry", return_value=None)
    def test_own_org_admin_other_org_id_gets_not_found(self, mock_entry: MagicMock):
        """An org admin requesting another org's id: the scoped fetch returns
        None (the DB predicate excludes it), so the handler shows the
        not-found branch — identical to a genuinely missing row, no 403 body."""
        from fasthtml.common import to_xml

        from app.admin.audit import admin_audit_detail

        result = admin_audit_detail(_detail_request("999", org_id="home-org"))
        html = to_xml(result)
        assert "Kirjet ei leitud" in html
        # The fetch was scoped to own-org; the foreign id simply did not match.
        assert mock_entry.call_args.kwargs.get("org_id") == "home-org"
        # Same not-found copy as TestAuditDetailFragment — no distinct 403.
        assert "keelatud" not in html.lower()

    @patch("app.admin.audit._get_audit_entry")
    def test_platform_admin_reads_any_entry(self, mock_entry: MagicMock):
        from fasthtml.common import to_xml

        from app.admin.audit import admin_audit_detail

        mock_entry.return_value = {
            "id": 7,
            "user_id": "u-9",
            "user_name": "Bob",
            "action": "draft.create",
            "detail": {"title": "Foo"},
            "created_at": datetime(2025, 6, 15, 12, 0),
        }
        # Platform admin: no org_id, no ?org= => no scoping predicate.
        result = admin_audit_detail(_detail_request("7", org_id=None))
        html = to_xml(result)
        assert "Foo" in html
        assert mock_entry.call_args.kwargs.get("org_id") is None

    @patch("app.admin.audit._get_audit_entry")
    def test_explicit_org_all_imposes_no_scope(self, mock_entry: MagicMock):
        from fasthtml.common import to_xml

        from app.admin.audit import admin_audit_detail

        mock_entry.return_value = {
            "id": 8,
            "user_id": "u-3",
            "user_name": "Cara",
            "action": "doc.upload",
            "detail": {"filename": "y.docx"},
            "created_at": datetime(2025, 6, 15, 12, 0),
        }
        # Org admin who explicitly opted into the cross-org view.
        result = admin_audit_detail(
            _detail_request("8", query_string="org=all", org_id="home-org")
        )
        html = to_xml(result)
        assert "y.docx" in html
        # ?org=all => cross-org => no org predicate on the fetch.
        assert mock_entry.call_args.kwargs.get("org_id") is None

    @patch("app.admin.audit._get_audit_entry")
    def test_explicit_specific_org_scopes_fetch(self, mock_entry: MagicMock):
        from app.admin.audit import admin_audit_detail

        mock_entry.return_value = None
        # Platform admin drilling into one specific org's entry.
        admin_audit_detail(_detail_request("9", query_string="org=other-org", org_id=None))
        assert mock_entry.call_args.kwargs.get("org_id") == "other-org"


class TestAuditDetailExpanderUrl:
    """#886: the list expander hx_get must carry the active org so the
    fragment resolves the same scope as the list it was rendered in."""

    def _render_list(self, req, *, org_rows: bool = True) -> str:
        from fasthtml.common import to_xml

        from app.admin.audit import admin_audit_page

        entries = [
            {
                "id": 42,
                "user_id": "u-1",
                "user_name": "U",
                "action": "doc.upload",
                "detail": {"filename": "z.docx"},
                "created_at": datetime(2026, 4, 8, 9, 0),
                "org_id": "home-org",
            }
        ]
        orgs = (
            [{"id": "home-org", "name": "Home"}, {"id": "other-org", "name": "Other"}]
            if org_rows
            else []
        )
        with (
            patch("app.admin.audit._get_audit_log_page", return_value=(entries, 1)),
            patch("app.admin.audit._get_distinct_actions", return_value=[]),
            patch("app.admin.audit._get_audit_users", return_value=[]),
            patch("app.admin.audit._get_audit_orgs", return_value=orgs),
        ):
            return to_xml(admin_audit_page(req))

    def test_explicit_all_expander_carries_org(self):
        html = self._render_list(_audit_scope("org=all", org_id="home-org"))
        assert "/admin/audit/detail/42?org=all" in html

    def test_default_own_org_expander_omits_org(self):
        # Default scope: the expander omits ?org= (the fragment re-defaults to
        # own-org), matching the pagination/CSV omission policy.
        html = self._render_list(_audit_scope("", org_id="home-org"))
        assert "/admin/audit/detail/42" in html
        assert "/admin/audit/detail/42?org=" not in html


# ---------------------------------------------------------------------------
# CSV export
# ---------------------------------------------------------------------------


class TestAuditExport:
    @patch("app.admin.audit._get_all_filtered_entries")
    def test_csv_export_returns_csv_response(self, mock_entries: MagicMock):
        from app.admin.audit import admin_audit_export

        now = datetime(2025, 3, 15, 10, 30)
        mock_entries.return_value = [
            {
                "id": 1,
                "user_id": "u-1",
                "user_name": "Alice",
                "action": "user.login",
                "detail": {"email": "alice@example.ee"},
                "created_at": now,
                "org_id": "org-1",
            }
        ]

        req = _make_request("/admin/audit/export", "action=user.login")
        response = admin_audit_export(req)

        assert response.status_code == 200  # type: ignore[union-attr]
        assert response.media_type == "text/csv"  # type: ignore[union-attr]
        assert "attachment" in response.headers["content-disposition"]  # type: ignore[union-attr]

        body = response.body.decode()  # type: ignore[union-attr]
        assert "Kasutaja" in body  # header row
        assert "Alice" in body
        assert "user.login" in body

    @patch("app.admin.audit._get_all_filtered_entries")
    def test_csv_export_empty(self, mock_entries: MagicMock):
        from app.admin.audit import admin_audit_export

        mock_entries.return_value = []
        req = _make_request("/admin/audit/export")
        response = admin_audit_export(req)

        assert response.status_code == 200  # type: ignore[union-attr]
        body = response.body.decode()  # type: ignore[union-attr]
        lines = body.strip().split("\n")
        assert len(lines) == 1  # header only

    @patch("app.admin.audit._get_all_filtered_entries")
    def test_csv_export_passes_filters(self, mock_entries: MagicMock):
        from app.admin.audit import admin_audit_export

        mock_entries.return_value = []
        req = _make_request(
            "/admin/audit/export",
            "action=doc.upload&user=u-2&query=test&from=2025-01-01&to=2025-12-31",
        )
        admin_audit_export(req)

        call_kwargs = mock_entries.call_args[1]
        assert call_kwargs["action"] == "doc.upload"
        assert call_kwargs["user_id"] == "u-2"
        assert call_kwargs["query"] == "test"
        assert call_kwargs["date_from"] == date(2025, 1, 1)
        assert call_kwargs["date_to"] == date(2025, 12, 31)

    @patch("app.admin.audit._get_all_filtered_entries")
    def test_csv_export_passes_multi_actions(self, mock_entries: MagicMock):
        from app.admin.audit import admin_audit_export

        mock_entries.return_value = []
        req = _make_request(
            "/admin/audit/export",
            "actions=user.login&actions=doc.upload&org=org-1",
        )
        admin_audit_export(req)

        call_kwargs = mock_entries.call_args[1]
        assert call_kwargs["actions"] == ["user.login", "doc.upload"]
        assert call_kwargs["org_id"] == "org-1"

    @patch("app.admin.audit._get_all_filtered_entries")
    def test_csv_export_includes_detail_column(self, mock_entries: MagicMock):
        from app.admin.audit import admin_audit_export

        mock_entries.return_value = [
            {
                "id": 1,
                "user_id": "u-1",
                "user_name": "Alice",
                "action": "doc.upload",
                "detail": {"filename": "eelnõu.docx", "size": 2048},
                "created_at": datetime(2025, 3, 15, 10, 30),
                "org_id": "org-1",
            }
        ]
        req = _make_request("/admin/audit/export")
        response = admin_audit_export(req)
        body = response.body.decode()  # type: ignore[union-attr]

        # Header includes detail and org id
        assert "Detailid (JSON)" in body
        assert "Organisatsioon ID" in body
        # Detail is JSON-stringified (single column)
        assert "filename" in body
        assert "eelnõu.docx" in body
        # Body has exactly one data line + header
        lines = [line for line in body.strip().split("\n") if line]
        assert len(lines) == 2

    @patch("app.admin.audit._get_all_filtered_entries")
    def test_csv_export_dict_detail_round_trips_as_json(self, mock_entries: MagicMock):
        from app.admin.audit import admin_audit_export

        detail = {"foo": "bar", "n": 7}
        mock_entries.return_value = [
            {
                "id": 1,
                "user_id": "u-1",
                "user_name": "X",
                "action": "x.y",
                "detail": detail,
                "created_at": datetime(2025, 1, 1),
                "org_id": "",
            }
        ]
        req = _make_request("/admin/audit/export")
        response = admin_audit_export(req)
        body = response.body.decode()  # type: ignore[union-attr]

        # The detail column should be valid JSON we can parse back
        rows = list(csv_reader_rows(body))
        # row[5] is the detail cell (id, user_id, user, org_id, action, detail, date)
        # Header order: ID, Kasutaja ID, Kasutaja, Organisatsioon ID, Tegevus,
        #              Detailid (JSON), Kuupäev
        data_row = rows[1]
        parsed = json.loads(data_row[5])
        assert parsed == detail


def csv_reader_rows(text: str):
    """Yield CSV rows parsed by the stdlib csv module."""
    import csv
    import io

    return list(csv.reader(io.StringIO(text)))


# ---------------------------------------------------------------------------
# Result-count + filter-context header
# ---------------------------------------------------------------------------


class TestFilterSummary:
    def test_no_filters(self):
        from app.admin.audit import _filter_summary_text

        text = _filter_summary_text(
            {
                "action": "",
                "actions": [],
                "user": "",
                "org": "",
                "from": "",
                "to": "",
                "query": "",
            },
            42,
        )
        assert "Leitud 42 kirjet" in text
        assert "Filtreerimisel" not in text

    def test_with_filters_shows_context(self):
        from app.admin.audit import _filter_summary_text

        text = _filter_summary_text(
            {
                "action": "",
                "actions": ["user.login"],
                "user": "u-1",
                "org": "",
                "from": "2025-01-01",
                "to": "2025-01-31",
                "query": "test",
            },
            3,
        )
        assert "Leitud 3 kirjet" in text
        assert "Filtreerimisel" in text
        assert "user.login" in text
        assert "kasutaja valitud" in text
        assert "kuupäev" in text
        assert "test" in text


# ---------------------------------------------------------------------------
# Page rendering
# ---------------------------------------------------------------------------


class TestAdminAuditPageRendering:
    @patch("app.admin.audit._get_audit_orgs")
    @patch("app.admin.audit._get_audit_users")
    @patch("app.admin.audit._get_distinct_actions")
    @patch("app.admin.audit._get_audit_log_page")
    def test_page_renders_filter_form(
        self,
        mock_page: MagicMock,
        mock_actions: MagicMock,
        mock_users: MagicMock,
        mock_orgs: MagicMock,
    ):
        from fasthtml.common import to_xml

        from app.admin.audit import admin_audit_page

        mock_page.return_value = ([], 0)
        mock_actions.return_value = ["user.login", "doc.upload"]
        mock_users.return_value = [{"id": "u-1", "name": "Alice"}]
        mock_orgs.return_value = []

        req = _make_request()
        result = admin_audit_page(req)

        html = to_xml(result)
        # Filter form elements
        assert "filter-action" in html
        assert "filter-user" in html
        assert "filter-from" in html
        assert "filter-to" in html
        assert "filter-query" in html
        # Estonian labels
        assert "Tegevus" in html
        assert "Kasutaja" in html
        assert "Alguskuupäev" in html
        assert "Lõppkuupäev" in html
        # Export link
        assert "Ekspordi" in html

    @patch("app.admin.audit._get_audit_orgs")
    @patch("app.admin.audit._get_audit_users")
    @patch("app.admin.audit._get_distinct_actions")
    @patch("app.admin.audit._get_audit_log_page")
    def test_page_renders_entries_with_expander(
        self,
        mock_page: MagicMock,
        mock_actions: MagicMock,
        mock_users: MagicMock,
        mock_orgs: MagicMock,
    ):
        from fasthtml.common import to_xml

        from app.admin.audit import admin_audit_page

        now = datetime(2025, 6, 15, 14, 30)
        mock_page.return_value = (
            [
                {
                    "id": 1,
                    "user_id": "u-1",
                    "user_name": "Alice",
                    "action": "user.login",
                    "detail": {"email": "alice@example.ee"},
                    "created_at": now,
                    "org_id": "org-1",
                }
            ],
            1,
        )
        mock_actions.return_value = []
        mock_users.return_value = []
        mock_orgs.return_value = []

        req = _make_request()
        result = admin_audit_page(req)

        html = to_xml(result)
        assert "Alice" in html
        assert "user.login" in html
        assert "15.06.2025" in html
        # Summary cell + lazy-load fragment URL
        assert "Sisselogimine" in html
        assert "/admin/audit/detail/1" in html
        assert "audit-detail" in html

    @patch("app.admin.audit._get_audit_orgs")
    @patch("app.admin.audit._get_audit_users")
    @patch("app.admin.audit._get_distinct_actions")
    @patch("app.admin.audit._get_audit_log_page")
    def test_org_dropdown_only_when_multi_org(
        self,
        mock_page: MagicMock,
        mock_actions: MagicMock,
        mock_users: MagicMock,
        mock_orgs: MagicMock,
    ):
        from fasthtml.common import to_xml

        from app.admin.audit import admin_audit_page

        mock_page.return_value = ([], 0)
        mock_actions.return_value = []
        mock_users.return_value = []
        mock_orgs.return_value = [
            {"id": "o-1", "name": "Justiits"},
            {"id": "o-2", "name": "Riigi"},
        ]

        req = _make_request()
        html = to_xml(admin_audit_page(req))
        assert "filter-org" in html
        assert "Organisatsioon" in html

    @patch("app.admin.audit._get_audit_orgs")
    @patch("app.admin.audit._get_audit_users")
    @patch("app.admin.audit._get_distinct_actions")
    @patch("app.admin.audit._get_audit_log_page")
    def test_org_dropdown_hidden_for_single_org(
        self,
        mock_page: MagicMock,
        mock_actions: MagicMock,
        mock_users: MagicMock,
        mock_orgs: MagicMock,
    ):
        from fasthtml.common import to_xml

        from app.admin.audit import admin_audit_page

        mock_page.return_value = ([], 0)
        mock_actions.return_value = []
        mock_users.return_value = []
        mock_orgs.return_value = [{"id": "o-1", "name": "Justiits"}]

        req = _make_request()
        html = to_xml(admin_audit_page(req))
        assert "filter-org" not in html

    @patch("app.admin.audit._get_audit_orgs")
    @patch("app.admin.audit._get_audit_users")
    @patch("app.admin.audit._get_distinct_actions")
    @patch("app.admin.audit._get_audit_log_page")
    def test_empty_state_with_filter_context(
        self,
        mock_page: MagicMock,
        mock_actions: MagicMock,
        mock_users: MagicMock,
        mock_orgs: MagicMock,
    ):
        from fasthtml.common import to_xml

        from app.admin.audit import admin_audit_page

        mock_page.return_value = ([], 0)
        mock_actions.return_value = []
        mock_users.return_value = []
        mock_orgs.return_value = []

        req = _make_request("/admin/audit", "action=does.not.exist&from=2025-01-01")
        html = to_xml(admin_audit_page(req))

        # Empty-state with filter context (NOT the unfiltered "log empty" copy)
        assert "Praeguste filtritega ei leitud kirjeid" in html
        assert "Tühjenda filtrid" in html
        assert "Auditilogis kirjeid ei leitud" not in html

    @patch("app.admin.audit._get_audit_orgs")
    @patch("app.admin.audit._get_audit_users")
    @patch("app.admin.audit._get_distinct_actions")
    @patch("app.admin.audit._get_audit_log_page")
    def test_unfiltered_empty_state(
        self,
        mock_page: MagicMock,
        mock_actions: MagicMock,
        mock_users: MagicMock,
        mock_orgs: MagicMock,
    ):
        from fasthtml.common import to_xml

        from app.admin.audit import admin_audit_page

        mock_page.return_value = ([], 0)
        mock_actions.return_value = []
        mock_users.return_value = []
        mock_orgs.return_value = []

        req = _make_request()
        html = to_xml(admin_audit_page(req))
        assert "Auditilogis kirjeid ei leitud" in html
        assert "Praeguste filtritega" not in html

    @patch("app.admin.audit._get_audit_orgs")
    @patch("app.admin.audit._get_audit_users")
    @patch("app.admin.audit._get_distinct_actions")
    @patch("app.admin.audit._get_audit_log_page")
    def test_result_count_header_renders(
        self,
        mock_page: MagicMock,
        mock_actions: MagicMock,
        mock_users: MagicMock,
        mock_orgs: MagicMock,
    ):
        from fasthtml.common import to_xml

        from app.admin.audit import admin_audit_page

        mock_page.return_value = ([], 7)
        mock_actions.return_value = []
        mock_users.return_value = []
        mock_orgs.return_value = []

        req = _make_request()
        html = to_xml(admin_audit_page(req))
        assert "Leitud 7 kirjet" in html

    @patch("app.admin.audit._get_audit_orgs")
    @patch("app.admin.audit._get_audit_users")
    @patch("app.admin.audit._get_distinct_actions")
    @patch("app.admin.audit._get_audit_log_page")
    def test_pagination_links_preserve_filters(
        self,
        mock_page: MagicMock,
        mock_actions: MagicMock,
        mock_users: MagicMock,
        mock_orgs: MagicMock,
    ):
        from fasthtml.common import to_xml

        from app.admin.audit import admin_audit_page

        # 100 records -> 4 pages with page_size=25 → pagination renders links
        mock_page.return_value = (
            [
                {
                    "id": i,
                    "user_id": "u-1",
                    "user_name": "Alice",
                    "action": "user.login",
                    "detail": None,
                    "created_at": datetime(2025, 6, 15),
                    "org_id": "org-1",
                }
                for i in range(25)
            ],
            100,
        )
        mock_actions.return_value = []
        mock_users.return_value = []
        mock_orgs.return_value = []

        req = _make_request("/admin/audit", "action=user.login&user=u-1")
        html = to_xml(admin_audit_page(req))

        # Pagination link to page 2 must carry filters AND page param
        assert "page=2" in html
        assert "action=user.login" in html
        assert "user=u-1" in html


# ---------------------------------------------------------------------------
# Route-level auth check
# ---------------------------------------------------------------------------


class TestAuditRouteAuth:
    def test_audit_export_redirects_unauthenticated(self):
        from app.main import app

        client = TestClient(app, follow_redirects=False)
        response = client.get("/admin/audit/export")
        assert response.status_code == 303
        assert response.headers["location"] == "/auth/login"

    def test_jobs_page_redirects_unauthenticated(self):
        from app.main import app

        client = TestClient(app, follow_redirects=False)
        response = client.get("/admin/jobs")
        assert response.status_code == 303
        assert response.headers["location"] == "/auth/login"

    def test_audit_detail_redirects_unauthenticated(self):
        from app.main import app

        client = TestClient(app, follow_redirects=False)
        response = client.get("/admin/audit/detail/1")
        assert response.status_code == 303
        assert response.headers["location"] == "/auth/login"
