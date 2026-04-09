"""Live validation endpoint used by FormField for HTMX on-blur checks.

A single generic route `POST /api/validate/{validator_name}` looks up the
validator by name from the registry and returns an HTMX partial that
replaces the error div for that field. The endpoint is unauthenticated so
it can be used on public forms like login and signup.
"""

from __future__ import annotations

from fasthtml.common import *  # noqa: F403
from starlette.requests import Request
from starlette.responses import HTMLResponse

from app.ui.forms.validators import get_validator


async def _validate_handler(req: Request, validator_name: str) -> HTMLResponse:
    """POST /api/validate/{validator_name} — validate a single field."""
    validator = get_validator(validator_name)
    form = await req.form()
    # The field name in the form body matches the validator name by default
    # but the form can also include the actual field name as `field`.
    field_name = form.get("field") or validator_name  # type: ignore[assignment]
    value = form.get(field_name) or form.get("value") or ""  # type: ignore[assignment]
    # Coerce possible UploadFile / list to str
    value = str(value or "")

    error = validator(value) if validator else None

    error_id = f"{field_name}-error"
    if error:
        html = f'<div id="{error_id}" class="form-field-error" role="alert">{error}</div>'
    else:
        html = f'<div id="{error_id}" class="form-field-error"></div>'

    return HTMLResponse(html)


def register_validation_routes(rt) -> None:  # type: ignore[no-untyped-def]
    """Mount the generic /api/validate/{name} endpoint."""

    @rt("/api/validate/{validator_name}", methods=["POST"])
    async def validate_field(req: Request, validator_name: str):
        return await _validate_handler(req, validator_name)
