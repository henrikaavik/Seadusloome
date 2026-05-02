"""Smoke tests for the LinkButton primitive (#632)."""

from typing import cast, get_args

import pytest
from fasthtml.common import to_xml

from app.ui.primitives.button import ButtonSize, ButtonVariant
from app.ui.primitives.link_button import LinkButton

_VARIANTS: tuple[ButtonVariant, ...] = cast(tuple[ButtonVariant, ...], get_args(ButtonVariant))
_SIZES: tuple[ButtonSize, ...] = cast(tuple[ButtonSize, ...], get_args(ButtonSize))


@pytest.mark.parametrize("variant", _VARIANTS)
def test_link_button_variants_render(variant: ButtonVariant):
    html = to_xml(LinkButton("Vaata", href="/x", variant=variant))
    assert f"btn-{variant}" in html
    assert "Vaata" in html
    assert "<a " in html
    assert 'href="/x"' in html


@pytest.mark.parametrize("size", _SIZES)
def test_link_button_sizes_render(size: ButtonSize):
    html = to_xml(LinkButton("Ava", href="/x", size=size))
    assert f"btn-{size}" in html


def test_link_button_default_is_primary_md():
    html = to_xml(LinkButton("Ava", href="/x"))
    assert "btn-primary" in html
    assert "btn-md" in html


def test_link_button_custom_cls_is_appended():
    html = to_xml(LinkButton("Hi", href="/x", cls="page-action"))
    assert "btn-primary" in html
    assert "page-action" in html


def test_link_button_kwargs_pass_through():
    html = to_xml(
        LinkButton(
            "Ava uurijas",
            href="/explorer?draft=1",
            title="Visualiseeri eelnõu",
            hx_boost="true",
        )
    )
    assert 'title="Visualiseeri eelnõu"' in html
    assert 'hx-boost="true"' in html


def test_link_button_with_icon_renders_svg():
    html = to_xml(LinkButton("Lisa", href="/new", icon="plus"))
    assert "<svg" in html
    assert "/static/icons/sprite.svg#plus" in html
    assert "icon icon-md" in html


def test_link_button_sm_icon_uses_sm_size():
    html = to_xml(LinkButton("Lisa", href="/new", icon="plus", size="sm"))
    assert "icon icon-sm" in html
