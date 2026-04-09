"""Smoke tests for form input primitives (Input, Textarea, Select, Checkbox, Radio)."""

from __future__ import annotations

from fasthtml.common import to_xml

from app.ui.primitives.input import Checkbox, Input, Radio, Select, Textarea


def _xml(ft) -> str:
    return to_xml(ft)


# ---------------------------------------------------------------------------
# Input
# ---------------------------------------------------------------------------


class TestInput:
    def test_renders_with_defaults(self):
        html = _xml(Input("email"))
        assert "<input" in html
        assert 'name="email"' in html
        assert 'type="text"' in html
        assert 'class="input"' in html

    def test_type_and_value(self):
        html = _xml(Input("email", type="email", value="a@b.ee", placeholder="E-post"))
        assert 'type="email"' in html
        assert 'value="a@b.ee"' in html
        assert 'placeholder="E-post"' in html

    def test_required_and_disabled(self):
        html = _xml(Input("name", required=True, disabled=True, readonly=True))
        assert "required" in html
        assert "disabled" in html
        assert "readonly" in html

    def test_error_sets_aria_invalid_and_class(self):
        html = _xml(Input("email", error=True))
        assert 'aria-invalid="true"' in html
        assert "input-error" in html

    def test_custom_cls_appended(self):
        html = _xml(Input("x", cls="my-extra"))
        assert "input" in html
        assert "my-extra" in html


# ---------------------------------------------------------------------------
# Textarea
# ---------------------------------------------------------------------------


class TestTextarea:
    def test_renders_with_rows(self):
        html = _xml(Textarea("bio", rows=6, placeholder="..."))
        assert "<textarea" in html
        assert 'name="bio"' in html
        assert 'rows="6"' in html
        assert "input-textarea" in html

    def test_value_is_body(self):
        html = _xml(Textarea("bio", value="hello"))
        assert ">hello</textarea>" in html

    def test_error_sets_aria_invalid(self):
        html = _xml(Textarea("bio", error=True))
        assert 'aria-invalid="true"' in html
        assert "input-error" in html


# ---------------------------------------------------------------------------
# Select
# ---------------------------------------------------------------------------


class TestSelect:
    def test_options_as_tuples(self):
        html = _xml(Select("lang", [("et", "Eesti"), ("en", "English")], value="et"))
        assert "<select" in html
        assert 'name="lang"' in html
        assert '<option value="et"' in html
        assert "selected" in html
        assert "Eesti" in html
        assert "English" in html

    def test_options_as_strings(self):
        html = _xml(Select("color", ["red", "green", "blue"]))
        assert '<option value="red"' in html
        assert '<option value="blue"' in html

    def test_disabled_and_error(self):
        html = _xml(Select("x", ["a"], disabled=True, error=True))
        assert "disabled" in html
        assert 'aria-invalid="true"' in html
        assert "input-error" in html


# ---------------------------------------------------------------------------
# Checkbox
# ---------------------------------------------------------------------------


class TestCheckbox:
    def test_unchecked_default(self):
        html = _xml(Checkbox("agree"))
        assert 'type="checkbox"' in html
        assert 'name="agree"' in html
        assert 'value="1"' in html
        assert "checked" not in html

    def test_checked_state(self):
        html = _xml(Checkbox("agree", checked=True))
        assert "checked" in html

    def test_label_wraps_in_check_label(self):
        html = _xml(Checkbox("agree", label="Nõustun"))
        assert "check-label" in html
        assert "Nõustun" in html
        assert "<label" in html

    def test_custom_cls(self):
        html = _xml(Checkbox("x", cls="extra"))
        assert "check-input" in html
        assert "extra" in html


# ---------------------------------------------------------------------------
# Radio
# ---------------------------------------------------------------------------


class TestRadio:
    def test_renders_with_value(self):
        html = _xml(Radio("size", "sm"))
        assert 'type="radio"' in html
        assert 'name="size"' in html
        assert 'value="sm"' in html

    def test_checked_and_label(self):
        html = _xml(Radio("size", "md", checked=True, label="Keskmine"))
        assert "checked" in html
        assert "Keskmine" in html
        assert "check-label" in html

    def test_disabled(self):
        html = _xml(Radio("size", "lg", disabled=True))
        assert "disabled" in html
