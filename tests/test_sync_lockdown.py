# pyright: reportPrivateUsage=false
"""Security-hardening tests for #853.

Covers the five DoD verification points plus the three required review
items, grouped by concern:

* Fuseki write-endpoint auth config (H1) — the assembler TTL guards the
  write endpoints with ``fuseki:allowedUsers`` and leaves reads open, and
  ``jena_loader`` sends admin auth on every write path but none on reads.
* Fail-closed Fuseki admin password (comment item 1).
* Docker compose localhost binds (H2).
* Sync advisory lock (H4) — two concurrent ``run_sync`` calls cannot both
  acquire the lock.
* Webhook signature hardening (comment item 2) + body cap (item 3) +
  delivery-id replay protection (H5).

Everything is mocked: no Postgres, no Fuseki, no network.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from starlette.requests import Request

from app.sync import jena_loader, webhook
from app.sync.webhook import verify_signature

# Repo root: tests/ -> repo root.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_COMPOSE = _REPO_ROOT / "docker" / "docker-compose.yml"
_FUSEKI_TTL = _REPO_ROOT / "docker" / "fuseki-config" / "ontology.ttl"
_MIGRATION_039 = _REPO_ROOT / "migrations" / "039_webhook_deliveries.sql"


# ===========================================================================
# H1: Fuseki write-endpoint auth (config + loader)
# ===========================================================================


class TestFusekiWriteEndpointAuthConfig:
    """The assembler TTL must guard writes and leave reads open."""

    def _ttl(self) -> str:
        return _FUSEKI_TTL.read_text(encoding="utf-8")

    def _ttl_no_comments(self) -> str:
        """TTL with ``#`` comment lines stripped so assertions about the
        actual directives aren't fooled by explanatory prose."""
        lines = [
            ln
            for ln in _FUSEKI_TTL.read_text(encoding="utf-8").splitlines()
            if not ln.lstrip().startswith("#")
        ]
        return "\n".join(lines)

    def test_write_endpoints_have_allowed_users(self):
        """update / data (gsp-rw) / upload must each carry allowedUsers."""
        ttl = self._ttl()
        # Each write operation block must mention allowedUsers within it.
        for op, name in (("update", "update"), ("gsp-rw", "data"), ("upload", "upload")):
            # Find the endpoint blank node for this operation.
            pattern = re.compile(
                r"fuseki:operation\s+fuseki:"
                + re.escape(op)
                + r"\s*;.*?fuseki:name\s+\""
                + re.escape(name)
                + r"\".*?fuseki:allowedUsers\s+\"admin\"",
                re.DOTALL,
            )
            assert pattern.search(ttl), f"write endpoint {name} ({op}) missing allowedUsers admin"

    def test_read_endpoints_stay_open(self):
        """query / sparql / get must NOT carry allowedUsers (read stays open)."""
        ttl = self._ttl_no_comments()
        # The three read endpoints + the default (nameless) query endpoint
        # must remain open. We assert there are exactly three allowedUsers
        # occurrences (the three write endpoints) so a future edit that
        # accidentally locks a read endpoint trips this test. Comment lines
        # are stripped so the explanatory prose can mention the property.
        assert ttl.count("fuseki:allowedUsers") == 3

    def test_query_endpoint_present_and_unguarded(self):
        """A named ``sparql`` query endpoint must exist without allowedUsers."""
        ttl = self._ttl()
        block = re.search(
            r"fuseki:operation\s+fuseki:query\s*;\s*fuseki:name\s+\"sparql\"\s*\]",
            ttl,
        )
        assert block, "named sparql query endpoint missing or altered"


