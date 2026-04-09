"""Smoke tests for Modal, ConfirmModal and their subparts."""

from typing import cast

import pytest
from fasthtml.common import to_xml

from app.ui.surfaces.modal import (
    ConfirmModal,
    Modal,
    ModalBody,
    ModalFooter,
    ModalHeader,
    ModalScript,
    ModalSize,
)


def test_modal_renders_with_title_and_aria():
    html = to_xml(Modal(ModalBody("Sisu"), title="Kinnitus", id="m1"))
    assert 'role="dialog"' in html
    assert 'aria-modal="true"' in html
    assert 'aria-labelledby="m1-title"' in html
    assert 'id="m1-title"' in html
    assert "Kinnitus" in html
    assert "Sisu" in html
    assert 'id="m1"' in html
    assert "modal-root" in html
    assert "modal-backdrop" in html


def test_modal_dismissible_flag_toggles_close_button():
    with_close = to_xml(Modal(title="T", id="x"))
    without = to_xml(Modal(title="T", id="y", dismissible=False))
    assert "modal-close" in with_close
    assert 'aria-label="Sulge"' in with_close
    assert "modal-close" not in without
    assert 'data-modal-dismissible="false"' in without


@pytest.mark.parametrize("size", ["sm", "md", "lg", "full"])
def test_modal_sizes_render(size: str):
    html = to_xml(Modal(title="T", id="m", size=cast(ModalSize, size)))
    assert f"modal-{size}" in html


def test_modal_header_body_footer_classes():
    html = to_xml(ModalHeader("Pealkiri", id="t", dismissible=False))
    assert "modal-header" in html and "modal-title" in html
    assert "Pealkiri" in html and "modal-close" not in html
    assert "modal-body" in to_xml(ModalBody("x"))
    assert "modal-footer" in to_xml(ModalFooter("x"))


def test_confirm_modal_has_buttons_and_defaults():
    html = to_xml(ConfirmModal("Kustuta?", "Kas oled kindel?", id="c1"))
    assert "Kustuta?" in html
    assert "Kas oled kindel?" in html
    assert "Kinnita" in html
    assert "Tühista" in html
    assert "modal-sm" in html
    assert 'id="c1-confirm"' in html


def test_confirm_modal_custom_labels_and_variant():
    html = to_xml(
        ConfirmModal(
            "Kustuta",
            "Jäädavalt",
            id="c2",
            confirm_label="Jah",
            cancel_label="Ei",
            confirm_variant="danger",
        )
    )
    assert "Jah" in html and "Ei" in html and "btn-danger" in html


def test_modal_script_loads_js():
    html = to_xml(ModalScript())
    assert "/static/js/modal.js" in html
