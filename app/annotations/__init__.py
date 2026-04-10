"""Annotation CRUD -- Phase 4.

This package contains dataclasses and query helpers for annotations and
annotation replies. Annotations can be attached to any target entity
(draft, provision, conversation, etc.) and are always org-scoped.

Routes and UI live in a separate ticket.
"""

from app.annotations.models import (
    Annotation,
    AnnotationReply,
    create_annotation,
    create_reply,
    delete_annotation,
    get_annotation,
    list_annotations_for_target,
    list_replies,
    resolve_annotation,
)

__all__ = [
    "Annotation",
    "AnnotationReply",
    "create_annotation",
    "create_reply",
    "delete_annotation",
    "get_annotation",
    "list_annotations_for_target",
    "list_replies",
    "resolve_annotation",
]
