"""Regression test for #802 (chat hang) — FastHTML param resolver.

Background
----------

The chat hang reported in #802 was caused by FastHTML's ``_find_p``
parameter resolver refusing to inject the special WS names (``send``,
``scope``, ``ws``) when they carry *any* type annotation — even
``typing.Any``. The relevant branch in
``fasthtml/core.py:_find_p`` only fires for ``if anno is empty:`` (i.e.
no annotation at all); anything else falls through to the generic
data / path-params / cookies / headers / query lookup and raises::

    ValueError: Missing required field: send

That ``ValueError`` is caught by FastHTML's ``_generic_handler`` and
forwarded to the socket as a text frame *after* the upgrade has
completed, then the next write tries ``send`` on the already-closed
socket and the orchestrator never runs. The user-visible symptom is a
WebSocket that opens and then "hangs" because the body never executed.

The existing unit tests in ``tests/test_chat_websocket*.py`` call
``ws_chat`` (the inner function) directly, completely bypassing
FastHTML's ``_find_p``. That is why this never tripped CI.

What this module guards
-----------------------

1. ``test_ws_handler_signature_keeps_send_unannotated`` — a static
   guard that introspects the registered ``_ws_handler`` and asserts
   ``send`` and ``scope`` carry no annotation (``inspect.Parameter.empty``).
   This is the smallest possible canary; any future contributor who
   "helpfully" adds ``send: Any`` to the handler signature will break
   this test before they ship.

2. ``test_find_p_resolves_send_for_ws_handler`` — invokes FastHTML's
   own ``_find_p`` against the handler's actual parameter signature
   with a fake WebSocket-shaped connection and asserts that ``send``
   resolves to a callable (``partial(_send_ws, conn)``) without
   raising. This exercises exactly the code path that crashed in prod
   for #802.

3. ``test_ws_handler_runs_via_fasthtml_resolver`` — the integration
   guard. Builds a minimal ``FastHTML()`` app, registers the real
   chat handler via ``register_chat_ws_routes`` with the heavyweight
   collaborators (JWTAuthProvider, ChatOrchestrator) mocked out, and
   then opens a WebSocket via ``starlette.testclient.TestClient`` and
   sends a real ``send_message`` envelope. With no auth cookies in
   the handshake the handler reaches its own "Autentimine nõutav"
   branch and emits an ``{"type": "error"}`` frame — and *that* is
   the proof we want. The frame proves:

   * ``_find_p`` injected ``send`` and ``scope`` successfully (no
     ``Missing required field`` ValueError).
   * The handler body executed (auth-extraction → ``ws_chat`` →
     ``_drive_orchestrator`` pre-check → emit error frame).

   If any future signature regression re-introduces the #802 trap,
   this test fails with the exact error frame body, not a timeout.
"""

from __future__ import annotations

import inspect
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

# ---------------------------------------------------------------------------
# Helpers — capture the registered handler without standing up a real app
# ---------------------------------------------------------------------------


def _capture_registered_chat_handler() -> Any:
    """Return the inner ``_ws_handler`` registered by ``register_chat_ws_routes``.

    Mirrors the pattern in ``tests/test_chat_websocket_refresh.py`` —
    we inject a fake ``app`` whose ``.ws()`` is a decorator factory
    that captures whatever handler the production module hands it.
    Used by the static and unit guards below; the integration guard
    further down stands up a real FastHTML app instead.
    """
    from app.chat.websocket import register_chat_ws_routes

    mock_app = MagicMock()
    captured: dict[str, Any] = {"handler": None}

    def capture_ws(path: str, conn: Any = None, disconn: Any = None) -> Any:
        def decorator(fn: Any) -> Any:
            captured["handler"] = fn
            return fn

        return decorator

    mock_app.ws = capture_ws
    register_chat_ws_routes(mock_app)
    handler = captured["handler"]
    assert handler is not None, "register_chat_ws_routes did not register a handler"
    return handler