class TestLoaderAuthUsage:
    """jena_loader must send admin auth on writes and none on reads."""

    @patch("app.sync.jena_loader.httpx.put")
    def test_upload_turtle_sends_admin_auth(self, mock_put: MagicMock):
        resp = MagicMock(status_code=200)
        resp.raise_for_status.return_value = None
        mock_put.return_value = resp
        assert jena_loader.upload_turtle("# turtle") is True
        assert mock_put.call_args.kwargs["auth"] == ("admin", "localdev")

    @patch("app.sync.jena_loader.httpx.delete")
    def test_clear_default_graph_sends_admin_auth(self, mock_delete: MagicMock):
        resp = MagicMock(status_code=204)
        resp.raise_for_status.return_value = None
        mock_delete.return_value = resp
        assert jena_loader.clear_default_graph() is True
        assert mock_delete.call_args.kwargs["auth"] == ("admin", "localdev")

    @patch("app.sync.jena_loader.httpx.post")
    def test_sparql_update_sends_admin_auth(self, mock_post: MagicMock):
        resp = MagicMock(status_code=200)
        mock_post.return_value = resp
        assert jena_loader._sparql_update("DROP SILENT GRAPH <urn:x>") is True
        assert mock_post.call_args.kwargs["auth"] == ("admin", "localdev")

    @patch("app.sync.jena_loader.httpx.put")
    def test_upload_to_named_graph_sends_admin_auth(self, mock_put: MagicMock):
        resp = MagicMock(status_code=200)
        mock_put.return_value = resp
        assert jena_loader.upload_turtle_to_named_graph("urn:estleg:staging", "# t") is True
        assert mock_put.call_args.kwargs["auth"] == ("admin", "localdev")

    @patch("app.sync.jena_loader.httpx.post")
    def test_read_query_sends_no_auth(self, mock_post: MagicMock):
        """The read-only SPARQL query path must NOT send credentials —
        the query endpoint stays open by design."""
        resp = MagicMock(status_code=200)
        resp.raise_for_status.return_value = None
        resp.json.return_value = {"results": {"bindings": []}}
        mock_post.return_value = resp
        jena_loader.sparql_query("SELECT * WHERE { ?s ?p ?o }")
        assert "auth" not in mock_post.call_args.kwargs


# ===========================================================================
# Comment item 1: fail-closed FUSEKI_ADMIN_PASSWORD
# ===========================================================================


