"""Shared helpers for admin sub-modules."""

from __future__ import annotations

from fasthtml.common import *  # noqa: F403


def _tooltip(text: str):
    """Return a small (?) icon with a CSS-only hover tooltip."""
    return Span("?", cls="admin-tooltip", data_tooltip=text)  # noqa: F405