# ---------------------------------------------------------------------------
# Guard 1 — static signature check
# ---------------------------------------------------------------------------


class TestWsHandlerSignature:
    """Pin the handler signature so FastHTML's ``_find_p`` can resolve it."""

    def test_send_and_scope_carry_no_annotation(self):
        """``send`` and ``scope`` must be unannotated.

        FastHTML's ``_find_p`` only honours the special WS names
        inside ``if anno is empty:``. Annotating either parameter —
        even with ``Any`` — re-introduces the #802 trap.
        """
        handler = _capture_registered_chat_handler()
        sig = inspect.signature(handler)

        for name in ("send", "scope"):
            assert name in sig.parameters, f"handler signature missing '{name}'"
            param = sig.parameters[name]
            assert param.annotation is inspect.Parameter.empty, (
                f"Parameter '{name}' must be unannotated for FastHTML's "
                f"_find_p to inject the special WS callable. Got "
                f"annotation={param.annotation!r}. See #802 / "
                f"docs/2026-05-18-bugfix-plan.md Wave 3."
            )

    def test_msg_is_annotated_dict(self):
        """``msg`` must be annotated ``dict``.

        After dropping the original ``send: Any`` trap we discovered a
        second-phase trap: with ``msg`` left unannotated FastHTML's
        empty-anno branch returned ``None`` (``msg`` is not in the
        ``_special_names`` set), and ``ws_chat`` then crashed on
        ``json.loads(None)`` → emits a ``Vigane JSON`` error to every
        client without ever touching the orchestrator. The fix is the
        ``dict`` annotation, which routes through ``_find_p``'s
        ``if anno is dict: return data`` branch (returning the parsed
        WS payload). Pin it so a future contributor who "cleans up"
        the annotation back to ``str`` or removes it doesn't silently
        re-break chat.
        """
        handler = _capture_registered_chat_handler()
        sig = inspect.signature(handler)

        assert "msg" in sig.parameters, "handler signature missing 'msg'"
        param = sig.parameters["msg"]
        assert param.annotation is dict, (
            f"Parameter 'msg' must be annotated 'dict' so FastHTML's "
            f"_find_p resolves it to the parsed WS JSON payload. Got "
            f"annotation={param.annotation!r}. See #802 / "
            f"docs/2026-05-18-bugfix-plan.md Wave 3."
        )


# ---------------------------------------------------------------------------
# Guard 2 — call FastHTML's _find_p directly against the handler params
# ---------------------------------------------------------------------------


