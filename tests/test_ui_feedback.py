"""Smoke tests for feedback components: Toast, Spinner, Skeleton, EmptyState."""

from types import SimpleNamespace
from typing import cast

import pytest
from fasthtml.common import Button, to_xml
from starlette.requests import Request

from app.ui.feedback import (
    EmptyState,
    LoadingSpinner,
    Skeleton,
    Toast,
    ToastContainer,
    pop_flashes,
    push_flash,
    render_flash_toasts,
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


# ---- Flash messages (#598) ------------------------------------------------


def _fake_request_with_session() -> Request:
    """Build a stand-in Request object exposing a ``.session`` dict.

    ``push_flash`` / ``pop_flashes`` only touch ``request.session``, so
    any object with that attribute is sufficient for unit testing.
    The cast keeps pyright quiet at the call sites.
    """
    return cast(Request, SimpleNamespace(session={}))


def test_push_flash_queues_entry():
    req = _fake_request_with_session()
    push_flash(req, "Salvestatud.", kind="success")
    assert req.session["flash"] == [{"kind": "success", "msg": "Salvestatud."}]


def test_push_flash_accumulates_in_order():
    req = _fake_request_with_session()
    push_flash(req, "Esimene.", kind="success")
    push_flash(req, "Teine.", kind="danger")
    assert [e["msg"] for e in req.session["flash"]] == ["Esimene.", "Teine."]


def test_pop_flashes_drains_session():
    req = _fake_request_with_session()
    push_flash(req, "Teade.", kind="info")
    out = pop_flashes(req)
    assert out == [{"kind": "info", "msg": "Teade."}]
    # Session key is cleared so a second call returns nothing.
    assert pop_flashes(req) == []
    assert "flash" not in req.session


def test_pop_flashes_ignores_malformed_entries():
    req = _fake_request_with_session()
    req.session["flash"] = [
        {"kind": "success", "msg": "ok"},
        "bogus-string",
        {"msg": ""},  # empty message dropped
        {"kind": "unknown", "msg": "coerced to info"},
    ]
    out = pop_flashes(req)
    assert len(out) == 2
    assert out[0]["msg"] == "ok"
    assert out[1]["kind"] == "info"


def test_push_flash_no_session_is_noop():
    # Request-like object without a session attribute — must not raise.
    class NoSession:
        @property
        def session(self):
            raise AssertionError("no session middleware")

    push_flash(NoSession(), "ignored.", kind="info")  # type: ignore[arg-type]
    assert pop_flashes(NoSession()) == []  # type: ignore[arg-type]


def test_render_flash_toasts_produces_toast_components():
    req = _fake_request_with_session()
    push_flash(req, "Eelnõu kustutatud.", kind="success")
    toasts = render_flash_toasts(req)
    assert len(toasts) == 1
    html = to_xml(toasts[0])
    assert "Eelnõu kustutatud." in html
    assert "toast-success" in html
    # Consumed — subsequent render returns nothing.
    assert render_flash_toasts(req) == []
