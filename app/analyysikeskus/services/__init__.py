"""Framework-free service functions for Analüüsikeskus workflows (#860).

This package holds the **orchestration logic** for the Analüüsikeskus
workflows as plain ``input → typed result`` functions with **zero**
``fasthtml`` / ``starlette`` imports. They are the Phase-5 REST/MCP reference
pattern (CLAUDE.md: "Internal service functions should have clean signatures
that can be wrapped as both REST endpoints and MCP tools").

The convention (documented in
``docs/2026-06-12-service-layer-convention.md``):

* **Signature** — keyword-only domain inputs (``sisend: str``,
  ``org_id: str | None``), no ``Request`` / no FastHTML components.
* **Result** — a frozen ``@dataclass`` (or a small discriminated union of
  them via a ``kind`` field) carrying the typed outcome. Never an
  ``FT``/HTML node and never an HTTP ``Response``.
* **Errors** — surfaced as result fields / a result *kind* (e.g.
  ``"unresolved"``), or raised as typed domain exceptions — **never** an
  HTTP status. A dead Jena degrades to an empty / unresolved result, exactly
  as the routes already expect.
* **Composition** — the services compose the existing engine modules
  (``input_parser`` / ``adhoc_analysis`` / ``impact.*`` / ``eu_transposition``
  / ``eu_lookup`` / ``reference_resolver``); they do not duplicate them.

A FastHTML route wraps a service by: parse request → call service → render
the typed result through ``analysis_result_shell``. A Phase-5 REST endpoint
or MCP tool wraps the *same* service by serialising the dataclass to JSON.
"""

from app.analyysikeskus.services.el_ulevott import (
    ElTranspositionResult,
    ElUlevottDisambiguation,
    ElUlevottResult,
    ElUlevottUnresolved,
    analyse_el_ulevott,
)
from app.analyysikeskus.services.normi_mojuahel import (
    NormiAdhocResult,
    NormiDisambiguation,
    NormiDraftBackedResult,
    NormiResult,
    NormiUnresolved,
    analyse_normi_mojuahel,
)

__all__ = [
    # Normi mõjuahel
    "analyse_normi_mojuahel",
    "NormiResult",
    "NormiAdhocResult",
    "NormiDraftBackedResult",
    "NormiDisambiguation",
    "NormiUnresolved",
    # EL ülevõtt
    "analyse_el_ulevott",
    "ElUlevottResult",
    "ElTranspositionResult",
    "ElUlevottDisambiguation",
    "ElUlevottUnresolved",
]
