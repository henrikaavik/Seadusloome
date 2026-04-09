import os
from pathlib import Path

from fasthtml.common import *
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import Request
from starlette.staticfiles import StaticFiles
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app.auth.middleware import SKIP_PATHS, auth_before
from app.auth.organizations import register_org_routes
from app.auth.routes import register_auth_routes
from app.auth.users import register_user_routes
from app.explorer.pages import register_explorer_pages
from app.explorer.routes import register_explorer_routes
from app.explorer.websocket import register_ws_routes
from app.sync.webhook import register_webhook_routes
from app.templates.admin_dashboard import register_admin_routes
from app.templates.dashboard import index_redirect, register_dashboard_routes
from app.ui.design_system_pages import register_design_system_routes
from app.ui.forms.live_validation import register_validation_routes
from app.ui.primitives.button import Button  # noqa: F401  -- shadow guard #419
from app.ui.theme import THEME_INIT_SCRIPT

_STATIC_DIR = Path(__file__).parent / "static"

# Head tags hoisted into <head> by FastHTML. The theme init script must run
# before the first paint to avoid a flash of the wrong theme (FOUC); it is
# included here rather than in page handlers because inline Script() tags
# returned from handlers land in <body>, not <head>.
_HDRS = (
    Script(THEME_INIT_SCRIPT),
    Meta(charset="utf-8"),
    Meta(name="viewport", content="width=device-width, initial-scale=1"),
    Meta(name="color-scheme", content="light dark"),
    Link(rel="stylesheet", href="/static/css/fonts.css"),
    Link(rel="stylesheet", href="/static/css/tokens.css"),
    Link(rel="stylesheet", href="/static/css/ui.css"),
)

bware = Beforeware(auth_before, skip=SKIP_PATHS)
# pico=False: using custom design system (app/ui) instead of Pico CSS.
app, rt = fast_app(before=bware, pico=False, hdrs=_HDRS)

# Trust X-Forwarded-* headers from the reverse proxy (Traefik/Coolify) so
# that request.url.scheme, request.client.host, etc. reflect the original
# client request rather than the proxy hop.
app.add_middleware(ProxyHeadersMiddleware, trusted_hosts="*")

# TrustedHostMiddleware is too strict for local dev (it rejects the
# `testserver` host used by Starlette's TestClient and plain `localhost`),
# so enable it only when we're running in a non-development environment.
if os.environ.get("APP_ENV", "development") != "development":
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=["seadusloome.sixtyfour.ee", "*.sixtyfour.ee"],
    )

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


@rt("/")
def index(req: Request):
    return index_redirect(req)


@rt("/api/ping", methods=["GET"])
def ping():
    """Lightweight liveness probe for Coolify/Docker HEALTHCHECK.

    Returns 200 OK without touching the database or Jena. Use `/api/health`
    for full readiness checks that include downstream dependencies.
    """
    from starlette.responses import PlainTextResponse

    return PlainTextResponse("ok")


# In production, uvicorn is invoked directly via the Dockerfile CMD, and
# this module is imported by it. Guard serve() so it only runs when the
# file is executed directly for local development
# (`python app/main.py` or `python -m app.main`). Note: FastHTML's serve()
# normally auto-checks __name__ internally, but that check fails for the
# uvicorn-imported case in our deployment layout, causing it to spawn a
# second uvicorn on the wrong module path.
if __name__ == "__main__":
    serve()  # noqa: F405
