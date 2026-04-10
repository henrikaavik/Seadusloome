"""Notification system -- in-app notifications for Seadusloome.

This package provides:

    - ``Notification`` dataclass + CRUD helpers (models.py)
    - ``notify()`` fire-and-forget helper (notify.py)
    - FastHTML routes for the notification inbox + HTMX endpoints (routes.py)
    - Wire-up functions that call ``notify()`` from domain events (wire.py)
"""

from app.notifications.models import (
    Notification,
    count_unread,
    create_notification,
    list_notifications_for_user,
    mark_all_read,
    mark_read,
)
from app.notifications.notify import notify

__all__ = [
    "Notification",
    "count_unread",
    "create_notification",
    "list_notifications_for_user",
    "mark_all_read",
    "mark_read",
    "notify",
]
