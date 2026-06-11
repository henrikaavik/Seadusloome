"""#852 E3 — ``submit_review`` claims the integrated review atomically.

Two concurrent POSTs to ``/drafter/{id}/step/6`` used to both observe
``integrated_draft_id IS NULL`` on a stale session snapshot and both run
``_trigger_integrated_review`` — duplicate draft rows, named graphs and
parse jobs. The route now serialises submitters with a per-session
advisory transaction lock, re-checks under the lock, and claims with a
conditional ``UPDATE ... WHERE integrated_draft_id IS NULL``; repeats
idempotently render the existing state.

Also covers the residual from #867: ``_trigger_integrated_review`` must
route its plaintext .docx through a request-scoped temp file (deleted in
``finally``) instead of leaving ``EXPORT_DIR/drafter-<session_id>.docx``
behind forever.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from app.drafter.session_model import DraftingSession

_ORG_ID = "11111111-1111-1111-1111-111111111111"
_USER_ID = "33333333-3333-3333-3333-333333333333"
_SESSION_ID = uuid.UUID("44444444-4444-4444-4444-444444444444")
_DRAFT_ID = uuid.UUID("55555555-5555-5555-5555-555555555555")


def _authed_user() -> dict[str, Any]:
    return {
        "id": _USER_ID,
        "email": "koostaja@seadusloome.ee",
        "full_name": "Test Koostaja",
        "role": "drafter",
        "org_id": _ORG_ID,
    }


def _make_session(*, integrated_draft_id: uuid.UUID | None = None) -> DraftingSession:
    now = datetime.now(UTC)
    return DraftingSession(
        id=_SESSION_ID,
        user_id=uuid.UUID(_USER_ID),
        org_id=uuid.UUID(_ORG_ID),
        workflow_type="full_law",
        current_step=6,
        intent="Testi ülevaadet",
        clarifications=[],
        research_data_encrypted=None,
        proposed_structure={"title": "Test", "chapters": []},
        draft_content_encrypted=None,
        integrated_draft_id=integrated_draft_id,
        status="active",
        created_at=now,
        updated_at=now,
    )


def _stub_provider() -> MagicMock:
    provider = MagicMock()
    provider.get_current_user.return_value = _authed_user()
    return provider


def _authed_client():
    from starlette.testclient import TestClient

    client = TestClient(
        __import__("app.main", fromlist=["app"]).app,
        follow_redirects=False,
    )
    client.cookies.set("access_token", "stub-token")
    return client


class _StatefulClaimConn:
    """Fake connection that models the ``integrated_draft_id`` column.

    The advisory lock is a no-op (the TestClient is sequential), but the
    SELECT/UPDATE pair behaves like Postgres: the conditional UPDATE only
    "writes" while the column is NULL, and the under-lock SELECT returns
    whatever the previous request claimed.
    """

    def __init__(self, state: dict):
        self.state = state
        self.executed: list[str] = []

    def execute(self, sql: str, params: tuple | None = None):
        sql_norm = " ".join(str(sql).split())
        self.executed.append(sql_norm)
        cursor = MagicMock()
        if "SELECT integrated_draft_id FROM drafting_sessions" in sql_norm:
            cursor.fetchone.return_value = (self.state.get("claimed"),)
            return cursor
        if "SET integrated_draft_id" in sql_norm and "IS NULL" in sql_norm:
            if self.state.get("claimed") is None:
                self.state["claimed"] = params[0]  # type: ignore[index]
                cursor.rowcount = 1
            else:
                cursor.rowcount = 0
            return cursor
        cursor.fetchone.return_value = None
        return cursor

    def commit(self) -> None:
        pass


class _ConnCM:
    def __init__(self, conn: Any):
        self.conn = conn

    def __enter__(self):
        return self.conn

    def __exit__(self, *_: Any) -> bool:
        return False


# ---------------------------------------------------------------------------
# Idempotent / atomic claim
# ---------------------------------------------------------------------------


class TestSubmitReviewIdempotent:
    @patch("app.drafter.routes._trigger_integrated_review")
    @patch("app.drafter.routes._connect")
    @patch("app.drafter.routes.fetch_session")
    @patch("app.auth.middleware._get_provider")
    def test_double_post_triggers_exactly_once(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_connect: MagicMock,
        mock_trigger: MagicMock,
    ):
        """Worst case: BOTH requests read a stale snapshot with
        ``integrated_draft_id=None``. The under-lock recheck must stop
        the second from creating a duplicate draft/graph/job."""
        mock_get_provider.return_value = _stub_provider()
        # Stale snapshot on every fetch — the pre-#852 race condition.
        mock_fetch.return_value = _make_session(integrated_draft_id=None)
        mock_trigger.return_value = _DRAFT_ID

        state: dict = {"claimed": None}
        mock_connect.side_effect = lambda: _ConnCM(_StatefulClaimConn(state))

        client = _authed_client()
        resp1 = client.post(f"/drafter/{_SESSION_ID}/step/6", data={})
        resp2 = client.post(f"/drafter/{_SESSION_ID}/step/6", data={})

        assert resp1.status_code == 200
        assert resp2.status_code == 200
        # Exactly ONE integrated draft (and hence one graph + one job —
        # both are created inside _trigger_integrated_review).
        mock_trigger.assert_called_once()
        assert state["claimed"] == str(_DRAFT_ID)

    @patch("app.drafter.routes._trigger_integrated_review")
    @patch("app.drafter.routes._connect")
    @patch("app.drafter.routes.fetch_session")
    @patch("app.auth.middleware._get_provider")
    def test_claim_is_serialised_by_advisory_lock(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_connect: MagicMock,
        mock_trigger: MagicMock,
    ):
        mock_get_provider.return_value = _stub_provider()
        mock_fetch.return_value = _make_session(integrated_draft_id=None)
        mock_trigger.return_value = _DRAFT_ID

        state: dict = {"claimed": None}
        conn = _StatefulClaimConn(state)
        mock_connect.side_effect = lambda: _ConnCM(conn)

        client = _authed_client()
        resp = client.post(f"/drafter/{_SESSION_ID}/step/6", data={})

        assert resp.status_code == 200
        joined = " ".join(conn.executed)
        # Lock taken BEFORE the recheck, claim is conditional.
        assert "pg_advisory_xact_lock" in conn.executed[0]
        assert "SELECT integrated_draft_id FROM drafting_sessions" in joined
        assert "integrated_draft_id IS NULL" in joined

    @patch("app.drafter.routes._trigger_integrated_review")
    @patch("app.drafter.routes._connect")
    @patch("app.drafter.routes.fetch_session")
    @patch("app.auth.middleware._get_provider")
    def test_fast_path_skips_lock_when_already_linked(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_connect: MagicMock,
        mock_trigger: MagicMock,
    ):
        mock_get_provider.return_value = _stub_provider()
        mock_fetch.return_value = _make_session(integrated_draft_id=_DRAFT_ID)

        client = _authed_client()
        resp = client.post(f"/drafter/{_SESSION_ID}/step/6", data={})

        assert resp.status_code == 200
        mock_trigger.assert_not_called()
        mock_connect.assert_not_called()

    @patch("app.drafter.routes._trigger_integrated_review")
    @patch("app.drafter.routes._connect")
    @patch("app.drafter.routes.fetch_session")
    @patch("app.auth.middleware._get_provider")
    def test_loser_renders_existing_state_without_triggering(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_connect: MagicMock,
        mock_trigger: MagicMock,
    ):
        """Stale snapshot + already-claimed column (the blocked-on-lock
        loser's view) → idempotent render, no second pipeline."""
        mock_get_provider.return_value = _stub_provider()
        mock_fetch.return_value = _make_session(integrated_draft_id=None)

        state: dict = {"claimed": str(_DRAFT_ID)}
        mock_connect.side_effect = lambda: _ConnCM(_StatefulClaimConn(state))

        client = _authed_client()
        resp = client.post(f"/drafter/{_SESSION_ID}/step/6", data={})

        assert resp.status_code == 200
        mock_trigger.assert_not_called()
        assert state["claimed"] == str(_DRAFT_ID)  # untouched


# ---------------------------------------------------------------------------
# Residual #867 — integrated-review docx must not persist in EXPORT_DIR
# ---------------------------------------------------------------------------


class TestIntegratedReviewTempfile:
    def _run_trigger(self, builder_side_effect=None) -> tuple[uuid.UUID | None, dict]:
        """Drive ``_trigger_integrated_review`` with everything mocked
        except the temp-file lifecycle. Returns (draft_id, captured)."""
        from app.drafter.routes import _trigger_integrated_review

        captured: dict = {}

        def fake_builder(**kwargs):
            out_path = kwargs.get("out_path")
            captured["out_path"] = out_path
            if builder_side_effect is not None:
                raise builder_side_effect
            assert out_path is not None
            Path(out_path).write_bytes(b"PK-fake-docx")
            return Path(out_path)

        stored = MagicMock()
        stored.storage_path = "stored/path.enc"
        draft = MagicMock()
        draft.id = _DRAFT_ID

        conn = MagicMock()

        with (
            patch("app.drafter.docx_builder.build_drafter_docx", side_effect=fake_builder),
            patch("app.storage.store_file", return_value=stored) as mock_store,
            patch("app.docs.draft_model.create_draft", return_value=draft),
            patch("app.drafter.routes._connect") as mock_connect,
            patch("app.drafter.routes.JobQueue") as mock_queue_cls,
        ):
            mock_connect.return_value.__enter__ = MagicMock(return_value=conn)
            mock_connect.return_value.__exit__ = MagicMock(return_value=False)
            session = _make_session()
            result = _trigger_integrated_review(session, _authed_user())  # type: ignore[arg-type]
            captured["store_calls"] = mock_store.call_args_list
            captured["enqueue_calls"] = mock_queue_cls.return_value.enqueue.call_args_list
        return result, captured

    def test_docx_goes_through_temp_file_and_is_removed(self):
        result, captured = self._run_trigger()

        assert result == _DRAFT_ID
        out_path = captured["out_path"]
        assert out_path is not None, "builder must receive an explicit out_path"
        # Request-scoped temp file, not the EXPORT_DIR default name.
        assert Path(out_path).name.startswith("seadusloome-integreeritud-")
        assert not Path(out_path).exists(), "plaintext temp docx must be removed"
        # The encrypted copy got the bytes the builder wrote.
        assert captured["store_calls"][0].args[0] == b"PK-fake-docx"
        # The pipeline job was still enqueued.
        assert captured["enqueue_calls"][0].args[0] == "parse_draft"

    def test_temp_file_removed_when_builder_raises(self):
        with pytest.raises(RuntimeError, match="render boom"):
            self._run_trigger(builder_side_effect=RuntimeError("render boom"))

        # The captured path was created by NamedTemporaryFile before the
        # builder ran; the finally must have removed it.
        # (captured dict is not returned on raise, so re-check via scan:
        # nothing matching the prefix may remain in the temp dir.)
        import tempfile

        leftovers = list(
            Path(tempfile.gettempdir()).glob(f"seadusloome-integreeritud-{_SESSION_ID}-*")
        )
        assert leftovers == []
