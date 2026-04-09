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


serve()
