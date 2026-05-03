"""Annotation CRUD -- Phase 4.

This package contains dataclasses and query helpers for annotations and
annotation replies. Annotations can be attached to any target entity
(draft, provision, conversation, etc.) and are always org-scoped.

Routes and UI live in a separate ticket.
"""

from app.annotations.models import (
    VALID_ROW_KINDS,
    Annotation,
    AnnotationReply,
    create_annotation,
    create_reply,
    create_row_annotation,
    delete_annotation,
    get_annotation,
    list_annotations_for_target,
    list_annotations_for_version_row,
    list_replies,
    parse_mentions,
    reopen_row_thread,
    resolve_annotation,
    resolve_row_thread,
)

__all__ = [
    "VALID_ROW_KINDS",
    "Annotation",
    "AnnotationReply",
    "create_annotation",
    "create_reply",
    "create_row_annotation",
    "delete_annotation",
    "get_annotation",
    "list_annotations_for_target",
    "list_annotations_for_version_row",
    "list_replies",
    "parse_mentions",
    "reopen_row_thread",
    "resolve_annotation",
    "resolve_row_thread",
]
