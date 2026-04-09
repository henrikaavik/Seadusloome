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


def test_button_icon_renders_placeholder_when_not_loading():
    html = to_xml(Button("Lisa", icon="plus"))
    assert "btn-icon-glyph" in html
    assert 'data-icon="plus"' in html


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
