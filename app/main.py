from fasthtml.common import *

from app.auth.middleware import SKIP_PATHS, auth_before
from app.auth.routes import register_auth_routes

bware = Beforeware(auth_before, skip=SKIP_PATHS)
app, rt = fast_app(before=bware)

register_auth_routes(rt)


@rt("/")
def index():
    return Titled(
        "Seadusloome",
        P("Estonian Legal Ontology Advisory Software"),
        P("System is running."),
    )


serve()
