"""Form building blocks: FormField wrapper, validators, live validation."""

from app.ui.forms.app_form import AppForm
from app.ui.forms.form_field import FormField
from app.ui.forms.live_validation import register_validation_routes
from app.ui.forms.validators import (
    ValidationError,
    get_validator,
    register_validator,
    validate_email,
    validate_max_length,
    validate_min_length,
    validate_password_strength,
    validate_required,
    validate_url,
)

__all__ = [
    "AppForm",
    "FormField",
    "ValidationError",
    "get_validator",
    "register_validator",
    "register_validation_routes",
    "validate_email",
    "validate_max_length",
    "validate_min_length",
    "validate_password_strength",
    "validate_required",
    "validate_url",
]