class TestFindPResolvesHandlerParams:
    """Exercise FastHTML's ``_find_p`` against the real handler signature.

    This is the focused unit guard: it picks up the registered
    handler, asks FastHTML for its parameters via ``_params`` (the
    same helper FastHTML uses at request time), and calls ``_find_p``
    on the ``send`` parameter. A regression where the resolver
    cannot inject ``send`` would raise ``ValueError`` here.
    """

    def test_find_p_returns_callable_for_send(self):
        import asyncio
        from functools import partial

        from fasthtml.core import _find_p, _params, _send_ws

        handler = _capture_registered_chat_handler()
        params = _params(handler)
        assert "send" in params, "_params() did not surface 'send' on the handler"
        param = params["send"]

        # Fake WS-shaped connection: ``_find_p`` only needs the scope
        # passed through and (for the ``send`` branch) the conn itself
        # so it can build ``partial(_send_ws, conn)``. We never
        # actually call _send_ws — we only assert _find_p returned a
        # callable bound to our fake conn.
        fake_conn = MagicMock()
        fake_conn.scope = {}

        # Note: FastHTML ships a stale ``core.pyi`` stub that advertises
        # a 3-arg ``_find_p(req, arg, p)``; the real runtime signature is
        # ``(conn, data, hdrs, arg, p)`` (5 args). We ignore pyright here
        # rather than re-import the runtime callable, because the whole
        # point of this test is exercising the runtime resolver.
        resolved = asyncio.run(_find_p(fake_conn, {}, {}, "send", param))  # type: ignore[call-arg]

        # The exact contract from ``_find_p``:
        #   if arg.lower()=='send': return partial(_send_ws, conn)
        # Anything else (or a raised ValueError) is the regression.
        assert isinstance(resolved, partial), (
            f"_find_p must resolve 'send' to a partial wrapping _send_ws "
            f"on this connection. Got: {resolved!r}. This is the #802 trap "
            f"reintroduced — re-check that 'send' is unannotated on the "
            f"_ws_handler signature."
        )
        assert resolved.func is _send_ws
        assert resolved.args == (fake_conn,)

    def test_find_p_returns_dict_for_scope(self):
        """``scope`` resolves to ``dict2obj(conn.scope)`` — must not raise."""
        import asyncio

        from fasthtml.core import _find_p, _params

        handler = _capture_registered_chat_handler()
        params = _params(handler)
        assert "scope" in params
        param = params["scope"]

        fake_conn = MagicMock()
        fake_conn.scope = {"path": "/ws/chat"}

        # If scope ever regresses to a typed parameter the resolver
        # would either inject the wrong value or raise — both are
        # caught by simply calling _find_p and asserting it returns
        # without an exception.
        resolved = asyncio.run(_find_p(fake_conn, {}, {}, "scope", param))  # type: ignore[call-arg]
        assert resolved is not None

    def test_find_p_returns_payload_for_msg(self):
        """``msg: dict`` resolves to the parsed WS JSON payload.

        ``_find_p``'s special-annotation branch at
        ``fasthtml/core.py`` says ``if anno is dict: return data``.
        That is the second half of the #802 fix: without it, the
        empty-anno branch returns ``None`` and ``json.loads(None)``
        crashes ``ws_chat``. Exercises the resolver against the real
        handler signature.
        """
        import asyncio

        from fasthtml.core import _find_p, _params

        handler = _capture_registered_chat_handler()
        params = _params(handler)
        assert "msg" in params, "_params() did not surface 'msg' on the handler"
        param = params["msg"]

        fake_conn = MagicMock()
        fake_conn.scope = {}
        payload = {
            "type": "send_message",
            "conversation_id": "33333333-3333-3333-3333-333333333333",
            "content": "Tere!",
        }

        resolved = asyncio.run(_find_p(fake_conn, payload, {}, "msg", param))  # type: ignore[call-arg]

        # Contract: ``_find_p`` returns the parsed payload as a dict.
        # Regressing to ``msg: str`` or ``msg`` (unannotated) would
        # either raise ``Missing required field: msg`` or return
        # ``None`` — both fail this assertion.
        assert resolved == payload, (
            f"_find_p must resolve 'msg' to the parsed WS payload dict "
            f"via the ``if anno is dict: return data`` branch. Got: "
            f"{resolved!r}. This is the #802 phase-2 trap — re-check "
            f"that 'msg' is annotated 'dict' on the _ws_handler signature."
        )


# ---------------------------------------------------------------------------
# Guard 3 — full integration via FastHTML + Starlette TestClient
# ---------------------------------------------------------------------------


