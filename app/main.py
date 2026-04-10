import logging
import os
import threading
from pathlib import Path

from fasthtml.common import *
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import Request
from starlette.staticfiles import StaticFiles
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app.admin import register_admin_routes
from app.annotations.routes import register_annotation_routes
from app.auth.middleware import SKIP_PATHS, auth_before
from app.auth.organizations import register_org_routes
from app.auth.routes import register_auth_routes
from app.auth.users import register_user_routes
from app.chat.routes import register_chat_routes
from app.chat.websocket import register_chat_ws_routes
from app.docs.report_routes import register_report_routes
from app.docs.routes import register_draft_routes
from app.drafter.routes import register_drafter_routes
from app.explorer.pages import explorer_page, register_explorer_pages
from app.explorer.routes import register_explorer_routes
from app.explorer.websocket import register_ws_routes
from app.notifications.routes import register_notification_routes
from app.sync.webhook import register_webhook_routes
from app.templates.dashboard import register_dashboard_routes
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
    global _worker_thread
    if os.environ.get("DISABLE_BACKGROUND_WORKER") == "1":
        logger.info("Background worker disabled via DISABLE_BACKGROUND_WORKER=1")
        yield
        return

    # Local import so tests that patch app.jobs.worker see the module
    # freshly re-imported if they reload after setting env vars.
    from app.jobs.worker import start_worker_thread

    _stop_worker.clear()
    _worker_thread = start_worker_thread(_stop_worker)
    logger.info("Background worker started")
    try:
        yield
    finally:
        logger.info("Stopping background worker...")
        _stop_worker.set()
        if _worker_thread is not None:
            _worker_thread.join(timeout=5.0)
            _worker_thread = None


# Head tags hoisted into <head> by FastHTML. The theme init script must run
# before the first paint to avoid a flash of the wrong theme (FOUC); it is
# included here rather than in page handlers because inline Script() tags
# returned from handlers land in <body>, not <head>.
# FastHTML's `fast_app(default_hdrs=True)` already injects `<meta charset>`
# and a viewport meta, so we do NOT add them here (#430 — duplicate meta
# tags are invalid HTML5).
_HDRS = (
    Script(THEME_INIT_SCRIPT),
    Meta(name="color-scheme", content="light dark"),
    Link(rel="stylesheet", href="/static/css/fonts.css"),
    Link(rel="stylesheet", href="/static/css/tokens.css"),
    Link(rel="stylesheet", href="/static/css/ui.css"),
)

# Initialize Sentry before the ASGI app is created so that the Starlette
# integration can wrap the app and capture unhandled exceptions. No-op when
# SENTRY_DSN is unset.
from app.observability import init_sentry  # noqa: E402

init_sentry()

bware = Beforeware(auth_before, skip=SKIP_PATHS)
# pico=False: using custom design system (app/ui) instead of Pico CSS.
# lifespan=lifespan: FastHTML forwards this to Starlette so the
# background job worker starts/stops with the ASGI app.
app, rt = fast_app(before=bware, pico=False, hdrs=_HDRS, lifespan=lifespan)

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
if os.environ.get("APP_ENV", "development") != "development":
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

# Trust X-Forwarded-* headers from the reverse proxy (Traefik/Coolify) so
# that request.url.scheme, request.client.host, etc. reflect the original
# client request rather than the proxy hop.
app.add_middleware(ProxyHeadersMiddleware, trusted_hosts="*")

# Record per-request latency into the metrics table for the admin
# performance dashboard.  Added after ProxyHeadersMiddleware so that
# the request path is already resolved when the middleware fires.
from app.metrics import MetricsMiddleware  # noqa: E402

app.add_middleware(MetricsMiddleware)

# FastHTML adds a default static-file route at `/{fname:path}.{ext:static}`
# that serves from the current working directory. Our assets live under
# `app/static/` and are referenced via absolute URLs like
# `/static/css/tokens.css`, so we strip the default route and mount an
# explicit StaticFiles at /static pointing at the correct directory.
app.routes[:] = [r for r in app.routes if getattr(r, "name", None) != "static_route_exts_get"]
app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

register_auth_routes(rt)
register_org_routes(rt)
register_user_routes(rt)
register_explorer_routes(rt)
register_explorer_pages(rt)
register_ws_routes(app)
register_webhook_routes(app)
register_dashboard_routes(rt)
register_admin_routes(rt)
register_validation_routes(rt)
register_design_system_routes(rt)
register_draft_routes(rt)
register_drafter_routes(rt)
register_report_routes(rt)
register_chat_routes(rt)
register_chat_ws_routes(app)
register_annotation_routes(rt)
register_notification_routes(rt)


@rt("/")
def index(req: Request):
    auth = req.scope.get("auth")
    if auth is None:
        return RedirectResponse(url="/auth/login", status_code=303)
    return explorer_page(req)


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
