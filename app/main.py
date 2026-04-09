from fasthtml.common import *
from starlette.requests import Request

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

bware = Beforeware(auth_before, skip=SKIP_PATHS)
# pico=False: using custom design system (app/ui) instead of Pico CSS.
app, rt = fast_app(before=bware, pico=False)

# FastHTML adds a default static-file route at `/{fname:path}.{ext:static}`
# that serves from the current working directory. Our assets live under
# `app/static/` and are referenced via absolute URLs like
# `/static/css/tokens.css`, so we strip the default route and mount an
# explicit StaticFiles at /static pointing at the correct directory.
from starlette.staticfiles import StaticFiles  # noqa: E402

app.routes[:] = [r for r in app.routes if getattr(r, "name", None) != "static_route_exts_get"]
app.mount("/static", StaticFiles(directory="app/static"), name="static")

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


# In production, uvicorn is invoked directly via the Dockerfile CMD, and
# this module is imported by it. Guard serve() so it only runs when the
# file is executed directly for local development
# (`python app/main.py` or `python -m app.main`). Note: FastHTML's serve()
# normally auto-checks __name__ internally, but that check fails for the
# uvicorn-imported case in our deployment layout, causing it to spawn a
# second uvicorn on the wrong module path.
if __name__ == "__main__":
    serve()  # noqa: F405
