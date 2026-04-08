from fasthtml.common import *

app, rt = fast_app()


@rt("/")
def index():
    return Titled(
        "Seadusloome",
        P("Estonian Legal Ontology Advisory Software"),
        P("System is running."),
    )


serve()
