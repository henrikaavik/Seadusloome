"""Smoke tests for Button and IconButton primitives."""

from typing import cast, get_args

import pytest
from fasthtml.common import to_xml

from app.ui.primitives.button import Button, ButtonSize, ButtonVariant, IconButton

_VARIANTS: tuple[ButtonVariant, ...] = cast(tuple[ButtonVariant, ...], get_args(ButtonVariant))
_SIZES: tuple[ButtonSize, ...] = cast(tuple[ButtonSize, ...], get_args(ButtonSize))


@pytest.mark.parametrize("variant", _VARIANTS)
def test_button_variants_render(variant: ButtonVariant):
    html = to_xml(Button("Salvesta", variant=variant))
    assert f"btn-{variant}" in html
    assert "Salvesta" in html
    assert "<button" in html


@pytest.mark.parametrize("size", _SIZES)
def test_button_sizes_render(size: ButtonSize):
    html = to_xml(Button("OK", size=size))
    assert f"btn-{size}" in html


def test_button_disabled_sets_attribute():
    html = to_xml(Button("Nope", disabled=True))
    assert "disabled" in html
    assert "btn-disabled" in html


def test_button_loading_shows_spinner_and_disables():
    html = to_xml(Button("Salvestan", loading=True))
    assert "btn-spinner" in html
    assert "disabled" in html
    assert "btn-disabled" in html


def test_button_icon_renders_svg_use_when_not_loading():
    """Buttons with ``icon=`` must render the real Icon primitive (#402).

    Previously a span placeholder was emitted; now the Icon component
    references the self-hosted Lucide sprite.
    """
    html = to_xml(Button("Lisa", icon="plus"))
    assert "<svg" in html
    assert "/static/icons/sprite.svg#plus" in html
    assert "icon icon-md" in html
    # Spinner is mutually exclusive with icon — must not appear here.
    assert "btn-spinner" not in html


def test_button_sm_icon_uses_sm_size():
    html = to_xml(Button("Add", icon="plus", size="sm"))
    assert "icon icon-sm" in html


def test_icon_button_renders_real_icon_svg():
    """IconButton must render the real Icon primitive too (#402)."""
    html = to_xml(IconButton("trash", aria_label="Kustuta"))
    assert "<svg" in html
    assert "/static/icons/sprite.svg#trash" in html


def test_button_custom_cls_is_appended_not_replaced():
    html = to_xml(Button("Hi", cls="extra-class"))
    assert "btn" in html
    assert "btn-primary" in html
    assert "extra-class" in html


def test_button_kwargs_pass_through():
    html = to_xml(Button("Save", hx_post="/api/save", hx_target="#out"))
    assert 'hx-post="/api/save"' in html
    assert 'hx-target="#out"' in html


def test_icon_button_renders_with_aria_label():
    html = to_xml(IconButton("x", aria_label="Sulge"))
    assert 'aria-label="Sulge"' in html
    assert "btn-icon" in html
    assert "btn-ghost" in html  # default variant


def test_icon_button_requires_aria_label():
    with pytest.raises(ValueError, match="aria_label"):
        IconButton("x", aria_label="")


def test_icon_button_custom_cls_appended():
    html = to_xml(IconButton("x", aria_label="Sulge", cls="close-btn"))
    assert "close-btn" in html
    assert "btn-icon" in html


def test_icon_button_kwargs_pass_through():
    html = to_xml(IconButton("trash", aria_label="Kustuta", hx_delete="/items/1"))
    assert 'hx-delete="/items/1"' in html
