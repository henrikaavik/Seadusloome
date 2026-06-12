import logging
import threading
import time
from pathlib import Path

from fasthtml.common import *
from starlette.datastructures import MutableHeaders
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import Request
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app import config
from app.admin import register_admin_routes
from app.analyysikeskus import register_analyysikeskus_routes
from app.annotations.routes import register_annotation_routes
from app.auth.middleware import SKIP_PATHS, auth_before
from app.auth.organizations import register_org_routes
from app.auth.perimeter import OriginCheckMiddleware, get_trusted_proxy_hosts
from app.auth.profile import register_profile_routes
from app.auth.routes import register_auth_routes
from app.auth.users import register_user_routes
from app.chat.routes import register_chat_routes
from app.chat.websocket import register_chat_ws_routes
from app.config import get_app_env
from app.dashboard import register_dashboard_routes
from app.docs.report_routes import register_report_routes
from app.docs.routes import register_draft_routes
from app.docs.websocket import register_draft_ws_routes
from app.docs.ws_export_progress import register_export_progress_ws_routes
from app.drafter.routes import register_drafter_routes
from app.explorer.pages import register_explorer_pages
from app.explorer.routes import register_explorer_routes
from app.explorer.websocket import register_ws_routes
from app.notifications.routes import register_notification_routes
from app.notifications.websocket import register_notifications_ws_routes
from app.sync.webhook import register_webhook_routes
from app.ui.components.search_routes import register_search_routes
from app.ui.design_system_pages import register_design_system_routes
from app.ui.forms.live_validation import register_validation_routes
from app.ui.primitives.button import Button  # noqa: F401  -- shadow guard #419
from app.ui.theme import THEME_INIT_SCRIPT

logger = logging.getLogger(__name__)

_STATIC_DIR = Path(__file__).parent / "static"

# Background worker handles: populated lazily by the lifespan hook so
# that importing this module does not spawn a thread. Tests that import
# ``app`` via ``TestClient`` still trigger the lifespan unless
# ``DISABLE_BACKGROUND_WORKER=1`` is set (see tests/conftest.py).
_stop_worker = threading.Event()
_worker_thread: threading.Thread | None = None

# Draft archive-warning scheduler (issue #572). Lifecycle mirrors the
# job worker: the lifespan hook starts the thread on ASGI startup and
# sets the stop event on shutdown. Coolify has no native cron, so a
# lifespan thread is the simplest way to run a daily scan. Tests opt
# out via the same ``DISABLE_BACKGROUND_WORKER=1`` flag so they never
# spawn real threads against a mocked DB.
_stop_archive_scheduler = threading.Event()
_archive_thread: threading.Thread | None = None


