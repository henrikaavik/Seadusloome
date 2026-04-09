"""Live validation endpoint used by FormField for HTMX on-blur checks.

A single generic route `POST /api/validate/{validator_name}` looks up the
validator by name from the registry and returns an HTMX partial that
replaces the error div for that field. The endpoint is unauthenticated so
it can be used on public forms like login and signup.
"""

from __future__ import annotations

import re

from fasthtml.common import *  # noqa: F403
from starlette.requests import Request
from starlette.responses import HTMLResponse

from app.ui.forms.validators import get_validator

# Identifier whitelist for validator and field names. Reject anything else
# with a 400 to keep the response payload free of attacker-controlled markup.
_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


async def _validate_handler(req: Request, validator_name: str) -> HTMLResponse:
    """POST /api/validate/{validator_name} — validate a single field."""
    if not _NAME_RE.match(validator_name):
        return HTMLResponse("Invalid validator name", status_code=400)

    validator = get_validator(validator_name)
    form = await req.form()
    # The field name in the form body matches the validator name by default
    # but the form can also include the actual field name as `field`.
    field_name_raw = form.get("field") or validator_name
    field_name = str(field_name_raw)
    if not _NAME_RE.match(field_name):
        return HTMLResponse("Invalid field name", status_code=400)

    value = form.get(field_name) or form.get("value") or ""
    # Coerce possible UploadFile / list to str
    value = str(value or "")

    error = validator(value) if validator else None

    error_id = f"{field_name}-error"
    # Build via FastHTML so all attribute/text content is auto-escaped.
    if error:
        div = Div(  # noqa: F405
            error,
            id=error_id,
            cls="form-field-error",
            role="alert",
        )
    else:
        div = Div(  # noqa: F405
            "",
            id=error_id,
            cls="form-field-error",
        )

    return HTMLResponse(to_xml(div))  # noqa: F405


def register_validation_routes(rt) -> None:  # type: ignore[no-untyped-def]
    """Mount the generic /api/validate/{name} endpoint."""

    @rt("/api/validate/{validator_name}", methods=["POST"])
    async def validate_field(req: Request, validator_name: str):
        return await _validate_handler(req, validator_name)