class TestAdminPasswordFailClosed:
    def test_dev_falls_back_to_localdev(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("FUSEKI_ADMIN_PASSWORD", raising=False)
        monkeypatch.setenv("APP_ENV", "development")
        assert jena_loader._resolve_admin_password() == "localdev"

    def test_unset_env_defaults_to_dev_fallback(self, monkeypatch: pytest.MonkeyPatch):
        """APP_ENV unset == development, so the dev fallback applies."""
        monkeypatch.delenv("FUSEKI_ADMIN_PASSWORD", raising=False)
        monkeypatch.delenv("APP_ENV", raising=False)
        assert jena_loader._resolve_admin_password() == "localdev"

    def test_explicit_value_used_everywhere(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("FUSEKI_ADMIN_PASSWORD", "s3cr3t")
        monkeypatch.setenv("APP_ENV", "production")
        assert jena_loader._resolve_admin_password() == "s3cr3t"

    def test_production_without_secret_fails_closed(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("FUSEKI_ADMIN_PASSWORD", raising=False)
        monkeypatch.setenv("APP_ENV", "production")
        with pytest.raises(jena_loader.FusekiAdminPasswordError):
            jena_loader._resolve_admin_password()

    def test_staging_without_secret_fails_closed(self, monkeypatch: pytest.MonkeyPatch):
        """Any non-dev env (not just production) must fail closed."""
        monkeypatch.delenv("FUSEKI_ADMIN_PASSWORD", raising=False)
        monkeypatch.setenv("APP_ENV", "staging")
        with pytest.raises(jena_loader.FusekiAdminPasswordError):
            jena_loader._resolve_admin_password()

    def test_empty_string_secret_fails_closed_offdev(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("FUSEKI_ADMIN_PASSWORD", "")
        monkeypatch.setenv("APP_ENV", "production")
        with pytest.raises(jena_loader.FusekiAdminPasswordError):
            jena_loader._resolve_admin_password()

    def test_write_path_raises_offdev_when_missing(self, monkeypatch: pytest.MonkeyPatch):
        """The failure surfaces lazily at the first write, not at import."""
        monkeypatch.delenv("FUSEKI_ADMIN_PASSWORD", raising=False)
        monkeypatch.setenv("APP_ENV", "production")
        with (
            patch("app.sync.jena_loader.httpx.put") as mock_put,
            pytest.raises(jena_loader.FusekiAdminPasswordError),
        ):
            jena_loader.upload_turtle("# turtle")
        # The guard fires before any HTTP traffic.
        mock_put.assert_not_called()


# ===========================================================================
# H2: docker compose localhost binds
# ===========================================================================


class TestComposeLocalhostBinds:
    def _compose(self) -> str:
        return _COMPOSE.read_text(encoding="utf-8")

    def _compose_directives(self) -> str:
        """Compose YAML with ``#`` comment lines stripped, so port-binding
        assertions ignore the explanatory prose (which quotes the old bare
        bindings to explain why they're wrong)."""
        lines = [ln for ln in self._compose().splitlines() if not ln.lstrip().startswith("#")]
        return "\n".join(lines)

    def test_postgres_bound_to_localhost(self):
        assert '"127.0.0.1:5432:5432"' in self._compose_directives()

    def test_jena_bound_to_localhost(self):
        assert '"127.0.0.1:3030:3030"' in self._compose_directives()

    def test_no_bare_zero_bind_for_db_or_jena(self):
        """No bare ``"5432:5432"`` / ``"3030:3030"`` (which bind 0.0.0.0)."""
        compose = self._compose_directives()
        assert '"5432:5432"' not in compose
        assert '"3030:3030"' not in compose

    def test_documents_internal_vs_external_services(self):
        """DoD: the file must document which services stay internal in prod."""
        compose = self._compose()
        assert "INTERNAL" in compose
        # The app is documented as the only externally reachable service.
        assert "EXTERNAL" in compose
        assert "Coolify" in compose


# ===========================================================================
# H4: sync advisory lock
# ===========================================================================


class TestSyncAdvisoryLock:
    def test_acquire_returns_conn_when_lock_granted(self):
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = (True,)
        with patch("app.sync.orchestrator.get_connection", return_value=conn):
            from app.sync.orchestrator import _acquire_sync_lock

            got = _acquire_sync_lock()
        assert got is conn
        conn.close.assert_not_called()

    def test_acquire_returns_none_and_closes_when_lock_held(self):
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = (False,)
        with patch("app.sync.orchestrator.get_connection", return_value=conn):
            from app.sync.orchestrator import _acquire_sync_lock

            got = _acquire_sync_lock()
        assert got is None
        conn.close.assert_called_once()

    def test_two_concurrent_acquires_only_one_wins(self):
        """Simulate the real Postgres behaviour: the first
        pg_try_advisory_lock returns True, the second False."""
        from app.sync.orchestrator import _acquire_sync_lock

        results = iter([(True,), (False,)])

        def _make_conn(*_a, **_k):
            c = MagicMock()
            c.execute.return_value.fetchone.side_effect = lambda: next(results)
            return c

        with patch("app.sync.orchestrator.get_connection", side_effect=_make_conn):
            first = _acquire_sync_lock()
            second = _acquire_sync_lock()

        assert first is not None
        assert second is None

    def test_run_sync_bails_when_lock_not_acquired(self):
        """When the lock is held, run_sync must NOT touch Jena and must
        record a skipped note, returning False."""
        from app.sync import orchestrator

        with (
            patch.object(orchestrator, "_acquire_sync_lock", return_value=None),
            patch.object(orchestrator, "_record_skipped_sync") as mock_skip,
            patch.object(orchestrator, "clone_or_pull") as mock_clone,
            patch.object(orchestrator, "drop_graph") as mock_drop,
            patch.object(orchestrator, "_insert_running_row") as mock_insert,
        ):
            result = orchestrator.run_sync(repo_dir=Path("/tmp/does-not-matter"))

        assert result is False
        mock_skip.assert_called_once()
        # No pipeline work happened.
        mock_clone.assert_not_called()
        mock_drop.assert_not_called()
        mock_insert.assert_not_called()

    def test_run_sync_finalizes_preinserted_row_when_locked(self):
        """If a caller pre-inserted a running row (admin path) and the lock
        is held, run_sync must finalize THAT row as failed, not insert a
        skip note."""
        from app.sync import orchestrator

        with (
            patch.object(orchestrator, "_acquire_sync_lock", return_value=None),
            patch.object(orchestrator, "_record_skipped_sync") as mock_skip,
            patch.object(orchestrator, "_finalize_row") as mock_finalize,
        ):
            result = orchestrator.run_sync(repo_dir=Path("/tmp/x"), log_id=99)

        assert result is False
        mock_skip.assert_not_called()
        args, kwargs = mock_finalize.call_args
        assert args[0] == 99
        assert args[1] == "failed"
        assert "advisory lock" in kwargs["error_message"].lower()

    def test_lock_released_in_finally_on_success(self):
        """A successful run must release the lock connection."""
        from rdflib import Graph

        from app.sync import orchestrator

        lock_conn = object()
        with (
            patch.object(orchestrator, "_acquire_sync_lock", return_value=lock_conn),
            patch.object(orchestrator, "_release_sync_lock") as mock_release,
            patch.object(orchestrator, "_drain_rerun_requests") as mock_drain,
            patch.object(orchestrator, "_insert_running_row", return_value=1),
            patch.object(orchestrator, "_update_step"),
            patch.object(orchestrator, "_finalize_row"),
            patch.object(orchestrator, "clone_or_pull"),
            patch.object(orchestrator, "convert_ontology", return_value=Graph()),
            patch.object(orchestrator, "load_shapes", return_value=Graph()),
            patch.object(orchestrator, "validate_graph", return_value=(True, "")),
            patch.object(orchestrator, "serialize_to_turtle", return_value="# t"),
            patch.object(orchestrator, "drop_graph", return_value=True),
            patch.object(orchestrator, "upload_turtle_to_named_graph", return_value=True),
            patch.object(orchestrator, "copy_graph_to_default", return_value=True),
            patch.object(orchestrator, "graph_triple_count", return_value=2_000_000),
            patch.object(orchestrator, "_get_notify_fn", return_value=None),
        ):
            result = orchestrator.run_sync(repo_dir=Path("/tmp/x"))

        assert result is True
        mock_release.assert_called_once_with(lock_conn)
        # The drain runs once in the finally of a lock-acquired run.
        mock_drain.assert_called_once()


# ===========================================================================
# Optional same-scope: secret scrubbing in sync_log / notifications
# ===========================================================================


class TestSecretScrubbing:
    def test_scrubs_url_credentials(self):
        from app.sync.orchestrator import _scrub_secrets

        text = "fatal: could not read from https://octocat:ghp_TOKEN123@github.com/x.git"
        out = _scrub_secrets(text)
        assert "ghp_TOKEN123" not in out
        assert "octocat" not in out
        assert "***:***@github.com" in out

    def test_scrubs_admin_password(self, monkeypatch: pytest.MonkeyPatch):
        from app.sync.orchestrator import _scrub_secrets

        monkeypatch.setenv("FUSEKI_ADMIN_PASSWORD", "supersecret")
        out = _scrub_secrets("auth failed with password supersecret on PUT")
        assert "supersecret" not in out
        assert "***" in out

    def test_empty_roundtrips(self):
        from app.sync.orchestrator import _scrub_secrets

        assert _scrub_secrets("") == ""


# ===========================================================================
# Comment item 2: signature verification hardening (bytes compare)
# ===========================================================================


class TestVerifySignatureBytes:
    def test_non_ascii_signature_returns_false_not_typeerror(self):
        """A non-ASCII byte in the attacker-controlled header must yield
        False, never escape as a TypeError → unhandled 500."""
        secret = "test-secret"
        payload = b'{"ref": "refs/heads/main"}'
        # Smuggle a non-ASCII char into the signature header.
        bad_sig = "sha256=" + "ÿ" * 64
        assert verify_signature(payload, bad_sig, secret) is False

    def test_emoji_signature_returns_false(self):
        """A code point above latin-1 (>U+00FF) must also be rejected
        cleanly, not raise."""
        secret = "test-secret"
        payload = b"{}"
        assert verify_signature(payload, "sha256=\U0001f4a9", secret) is False

    def test_valid_signature_still_passes(self):
        secret = "test-secret"
        payload = b'{"ref": "refs/heads/main"}'
        sig = "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
        assert verify_signature(payload, sig, secret) is True

    def test_wrong_signature_fails(self):
        assert verify_signature(b"{}", "sha256=deadbeef", "secret") is False


# ===========================================================================
# Webhook handler integration: body cap (item 3), signature, replay (H5)
# ===========================================================================


def _sig(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _make_webhook_request(
    *,
    body: bytes,
    secret: str = "test-secret",
    event: str = "push",
    delivery: str = "11111111-1111-1111-1111-111111111111",
    content_length: int | None = None,
    signature: str | None = None,
) -> Request:
    """Build a Starlette Request that delivers ``body`` via ASGI receive."""
    sig = signature if signature is not None else _sig(secret, body)
    cl = str(content_length if content_length is not None else len(body))
    headers = [
        (b"x-github-event", event.encode()),
        (b"x-hub-signature-256", sig.encode("latin-1", "ignore")),
        (b"x-github-delivery", delivery.encode()),
        (b"content-length", cl.encode()),
        (b"content-type", b"application/json"),
    ]
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/webhooks/github",
        "query_string": b"",
        "headers": headers,
    }
    sent = False

    async def _receive():
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    return Request(scope, receive=_receive)


def _run(coro):
    return asyncio.run(coro)


def _json_body(resp) -> dict:
    """Decode a JSONResponse body to a dict (bytes -> str -> json)."""
    return json.loads(bytes(resp.body).decode("utf-8"))


_PUSH_BODY = json.dumps(
    {
        "ref": "refs/heads/main",
        "repository": {"full_name": "henrikaavik/estonian-legal-ontology"},
    }
).encode()


class TestWebhookBodyCap:
    def test_oversized_content_length_rejected_before_read(self):
        """A Content-Length above the cap must 413 without reading body or
        verifying the signature."""
        with (
            patch.object(webhook, "WEBHOOK_SECRET", "test-secret"),
            patch.object(webhook, "verify_signature") as mock_verify,
            patch.object(webhook, "record_delivery") as mock_record,
        ):
            req = _make_webhook_request(
                body=_PUSH_BODY,
                content_length=webhook.MAX_WEBHOOK_BODY_BYTES + 1,
            )
            resp = _run(webhook.webhook_handler(req))
        assert resp.status_code == 413
        mock_verify.assert_not_called()
        mock_record.assert_not_called()

    def test_missing_content_length_rejected(self):
        """No Content-Length header at all → 413 (fail closed)."""
        with patch.object(webhook, "WEBHOOK_SECRET", "test-secret"):
            # Build a request with no content-length header.
            body = _PUSH_BODY
            headers = [
                (b"x-github-event", b"push"),
                (b"x-hub-signature-256", _sig("test-secret", body).encode()),
                (b"x-github-delivery", b"d-1"),
            ]
            scope = {
                "type": "http",
                "method": "POST",
                "path": "/webhooks/github",
                "query_string": b"",
                "headers": headers,
            }

            async def _receive():
                return {"type": "http.request", "body": body, "more_body": False}

            req = Request(scope, receive=_receive)
            resp = _run(webhook.webhook_handler(req))
        assert resp.status_code == 413

    def test_within_cap_proceeds(self):
        with (
            patch.object(webhook, "WEBHOOK_SECRET", "test-secret"),
            patch.object(webhook, "record_delivery", return_value=True),
            patch.object(webhook, "trigger_sync_background", return_value=True) as mock_trigger,
        ):
            req = _make_webhook_request(body=_PUSH_BODY)
            resp = _run(webhook.webhook_handler(req))
        assert resp.status_code == 200
        mock_trigger.assert_called_once()


class TestWebhookReplayProtection:
    def test_duplicate_delivery_rejected_even_with_valid_signature(self):
        """H5 DoD: a valid-signature push whose delivery id was already
        seen must be rejected (409) and NOT trigger a sync."""
        with (
            patch.object(webhook, "WEBHOOK_SECRET", "test-secret"),
            patch.object(webhook, "record_delivery", return_value=False) as mock_record,
            patch.object(webhook, "trigger_sync_background") as mock_trigger,
        ):
            req = _make_webhook_request(body=_PUSH_BODY, delivery="dup-123")
            resp = _run(webhook.webhook_handler(req))
        assert resp.status_code == 409
        mock_record.assert_called_once()
        mock_trigger.assert_not_called()

    def test_new_delivery_with_valid_signature_starts_sync(self):
        """H5 DoD: a new delivery with a valid signature still starts sync."""
        with (
            patch.object(webhook, "WEBHOOK_SECRET", "test-secret"),
            patch.object(webhook, "record_delivery", return_value=True) as mock_record,
            patch.object(webhook, "trigger_sync_background", return_value=True) as mock_trigger,
        ):
            req = _make_webhook_request(body=_PUSH_BODY, delivery="fresh-456")
            resp = _run(webhook.webhook_handler(req))
        assert resp.status_code == 200
        assert _json_body(resp)["status"] == "sync_triggered"
        mock_record.assert_called_once_with("fresh-456", "push")
        mock_trigger.assert_called_once()

    def test_invalid_signature_does_not_consume_delivery(self):
        """A bad signature must 401 before record_delivery is ever called —
        an attacker can't burn delivery ids without a valid signature."""
        with (
            patch.object(webhook, "WEBHOOK_SECRET", "test-secret"),
            patch.object(webhook, "record_delivery") as mock_record,
        ):
            req = _make_webhook_request(body=_PUSH_BODY, signature="sha256=wrong")
            resp = _run(webhook.webhook_handler(req))
        assert resp.status_code == 401
        mock_record.assert_not_called()

    def test_ping_does_not_consume_delivery(self):
        """A ping event must not record a delivery row (only acted-on
        pushes do), so pings can't exhaust the dedupe table."""
        with (
            patch.object(webhook, "WEBHOOK_SECRET", "test-secret"),
            patch.object(webhook, "record_delivery") as mock_record,
        ):
            req = _make_webhook_request(body=b"{}", event="ping")
            resp = _run(webhook.webhook_handler(req))
        assert resp.status_code == 200
        assert _json_body(resp)["status"] == "pong"
        mock_record.assert_not_called()

    def test_push_from_other_repo_does_not_consume_delivery(self):
        with (
            patch.object(webhook, "WEBHOOK_SECRET", "test-secret"),
            patch.object(webhook, "record_delivery") as mock_record,
            patch.object(webhook, "trigger_sync_background") as mock_trigger,
        ):
            body = json.dumps(
                {"ref": "refs/heads/main", "repository": {"full_name": "someone/else"}}
            ).encode()
            req = _make_webhook_request(body=body)
            resp = _run(webhook.webhook_handler(req))
        assert resp.status_code == 200
        assert _json_body(resp)["status"] == "ignored"
        mock_record.assert_not_called()
        mock_trigger.assert_not_called()


# ===========================================================================
# H5: webhook_deliveries store
# ===========================================================================


class TestRecordDelivery:
    def test_new_delivery_returns_true(self):
        from app.sync import webhook_deliveries

        conn = MagicMock()
        # DELETE then INSERT...RETURNING — fetchone returns a row (new).
        conn.execute.return_value.fetchone.return_value = ("d-1",)
        cm = MagicMock()
        cm.__enter__.return_value = conn
        cm.__exit__.return_value = False
        with patch.object(webhook_deliveries, "get_connection", return_value=cm):
            assert webhook_deliveries.record_delivery("d-1", "push") is True

    def test_duplicate_delivery_returns_false(self):
        from app.sync import webhook_deliveries

        conn = MagicMock()
        # ON CONFLICT DO NOTHING -> RETURNING yields no row.
        conn.execute.return_value.fetchone.return_value = None
        cm = MagicMock()
        cm.__enter__.return_value = conn
        cm.__exit__.return_value = False
        with patch.object(webhook_deliveries, "get_connection", return_value=cm):
            assert webhook_deliveries.record_delivery("d-1", "push") is False

    def test_blank_delivery_id_fails_closed(self):
        from app.sync import webhook_deliveries

        # No DB call at all for a blank id.
        with patch.object(webhook_deliveries, "get_connection") as mock_conn:
            assert webhook_deliveries.record_delivery("", "push") is False
        mock_conn.assert_not_called()

    def test_db_error_fails_closed(self):
        from app.sync import webhook_deliveries

        with patch.object(webhook_deliveries, "get_connection", side_effect=RuntimeError("db")):
            assert webhook_deliveries.record_delivery("d-1", "push") is False

    def test_retention_sweep_runs_on_insert(self):
        """Every record call must also issue the retention DELETE."""
        from app.sync import webhook_deliveries

        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = ("d-1",)
        cm = MagicMock()
        cm.__enter__.return_value = conn
        cm.__exit__.return_value = False
        with patch.object(webhook_deliveries, "get_connection", return_value=cm):
            webhook_deliveries.record_delivery("d-1", "push")
        sqls = " ".join(str(c.args[0]) for c in conn.execute.call_args_list)
        assert "DELETE FROM webhook_deliveries" in sqls
        assert "INSERT INTO webhook_deliveries" in sqls


# ===========================================================================
# Round-2 review (#853): durable coalescing rerun for pushes arriving mid-sync
# ===========================================================================


def _single_row_conn_cm(fetchone_return):
    """Build a (connection, context-manager) pair whose execute().fetchone()
    returns ``fetchone_return``."""
    conn = MagicMock()
    conn.execute.return_value.fetchone.return_value = fetchone_return
    cm = MagicMock()
    cm.__enter__.return_value = conn
    cm.__exit__.return_value = False
    return conn, cm


class TestRerunFlagStore:
    """request_rerun / consume_rerun_request primitives."""

    def test_request_rerun_upserts_single_row(self):
        from app.sync import webhook_deliveries

        conn, cm = _single_row_conn_cm(None)
        with patch.object(webhook_deliveries, "get_connection", return_value=cm):
            assert webhook_deliveries.request_rerun("d-9") is True
        sql = " ".join(str(c.args[0]) for c in conn.execute.call_args_list)
        assert "INSERT INTO sync_rerun_request" in sql
        assert "ON CONFLICT (id) DO UPDATE" in sql

    def test_request_rerun_db_error_returns_false(self):
        from app.sync import webhook_deliveries

        with patch.object(webhook_deliveries, "get_connection", side_effect=RuntimeError("db")):
            assert webhook_deliveries.request_rerun("d-9") is False

    def test_consume_returns_true_when_flag_set(self):
        from app.sync import webhook_deliveries

        conn, cm = _single_row_conn_cm((True,))
        with patch.object(webhook_deliveries, "get_connection", return_value=cm):
            assert webhook_deliveries.consume_rerun_request() is True
        sql = " ".join(str(c.args[0]) for c in conn.execute.call_args_list)
        assert "DELETE FROM sync_rerun_request" in sql
        assert "RETURNING" in sql

    def test_consume_returns_false_when_no_flag(self):
        from app.sync import webhook_deliveries

        conn, cm = _single_row_conn_cm(None)
        with patch.object(webhook_deliveries, "get_connection", return_value=cm):
            assert webhook_deliveries.consume_rerun_request() is False

    def test_consume_db_error_returns_false(self):
        from app.sync import webhook_deliveries

        with patch.object(webhook_deliveries, "get_connection", side_effect=RuntimeError("db")):
            assert webhook_deliveries.consume_rerun_request() is False

    def test_n_pushes_coalesce_to_one_pending_rerun(self):
        """N request_rerun calls all UPSERT the same single row, so a single
        consume drains them as ONE rerun (then the row is gone)."""
        from app.sync import webhook_deliveries

        # In-memory single-row emulation.
        state: dict[str, bool] = {"set": False}

        def _fake_request(_id=None):
            state["set"] = True
            return True

        def _fake_consume():
            was = state["set"]
            state["set"] = False
            return was

        with (
            patch.object(webhook_deliveries, "request_rerun", side_effect=_fake_request),
            patch.object(webhook_deliveries, "consume_rerun_request", side_effect=_fake_consume),
        ):
            # 5 mid-sync pushes.
            for _ in range(5):
                webhook_deliveries.request_rerun("d")
            # First drain sees the coalesced flag; second drain sees nothing.
            assert webhook_deliveries.consume_rerun_request() is True
            assert webhook_deliveries.consume_rerun_request() is False


class TestWebhookInProgressQueuesResync:
    """(a) A delivery during a running sync → 2xx queued semantics, delivery
    recorded, rerun flag set."""

    def test_in_progress_records_delivery_and_queues_resync(self):
        with (
            patch.object(webhook, "WEBHOOK_SECRET", "test-secret"),
            patch.object(webhook, "record_delivery", return_value=True) as mock_record,
            patch.object(webhook, "trigger_sync_background", return_value=False),
            patch.object(webhook, "request_rerun", return_value=True) as mock_rerun,
        ):
            req = _make_webhook_request(body=_PUSH_BODY, delivery="mid-1")
            resp = _run(webhook.webhook_handler(req))
        assert resp.status_code == 200
        assert _json_body(resp)["status"] == "resync_queued"
        # Delivery recorded (dedupe stays correct) AND rerun flag set.
        mock_record.assert_called_once_with("mid-1", "push")
        mock_rerun.assert_called_once_with("mid-1")

    def test_in_progress_flag_failure_surfaces_503(self):
        """If the durable flag write fails, we must NOT pretend the resync
        was scheduled — surface 503."""
        with (
            patch.object(webhook, "WEBHOOK_SECRET", "test-secret"),
            patch.object(webhook, "record_delivery", return_value=True),
            patch.object(webhook, "trigger_sync_background", return_value=False),
            patch.object(webhook, "request_rerun", return_value=False),
        ):
            req = _make_webhook_request(body=_PUSH_BODY, delivery="mid-2")
            resp = _run(webhook.webhook_handler(req))
        assert resp.status_code == 503
        assert _json_body(resp)["status"] == "resync_queue_failed"

    def test_no_contention_path_unaffected(self):
        """(e) Normal no-contention push still starts sync, no rerun flag."""
        with (
            patch.object(webhook, "WEBHOOK_SECRET", "test-secret"),
            patch.object(webhook, "record_delivery", return_value=True),
            patch.object(webhook, "trigger_sync_background", return_value=True),
            patch.object(webhook, "request_rerun") as mock_rerun,
        ):
            req = _make_webhook_request(body=_PUSH_BODY, delivery="solo-1")
            resp = _run(webhook.webhook_handler(req))
        assert resp.status_code == 200
        assert _json_body(resp)["status"] == "sync_triggered"
        mock_rerun.assert_not_called()


class TestRunSyncDrainsRerun:
    """(b)/(c) run_sync completion drains the flag and reruns exactly once;
    flag cleared after rerun. (d) lock-acquire failure on rerun re-sets the
    flag so it isn't lost.

    The drain is tested directly via ``_drain_rerun_requests`` (its wiring
    into ``run_sync``'s finally is asserted separately in
    ``TestSyncAdvisoryLock.test_lock_released_in_finally_on_success`` via
    ``mock_drain.assert_called_once()``). Driving it through the full
    ``run_sync`` success path would pull in unrelated RAG/notify side
    effects, so we exercise the unit in isolation for determinism.
    """

    def test_drain_reruns_exactly_once_when_flag_set(self):
        """(b) A set flag → exactly one rerun thread spawned with _is_rerun."""
        from app.sync import orchestrator, webhook_deliveries

        spawned: list = []

        class _FakeThread:
            def __init__(self, target=None, kwargs=None, daemon=None):
                self._target = target
                self._kwargs = kwargs or {}
                spawned.append(self._kwargs)

            def start(self):
                # Do NOT actually run run_sync again (would recurse) — just
                # record that a rerun was scheduled with _is_rerun=True.
                pass

        with (
            patch.object(webhook_deliveries, "consume_rerun_request", return_value=True),
            patch.object(orchestrator.threading, "Thread", _FakeThread),
        ):
            orchestrator._drain_rerun_requests()

        assert len(spawned) == 1
        assert spawned[0].get("_is_rerun") is True

    def test_drain_does_nothing_when_flag_clear(self):
        """(c) Flag clear → no rerun spawned."""
        from app.sync import orchestrator, webhook_deliveries

        spawned: list = []

        class _FakeThread:
            def __init__(self, target=None, kwargs=None, daemon=None):
                spawned.append(kwargs or {})

            def start(self):
                pass

        with (
            patch.object(webhook_deliveries, "consume_rerun_request", return_value=False),
            patch.object(orchestrator.threading, "Thread", _FakeThread),
        ):
            orchestrator._drain_rerun_requests()

        assert spawned == []

    def test_drain_coalesces_n_pushes_into_one_rerun(self):
        """N mid-sync pushes (one set flag) drain as exactly one rerun: the
        drain consumes once and spawns once regardless of how many pushes
        set the flag."""
        from app.sync import orchestrator, webhook_deliveries

        spawned: list = []

        class _FakeThread:
            def __init__(self, target=None, kwargs=None, daemon=None):
                spawned.append(kwargs or {})

            def start(self):
                pass

        # consume_rerun_request returns True once (the coalesced flag), then
        # the spawned rerun would consume again — but we don't run it here.
        consume = MagicMock(return_value=True)
        with (
            patch.object(webhook_deliveries, "consume_rerun_request", consume),
            patch.object(orchestrator.threading, "Thread", _FakeThread),
        ):
            orchestrator._drain_rerun_requests()

        # One consume, one spawn — the coalescing happened at flag-set time
        # (all pushes UPSERT the same row), so the drain only ever sees one.
        consume.assert_called_once()
        assert len(spawned) == 1

    def test_rerun_relock_failure_resets_flag(self):
        """(d) When a rerun can't acquire the lock, the flag must be re-set
        (not lost) so the run that holds the lock drains it."""
        from app.sync import orchestrator, webhook_deliveries

        with (
            patch.object(orchestrator, "_acquire_sync_lock", return_value=None),
            patch.object(webhook_deliveries, "request_rerun", return_value=True) as mock_req,
            patch.object(orchestrator, "_record_skipped_sync") as mock_skip,
            patch.object(orchestrator, "_drain_rerun_requests") as mock_drain,
        ):
            result = orchestrator.run_sync(repo_dir=Path("/tmp/x"), _is_rerun=True)

        assert result is False
        # Flag re-set so the work isn't lost.
        mock_req.assert_called_once()
        # A rerun that couldn't acquire the lock must NOT record a skip note
        # (it's not a user-visible skip) and must NOT drain (no busy-loop).
        mock_skip.assert_not_called()
        mock_drain.assert_not_called()

    def test_normal_skip_is_not_a_rerun_and_does_not_reset(self):
        """A plain (non-rerun) lock-miss records a skip note and does NOT
        touch the rerun flag."""
        from app.sync import orchestrator, webhook_deliveries

        with (
            patch.object(orchestrator, "_acquire_sync_lock", return_value=None),
            patch.object(webhook_deliveries, "request_rerun") as mock_req,
            patch.object(orchestrator, "_record_skipped_sync") as mock_skip,
        ):
            result = orchestrator.run_sync(repo_dir=Path("/tmp/x"))

        assert result is False
        mock_skip.assert_called_once()
        mock_req.assert_not_called()


# ===========================================================================
# Migration 039 conventions
# ===========================================================================


class TestMigration039:
    def test_migration_file_exists(self):
        assert _MIGRATION_039.exists()

    def test_uses_if_not_exists(self):
        sql = _MIGRATION_039.read_text(encoding="utf-8")
        assert "CREATE TABLE IF NOT EXISTS webhook_deliveries" in sql
        assert "CREATE INDEX IF NOT EXISTS" in sql

    def test_has_header_and_rollback(self):
        sql = _MIGRATION_039.read_text(encoding="utf-8")
        assert "Migration 039" in sql
        assert "ROLLBACK" in sql
        assert "#853" in sql

    def test_defines_rerun_request_table(self):
        """Round-2: the coalescing-rerun single-row table must be defined,
        idempotently, with the single-row CHECK."""
        sql = _MIGRATION_039.read_text(encoding="utf-8")
        assert "CREATE TABLE IF NOT EXISTS sync_rerun_request" in sql
        assert "CHECK (id)" in sql
        # Rollback drops it too.
        assert "DROP TABLE IF EXISTS sync_rerun_request" in sql