async def lifespan(_app):  # type: ignore[no-untyped-def]
    """ASGI lifespan hook: start the background worker, stop it on shutdown.

    FastHTML iterates this with ``async for state in self.ls(app)`` in
    ``fasthtml/core.py::Lifespan._run``, which means it expects a plain
    async generator function — NOT an ``@asynccontextmanager``-decorated
    function. The decorator wraps the generator in an
    ``_AsyncGeneratorContextManager``, which FastHTML's ``async for``
    loop cannot iterate, and the lifespan startup crashes with
    ``TypeError: 'async for' requires an object with __aiter__ method``.
    That failure brought down every Phase 2 deploy 68e1259..9389d52
    before the healthcheck could reach ``/api/ping``.

    Set ``DISABLE_BACKGROUND_WORKER=1`` to skip worker startup entirely —
    the pytest suite uses this flag so mocked DB calls are not racing
    a real worker thread.
    """
    global _worker_thread, _archive_thread

    # Reconcile any sync_log rows orphaned by a previous process crash
    # (issue #567). A lingering 'running' row would otherwise make the
    # admin card display a stuck progress indicator and the DB-level
    # lock would block new syncs. Best-effort: swallow failures.
    try:
        from app.sync.orchestrator import mark_stale_running_as_failed

        stale = mark_stale_running_as_failed()
        if stale:
            logger.warning("Marked %d stale 'running' sync_log row(s) as failed", stale)
    except Exception:
        logger.exception("Startup: stale sync_log cleanup failed (non-critical)")

    # #608: capture the running event loop so the sync pipeline worker
    # thread can schedule WS broadcast coroutines via
    # ``asyncio.run_coroutine_threadsafe``. Done unconditionally — even
    # if the worker is disabled (test mode), an in-process emit can
    # still happen if/when somebody triggers a status transition from
    # an async route.
    try:
        import asyncio as _asyncio

        from app.docs import status_events as _status_events

        _status_events.register_event_loop(_asyncio.get_running_loop())
    except Exception:
        logger.debug(
            "Failed to register event loop for draft status events",
            exc_info=True,
        )

    # #180 — capture the running loop so notify() can push WS events
    # from background threads (job worker etc.) via
    # ``asyncio.run_coroutine_threadsafe``.
    try:
        import asyncio as _asyncio

        from app.notifications import websocket as _notif_ws

        _notif_ws.register_event_loop(_asyncio.get_running_loop())
    except Exception:
        logger.debug(
            "Failed to register event loop for notifications WS",
            exc_info=True,
        )

    if config.env_bool("DISABLE_BACKGROUND_WORKER"):
        logger.info("Background worker disabled via DISABLE_BACKGROUND_WORKER=1")
        yield
        return

    # Draft archive-warning scheduler (issue #572): a daily background
    # scan that emits notifications for drafts older than 90 days. This
    # MUST run regardless of WORKER_MODE because the web process is the
    # canonical singleton host for it — ``scripts/run_worker.py`` does
    # NOT start the scheduler (see the "Why not also start the
    # archive-warning scheduler here?" docstring there: a single daily
    # scan must not run on multiple worker processes at once). So the
    # scheduler starts before the WORKER_MODE gate, otherwise a
    # web+standalone split deployment would silently lose the 90-day
    # auto-archive warning — a compliance feature for draft sensitivity
    # (see CLAUDE.md "Draft sensitivity"). (#348 review fix.)
    from app.jobs.archive_warning import start_archive_warning_scheduler

    _stop_archive_scheduler.clear()
    _archive_thread = start_archive_warning_scheduler(_stop_archive_scheduler)
    logger.info("Draft archive-warning scheduler started")

    # WORKER_MODE gate (#348): in 'standalone' mode the worker runs in
    # a separate Coolify container launched via ``scripts/run_worker.py``,
    # so the web container's lifespan must NOT spawn its own worker
    # thread (otherwise jobs get double-claimed at low priority load).
    from app.config import get_worker_mode

    worker_mode = get_worker_mode()
    if worker_mode == "standalone":
        logger.info(
            "WORKER_MODE=standalone — skipping in-process worker startup; "
            "expecting a separate worker process (scripts/run_worker.py)"
        )
        try:
            yield
        finally:
            logger.info("Stopping draft archive-warning scheduler...")
            _stop_archive_scheduler.set()
            if _archive_thread is not None:
                _archive_thread.join(timeout=30.0)
                _archive_thread = None
        return

    # Local import so tests that patch app.jobs.worker see the module
    # freshly re-imported if they reload after setting env vars.
    from app.jobs.registry import register_all_handlers
    from app.jobs.worker import start_worker_thread

    # Ensure the handler registry is populated before the worker thread
    # claims its first job. The route-registration imports at module top
    # transitively import ``app.docs`` and ``app.drafter`` (which trigger
    # @register_handler as a side effect), but we call this explicitly so
    # the wiring is identical to standalone mode and any future refactor
    # of the route imports cannot accidentally drop a handler.
    register_all_handlers()

    _stop_worker.clear()
    _worker_thread = start_worker_thread(_stop_worker)
    logger.info("Background worker started (WORKER_MODE=inproc)")

    try:
        yield
    finally:
        # #836 / #839 review fix: signal BOTH stop events first so the
        # worker and the archive scheduler wind down in parallel, then
        # join against a shared 30-second deadline. The earlier code
        # joined sequentially (worker for 30s, THEN scheduler for 30s)
        # which could exceed the #304 DoD's single-30s window and let
        # the scheduler keep ticking while the worker was still shutting
        # down. With a shared deadline, total shutdown is bounded at
        # 30s; whichever thread finishes first leaves its remaining
        # budget for the other.
        #
        # NOTE: This 30s only matters if the container's SIGTERM-to-SIGKILL
        # grace is also ≥30s. The default Docker grace is 10s, and
        # ``docker/entrypoint.sh`` starts uvicorn without an explicit
        # ``--timeout-graceful-shutdown`` flag, so the platform may
        # truncate this window. If you care about the full 30s in prod,
        # set Coolify's stop-grace to ≥30s (or add ``STOPSIGNAL`` +
        # ``--stop-timeout`` in the Dockerfile).
        logger.info("Stopping background threads (30s shared deadline)...")
        _stop_worker.set()
        _stop_archive_scheduler.set()

        deadline = time.monotonic() + 30.0
        if _worker_thread is not None:
            remaining = max(0.0, deadline - time.monotonic())
            _worker_thread.join(timeout=remaining)
            _worker_thread = None
        if _archive_thread is not None:
            remaining = max(0.0, deadline - time.monotonic())
            _archive_thread.join(timeout=remaining)
            _archive_thread = None


