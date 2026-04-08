from fasthtml.common import *

from app.auth.middleware import SKIP_PATHS, auth_before
from app.auth.organizations import register_org_routes
from app.auth.routes import register_auth_routes
from app.auth.users import register_user_routes

bware = Beforeware(auth_before, skip=SKIP_PATHS)
app, rt = fast_app(before=bware)

register_auth_routes(rt)
register_org_routes(rt)
register_user_routes(rt)


@rt("/")
def index():
    return Titled(
        "Seadusloome",
        P("Estonian Legal Ontology Advisory Software"),
        P("System is running."),
    )


serve()
