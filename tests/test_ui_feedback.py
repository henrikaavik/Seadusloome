"""Smoke tests for feedback components: Toast, Spinner, Skeleton, EmptyState."""

import pytest
from fasthtml.common import Button, to_xml

from app.ui.feedback import (
    EmptyState,
    LoadingSpinner,
    Skeleton,
    Toast,
    ToastContainer,
)

# ---- Toast ----------------------------------------------------------------


def test_toast_renders_message():
    html = to_xml(Toast("Salvestatud"))
    assert "Salvestatud" in html
    assert "toast" in html
    assert 'role="status"' in html
    assert 'aria-live="polite"' in html


def test_toast_default_variant_is_info():
    assert "toast-info" in to_xml(Toast("hi"))


@pytest.mark.parametrize("variant", ["info", "success", "warning", "danger"])
def test_toast_variants(variant):
    html = to_xml(Toast("Teade", variant=variant))
    assert f"toast-{variant}" in html


def test_toast_with_title_and_duration():
    html = to_xml(Toast("Kasutaja loodud", title="Valmis", duration=3000))
    assert "Valmis" in html
    assert "toast-title" in html
    assert 'data-duration="3000"' in html


def test_toast_has_dismiss_button():
    html = to_xml(Toast("x"))
    assert "toast-dismiss" in html
    assert "Sulge teade" in html


def test_toast_container_wraps_children():
    html = to_xml(ToastContainer(Toast("a"), Toast("b")))
    assert 'id="toast-container"' in html
    assert html.count("toast-") >= 2


# ---- LoadingSpinner -------------------------------------------------------


@pytest.mark.parametrize("size", ["sm", "md", "lg"])
def test_spinner_sizes(size):
    html = to_xml(LoadingSpinner(size=size))
    assert f"loading-spinner-{size}" in html
    assert 'role="status"' in html
    assert "sr-only" in html
    assert "Laadimine" in html


def test_spinner_custom_aria_label():
    html = to_xml(LoadingSpinner(aria_label="Otsing..."))
    assert "Otsing..." in html


# ---- Skeleton -------------------------------------------------------------


@pytest.mark.parametrize("variant", ["text", "card", "avatar"])
def test_skeleton_variants(variant):
    html = to_xml(Skeleton(variant=variant))
    assert f"skeleton-{variant}" in html
    assert 'aria-busy="true"' in html
    assert 'aria-live="polite"' in html


# ---- EmptyState -----------------------------------------------------------


def test_empty_state_title_only():
    html = to_xml(EmptyState("Tulemusi ei leitud"))
    assert "Tulemusi ei leitud" in html
    assert "empty-state" in html


def test_empty_state_with_message_and_icon():
    html = to_xml(EmptyState("Pole andmeid", message="Proovi uuesti", icon="inbox"))
    assert "Proovi uuesti" in html
    assert "empty-state-icon" in html
    assert "inbox" in html


def test_empty_state_with_action():
    action = Button("Lisa uus", type="button")
    html = to_xml(EmptyState("Pole eelnõusid", action=action))
    assert "Lisa uus" in html
    assert "empty-state-action" in html