# Head tags hoisted into <head> by FastHTML. The theme init script must run
# before the first paint to avoid a flash of the wrong theme (FOUC); it is
# included here rather than in page handlers because inline Script() tags
# returned from handlers land in <body>, not <head>.
# FastHTML's `fast_app(default_hdrs=True)` already injects `<meta charset>`
# and a viewport meta, so we do NOT add them here (#430 — duplicate meta
# tags are invalid HTML5).
_HDRS = (
    Script(THEME_INIT_SCRIPT),
    Meta(name="color-scheme", content="dark"),
    Link(rel="stylesheet", href="/static/css/fonts.css"),
    Link(rel="stylesheet", href="/static/css/tokens.css"),
    Link(rel="stylesheet", href="/static/css/ui.css"),
    # Chat-specific stylesheet — small file (~7 KB), loaded globally so the
    # card on /chat list page and the conversation view both pick it up without
    # needing per-route <head> injection (which would land in <body> in FastHTML).
    Link(rel="stylesheet", href="/static/css/chat.css"),
    # B1 global search bar (epic #784) — small (~5 KB), loaded globally so the
    # bar renders on every PageShell page. Defer so it doesn't block first paint;
    # the script binds on DOMContentLoaded.
    Script(src="/static/js/global_search.js", defer=True),
    # #176 annotation @mention typeahead — loaded globally because the
    # annotation popover is injected via HTMX swap at runtime, so we
    # cannot scope the script to a single page. The widget binds itself
    # on DOMContentLoaded and re-binds on every ``htmx:afterSwap``.
    Script(src="/static/js/annotation_mentions.js", defer=True),
)

