"""Integration tests for ``POST /drafts/{id}/reanalyze`` (#306).

The handler lives in :mod:`app.docs.routes._lifecycle` next to the
delete + keep handlers. It is distinct from:

* ``POST /drafts/{id}/retry`` — restarts the pipeline from
  ``parse_draft`` on a *failed* draft (covered by ``test_docs_routes``).
* ``POST /drafts/{id}/report/reanalyze`` — ontology-drift banner
  re-run on the report page (covered by ``test_docs_report_routes``).

These tests exercise the full ``app.main.app`` via ``TestClient`` so
they validate the FastHTML wiring, the auth Beforeware, and the
status-state-machine guard. External dependencies — Postgres, the
JobQueue, the audit log — are mocked. Patch paths follow the
"patch where used" rule pinned by ``tests/test_docs_routes_patch_paths``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch

from starlette.testclient import TestClient

from app.docs.draft_model import Draft

# ---------------------------------------------------------------------------
# Helpers — copied from tests/test_docs_routes.py so this module stays
# self-contained and a refactor of the shared helpers can't quietly drop
# coverage of the #306 endpoint.
# ---------------------------------------------------------------------------


_ORG_ID = "11111111-1111-1111-1111-111111111111"
_OTHER_ORG_ID = "22222222-2222-2222-2222-222222222222"
_USER_ID = "33333333-3333-3333-3333-333333333333"
_DRAFT_ID = "44444444-4444-4444-4444-444444444444"


def _authed_user(role: str = "drafter") -> dict[str, Any]:
    return {
        "id": _USER_ID,
        "email": "koostaja@seadusloome.ee",
        "full_name": "Test Koostaja",
        "role": role,
        "org_id": _ORG_ID,
    }


def _make_draft(
    *,
    draft_id: uuid.UUID | None = None,
    org_id: str = _ORG_ID,
    user_id: str = _USER_ID,
    status: str = "ready",
    title: str = "Test eelnõu",
    error_message: str | None = None,
    created_at: datetime | None = None,
    updated_at: datetime | None = None,
    doc_type: str = "eelnou",
    parent_vtk_id: uuid.UUID | None = None,
    processing_completed_at: datetime | None = None,
) -> Draft:
    now = datetime.now(UTC)
    resolved_id = draft_id or uuid.UUID(_DRAFT_ID)
    return Draft(
        id=resolved_id,
        user_id=uuid.UUID(user_id),
        org_id=uuid.UUID(org_id),
        title=title,
        filename="eelnou.docx",
        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        file_size=2048,
        storage_path="/tmp/ciphertext.enc",
        graph_uri=f"https://data.riik.ee/ontology/estleg/drafts/{resolved_id}",
        status=status,
        parsed_text_encrypted=None,
        entity_count=None,
        error_message=error_message,
        created_at=created_at or now,
        updated_at=updated_at or now,
        doc_type=doc_type,  # type: ignore[arg-type]
        parent_vtk_id=parent_vtk_id,
        processing_completed_at=processing_completed_at,
    )


def _stub_provider(role: str = "drafter") -> MagicMock:
    provider = MagicMock()
    provider.get_current_user.return_value = _authed_user(role=role)
    return provider


def _authed_client() -> TestClient:
    client = TestClient(__import__("app.main", fromlist=["app"]).app, follow_redirects=False)
    client.cookies.set("access_token", "stub-token")
    return client


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestReanalyzeHappyPath:
    """Owner on a ready draft → status flips to analyzing, job enqueued,
    audit row written."""

    @patch("app.docs.routes._lifecycle.log_draft_reanalyze")
    @patch("app.docs.routes._lifecycle.JobQueue")
    @patch("app.docs.routes._lifecycle.update_draft_status")
    @patch("app.docs.routes._lifecycle._connect")
    @patch("app.docs.routes._lifecycle.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_ready_draft_htmx_redirects(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_connect: MagicMock,
        mock_update: MagicMock,
        mock_queue_cls: MagicMock,
        mock_log: MagicMock,
    ):
        """HTMX POST on a ready draft: status flipped to ``analyzing`` via
        the SSOT helper, analyze_impact job enqueued, audit row written,
        HX-Redirect back to the detail page."""
        mock_get_provider.return_value = _stub_provider()
        draft = _make_draft(status="ready")
        mock_fetch.return_value = draft
        conn = MagicMock()
        mock_connect.return_value.__enter__.return_value = conn
        mock_update.return_value = True
        mock_queue = MagicMock()
        mock_queue.enqueue.return_value = 99
        mock_queue_cls.return_value = mock_queue

        client = _authed_client()
        resp = client.post(
            f"/drafts/{draft.id}/reanalyze",
            headers={"HX-Request": "true"},
        )

        assert resp.status_code == 204
        assert resp.headers["HX-Redirect"] == f"/drafts/{draft.id}"

        # update_draft_status called with the analyzing status AND the
        # ``expected_status=prior_status`` optimistic-concurrency guard
        # (PR #826 P1 review fix — without this kwarg, two concurrent
        # POSTs both flip ``ready`` -> ``analyzing`` and both enqueue).
        mock_update.assert_called_once()
        update_args = mock_update.call_args.args
        assert update_args[0] is conn
        assert update_args[1] == draft.id
        assert update_args[2] == "analyzing"
        assert mock_update.call_args.kwargs["expected_status"] == "ready"

        # Stale queue entries purged in the same transaction.
        delete_calls = [
            c for c in conn.execute.call_args_list if "delete from background_jobs" in c.args[0]
        ]
        assert len(delete_calls) == 1
        assert delete_calls[0].args[1] == (str(draft.id),)
        conn.commit.assert_called_once()

        # analyze_impact enqueued with the draft id.
        mock_queue.enqueue.assert_called_once()
        enq_args = mock_queue.enqueue.call_args
        assert enq_args.args[0] == "analyze_impact"
        assert enq_args.args[1] == {"draft_id": str(draft.id)}

        # Audit row written with the right shape.
        mock_log.assert_called_once()
        log_args = mock_log.call_args
        # log_draft_reanalyze(user_id, draft_id, **extra)
        assert log_args.args[0] == _USER_ID
        assert log_args.args[1] == draft.id
        assert log_args.kwargs["job_id"] == 99
        assert log_args.kwargs["prior_status"] == "ready"

    @patch("app.docs.routes._lifecycle.log_draft_reanalyze")
    @patch("app.docs.routes._lifecycle.JobQueue")
    @patch("app.docs.routes._lifecycle.update_draft_status")
    @patch("app.docs.routes._lifecycle._connect")
    @patch("app.docs.routes._lifecycle.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_failed_draft_can_reanalyze(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_connect: MagicMock,
        mock_update: MagicMock,
        mock_queue_cls: MagicMock,
        mock_log: MagicMock,
    ):
        """A failed draft is also re-analyzable: the user might want to
        re-run analysis after a transient ontology hiccup without
        going all the way back to the parse stage."""
        mock_get_provider.return_value = _stub_provider()
        draft = _make_draft(status="failed", error_message="Vana viga")
        mock_fetch.return_value = draft
        conn = MagicMock()
        mock_connect.return_value.__enter__.return_value = conn
        mock_update.return_value = True
        mock_queue = MagicMock()
        mock_queue.enqueue.return_value = 42
        mock_queue_cls.return_value = mock_queue

        client = _authed_client()
        resp = client.post(
            f"/drafts/{draft.id}/reanalyze",
            headers={"HX-Request": "true"},
        )

        assert resp.status_code == 204
        mock_queue.enqueue.assert_called_once()
        # Prior status captured in audit log for traceability.
        assert mock_log.call_args.kwargs["prior_status"] == "failed"
        # And the same prior status is the predicate on the conditional
        # UPDATE so a parallel reanalyze on a ``failed`` draft can only
        # succeed once (PR #826 P1).
        assert mock_update.call_args.kwargs["expected_status"] == "failed"

    @patch("app.docs.routes._lifecycle.log_draft_reanalyze")
    @patch("app.docs.routes._lifecycle.JobQueue")
    @patch("app.docs.routes._lifecycle.update_draft_status")
    @patch("app.docs.routes._lifecycle._connect")
    @patch("app.docs.routes._lifecycle.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_non_htmx_returns_303(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_connect: MagicMock,
        mock_update: MagicMock,
        mock_queue_cls: MagicMock,
        mock_log: MagicMock,
    ):
        """No HX-Request header: server returns a plain 303 redirect so
        full-page form submits navigate back to the detail page."""
        mock_get_provider.return_value = _stub_provider()
        draft = _make_draft(status="ready")
        mock_fetch.return_value = draft
        conn = MagicMock()
        mock_connect.return_value.__enter__.return_value = conn
        mock_update.return_value = True
        mock_queue = MagicMock()
        mock_queue.enqueue.return_value = 7
        mock_queue_cls.return_value = mock_queue

        client = _authed_client()
        resp = client.post(f"/drafts/{draft.id}/reanalyze")

        assert resp.status_code == 303
        assert resp.headers["location"] == f"/drafts/{draft.id}"
        mock_queue.enqueue.assert_called_once()


# ---------------------------------------------------------------------------
# State-machine guards
# ---------------------------------------------------------------------------


class TestReanalyzeStateMachine:
    """The handler refuses to re-enqueue when the pipeline is already
    in-flight or in a non-terminal state."""

    @patch("app.docs.routes._lifecycle.JobQueue")
    @patch("app.docs.routes._lifecycle.update_draft_status")
    @patch("app.docs.routes._lifecycle.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_analyzing_draft_is_noop(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_update: MagicMock,
        mock_queue_cls: MagicMock,
    ):
        """Posting against a draft already in ``analyzing`` must NOT
        re-enqueue — that would double-fire the analyze job."""
        mock_get_provider.return_value = _stub_provider()
        draft = _make_draft(status="analyzing")
        mock_fetch.return_value = draft
        mock_queue = MagicMock()
        mock_queue_cls.return_value = mock_queue

        client = _authed_client()
        resp = client.post(f"/drafts/{draft.id}/reanalyze")

        assert resp.status_code == 303
        assert resp.headers["location"] == f"/drafts/{draft.id}"
        mock_update.assert_not_called()
        mock_queue.enqueue.assert_not_called()

    @patch("app.docs.routes._lifecycle.JobQueue")
    @patch("app.docs.routes._lifecycle.update_draft_status")
    @patch("app.docs.routes._lifecycle.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_parsing_draft_is_noop(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_update: MagicMock,
        mock_queue_cls: MagicMock,
    ):
        """Same guard applies to earlier pipeline stages — the natural
        flow will reach analyzing on its own; manual re-enqueue here is
        a no-op."""
        mock_get_provider.return_value = _stub_provider()
        draft = _make_draft(status="parsing")
        mock_fetch.return_value = draft
        mock_queue = MagicMock()
        mock_queue_cls.return_value = mock_queue

        client = _authed_client()
        resp = client.post(
            f"/drafts/{draft.id}/reanalyze",
            headers={"HX-Request": "true"},
        )

        # HTMX still gets a redirect, just via HX-Redirect.
        assert resp.status_code == 204
        assert resp.headers["HX-Redirect"] == f"/drafts/{draft.id}"
        mock_update.assert_not_called()
        mock_queue.enqueue.assert_not_called()

    @patch("app.docs.routes._lifecycle.log_draft_reanalyze")
    @patch("app.docs.routes._lifecycle.JobQueue")
    @patch("app.docs.routes._lifecycle.update_draft_status")
    @patch("app.docs.routes._lifecycle._connect")
    @patch("app.docs.routes._lifecycle.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_optimistic_race_returns_redirect(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_connect: MagicMock,
        mock_update: MagicMock,
        mock_queue_cls: MagicMock,
        mock_log: MagicMock,
    ):
        """``update_draft_status`` matching 0 rows means a parallel writer
        won — bounce back to the detail page without enqueueing, without
        purging the winner's queue entries, and without writing an audit
        row.

        Pinned by PR #826 P1 review fix: without ``expected_status``
        passed to the SSOT helper the underlying UPDATE has no row-level
        guard and two concurrent POSTs would both flip ``ready`` ->
        ``analyzing`` idempotently and both would land here as winners.
        """
        mock_get_provider.return_value = _stub_provider()
        draft = _make_draft(status="ready")
        mock_fetch.return_value = draft
        conn = MagicMock()
        mock_connect.return_value.__enter__.return_value = conn
        mock_update.return_value = False
        mock_queue = MagicMock()
        mock_queue_cls.return_value = mock_queue

        client = _authed_client()
        resp = client.post(f"/drafts/{draft.id}/reanalyze")

        assert resp.status_code == 303
        assert resp.headers["location"] == f"/drafts/{draft.id}"
        # The handler MUST have asked for the optimistic guard.
        assert mock_update.call_args.kwargs["expected_status"] == "ready"
        # Loser-of-the-race side effects: no enqueue, no purge, no audit.
        mock_queue.enqueue.assert_not_called()
        delete_calls = [
            c for c in conn.execute.call_args_list if "delete from background_jobs" in c.args[0]
        ]
        assert delete_calls == [], "loser of the race must NOT purge the winner's queue entries"
        mock_log.assert_not_called()

    def test_concurrent_writers_only_one_enqueues(self):
        """End-to-end simulation: two POSTs against the same ``ready``
        draft arrive concurrently. The conditional UPDATE on
        ``app.docs.status.update_draft_status`` is the choke point: the
        first writer's ``UPDATE drafts SET status='analyzing' WHERE id=?
        AND status='ready'`` matches one row; the second sees zero rows
        because the row is now ``analyzing``. Only the winner enqueues
        ``analyze_impact`` and only the winner purges the queue.

        We simulate this by driving two sequential handler invocations
        against a single shared fake DB whose draft row tracks its own
        status. The second invocation models the second tab — it reads
        the SAME ``ready`` draft (the read happens BEFORE the first
        writer commits in the real race) and then loses on the
        conditional UPDATE.
        """
        from unittest.mock import patch as _patch

        # Shared row state — mutated by the first writer, observed by
        # the second. ``fetch_draft`` always returns ``ready`` to mimic
        # the race window where both reads happen before either write.
        row_status = {"value": "ready"}

        def fake_update(conn, draft_id, status, **kwargs):
            expected = kwargs.get("expected_status")
            if expected is not None and row_status["value"] != expected:
                # The conditional WHERE matched zero rows.
                return False
            row_status["value"] = status
            return True

        draft = _make_draft(status="ready")

        with (
            _patch("app.auth.middleware._get_provider", return_value=_stub_provider()),
            _patch("app.docs.routes._lifecycle.fetch_draft", return_value=draft),
            _patch("app.docs.routes._lifecycle._connect") as mock_connect,
            _patch(
                "app.docs.routes._lifecycle.update_draft_status",
                side_effect=fake_update,
            ),
            _patch("app.docs.routes._lifecycle.JobQueue") as mock_queue_cls,
            _patch("app.docs.routes._lifecycle.log_draft_reanalyze") as mock_log,
        ):
            mock_connect.return_value.__enter__.return_value = MagicMock()
            mock_queue = MagicMock()
            mock_queue.enqueue.return_value = 1234
            mock_queue_cls.return_value = mock_queue

            client = _authed_client()
            first = client.post(f"/drafts/{draft.id}/reanalyze")
            second = client.post(f"/drafts/{draft.id}/reanalyze")

        # Both responses are user-visible redirects; the user can't
        # tell which one won, and both UIs will reload the detail page.
        assert first.status_code == 303
        assert second.status_code == 303

        # The first POST flipped ``ready`` -> ``analyzing`` (winner).
        # The second POST's UPDATE matched zero rows so its handler
        # short-circuited: NO duplicate enqueue, NO duplicate audit row.
        assert mock_queue.enqueue.call_count == 1
        assert mock_log.call_count == 1
        assert row_status["value"] == "analyzing"


# ---------------------------------------------------------------------------
# Auth + org scoping
# ---------------------------------------------------------------------------


class TestReanalyzeAuth:
    """Cross-org request → 404; anonymous → 303 login redirect."""

    @patch("app.docs.routes._lifecycle.JobQueue")
    @patch("app.docs.routes._lifecycle.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_other_org_returns_not_found(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_queue_cls: MagicMock,
    ):
        """A user in org A POSTing against a draft in org B sees a 404
        (NOT a 403) so we never leak the draft's existence."""
        mock_get_provider.return_value = _stub_provider()
        foreign = _make_draft(org_id=_OTHER_ORG_ID, status="ready")
        mock_fetch.return_value = foreign
        mock_queue = MagicMock()
        mock_queue_cls.return_value = mock_queue

        client = _authed_client()
        resp = client.post(f"/drafts/{foreign.id}/reanalyze")

        assert resp.status_code == 404
        assert "Eelnõu ei leitud" in resp.text
        mock_queue.enqueue.assert_not_called()

    def test_anonymous_redirects_to_login(self):
        from app.main import app

        client = TestClient(app, follow_redirects=False)
        resp = client.post(f"/drafts/{_DRAFT_ID}/reanalyze")
        assert resp.status_code == 303
        assert resp.headers["location"] == "/auth/login"

    @patch("app.docs.routes._lifecycle.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_missing_draft_returns_not_found(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
    ):
        """A draft id that doesn't resolve (deleted between page render
        and POST) returns the standard 404 page."""
        mock_get_provider.return_value = _stub_provider()
        mock_fetch.return_value = None

        client = _authed_client()
        resp = client.post(f"/drafts/{_DRAFT_ID}/reanalyze")

        assert resp.status_code == 404
        assert "Eelnõu ei leitud" in resp.text

    @patch("app.docs.routes._lifecycle.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_malformed_draft_id_returns_not_found(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
    ):
        """Non-UUID path param resolves to 404 without ever touching the
        DB (the parsed-uuid helper rejects it first)."""
        mock_get_provider.return_value = _stub_provider()

        client = _authed_client()
        resp = client.post("/drafts/not-a-uuid/reanalyze")

        assert resp.status_code == 404
        mock_fetch.assert_not_called()


