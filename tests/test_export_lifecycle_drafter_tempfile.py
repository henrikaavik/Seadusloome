"""#845 (B4a) — drafter export is response-scoped, not persisted.

``GET /drafter/{id}/export`` used to render the politically sensitive
draft as ``EXPORT_DIR/drafter-<session_id>.docx`` and leave it there
forever (one such file was found *tracked in git*). The route now
renders into a 0600 temp file and removes it via a Starlette
``BackgroundTask`` right after the response is delivered — on the
error path the ``except`` block removes it immediately.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from starlette.testclient import TestClient

from app.drafter.session_model import DraftingSession

_ORG_ID = "11111111-1111-1111-1111-111111111111"
_USER_ID = "33333333-3333-3333-3333-333333333333"
_SESSION_ID = uuid.UUID("99999999-9999-9999-9999-999999999999")

_STRUCTURE = {
    "title": "Test seadus",
    "chapters": [
        {
            "number": "1",
            "title": "Üldsätted",
            "sections": [{"paragraph": "§ 1", "title": "Reguleerimisala"}],
        }
    ],
}
_CLAUSES_JSON = json.dumps(
    {
        "clauses": [
            {
                "chapter": "1",
                "paragraph": "§ 1",
                "text": "Seaduse tekst.",
                "citations": [],
                "notes": "",
            }
        ]
    }
)


def _authed_user() -> dict[str, Any]:
    return {
        "id": _USER_ID,
        "email": "koostaja@seadusloome.ee",
        "full_name": "Test Koostaja",
        "role": "drafter",
        "org_id": _ORG_ID,
    }


def _make_session() -> DraftingSession:
    now = datetime.now(UTC)
    return DraftingSession(
        id=_SESSION_ID,
        user_id=uuid.UUID(_USER_ID),
        org_id=uuid.UUID(_ORG_ID),
        workflow_type="full_law",
        current_step=7,
        intent="Testi eksporti",
        clarifications=[],
        research_data_encrypted=None,
        proposed_structure=_STRUCTURE,
        draft_content_encrypted=b"encrypted",
        integrated_draft_id=None,
        status="active",
        created_at=now,
        updated_at=now,
    )


def _stub_provider() -> MagicMock:
    provider = MagicMock()
    provider.get_current_user.return_value = _authed_user()
    return provider


def _client(*, raise_server_exceptions: bool = True) -> TestClient:
    client = TestClient(
        __import__("app.main", fromlist=["app"]).app,
        follow_redirects=False,
        raise_server_exceptions=raise_server_exceptions,
    )
    client.cookies.set("access_token", "stub-token")
    return client


def _mock_connect(mock_connect: MagicMock) -> None:
    conn = MagicMock()
    mock_connect.return_value.__enter__ = MagicMock(return_value=conn)
    mock_connect.return_value.__exit__ = MagicMock(return_value=False)


class TestDrafterExportTempLifecycle:
    @patch("app.drafter.routes.update_session")
    @patch("app.drafter.routes.log_drafter_export")
    @patch("app.drafter.routes._connect")
    @patch("app.drafter.routes.decrypt_text", return_value=_CLAUSES_JSON)
    @patch("app.drafter.routes.fetch_session")
    @patch("app.auth.middleware._get_provider")
    def test_export_serves_docx_then_removes_temp_file(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_decrypt: MagicMock,
        mock_connect: MagicMock,
        mock_log: MagicMock,
        mock_update: MagicMock,
        tmp_path: Path,
        monkeypatch: Any,
    ):
        # Point EXPORT_DIR somewhere observable: the route must NOT use it.
        monkeypatch.setenv("EXPORT_DIR", str(tmp_path))
        mock_get_provider.return_value = _stub_provider()
        mock_fetch.return_value = _make_session()

        from app.drafter import docx_builder

        real_builder = docx_builder.build_drafter_docx
        produced: dict[str, Path] = {}

        def _spy(*args: Any, **kwargs: Any) -> Path:
            out = real_builder(*args, **kwargs)
            produced["path"] = out
            return out

        with patch("app.drafter.docx_builder.build_drafter_docx", side_effect=_spy):
            resp = _client().get(f"/drafter/{_SESSION_ID}/export")

        assert resp.status_code == 200
        assert "application/vnd.openxmlformats" in resp.headers.get("content-type", "")
        assert resp.content.startswith(b"PK"), "response is not a .docx zip"

        # The artifact was rendered into a response-scoped temp file...
        out_path = produced["path"]
        assert out_path.suffix == ".docx"
        # ...outside EXPORT_DIR (no resurrectable copy keyed by session id)...
        assert tmp_path not in out_path.parents
        assert list(tmp_path.iterdir()) == []
        # ...and the BackgroundTask removed it after the response body
        # was delivered (TestClient runs background tasks synchronously).
        assert not out_path.exists()

    @patch("app.drafter.routes.update_session")
    @patch("app.drafter.routes.log_drafter_export")
    @patch("app.drafter.routes._connect")
    @patch("app.drafter.routes.decrypt_text", return_value=_CLAUSES_JSON)
    @patch("app.drafter.routes.fetch_session")
    @patch("app.auth.middleware._get_provider")
    def test_render_failure_removes_temp_file_immediately(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_decrypt: MagicMock,
        mock_connect: MagicMock,
        mock_log: MagicMock,
        mock_update: MagicMock,
    ):
        mock_get_provider.return_value = _stub_provider()
        mock_fetch.return_value = _make_session()

        captured: dict[str, Path] = {}

        def _boom(*args: Any, **kwargs: Any) -> Path:
            captured["path"] = kwargs["out_path"]
            raise RuntimeError("render boom")

        with patch("app.drafter.docx_builder.build_drafter_docx", side_effect=_boom):
            resp = _client(raise_server_exceptions=False).get(f"/drafter/{_SESSION_ID}/export")

        assert resp.status_code == 500
        # The handler pre-created the temp file, so the except path must
        # have removed it — no orphaned 0-byte sensitive-named files.
        assert "path" in captured
        assert not captured["path"].exists()
        # Nothing was logged as a successful export.
        mock_log.assert_not_called()

    @patch("app.drafter.routes.update_session")
    @patch("app.drafter.routes.log_drafter_export")
    @patch("app.drafter.routes._connect")
    @patch("app.drafter.routes.decrypt_text", return_value=_CLAUSES_JSON)
    @patch("app.drafter.routes.fetch_session")
    @patch("app.auth.middleware._get_provider")
    def test_export_passes_response_scoped_out_path_to_builder(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_decrypt: MagicMock,
        mock_connect: MagicMock,
        mock_log: MagicMock,
        mock_update: MagicMock,
    ):
        """The route must pin the output location explicitly (out_path=…)
        rather than letting the builder fall back to EXPORT_DIR."""
        mock_get_provider.return_value = _stub_provider()
        mock_fetch.return_value = _make_session()

        from app.drafter import docx_builder

        real_builder = docx_builder.build_drafter_docx
        seen_kwargs: dict[str, Any] = {}

        def _spy(*args: Any, **kwargs: Any) -> Path:
            seen_kwargs.update(kwargs)
            return real_builder(*args, **kwargs)

        with patch("app.drafter.docx_builder.build_drafter_docx", side_effect=_spy):
            resp = _client().get(f"/drafter/{_SESSION_ID}/export")

        assert resp.status_code == 200
        assert isinstance(seen_kwargs.get("out_path"), Path)
        assert str(_SESSION_ID) in seen_kwargs["out_path"].name