# ---------------------------------------------------------------------------
# Security headers (#857, review D4/D5)
# ---------------------------------------------------------------------------
#
# Content-Security-Policy — directives derived EMPIRICALLY from what the app
# ships today (grep date 2026-06-11):
#
#   script-src
#     'self'                          /static/js/* (global_search, explorer, chat…)
#     'unsafe-inline'                 ~30 inline Script() islands (theme init in
#                                     <head>, explorer bridge payloads, chat/docs
#                                     modal+status scripts, dashboard capability
#                                     map) AND inline onclick= handlers (explorer
#                                     toolbar, toasts, cost dashboard). A nonce or
#                                     hash here would make CSP2+ browsers IGNORE
#                                     'unsafe-inline' and break all of those —
#                                     several payloads are per-request dynamic, so
#                                     static hashes cannot cover them, and nonces
#                                     cannot authorize event-handler attributes at
#                                     all. Tightening requires first externalizing
#                                     those islands (follow-up; out of #857 scope).
#                                     tests/test_security_headers.py carries a
#                                     tripwire that fails if a nonce/hash sneaks in
#                                     while 'unsafe-inline' is still load-bearing.
#     'unsafe-eval'                   htmx evaluates hx-trigger event filters
#                                     (e.g. keyup[key=='Enter'] in the draft list)
#                                     via Function(); without this the filter
#                                     throws under CSP and Enter-to-search dies.
#     https://cdn.jsdelivr.net        FastHTML default hdrs (htmx, fasthtml-js,
#                                     surreal, css-scope-inline) + chat (marked,
#                                     dompurify)
#     https://cdnjs.cloudflare.com    explorer D3 7.9.0
#
#   style-src 'self' 'unsafe-inline'  style= attributes (cost-dashboard progress
#                                     bars, explorer legend dots / display:none
#                                     toggles) and css-scope-inline <style> blocks
#   img-src 'self' data:              data:image/svg+xml select-chevron in ui.css
#   font-src 'self'                   Aino woff2 under /static/fonts
#   connect-src 'self' ws: wss:       HTMX XHR is same-origin; chat/explorer/
#                                     notifications WebSockets are built from
#                                     location.host — explicit ws:/wss: schemes
#                                     because Safari's 'self'-matches-ws upgrade
#                                     handling has been unreliable
#   object-src 'none', base-uri 'self', form-action 'self',
#   frame-ancestors 'none'            no plugins, no <base> pivots, no cross-site
#                                     form posts, no framing (+ X-Frame-Options
#                                     DENY for pre-CSP2 agents)
_CSP_POLICY = "; ".join(
    (
        "default-src 'self'",
        "script-src 'self' 'unsafe-inline' 'unsafe-eval' "
        "https://cdn.jsdelivr.net https://cdnjs.cloudflare.com",
        "style-src 'self' 'unsafe-inline'",
        "img-src 'self' data:",
        "font-src 'self'",
        "connect-src 'self' ws: wss:",
        "object-src 'none'",
        "base-uri 'self'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    )
)

# One year, no preload. ``includeSubDomains`` is safe: the app owns
# seadusloome.sixtyfour.ee and nothing is served from below it.
_HSTS_VALUE = "max-age=31536000; includeSubDomains"


def _is_prod_env() -> bool:
    """True when the normalized APP_ENV is ``production``.

    Evaluated PER REQUEST (env reads are cheap) so tests can flip
    ``APP_ENV`` with monkeypatch and observe HSTS without re-importing
    this module. Production is the only TLS-fronted environment
    (Traefik); emitting Strict-Transport-Security over plain-http local
    dev would poison the browser's HSTS cache for localhost for the
    whole max-age.
    """
    return get_app_env() == "production"


class SecurityHeadersMiddleware:
    """Stamp defensive headers on every HTTP response (#857).

    Pure ASGI (no BaseHTTPMiddleware) so streaming responses are not
    buffered and WebSocket scopes pass through untouched. Registered
    LAST → outermost, so even short-circuit responses from inner
    middleware (TrustedHost 400, OriginCheck 403) carry the headers.
    ``setdefault`` semantics: a route that needs a different value for
    a specific response can set its own header and win.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers.setdefault("Content-Security-Policy", _CSP_POLICY)
                headers.setdefault("X-Content-Type-Options", "nosniff")
                headers.setdefault("X-Frame-Options", "DENY")
                headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
                if _is_prod_env():
                    headers.setdefault("Strict-Transport-Security", _HSTS_VALUE)
            await send(message)

        await self.app(scope, receive, send_with_headers)


# Initialize Sentry before the ASGI app is created so that the Starlette
# integration can wrap the app and capture unhandled exceptions. No-op when
# SENTRY_DSN is unset.
from app.observability import init_sentry  # noqa: E402

init_sentry()

bware = Beforeware(auth_before, skip=SKIP_PATHS)
# pico=False: using custom design system (app/ui) instead of Pico CSS.
# lifespan=lifespan: FastHTML forwards this to Starlette so the
# background job worker starts/stops with the ASGI app.
# htmlkw={"lang": "et"}: set the document language to Estonian so
# Chrome's native validation bubbles, screen readers, and translation
# tools all use the correct locale (P1 from the 2026-04-29 UI review).
# sess_https_only (#857): mark the session cookie ``Secure`` in
# production — the only TLS-fronted environment. A hard ``True`` would
# break local http dev and the test suite outright: httpx/TestClient
# (http://testserver) and browsers both refuse to return ``Secure``
# cookies over plain http, which silently kills flash messages, the
# temp-password reveal, and the chat seed. Evaluated at import time
# because Starlette's SessionMiddleware fixes the flag at construction.
app, rt = fast_app(
    before=bware,
    pico=False,
    hdrs=_HDRS,
    lifespan=lifespan,
    htmlkw={"lang": "et"},
    sess_https_only=_is_prod_env(),
)

# Middleware order matters: Starlette's `add_middleware` prepends to the
# user_middleware list, so the LAST middleware added becomes the OUTERMOST
# (runs first on incoming requests). We want the request to hit
# ProxyHeadersMiddleware first so `scope['scheme']` is rewritten from the
# `X-Forwarded-Proto` header BEFORE any downstream middleware inspects the
# scheme — so TrustedHostMiddleware is added first and ProxyHeadersMiddleware
# second (#431).
#
# TrustedHostMiddleware is too strict for local dev (it rejects the
# `testserver` host used by Starlette's TestClient and plain `localhost`),
# so enable it only when we're running in a non-development environment.
if config.get_app_env() != "development":
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=[
            "seadusloome.sixtyfour.ee",
            "*.sixtyfour.ee",
            # Docker HEALTHCHECK in the Dockerfile calls
            # `curl http://localhost:5001/api/ping` from inside the running
            # container. That request carries `Host: localhost:5001`, which
            # TrustedHostMiddleware would otherwise reject with 400, causing
            # Coolify to mark the container unhealthy and roll back the
            # deploy. Allowing localhost/127.0.0.1 is safe because Traefik
            # never forwards external traffic with those Host headers —
            # only the in-container healthcheck can use them.
            "localhost",
            "127.0.0.1",
        ],
    )

# #851 (D2 + WS comment): CSRF origin verification for unsafe-method HTTP
# requests and an Origin allowlist for every /ws/* handshake. Added BEFORE
# ProxyHeadersMiddleware so it executes AFTER it (add_middleware prepends):
# by the time the check runs, scope['scheme'] and scope['client'] have been
# rewritten from X-Forwarded-* iff the peer is a trusted proxy — rejection
# logs therefore carry the validated client IP, and the request's own
# origin is computed from the real external scheme.
app.add_middleware(OriginCheckMiddleware)

# Trust X-Forwarded-* headers from the reverse proxy (Traefik/Coolify) so
# that request.url.scheme, request.client.host, etc. reflect the original
# client request rather than the proxy hop.
#
# #851 (D3): trust is restricted to TRUSTED_PROXY_HOSTS (default: loopback
# + RFC1918/Docker ranges, which cover the Coolify/Traefik bridge network).
# Previously trusted_hosts="*" let ANY direct client spoof X-Forwarded-For,
# bypassing IP rate limits and forging audit-log IPs.
app.add_middleware(ProxyHeadersMiddleware, trusted_hosts=get_trusted_proxy_hosts())

# Record per-request latency into the metrics table for the admin
# performance dashboard.  Added after ProxyHeadersMiddleware so that
# the request path is already resolved when the middleware fires.
from app.metrics_middleware import MetricsMiddleware  # noqa: E402

app.add_middleware(MetricsMiddleware)

# #857: security headers on EVERY HTTP response. Added LAST so it is the
# OUTERMOST middleware (add_middleware prepends) — responses produced by
# any inner middleware short-circuit (TrustedHost 400, OriginCheck 403,
# session redirects) are stamped too, as are /static files.
app.add_middleware(SecurityHeadersMiddleware)

# FastHTML adds a default static-file route at `/{fname:path}.{ext:static}`
# that serves from the current working directory. Our assets live under
# `app/static/` and are referenced via absolute URLs like
# `/static/css/tokens.css`, so we strip the default route and mount an
# explicit StaticFiles at /static pointing at the correct directory.
app.routes[:] = [r for r in app.routes if getattr(r, "name", None) != "static_route_exts_get"]
app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

register_auth_routes(rt)
register_profile_routes(rt)
register_org_routes(rt)
register_user_routes(rt)
register_explorer_routes(rt)
register_explorer_pages(rt)
register_ws_routes(app)
register_webhook_routes(app)
register_dashboard_routes(rt)
register_analyysikeskus_routes(rt)
register_admin_routes(rt)
register_validation_routes(rt)
register_design_system_routes(rt)
register_draft_routes(rt)
register_drafter_routes(rt)
register_report_routes(rt)
register_chat_routes(rt)
register_chat_ws_routes(app)
register_draft_ws_routes(app)
register_export_progress_ws_routes(app)
register_annotation_routes(rt)
register_notification_routes(rt)
register_notifications_ws_routes(app)
register_search_routes(rt)


@rt("/")
def index(req: Request):
    """GET / — route the visitor to the right landing page.

    Unauthenticated users are sent to the login page; authenticated users
    land on the operational dashboard (``/dashboard`` — the "Töölaud" work
    queue), not the Õiguskaart graph. See issue #746 / the design rationale
    in ``docs/2026-05-12-ui-plan-explorer-home.html``.
    """
    auth = req.scope.get("auth")
    if auth is None:
        return RedirectResponse(url="/auth/login", status_code=303)
    return RedirectResponse(url="/dashboard", status_code=303)


@rt("/api/ping", methods=["GET"])
def ping():
    """Lightweight liveness probe for Coolify/Docker HEALTHCHECK.

    Returns 200 OK without touching the database or Jena. Use `/api/health`
    for full readiness checks that include downstream dependencies.
    """
    return "ok"


# In production, uvicorn is invoked directly via the Dockerfile CMD, and
# this module is imported by it. Guard serve() so it only runs when the
# file is executed directly for local development
# (`python app/main.py` or `python -m app.main`). Note: FastHTML's serve()
# normally auto-checks __name__ internally, but that check fails for the
# uvicorn-imported case in our deployment layout, causing it to spawn a
# second uvicorn on the wrong module path.
if __name__ == "__main__":
    serve()  # noqa: F405
