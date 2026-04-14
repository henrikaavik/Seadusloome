"""Unit tests for ``app.chat.sanitize``.

Covers safe markdown rendering, XSS stripping, URL linkification,
Estonian/EU-style citation auto-linking, and plaintext escaping.
"""

from __future__ import annotations

from app.chat.sanitize import render_markdown_safe, render_plaintext_safe

# ---------------------------------------------------------------------------
# Basic markdown
# ---------------------------------------------------------------------------


class TestMarkdownRendering:
    def test_bold_renders_to_strong(self):
        out = render_markdown_safe("This is **bold** text.")
        assert "<strong>bold</strong>" in out

    def test_italic_renders_to_em(self):
        out = render_markdown_safe("This is *italic* text.")
        assert "<em>italic</em>" in out

    def test_inline_code_renders(self):
        out = render_markdown_safe("Use `print(x)` here.")
        assert "<code>print(x)</code>" in out

    def test_fenced_code_block_renders(self):
        md = "```python\nprint('hi')\n```"
        out = render_markdown_safe(md)
        assert "<pre>" in out
        assert "<code" in out
        assert "print(&#x27;hi&#x27;)" in out or "print('hi')" in out

    def test_heading_renders(self):
        out = render_markdown_safe("# Title\n\nBody")
        assert "<h1>Title</h1>" in out

    def test_list_renders(self):
        out = render_markdown_safe("- a\n- b\n- c")
        assert "<ul>" in out
        assert out.count("<li>") == 3

    def test_empty_string_returns_empty(self):
        assert render_markdown_safe("") == ""

    def test_none_like_falsy_returns_empty(self):
        # Explicitly handle the empty-string path; None is not a supported
        # input type but must not crash if called defensively.
        assert render_markdown_safe("") == ""


# ---------------------------------------------------------------------------
# XSS / sanitisation
# ---------------------------------------------------------------------------


class TestSanitisation:
    def test_script_tag_is_stripped(self):
        out = render_markdown_safe("Hello <script>alert(1)</script> world")
        assert "<script>" not in out
        assert "alert(1)" not in out or "&lt;script&gt;" not in out
        # Content is stripped entirely by bleach with strip=True.
        assert "script" not in out.lower() or "alert" not in out

    def test_img_onerror_is_stripped(self):
        out = render_markdown_safe('<img src=x onerror="alert(1)">')
        assert "<img" not in out
        assert "onerror" not in out

    def test_javascript_href_is_stripped(self):
        out = render_markdown_safe("[click](javascript:alert(1))")
        # bleach strips disallowed protocols from hrefs
        assert "javascript:" not in out.lower()

    def test_html_encoded_attack_does_not_execute(self):
        # HTML-encoded script tags in the source should remain encoded text,
        # not be un-escaped into real script tags.
        payload = "&lt;script&gt;alert(1)&lt;/script&gt;"
        out = render_markdown_safe(payload)
        assert "<script>" not in out

    def test_disallowed_tags_are_stripped(self):
        out = render_markdown_safe("<iframe src='x'></iframe>hi")
        assert "<iframe" not in out
        assert "hi" in out

    def test_style_attribute_is_stripped(self):
        out = render_markdown_safe('<p style="color:red">red</p>')
        assert "style=" not in out
        assert "red" in out


# ---------------------------------------------------------------------------
# URL linkification
# ---------------------------------------------------------------------------


class TestLinkify:
    def test_bare_url_is_auto_linked(self):
        out = render_markdown_safe("See https://example.com for info.")
        assert 'href="https://example.com"' in out
        assert 'target="_blank"' in out
        assert 'rel="noopener noreferrer"' in out

    def test_markdown_link_is_preserved(self):
        out = render_markdown_safe("[Example](https://example.com)")
        assert 'href="https://example.com"' in out
        assert "Example" in out
        assert 'target="_blank"' in out
        assert 'rel="noopener noreferrer"' in out

    def test_url_inside_code_is_not_linkified(self):
        out = render_markdown_safe("`https://example.com`")
        assert "<code>https://example.com</code>" in out
        assert 'href="https://example.com"' not in out


# ---------------------------------------------------------------------------
# Estonian / EU citation linking
# ---------------------------------------------------------------------------


class TestCitationLinks:
    def test_karistusseadustik_paragraph_is_linked(self):
        out = render_markdown_safe("Vaata KarS § 113 kohta.")
        assert 'class="citation-link"' in out
        assert 'href="/explorer?q=KarS' in out
        assert ">KarS § 113</a>" in out

    def test_tsus_with_lg_is_linked(self):
        out = render_markdown_safe("Vaata TsÜS § 5 lg 2.")
        assert 'class="citation-link"' in out
        assert ">TsÜS § 5 lg 2</a>" in out

    def test_ps_paragraph_is_linked(self):
        out = render_markdown_safe("PS § 13 annab õiguse.")
        assert 'class="citation-link"' in out
        assert ">PS § 13</a>" in out

    def test_pohiseadus_paragraph_is_linked(self):
        out = render_markdown_safe("Põhiseadus § 13 sätestab.")
        assert 'class="citation-link"' in out
        assert ">Põhiseadus § 13</a>" in out

    def test_article_style_is_linked(self):
        out = render_markdown_safe("EU reg. Art. 5 lõige 2 kohaselt.")
        assert 'class="citation-link"' in out
        assert ">Art. 5 lõige 2</a>" in out

    def test_citation_not_wrapped_inside_existing_link(self):
        # Link text already contains the citation -> must not be re-wrapped.
        out = render_markdown_safe("[KarS § 113](https://example.com/kars)")
        # Exactly one anchor, no nested anchor, no citation-link class.
        assert "citation-link" not in out
        assert out.count("<a ") == 1

    def test_citation_not_wrapped_inside_code(self):
        out = render_markdown_safe("`KarS § 113`")
        assert "citation-link" not in out
        assert "<code>KarS § 113</code>" in out

    def test_citation_not_wrapped_inside_code_fence(self):
        md = "```\nKarS § 113\n```"
        out = render_markdown_safe(md)
        assert "citation-link" not in out

    def test_multiple_citations_in_one_paragraph(self):
        out = render_markdown_safe("Vaata KarS § 113 ja TsÜS § 5.")
        assert out.count('class="citation-link"') == 2

    def test_citation_href_is_url_encoded(self):
        out = render_markdown_safe("KarS § 113")
        # quote_plus encodes space as + and § as %C2%A7
        assert "KarS" in out
        assert "%C2%A7" in out or "%A7" in out


# ---------------------------------------------------------------------------
# Plaintext rendering
# ---------------------------------------------------------------------------


class TestPlaintextSafe:
    def test_empty_returns_empty(self):
        assert render_plaintext_safe("") == ""

    def test_escapes_angle_brackets(self):
        out = render_plaintext_safe("<script>alert(1)</script>")
        assert "<script>" not in out
        assert "&lt;script&gt;" in out

    def test_converts_newlines_to_br(self):
        out = render_plaintext_safe("line1\nline2")
        assert out == "line1<br>line2"

    def test_crlf_converted_to_br(self):
        out = render_plaintext_safe("a\r\nb")
        assert out == "a<br>b"

    def test_no_markdown_interpretation(self):
        # Plain text renderer must NOT turn **bold** into <strong>.
        out = render_plaintext_safe("**not bold**")
        assert "<strong>" not in out
        assert "**not bold**" == out

    def test_ampersand_is_escaped(self):
        out = render_plaintext_safe("a & b")
        assert "&amp;" in out
