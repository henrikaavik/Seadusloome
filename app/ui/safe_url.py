"""Shared safe-URL helpers — block stored XSS and open-redirect vectors.

Issue #848 (P0, review ID C4): ``/api/bookmarks`` stored a user-controlled
``entity_uri`` without scheme validation and the dashboard rendered it as
``A(uri, href=uri)``. FastHTML escapes attribute *values* but does **not**
strip dangerous URL *schemes*, so a ``javascript:`` (or ``data:`` /
``vbscript:``) href becomes stored XSS the moment the dashboard loads. The
same class of bug lurks anywhere a stored/derived string lands in an ``href``
(notification links) or a deep-link query param (global search ``?focus=``).

This module is the single place that answers "is this URL safe to put in an
``href`` we control?". It deliberately stands alone from
``app.explorer.routes._validate_uri``:

    * ``explorer._validate_uri`` answers a *different* question — "is this
      string safe to interpolate into a ``<uri>`` slot inside a SPARQL query
      string?" (it guards against SPARQL injection, e.g. ``>``, ``{``, quotes).
      Its character allow-list happens to also reject ``javascript:`` because
      a colon-with-no-``//`` won't match ``^https?://…``, but its purpose,
      surface, and failure mode are SPARQL-shaped, not browser-href-shaped.
    * ``is_safe_http_url`` answers the *href* question and is deliberately
      stricter about the things browsers normalise (backslashes,
      protocol-relative ``//host``, embedded control/whitespace characters)
      which a SPARQL allow-list never has to think about.

Keeping the two policies separate (and documented) is intentional: a future
change to the SPARQL allow-list (e.g. to permit URN schemes) must not silently
widen what we are willing to render as a clickable link, and vice versa.
"""

from __future__ import annotations

from urllib.parse import quote, urlsplit

#: Schemes we are willing to emit into an ``href`` we control. Ontology /
#: entity URIs are ``http(s)://…`` absolute URLs; nothing else is expected.
#: ``http`` is allowed alongside ``https`` because the ontology source data
#: (and many Riigi Teataja / EUR-Lex identifiers) use bare ``http://`` URIs.
_ALLOWED_SCHEMES = frozenset({"http", "https"})


def is_safe_http_url(value: str | None) -> bool:
    """Return ``True`` only for a safe, absolute ``http(s)://`` URL.

    Safe means: it has an explicit ``http``/``https`` scheme **and** a host,
    contains no embedded whitespace/control characters, and uses no backslash
    (which browsers silently normalise to ``/``). Everything else is rejected,
    including:

    * dangerous schemes — ``javascript:``, ``data:``, ``vbscript:``, ``file:``,
      ``mailto:``, …;
    * protocol-relative URLs — ``//evil.example`` (browsers inherit the page
      scheme and navigate off-site);
    * backslash variants — ``/\\evil.example``, ``\\\\evil.example``,
      ``https:/\\evil.example`` (``\\`` → ``/`` in every major browser, so
      these become protocol-relative / off-site after normalisation);
    * relative or scheme-less values — ``/dashboard``, ``foo``, ``example.com``;
    * empty / whitespace-only / ``None``.

    The check is intentionally conservative: when in doubt, reject. Callers
    that want to *render* an unsafe legacy value should fall back to plain text
    rather than an anchor (defense in depth for data that predates this guard).

    Args:
        value: The candidate URL (typically user-controlled or derived from
            stored data / SPARQL results).

    Returns:
        ``True`` if the value is safe to use as an ``href`` target.
    """
    if not value:
        return False

    # Reject any control character (incl. tab / newline / NUL) or whitespace
    # *anywhere* in the string. Browsers strip tab/CR/LF from URLs before
    # acting on the scheme, so ``java\tscript:alert(1)`` would otherwise slip
    # an unsafe scheme past a naive ``startswith`` check. A legitimate absolute
    # http(s) URL never contains raw whitespace or control bytes (spaces are
    # percent-encoded as ``%20``).
    if any(ord(ch) <= 0x20 or ord(ch) == 0x7F for ch in value):
        return False

    # Backslashes are normalised to forward slashes by browsers, turning
    # ``/\evil`` and ``https:/\evil`` into protocol-relative / off-site URLs.
    # No valid http(s) URL needs a literal backslash, so reject outright.
    if "\\" in value:
        return False

    try:
        parts = urlsplit(value)
    except ValueError:
        # Malformed (e.g. bad IPv6 literal / port) — treat as unsafe.
        return False

    scheme = parts.scheme.lower()
    if scheme not in _ALLOWED_SCHEMES:
        # Covers ``javascript:``, ``data:``, protocol-relative ``//host``
        # (empty scheme), and bare relative paths (empty scheme).
        return False

    # Require a real host. ``http:///path`` or ``https://`` alone is not a
    # navigable absolute URL and should not be rendered as a link.
    if not parts.netloc or not parts.hostname:
        return False

    return True


def quote_uri_param(uri: str) -> str:
    """Percent-encode *uri* for safe use as a single URL query-param value.

    Encodes every reserved character (``safe=""``) so ``&``, ``#``, ``?`` and
    friends embedded in an entity URI cannot truncate or hijack the query
    string of a deep link such as ``/explorer?focus=<uri>``. This is *not* a
    security boundary on its own (FastHTML escapes attribute values), but it
    keeps deep links pointing at the intended target.

    Args:
        uri: The raw URI to embed as a query-param value.

    Returns:
        The percent-encoded value (empty string for falsy input).
    """
    if not uri:
        return ""
    return quote(uri, safe="")
