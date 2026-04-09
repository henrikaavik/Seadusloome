"""Smoke tests for surface and badge components."""

from fasthtml.common import to_xml

from app.ui.primitives.badge import Badge, StatusBadge
from app.ui.surfaces import Alert, Card, CardBody, CardFooter, CardHeader

# ---- Card ----------------------------------------------------------------


def test_card_default_renders():
    html = to_xml(Card("content"))
    assert "card card-default" in html
    assert "content" in html


def test_card_bordered_variant():
    html = to_xml(Card("x", variant="bordered"))
    assert "card-bordered" in html


def test_card_flat_variant():
    html = to_xml(Card("x", variant="flat"))
    assert "card-flat" in html


def test_card_custom_cls_appended():
    html = to_xml(Card("x", cls="my-card"))
    assert "my-card" in html
    assert "card card-default" in html


def test_card_with_header_body_footer():
    html = to_xml(
        Card(
            CardHeader("Pealkiri"),
            CardBody("Sisu"),
            CardFooter("Jalus"),
        )
    )
    assert "card-header" in html
    assert "card-body" in html
    assert "card-footer" in html
    assert "Pealkiri" in html
    assert "Sisu" in html
    assert "Jalus" in html


# ---- Alert ---------------------------------------------------------------


def test_alert_info_default():
    html = to_xml(Alert("Teade"))
    assert "alert alert-info" in html
    assert 'role="alert"' in html
    assert "Teade" in html


def test_alert_success_variant():
    html = to_xml(Alert("ok", variant="success"))
    assert "alert-success" in html


def test_alert_warning_variant():
    html = to_xml(Alert("watch", variant="warning"))
    assert "alert-warning" in html


def test_alert_danger_variant():
    html = to_xml(Alert("boom", variant="danger"))
    assert "alert-danger" in html


def test_alert_with_title():
    html = to_xml(Alert("body", title="Pealkiri", variant="info"))
    assert "alert-title" in html
    assert "Pealkiri" in html
    assert "body" in html


def test_alert_dismissible():
    html = to_xml(Alert("msg", dismissible=True))
    assert "alert-dismiss" in html
    assert 'aria-label="Sulge"' in html


def test_alert_custom_cls():
    html = to_xml(Alert("msg", cls="pinned"))
    assert "pinned" in html


# ---- Badge ---------------------------------------------------------------


def test_badge_default():
    html = to_xml(Badge("12"))
    assert "badge badge-default" in html
    assert "12" in html


def test_badge_primary_variant():
    html = to_xml(Badge("new", variant="primary"))
    assert "badge-primary" in html


def test_badge_success_variant():
    html = to_xml(Badge("ok", variant="success"))
    assert "badge-success" in html


def test_badge_warning_variant():
    html = to_xml(Badge("hmm", variant="warning"))
    assert "badge-warning" in html


def test_badge_danger_variant():
    html = to_xml(Badge("err", variant="danger"))
    assert "badge-danger" in html


def test_badge_custom_cls():
    html = to_xml(Badge("x", cls="ml-2"))
    assert "ml-2" in html


# ---- StatusBadge ---------------------------------------------------------


def test_status_badge_ok():
    html = to_xml(StatusBadge("ok"))
    assert "status-badge" in html
    assert "badge-success" in html
    assert "OK" in html


def test_status_badge_running():
    html = to_xml(StatusBadge("running"))
    assert "badge-primary" in html
    assert "Töötab" in html


def test_status_badge_pending():
    html = to_xml(StatusBadge("pending"))
    assert "badge-default" in html
    assert "Ootel" in html


def test_status_badge_failed():
    html = to_xml(StatusBadge("failed"))
    assert "badge-danger" in html
    assert "Ebaõnnestus" in html


def test_status_badge_warning():
    html = to_xml(StatusBadge("warning"))
    assert "badge-warning" in html
    assert "Hoiatus" in html


def test_status_badge_has_dot():
    html = to_xml(StatusBadge("ok"))
    assert "status-dot" in html


def test_status_badge_custom_cls():
    html = to_xml(StatusBadge("ok", cls="ml-auto"))
    assert "ml-auto" in html
