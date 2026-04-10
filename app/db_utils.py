"""Shared database utility helpers used across model modules."""

from __future__ import annotations

import json
import uuid
from typing import Any


def coerce_uuid(value: Any) -> uuid.UUID:
    """Return a ``UUID`` from either a string or a ``UUID`` instance."""
    if isinstance(value, uuid.UUID):
        return value
    return uuid.UUID(str(value))


def parse_jsonb(value: Any) -> Any:
    """Parse a JSONB value that psycopg may return as a string or dict/list."""
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return value
