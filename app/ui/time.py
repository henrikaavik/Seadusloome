"""Timestamp formatting in the project's canonical timezone (Europe/Tallinn).

The database stores timestamps in UTC (TIMESTAMPTZ) and the application
works in UTC internally. For display we convert to Europe/Tallinn so
officials see the times that match their own clock (UTC+2 in winter,
UTC+3 in summer — the zone handles DST automatically).

Usage::

    from app.ui.time import format_tallinn, TALLINN_TZ

    Dd(format_tallinn(draft.created_at))

The helpers tolerate naive datetimes (treated as UTC — matches the
psycopg default when a column's TZ info is dropped by the driver),
``None`` (renders an em-dash), and non-datetime inputs (stringified
as a fallback so a buggy caller can't crash the page).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

#: Canonical display timezone for the project (DST-aware).
TALLINN_TZ: ZoneInfo = ZoneInfo("Europe/Tallinn")

#: Standard display format: ``DD.MM.YYYY HH:MM`` — matches the admin
#: dashboard conventions already in use across the codebase.
_DEFAULT_FMT = "%d.%m.%Y %H:%M"

#: Em-dash fallback for ``None`` values.
_DASH = "\u2014"


def to_tallinn(value: datetime) -> datetime:
    """Convert a ``datetime`` to ``Europe/Tallinn``.

    Naive datetimes are treated as UTC (our storage convention). Aware
    datetimes are converted via ``astimezone``.
    """
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(TALLINN_TZ)


def format_tallinn(value: Any, fmt: str = _DEFAULT_FMT) -> str:
    """Format a ``datetime`` in Europe/Tallinn using the given ``strftime``.

    Non-datetime values fall through: ``None`` renders as an em-dash,
    anything else is stringified. This makes the helper safe to call on
    dict values coming from the DB layer without type-guarding each
    call site.
    """
    if value is None:
        return _DASH
    if isinstance(value, datetime):
        return to_tallinn(value).strftime(fmt)
    try:
        # pandas/psycopg/Timestamp subclasses without isinstance(datetime) hit.
        return to_tallinn(value).strftime(fmt)  # type: ignore[arg-type]
    except Exception:
        try:
            return value.strftime(fmt)  # type: ignore[attr-defined]
        except Exception:
            return str(value)


def now_tallinn() -> datetime:
    """Return the current time in ``Europe/Tallinn``."""
    return datetime.now(UTC).astimezone(TALLINN_TZ)
