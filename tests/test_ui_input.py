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


# ---------------------------------------------------------------------------
# #813 — HTML4 string form survives the FastHTML 0.13.3 HTTP renderer
# ---------------------------------------------------------------------------
#
# The HTTP-response renderer silently drops bool-true HTML attributes
# (verified live post-deploy in commit da51a5d). ``to_xml()`` keeps
# them, which is why earlier ``to_xml``-based tests passed while the
# rendered page was broken. The HTML4-compatible string form
# (``required="required"`` etc.) round-trips through every serializer
# path; the primitives in ``app/ui/primitives/input.py`` are the
# single source of truth and write the string form before calling
# ``ft_hx``.
#
# These tests pin the string form so a future contributor doesn't
# revert to bool-true based on "looks the same locally" — the trap
# only fires through the full HTTP renderer (see test_auth_routes.py
# for the integration-level pin).


class TestBoolAttrStringForm813:
    def test_input_required_emits_string_form(self):
        html = _xml(Input("email", required=True))
        assert 'required="required"' in html

    def test_input_disabled_emits_string_form(self):
        html = _xml(Input("x", disabled=True))
        assert 'disabled="disabled"' in html

    def test_input_readonly_emits_string_form(self):
        html = _xml(Input("x", readonly=True))
        assert 'readonly="readonly"' in html

    def test_textarea_required_emits_string_form(self):
        html = _xml(Textarea("bio", required=True))
        assert 'required="required"' in html

    def test_textarea_disabled_emits_string_form(self):
        html = _xml(Textarea("bio", disabled=True))
        assert 'disabled="disabled"' in html

    def test_select_disabled_emits_string_form(self):
        html = _xml(Select("x", ["a", "b"], disabled=True))
        assert 'disabled="disabled"' in html

    def test_select_required_emits_string_form(self):
        html = _xml(Select("x", ["a", "b"], required=True))
        assert 'required="required"' in html

    def test_select_option_selected_emits_string_form(self):
        html = _xml(Select("x", [("a", "A"), ("b", "B")], value="a"))
        # Selected option uses HTML4 string form; non-selected option
        # has no ``selected`` attribute at all. The exact substring
        # ``selected="selected"`` appears exactly once (matching the
        # one selected option). ``b`` must render with no ``selected``
        # attribute at all so the browser does not mark both selected.
        assert 'selected="selected"' in html
        assert html.count('selected="selected"') == 1
        assert '<option value="b">B</option>' in html

    def test_select_option_no_selection_when_no_value(self):
        html = _xml(Select("x", [("a", "A"), ("b", "B")]))
        # When ``value`` is None, no option should be marked selected.
        assert "selected" not in html

    def test_checkbox_checked_emits_string_form(self):
        html = _xml(Checkbox("agree", checked=True))
        assert 'checked="checked"' in html

    def test_checkbox_disabled_emits_string_form(self):
        html = _xml(Checkbox("agree", disabled=True))
        assert 'disabled="disabled"' in html

    def test_radio_checked_emits_string_form(self):
        html = _xml(Radio("size", "md", checked=True))
        assert 'checked="checked"' in html

    def test_radio_disabled_emits_string_form(self):
        html = _xml(Radio("size", "md", disabled=True))
        assert 'disabled="disabled"' in html

    def test_bool_attrs_omitted_when_false(self):
        """When the bool kwarg is False, the attribute must not appear
        at all in the output — not as ``required=""`` or any other
        zombie form. This pins the ``if X:`` guard in each primitive."""
        html = _xml(Input("x", required=False, disabled=False, readonly=False))
        assert "required" not in html
        assert "disabled" not in html
        assert "readonly" not in html

        html2 = _xml(Checkbox("x", checked=False, disabled=False))
        assert "checked" not in html2
        assert "disabled" not in html2
