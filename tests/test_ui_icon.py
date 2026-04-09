"""Smoke tests for the Icon primitive (Lucide sprite wrapper)."""

from typing import cast, get_args

import pytest
from fasthtml.common import to_xml

from app.ui.primitives.icon import SPRITE_URL, Icon, IconSize

_SIZES: tuple[IconSize, ...] = cast(tuple[IconSize, ...], get_args(IconSize))


def test_icon_renders_svg_with_use_href():
    html = to_xml(Icon("check"))
    assert "<svg" in html
    assert "<use" in html
    assert f'href="{SPRITE_URL}#check"' in html


def test_icon_default_class_is_md():
    html = to_xml(Icon("check"))
    assert 'class="icon icon-md"' in html


@pytest.mark.parametrize("size", _SIZES)
def test_icon_size_variants(size: IconSize):
    html = to_xml(Icon("plus", size=size))
    assert f"icon-{size}" in html


def test_icon_is_aria_hidden_by_default():
    html = to_xml(Icon("plus"))
    assert 'aria-hidden="true"' in html
    assert "aria-label" not in html
    assert 'role="img"' not in html


def test_icon_with_aria_label_is_semantic():
    html = to_xml(Icon("alert-circle", aria_label="Viga"))
    assert 'aria-label="Viga"' in html
    assert 'role="img"' in html
    assert 'aria-hidden="true"' not in html


def test_icon_custom_cls_is_appended():
    html = to_xml(Icon("plus", cls="text-success"))
    assert "icon icon-md text-success" in html


def test_icon_kwargs_pass_through():
    html = to_xml(Icon("search", data_testid="search-icon"))
    assert 'data-testid="search-icon"' in html