# ---------------------------------------------------------------------------
# Button rendering on the detail page
# ---------------------------------------------------------------------------


class TestReanalyzeButtonInDetailPage:
    """The 'Analüüsi uuesti' button must appear on ready/failed drafts
    and stay hidden during the pipeline."""

    @patch("app.docs.routes._detail.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_ready_draft_renders_reanalyze_button(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
    ):
        """Ready drafts surface the button so the user can rerun
        analysis without going through the report page. The button is
        wired to the shared ConfirmModal primitive (per #601) so no
        ``hx-confirm`` artefacts leak into the page."""
        mock_get_provider.return_value = _stub_provider()
        draft = _make_draft(status="ready")
        mock_fetch.return_value = draft

        client = _authed_client()
        resp = client.get(f"/drafts/{draft.id}")

        assert resp.status_code == 200
        body = resp.text
        assert "Analüüsi uuesti" in body
        # Hidden form posts to the endpoint.
        assert f'hx-post="/drafts/{draft.id}/reanalyze"' in body
        assert f'action="/drafts/{draft.id}/reanalyze"' in body
        # Modal trigger + dialog mounted.
        assert 'id="reanalyze-draft-trigger"' in body
        assert 'id="reanalyze-draft-modal"' in body
        # Inline ``hx-confirm`` MUST NOT leak in — confirm flows go
        # through the Modal primitive (#601 regression guard).
        # ``hx-confirm=`` is only blocked on the *reanalyze* form here;
        # other forms remain free to use it. We assert it is absent on
        # the reanalyze form by checking the page doesn't contain the
        # specific Estonian prompt the plan originally proposed.
        assert "Käivita analüüs uuesti?" not in body
        # The ConfirmModal renders the Estonian copy inside the dialog.
        assert "Käivita analüüs uuesti" in body

    @patch("app.docs.routes._detail.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_failed_draft_renders_reanalyze_button(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
    ):
        """Failed drafts also surface the button alongside the retry
        button — the user can choose to re-run *just* the analyze stage
        instead of restarting parse from scratch."""
        mock_get_provider.return_value = _stub_provider()
        draft = _make_draft(status="failed", error_message="midagi läks valesti")
        mock_fetch.return_value = draft

        client = _authed_client()
        resp = client.get(f"/drafts/{draft.id}")

        assert resp.status_code == 200
        assert "Analüüsi uuesti" in resp.text
        assert f"/drafts/{draft.id}/reanalyze" in resp.text

    @patch("app.docs.routes._detail.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_in_flight_draft_hides_reanalyze_button(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
    ):
        """Mid-pipeline statuses must NOT render the button — that
        avoids the user race that the server-side guard already
        defends against."""
        mock_get_provider.return_value = _stub_provider()
        draft = _make_draft(status="analyzing")
        mock_fetch.return_value = draft

        client = _authed_client()
        resp = client.get(f"/drafts/{draft.id}")

        assert resp.status_code == 200
        body = resp.text
        # The URL is the discriminating signal — the label could
        # legitimately appear in other UI copy.
        assert f"/drafts/{draft.id}/reanalyze" not in body
        assert 'id="reanalyze-draft-modal"' not in body
