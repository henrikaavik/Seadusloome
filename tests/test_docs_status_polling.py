"""DoD coverage for the HTMX status-polling fallback (#347).

The status-tracker fragment endpoint and the WebSocket push path were
landed by earlier issues (#608, #470, #600, #607, #625). This module
pins the contract that is specifically called out by #347 so a future
refactor can't silently regress the polling fallback when the WS path
is unavailable:

    1. ``GET /drafts/{id}/status`` returns the tracker fragment with
       ``hx-trigger="every Ns"`` while the status is non-terminal.
    2. On ``ready`` / ``failed`` the fragment is returned WITHOUT any
       ``hx-trigger`` polling attribute — HTMX therefore stops polling
       on its own without a custom JS hook.
    3. Cross-org access returns the same "not found" placeholder as a
       missing draft (no existence leak).
    4. Anonymous callers get a 303 redirect to the login page.
    5. The "spinner" — defined here as the live elapsed-time ticker
       (``.draft-stage-elapsed`` + the window-level
       ``__draftElapsedTimer`` setInterval, see ``_status_tracker.py``)
       — is rendered while polling and absent on terminal states (no
       counter that keeps incrementing forever on a finished pipeline).

       Note on ``.draft-stage-active``: this class is NOT the spinner.
       It's a static CSS color highlight (primary tint, see
       ``ui.css`` — no ``@keyframes``, no ``animation`` property) that
       marks the current stage row. On ``ready`` it legitimately
       remains on the last ("Valmis") stage so the user sees a coloured
       checkmark-style final row instead of a fully-grey tracker; on
       ``failed`` it is dropped because every stage falls through to
       ``draft-stage-idle``. The live ticker is what tells the user
       "still working" — and that is what the terminal-state
       assertions below pin to absent.

These are intentionally narrow assertions: detailed behaviour (poll
back-off, stale-pipeline alert, HX-Trigger draft-ready) is covered by
``tests/test_docs_routes.py::TestDraftStatusFragment`` and
``tests/test_docs_routes.py::TestDraftReadyTrigger``. The point of
this file is the polling-fallback DoD: it stops on terminal, the live
ticker stops while progressing remains animated, it 404-style refuses
cross-org reads, and it never serves anonymous traffic.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from starlette.testclient import TestClient

from app.docs.draft_model import Draft
from app.docs.status import TERMINAL_STATUSES

_ORG_ID = "11111111-1111-1111-1111-111111111111"
_OTHER_ORG_ID = "22222222-2222-2222-2222-222222222222"
_USER_ID = "33333333-3333-3333-3333-333333333333"
_DRAFT_ID = uuid.UUID("44444444-4444-4444-4444-444444444444")


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


def _authed_client() -> TestClient:
    from app.main import app

    client = TestClient(app, follow_redirects=False)
    client.cookies.set("access_token", "stub-token")
    return client


def _make_draft(
    *,
    status: str = "extracting",
    org_id: str = _ORG_ID,
    error_message: str | None = None,
) -> Draft:
    """Build a draft whose timestamps stay inside the polling budget.

    The status-tracker fragment drops polling attributes once the
    draft's ``updated_at`` is older than the polling timeout
    (``_is_status_polling_stale`` — see ``_shared.py``). Pinning
    ``updated_at`` to "5 seconds ago" guarantees that the polling
    attributes appear in the rendered fragment for every status under
    test except the terminal ones.
    """
    now = datetime.now(UTC)
    return Draft(
        id=_DRAFT_ID,
        user_id=uuid.UUID(_USER_ID),
        org_id=uuid.UUID(org_id),
        title="Test eelnõu",
        filename="eelnou.docx",
        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        file_size=2048,
        storage_path="/tmp/ciphertext.enc",
        graph_uri=f"https://data.riik.ee/ontology/estleg/drafts/{_DRAFT_ID}",
        status=status,
        parsed_text_encrypted=None,
        entity_count=None,
        error_message=error_message,
        created_at=now - timedelta(seconds=10),
        updated_at=now - timedelta(seconds=5),
        doc_type="eelnou",  # type: ignore[arg-type]
        parent_vtk_id=None,
        processing_completed_at=None,
    )


# ---------------------------------------------------------------------------
# #347 DoD: poll every 3s while progressing
# ---------------------------------------------------------------------------


class TestPollingWhileProgressing:
    """While ``status`` is non-terminal the fragment must carry the
    HTMX polling attributes so the page keeps refreshing the tracker
    every Ns even if the WebSocket is unreachable.
    """

    @patch("app.docs.routes._detail.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_fresh_draft_polls_every_3s(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
    ) -> None:
        """A draft <30s old must poll on the 3s cadence the DoD specifies."""
        mock_get_provider.return_value = _stub_provider()
        mock_fetch.return_value = _make_draft(status="parsing")

        client = _authed_client()
        resp = client.get(
            f"/drafts/{_DRAFT_ID}/status",
            headers={"HX-Request": "true"},
        )

        assert resp.status_code == 200
        # The DoD's "poll every 3s" cadence. The endpoint URL is itself
        # the polling target.
        assert 'hx-trigger="every 3s"' in resp.text
        assert f"/drafts/{_DRAFT_ID}/status" in resp.text
        # The wrapper element is what HTMX swaps with outerHTML, so the
        # next poll cycle reuses the same id.
        assert f'id="draft-status-{_DRAFT_ID}"' in resp.text
        assert 'hx-swap="outerHTML"' in resp.text

    @pytest.mark.parametrize("status", ["uploaded", "parsing", "extracting", "analyzing"])
    @patch("app.docs.routes._detail.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_every_non_terminal_status_keeps_polling(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        status: str,
    ) -> None:
        """Every non-terminal pipeline stage must keep the hx-trigger."""
        mock_get_provider.return_value = _stub_provider()
        mock_fetch.return_value = _make_draft(status=status)

        client = _authed_client()
        resp = client.get(
            f"/drafts/{_DRAFT_ID}/status",
            headers={"HX-Request": "true"},
        )

        assert resp.status_code == 200
        # The exact cadence depends on the back-off window
        # (3s / 6s / 10s) — what matters for the DoD is that *some*
        # ``every Ns`` trigger is present so polling never stops mid-run.
        assert 'hx-trigger="every ' in resp.text


# ---------------------------------------------------------------------------
# #347 DoD: stop polling on terminal status
# ---------------------------------------------------------------------------


class TestPollingStopsOnTerminal:
    """Terminal states (``ready``, ``failed``) must drop the
    hx-trigger so HTMX stops polling without any custom JS. The
    fragment is still served — the swap that landed it on the page
    is the final swap.
    """

    @pytest.mark.parametrize("status", ["ready", "failed"])
    @patch("app.docs.routes._detail.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_terminal_status_omits_polling_attrs(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        status: str,
    ) -> None:
        mock_get_provider.return_value = _stub_provider()
        # ``failed`` needs an error message for the alert path; the
        # branch otherwise renders an empty banner.
        err = "Boom" if status == "failed" else None
        mock_fetch.return_value = _make_draft(status=status, error_message=err)

        client = _authed_client()
        resp = client.get(
            f"/drafts/{_DRAFT_ID}/status",
            headers={"HX-Request": "true"},
        )

        assert resp.status_code == 200
        # The wrapper Div is still rendered — the HTMX swap that
        # *landed* this response replaced the previous tracker — but
        # without any hx-trigger attribute. HTMX therefore has no
        # schedule to fire and polling stops naturally.
        assert f'id="draft-status-{_DRAFT_ID}"' in resp.text
        assert "hx-trigger" not in resp.text
        # And no leftover hx-get/hx-swap on the wrapper either.
        # (The retry button on ``failed`` carries its own hx-post; the
        # absence we're asserting is the periodic ``every Ns`` trigger.)
        assert "every " not in resp.text

    def test_terminal_statuses_set_covers_ready_and_failed(self) -> None:
        """Guard rail: if a future refactor adds a third terminal state
        (e.g. ``cancelled``) the parametrised test above must be
        extended too. This assertion fails loudly on that boundary
        change instead of silently leaving the new terminal polling
        forever.
        """
        assert TERMINAL_STATUSES == {"ready", "failed"}


# ---------------------------------------------------------------------------
# #347 DoD: spinner is present while polling, absent on terminal
# ---------------------------------------------------------------------------


class TestSpinnerVisibility:
    """The "still working" spinner is the live elapsed-time ticker
    rendered under the active stage (``.draft-stage-elapsed`` plus the
    inline ``setInterval`` script that bumps it once per second — see
    ``_status_tracker.py``). Functionally it IS the spinner: it tells
    the user the pipeline is still ticking. On terminal states the
    ticker must be gone so the user sees a static, finished tracker
    instead of a counter that keeps incrementing forever.
    """

    @patch("app.docs.routes._detail.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_spinner_present_while_polling(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
    ) -> None:
        mock_get_provider.return_value = _stub_provider()
        mock_fetch.return_value = _make_draft(status="extracting")

        client = _authed_client()
        resp = client.get(
            f"/drafts/{_DRAFT_ID}/status",
            headers={"HX-Request": "true"},
        )

        assert resp.status_code == 200
        # The active stage carries the ``draft-stage-active`` class
        # (the CSS rule that highlights the running stage hooks off
        # this class). The live ticker span underneath it is what
        # the user reads as "still working — Nm:Ss möödas".
        assert "draft-stage-active" in resp.text
        assert "draft-stage-elapsed" in resp.text
        # The window-level setInterval tick script ships with the
        # fragment so the counter increments client-side without
        # extra HTMX polls.
        assert "__draftElapsedTimer" in resp.text

    @pytest.mark.parametrize(
        ("status", "error_message"),
        [
            ("ready", None),
            ("failed", "Töötlemine ebaõnnestus"),
        ],
    )
    @patch("app.docs.routes._detail.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_spinner_absent_on_terminal(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        status: str,
        error_message: str | None,
    ) -> None:
        """On terminal status no live ticker is rendered.

        ``ready`` substitutes a frozen ``.draft-stage-done-label`` span
        ("Analüüsitud N min") for the live ``.draft-stage-elapsed``
        ticker, and ``failed`` renders no ticker at all because no
        stage is marked active. Either way the tick script does not
        ship — the JS would clear an existing interval anyway, but
        skipping it on terminal swaps avoids a final useless tick.
        """
        mock_get_provider.return_value = _stub_provider()
        mock_fetch.return_value = _make_draft(
            status=status,
            error_message=error_message,
        )

        client = _authed_client()
        resp = client.get(
            f"/drafts/{_DRAFT_ID}/status",
            headers={"HX-Request": "true"},
        )

        assert resp.status_code == 200
        # The live-ticker span must be gone; ``ready`` swaps in the
        # frozen done-label and ``failed`` skips the active-stage
        # branch entirely.
        assert "draft-stage-elapsed" not in resp.text
        # And the setInterval bootstrap must not be re-attached. Its
        # presence relies on at least one ``.draft-stage-elapsed`` in
        # the children (see _status_tracker.py).
        assert "__draftElapsedTimer" not in resp.text

    @patch("app.docs.routes._detail.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_active_class_remains_on_ready_final_stage(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
    ) -> None:
        """``.draft-stage-active`` is a static color highlight, NOT the
        spinner. On ``ready`` it must stay on the final ("Valmis")
        stage row so the tracker shows a coloured final stage instead
        of an all-grey tracker. The live ticker (the real spinner) is
        already pinned absent by ``test_spinner_absent_on_terminal``.

        Pinning this so a future "drop active on terminal" refactor
        can't silently strip the final-row highlight without an
        explicit DoD review.
        """
        mock_get_provider.return_value = _stub_provider()
        mock_fetch.return_value = _make_draft(status="ready")

        client = _authed_client()
        resp = client.get(
            f"/drafts/{_DRAFT_ID}/status",
            headers={"HX-Request": "true"},
        )

        assert resp.status_code == 200
        # The class is still on the "Valmis" row — see the
        # ``elif is_active and draft.status == "ready"`` branch in
        # ``_status_tracker.py``: it renders the frozen done-label
        # span under a stage that is still marked ``draft-stage-active``.
        assert "draft-stage-active" in resp.text
        # Sanity: the frozen "Analüüsitud" label that goes with the
        # final active row is present.
        assert "draft-stage-done-label" in resp.text

    @patch("app.docs.routes._detail.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_active_class_absent_on_failed(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
    ) -> None:
        """``failed`` drafts dim every stage to ``draft-stage-idle``
        (see the ``if draft.status == "failed"`` branch in
        ``_status_tracker.py``). No row is marked active, so no row
        gets the primary-colour highlight — the user reads the red
        error banner as the dominant state cue instead.
        """
        mock_get_provider.return_value = _stub_provider()
        mock_fetch.return_value = _make_draft(
            status="failed",
            error_message="Töötlemine ebaõnnestus",
        )

        client = _authed_client()
        resp = client.get(
            f"/drafts/{_DRAFT_ID}/status",
            headers={"HX-Request": "true"},
        )

        assert resp.status_code == 200
        assert "draft-stage-active" not in resp.text


# ---------------------------------------------------------------------------
# #347 DoD: graceful degradation — cross-org + anonymous
# ---------------------------------------------------------------------------


class TestGracefulDegradation:
    """The polling endpoint must refuse to leak existence of out-of-
    scope drafts and must not serve anonymous traffic at all. Both
    are part of "gracefully degrade": polling never escalates into a
    way to enumerate drafts or to bypass auth.
    """

    @patch("app.docs.routes._detail.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_cross_org_returns_not_found_placeholder(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
    ) -> None:
        """A draft owned by another org returns the same placeholder a
        non-existent draft returns — no existence leak."""
        mock_get_provider.return_value = _stub_provider()
        mock_fetch.return_value = _make_draft(org_id=_OTHER_ORG_ID)

        client = _authed_client()
        resp = client.get(f"/drafts/{_DRAFT_ID}/status")

        assert resp.status_code == 200
        assert "Eelnõu ei leitud" in resp.text
        # And critically: no polling attributes — the failing fragment
        # would otherwise hammer the endpoint forever.
        assert "hx-trigger" not in resp.text

    @patch("app.docs.routes._detail.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_missing_draft_returns_not_found_placeholder(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
    ) -> None:
        mock_get_provider.return_value = _stub_provider()
        mock_fetch.return_value = None

        client = _authed_client()
        resp = client.get(f"/drafts/{_DRAFT_ID}/status")

        assert resp.status_code == 200
        assert "Eelnõu ei leitud" in resp.text
        assert "hx-trigger" not in resp.text

    def test_anonymous_caller_redirects_to_login(self) -> None:
        """Unauthenticated GETs are 303'd to the login page by the
        global auth Beforeware — same gate as the detail page itself."""
        from app.main import app

        client = TestClient(app, follow_redirects=False)
        resp = client.get(f"/drafts/{_DRAFT_ID}/status")

        assert resp.status_code == 303
        assert resp.headers["location"] == "/auth/login"
