"""Tests for ``app.ui.safe_url`` — the shared safe-href / open-redirect guard.

Issue #848 (P0, review ID C4): block stored XSS via ``javascript:`` (and
friends) URLs landing in ``href`` attributes we control, plus the related
protocol-relative / backslash-normalised open-redirect vectors.
"""

from __future__ import annotations

import pytest

from app.ui.safe_url import is_safe_http_url, quote_uri_param

# ---------------------------------------------------------------------------
# is_safe_http_url — accepted values
# ---------------------------------------------------------------------------


class TestIsSafeHttpUrlAccepts:
    @pytest.mark.parametrize(
        "url",
        [
            "https://example.org/entity/1",
            "http://example.org/entity/1",
            # Real ontology / legal identifiers use bare http and #fragments.
            "https://www.riigiteataja.ee/akt/13201407",
            "http://data.europa.eu/eli/dir/2016/680/oj",
            "https://example.org/ns#HKTS_Par_13",
            "https://example.org:8443/path?q=1&x=2#frag",
            "HTTPS://EXAMPLE.ORG/UPPER",  # scheme is case-insensitive
        ],
    )
    def test_valid_http_urls_accepted(self, url: str):
        assert is_safe_http_url(url) is True


# ---------------------------------------------------------------------------
# is_safe_http_url — rejected values (the attack matrix from the DoD)
# ---------------------------------------------------------------------------


class TestIsSafeHttpUrlRejects:
    @pytest.mark.parametrize(
        "url",
        [
            # Dangerous schemes
            "javascript:alert(1)",
            "JavaScript:alert(1)",
            "  javascript:alert(1)",  # leading whitespace + scheme
            "data:text/html,<script>alert(1)</script>",
            "data:text/html;base64,PHNjcmlwdD4=",
            "vbscript:msgbox(1)",
            "file:///etc/passwd",
            "mailto:evil@example.org",
            # Protocol-relative — browsers inherit page scheme, navigate off-site
            "//evil.example",
            "//evil.example/path",
            # Backslash variants — browsers normalise \ to /
            "/\\evil.example",
            "\\\\evil.example",
            "https:/\\evil.example",
            "https://good.example\\@evil.example",
            # Embedded control/whitespace that browsers strip before acting
            "java\tscript:alert(1)",
            "java\nscript:alert(1)",
            "http://exa mple.org/path",
            "https://example.org/\x00",
            # Relative / scheme-less
            "/dashboard",
            "dashboard",
            "example.org/path",
            "../../etc/passwd",
            "#frag",
            "?q=1",
            # No host
            "http://",
            "https:///path",
            # Empty-ish
            "",
            "   ",
            None,
        ],
    )
    def test_unsafe_values_rejected(self, url: str | None):
        assert is_safe_http_url(url) is False


# ---------------------------------------------------------------------------
# quote_uri_param
# ---------------------------------------------------------------------------


class TestQuoteUriParam:
    def test_encodes_reserved_characters(self):
        # &, #, ? must all be percent-encoded so they can't truncate a query.
        encoded = quote_uri_param("https://example.org/a?b=1&c#frag")
        assert "&" not in encoded
        assert "#" not in encoded
        assert "?" not in encoded
        assert "%3F" in encoded  # ?
        assert "%26" in encoded  # &
        assert "%23" in encoded  # #

    def test_encodes_slashes(self):
        # safe="" means even '/' is encoded — it's a single param value.
        assert quote_uri_param("https://example.org/x") == quote_uri_param("https://example.org/x")
        assert "/" not in quote_uri_param("https://example.org/x")

    def test_roundtrips_via_unquote(self):
        from urllib.parse import unquote

        uri = "https://example.org/ns#Par_13?x=1&y=2"
        assert unquote(quote_uri_param(uri)) == uri

    def test_empty_input_returns_empty(self):
        assert quote_uri_param("") == ""