class TestWsHandlerRunsViaFastHTMLResolver:
    """End-to-end WebSocket test: real FastHTML app, real ``_find_p``.

    With heavyweight collaborators (JWT provider, orchestrator) mocked
    out, we still drive the handshake → ``_find_p`` → handler-body path
    that crashed in prod for #802. The narrow pass-criterion here is
    "the WS does not respond with a plain-text 'Missing required field:'
    frame", because that is the exact signature of the #802 trap as it
    manifests in prod (``_generic_handler`` catches the ``ValueError``
    and forwards ``str(e)`` to the socket as a non-JSON text frame
    before the handler body ever runs).

    Asserting on a specific JSON error type from ``ws_chat`` would
    couple this test to a downstream concern outside the scope of the
    resolver fix (see report note in the PR description).
    """

    @patch("app.chat.websocket.ChatOrchestrator")
    @patch("app.chat.websocket.get_default_provider")
    @patch("app.chat.websocket.JWTAuthProvider")
    def test_send_message_reaches_handler_body(
        self,
        mock_jwt_cls: MagicMock,
        mock_provider: MagicMock,
        mock_orch_cls: MagicMock,
    ):
        from fasthtml.common import FastHTML
        from starlette.testclient import TestClient

        from app.chat.websocket import register_chat_ws_routes

        # The orchestrator must never be touched on the no-auth path,
        # but mock it defensively so a future refactor that moves
        # auth-after-orchestrator-init doesn't silently summon real
        # SQL / Claude clients during this test.
        mock_orch_instance = MagicMock()
        mock_orch_instance.handle_message = AsyncMock()
        mock_orch_cls.return_value = mock_orch_instance

        # JWT provider mocked so JWTAuthProvider() construction never
        # touches a real DB. We send the handshake with NO cookies, so
        # the auth-extraction block is skipped entirely.
        mock_jwt_instance = MagicMock()
        mock_jwt_instance.get_current_user.return_value = None
        mock_jwt_cls.return_value = mock_jwt_instance

        app = FastHTML()
        register_chat_ws_routes(app)

        client = TestClient(app)
        with client.websocket_connect("/ws/chat") as ws:
            ws.send_text(
                json.dumps(
                    {
                        "type": "send_message",
                        "conversation_id": "33333333-3333-3333-3333-333333333333",
                        "content": "Tere!",
                    }
                )
            )

            # Drain frames until we see a post-connect event from
            # ``ws_chat`` (the receive handler), or the failure-mode
            # bare text frame. The ``{"type": "connected"}`` frame
            # comes from the ``on_connect`` resolver path; we want
            # proof that the *receive* resolver path also runs.
            saw_receive_handler = False
            saw_missing_field = False
            for _ in range(5):
                try:
                    frame = ws.receive_text()
                except Exception:
                    break
                # The regression symptom: FastHTML's resolver raises
                # ValueError and ``_generic_handler`` forwards
                # ``str(e)`` as a non-JSON text frame. Detecting this
                # exact string is the strongest possible #802 canary.
                if "Missing required field" in frame:
                    saw_missing_field = True
                    break
                try:
                    parsed = json.loads(frame)
                except json.JSONDecodeError:
                    continue
                if not isinstance(parsed, dict):
                    continue
                # ``connected`` comes from ``on_connect`` — proves the
                # connect resolver works, but not the receive resolver.
                # Keep draining until we see something from
                # ``ws_chat``.
                if parsed.get("type") == "connected":
                    continue
                # Any other typed frame is from ``ws_chat`` itself —
                # proof that ``_find_p`` resolved ``send`` and
                # ``scope`` for the receive handler and the body ran.
                if "type" in parsed:
                    saw_receive_handler = True
                    break

            assert not saw_missing_field, (
                "FastHTML _find_p raised 'Missing required field' — the "
                "#802 phase-1 regression is back. Check that send/scope "
                "on _ws_handler are unannotated."
            )
            assert saw_receive_handler, (
                "Did not observe any post-'connected' JSON frame from "
                "the receive handler. This means ``_ws_handler``'s body "
                "never ran for the incoming send_message — the most "
                "likely cause is the FastHTML _find_p trap from #802 "
                "(send/scope annotated)."
            )

    @patch("app.chat.websocket.ChatOrchestrator")
    @patch("app.chat.websocket.get_default_provider")
    @patch("app.chat.websocket.JWTAuthProvider")
    def test_msg_payload_is_parsed_not_dropped_as_invalid_json(
        self,
        mock_jwt_cls: MagicMock,
        mock_provider: MagicMock,
        mock_orch_cls: MagicMock,
    ):
        """Phase-2 regression guard: ``msg`` must reach ``ws_chat`` as a
        valid JSON string, never ``None``.

        Background. With ``msg`` left unannotated (the state immediately
        after the original ``send: Any`` fix), FastHTML's empty-anno
        branch returned ``None`` — ``msg`` is not in its
        ``_special_names`` set. ``ws_chat`` then did
        ``json.loads(None)`` → ``TypeError`` → emitted
        ``{"type": "error", "message": "Vigane JSON."}`` and returned.
        Every chat request crashed pre-orchestrator with that exact
        error, with no other observable symptom. The fix is the
        ``msg: dict`` annotation that routes through
        ``if anno is dict: return data``. This test reproduces the
        client-visible symptom and asserts it doesn't recur.

        We send a real ``send_message`` envelope with NO auth cookies.
        That intentionally fails at the "Autentimine nõutav" check
        *inside* ``ws_chat`` (after the JSON parse), which proves the
        JSON parse succeeded. A regression to ``msg=None`` would short-
        circuit before that check and emit ``Vigane JSON`` instead.
        """
        from fasthtml.common import FastHTML
        from starlette.testclient import TestClient

        from app.chat.websocket import register_chat_ws_routes

        mock_orch_instance = MagicMock()
        mock_orch_instance.handle_message = AsyncMock()
        mock_orch_cls.return_value = mock_orch_instance

        mock_jwt_instance = MagicMock()
        mock_jwt_instance.get_current_user.return_value = None
        mock_jwt_cls.return_value = mock_jwt_instance

        app = FastHTML()
        register_chat_ws_routes(app)

        client = TestClient(app)
        with client.websocket_connect("/ws/chat") as ws:
            ws.send_text(
                json.dumps(
                    {
                        "type": "send_message",
                        "conversation_id": "44444444-4444-4444-4444-444444444444",
                        "content": "Tere!",
                    }
                )
            )

            saw_vigane_json = False
            saw_post_json_parse_branch = False
            for _ in range(5):
                try:
                    frame = ws.receive_text()
                except Exception:
                    break
                if "Missing required field" in frame:
                    # Phase-1 regression — different test owns this
                    # assertion, but flagging it here too keeps the
                    # failure mode unambiguous.
                    break
                try:
                    parsed = json.loads(frame)
                except json.JSONDecodeError:
                    continue
                if not isinstance(parsed, dict):
                    continue
                if parsed.get("type") == "connected":
                    continue
                # Phase-2 trap: ws_chat sends this exact body from the
                # ``except (json.JSONDecodeError, TypeError)`` branch
                # when ``msg`` arrives as ``None``.
                if parsed.get("type") == "error" and parsed.get("message") == "Vigane JSON.":
                    saw_vigane_json = True
                    break
                # Any OTHER typed frame proves we got past
                # ``json.loads(msg)`` — i.e. ``msg`` was injected as a
                # real dict by FastHTML and our wrapper's
                # ``json.dumps(msg)`` boundary produced valid JSON.
                if "type" in parsed:
                    saw_post_json_parse_branch = True
                    break

            assert not saw_vigane_json, (
                "ws_chat emitted 'Vigane JSON.' — the #802 phase-2 trap "
                "is back. Either 'msg' lost its 'dict' annotation on "
                "_ws_handler or the wrapper stopped re-serialising the "
                "dict payload via json.dumps() at the ws_chat boundary. "
                "See docs/2026-05-18-bugfix-plan.md Wave 3."
            )
            assert saw_post_json_parse_branch, (
                "Did not observe any post-JSON-parse frame from ws_chat. "
                "Either the resolver injection broke (phase-1 trap) or "
                "the JSON parse failed (phase-2 trap). Check both the "
                "send/scope (unannotated) and msg ('dict') annotations."
            )
