"""Auth perimeter hardening: CSRF origin check, WS origin allowlist, proxy trust.

Issue #851 (review findings D2 + D3 and the WS-hijack comment) in one
enforcement point:

**CSRF via origin verification (D2).** Instead of per-form token
plumbing, :class:`OriginCheckMiddleware` verifies browser *provenance*
headers on every unsafe-method HTTP request (POST/PUT/PATCH/DELETE):

1. ``Origin`` present → must equal one of the allowed origins
   (the request's own ``scheme://host`` and the origin of
   ``APP_BASE_URL``). Browsers attach ``Origin`` to **every**
   cross-origin POST — fetch, XHR, and auto-submitted ``<form>`` alike —
   so a forged cross-site request always carries the attacker's origin
   and is rejected here with 403.
2. No ``Origin`` → fall back to ``Referer``'s origin, same comparison.
3. Neither → fall back to ``Sec-Fetch-Site``; only ``same-origin`` and
   ``none`` (direct navigation) pass — ``same-site`` (sibling
   subdomains) and ``cross-site`` are rejected. The app is strictly
   single-origin, so a bare same-site signal is insufficient.
4. None of the three headers → the request cannot have been initiated
   by a (modern) browser from a foreign site; it is allowed. This keeps
   server-to-server callers, curl, and test clients working. CSRF is
   strictly a browser-credential attack, and browser-issued cross-site
   requests are guaranteed to carry positive evidence (header 1 or 3).

This is HTMX-compatible with zero per-form changes: HTMX requests are
same-origin fetches and carry a matching ``Origin``/``Sec-Fetch-Site``.

**WebSocket origin allowlist (cross-site WS hijacking).** Browsers do
not apply SOP to WebSocket handshakes, but they DO send ``Origin``. All
``/ws/*`` channels authenticate via cookies, so a foreign page could
otherwise open an authenticated socket. The same middleware validates
``Origin`` on ``websocket`` scopes and rejects the handshake (close
code 1008) before the app ever accepts it — one enforcement point
instead of edits to the five hand-rolled WS modules.

**Trusted proxy ranges (D3).** :func:`get_trusted_proxy_hosts` feeds
uvicorn's ``ProxyHeadersMiddleware`` so ``X-Forwarded-For`` /
``X-Forwarded-Proto`` are only honoured when the direct peer is the
Traefik/Coolify proxy (private/Docker ranges by default, overridable
via ``TRUSTED_PROXY_HOSTS``). Everything downstream — login throttling,
audit logging, rejection logs — then reads a *validated* client IP via
:func:`client_ip`.

Escape hatch: ``CSRF_ORIGIN_CHECK=off`` disables the HTTP and WS origin
checks for emergencies (default: enforced).
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any
from urllib.parse import urlsplit

from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import HTMLResponse

from app.auth.middleware import _TOKEN_BEARER_PATHS

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Trusted proxy configuration (D3)
# ---------------------------------------------------------------------------

# Default trust: loopback + RFC1918 ranges. Coolify runs the app and
# Traefik on a Docker bridge network, so the direct peer of the app
# container is the proxy's container address (typically 10.x / 172.x).
# A real client connecting directly (i.e. not through Traefik) gets a
# public source address, falls outside these ranges, and its
# X-Forwarded-* headers are ignored — so it cannot spoof its IP for the
# login throttle or the audit log.
DEFAULT_TRUSTED_PROXY_HOSTS = (
    "127.0.0.1",
    "::1",
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
)


def get_trusted_proxy_hosts() -> list[str]:
    """Return the proxy hosts/CIDRs whose X-Forwarded-* headers are trusted.

    Reads ``TRUSTED_PROXY_HOSTS`` (comma-separated IPs / CIDR networks).
    Defaults to :data:`DEFAULT_TRUSTED_PROXY_HOSTS`.

    Wildcards are REFUSED, not honoured (#851 review round 1): a value
    containing ``*`` anywhere (``*``, ``10.*``, ``*.example.com``) is a
    misconfiguration that would reopen the exact D3 hole this module
    closes — ``trusted_hosts=["*"]`` flips uvicorn's
    ``_TrustedHosts.always_trust`` and makes ``X-Forwarded-For`` fully
    spoofable again. We log at ERROR and fall back to the private-range
    defaults instead of crashing at startup: the defaults are the
    correct production posture behind Traefik/Coolify, so degrading
    keeps the service available while the error log (and Sentry) makes
    the bad value visible. Globs are not supported by uvicorn anyway —
    any non-``*`` wildcard entry would silently never match.
    """
    raw = os.environ.get("TRUSTED_PROXY_HOSTS", "").strip()
    if not raw:
        return list(DEFAULT_TRUSTED_PROXY_HOSTS)
    hosts = [item.strip() for item in raw.split(",") if item.strip()]
    if any("*" in host for host in hosts):
        logger.error(
            "TRUSTED_PROXY_HOSTS=%r contains a wildcard entry — REFUSING it. "
            "Trusting '*' would let any direct client spoof X-Forwarded-For "
            "(issue #851 D3: throttle bypass + audit-IP forging). Falling "
            "back to the private-range defaults %s; set explicit IPs/CIDRs "
            "to override.",
            raw,
            list(DEFAULT_TRUSTED_PROXY_HOSTS),
        )
        return list(DEFAULT_TRUSTED_PROXY_HOSTS)
    return hosts or list(DEFAULT_TRUSTED_PROXY_HOSTS)


def client_ip(req: Request) -> str:
    """Return the validated client IP for throttling/audit purposes.

    After ProxyHeaders hardening (D3) ``req.client.host`` reflects the
    real client when the request came through a trusted proxy, and the
    direct peer otherwise — it can no longer be forged via
    ``X-Forwarded-For`` from untrusted sources.
    """
    if req.client is not None and req.client.host:
        return str(req.client.host)
    return "unknown"


# ---------------------------------------------------------------------------
# CSRF origin verification (D2) + WS origin allowlist
# ---------------------------------------------------------------------------

_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# Paths exempt from the HTTP origin check (matched with ``re.fullmatch``,
# same semantics as SKIP_PATHS). Each exemption is justified in place:
CSRF_EXEMPT_PATHS: list[str] = [
    # GitHub push webhook — server-to-server, authenticated by an HMAC
    # signature over the body (app/sync/webhook.py::verify_signature),
    # never carries browser cookies, never sends an Origin header.
    r"/webhooks/github",
    # Health endpoints — GET-only in practice, but kept here so a probe
    # implementation change can never lock out the Coolify healthcheck.
    r"/api/health",
    r"/api/ping",
    # Signed-URL downloads (#307) — bearer credential in the ``?token=``
    # query param, validated by the handler. GET-only today (so already
    # outside _UNSAFE_METHODS); listed for belt-and-braces parity with
    # the auth middleware's bypass list.
    *_TOKEN_BEARER_PATHS,
]

# Sec-Fetch-Site values accepted when it is the DECIDING signal (i.e.
# Origin and Referer are both absent). ``same-origin`` covers in-app
# requests; ``none`` covers direct user navigations (address bar,
# bookmark). ``same-site`` is deliberately ABSENT (#851 review round 1):
# accepting it would extend write trust to every sibling subdomain of
# the registrable domain (anything under sixtyfour.ee), and this app is
# strictly single-origin. If a legitimate sibling-subdomain flow ever
# appears, the revert is one word: add "same-site" back to this set.
_ALLOWED_SEC_FETCH_SITE = frozenset({"same-origin", "none"})

# Estonian rejection body. Plain page on purpose: this is shown to
# browsers only in attack/misconfiguration scenarios.
_REJECT_BODY_ET = (
    "<!doctype html><html lang='et'><head><meta charset='utf-8'>"
    "<title>403 — Keelatud</title></head><body>"
    "<h1>403 — Keelatud</h1>"
    "<p>Päringu päritolu ei vasta lubatud aadressile (CSRF-kaitse). "
    "Palun avage leht uuesti rakenduse aadressilt ja proovige uuesti.</p>"
    "</body></html>"
)


def is_origin_check_enabled() -> bool:
    """Return True unless ``CSRF_ORIGIN_CHECK`` disables the check.

    Enforced by default; ``off``/``0``/``false``/``no``/``disabled``
    (case-insensitive) switch it off — an emergency escape hatch only.
    Read per-request so a container restart is not required to flip it
    in tests; the cost is one env lookup.
    """
    raw = os.environ.get("CSRF_ORIGIN_CHECK", "").strip().lower()
    return raw not in {"off", "0", "false", "no", "disabled"}


def _origin_of_url(url: str) -> str | None:
    """Return the lowercased ``scheme://host[:port]`` origin of *url*."""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return None
    if not parts.scheme or not parts.netloc:
        return None
    return f"{parts.scheme.lower()}://{parts.netloc.lower()}"


def _request_own_origin(scope: dict[str, Any]) -> str | None:
    """Origin of the request target itself: ``scheme://Host-header``.

    The scheme comes from the ASGI scope, which ProxyHeadersMiddleware
    has already corrected from ``X-Forwarded-Proto`` when (and only
    when) the peer is a trusted proxy. WS schemes map onto their HTTP
    equivalents because the browser's ``Origin`` header is always
    ``http(s)://``.
    """
    host = Headers(scope=scope).get("host")
    if not host:
        return None
    scheme = str(scope.get("scheme") or "http").lower()
    scheme = {"ws": "http", "wss": "https"}.get(scheme, scheme)
    return f"{scheme}://{host.strip().lower()}"


def allowed_origins(scope: dict[str, Any]) -> set[str]:
    """The set of origins allowed to issue mutating/WS requests.

    The request's own origin (Host header + validated scheme) plus the
    origin of ``APP_BASE_URL``. The latter keeps production working even
    if scheme detection degrades (e.g. proxy trust misconfigured →
    scope scheme stays ``http`` while browsers send the canonical
    ``https://…`` Origin).
    """
    allowed: set[str] = set()
    own = _request_own_origin(scope)
    if own:
        allowed.add(own)
    base = os.environ.get("APP_BASE_URL", "").strip()
    if base:
        base_origin = _origin_of_url(base)
        if base_origin:
            allowed.add(base_origin)
    return allowed


def is_csrf_exempt(path: str) -> bool:
    """True when *path* is exempt from the HTTP origin check."""
    return any(re.fullmatch(pattern, path) for pattern in CSRF_EXEMPT_PATHS)


def evaluate_http_request(scope: dict[str, Any]) -> str | None:
    """Return a rejection reason for an unsafe HTTP request, or None.

    Decision ladder (first available signal wins — see module docstring
    for why absent-everything is allowed):
    Origin → Referer → Sec-Fetch-Site → allow.
    """
    headers = Headers(scope=scope)
    allowed = allowed_origins(scope)

    origin = headers.get("origin")
    if origin:
        # ``Origin: null`` (sandboxed iframes, some privacy modes) is
        # deliberately rejected: it cannot be proven same-origin.
        if origin.strip().lower() in allowed:
            return None
        return f"Origin {origin!r} not in allowed origins {sorted(allowed)}"

    referer = headers.get("referer")
    if referer:
        ref_origin = _origin_of_url(referer)
        if ref_origin and ref_origin in allowed:
            return None
        return f"Referer {referer!r} not same-origin (allowed {sorted(allowed)})"

    fetch_site = headers.get("sec-fetch-site")
    if fetch_site:
        if fetch_site.strip().lower() in _ALLOWED_SEC_FETCH_SITE:
            return None
        return f"Sec-Fetch-Site {fetch_site!r}"

    # No browser provenance headers → not a cross-site browser request.
    return None


def evaluate_ws_handshake(scope: dict[str, Any]) -> str | None:
    """Return a rejection reason for a WS handshake, or None.

    Browsers always send ``Origin`` on WebSocket handshakes; a present
    but foreign Origin is the cross-site WS hijack signature. Absent
    Origin means a non-browser client, which holds its own credentials
    legitimately (no ambient-cookie confusion to exploit).
    """
    origin = Headers(scope=scope).get("origin")
    if not origin:
        return None
    if origin.strip().lower() in allowed_origins(scope):
        return None
    return f"Origin {origin!r} not in allowed origins"


class OriginCheckMiddleware:
    """Pure ASGI middleware enforcing the origin checks described above.

    Added in ``app/main.py`` *inside* ProxyHeadersMiddleware (so the
    scheme/client have already been validated) and outside the FastHTML
    beforeware (so rejections fire before any auth/handler code,
    including the ``app.ws()`` handshakes that beforeware never sees).
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        scope_type = scope.get("type") if isinstance(scope, dict) else None

        if scope_type == "http" and is_origin_check_enabled():
            method = str(scope.get("method", "GET")).upper()
            path = str(scope.get("path", ""))
            if method in _UNSAFE_METHODS and not is_csrf_exempt(path):
                reason = evaluate_http_request(scope)
                if reason is not None:
                    client = scope.get("client")
                    logger.warning(
                        "CSRF origin check rejected %s %s from %s: %s",
                        method,
                        path,
                        client[0] if client else "unknown",
                        reason,
                    )
                    response = HTMLResponse(_REJECT_BODY_ET, status_code=403)
                    await response(scope, receive, send)
                    return

        elif scope_type == "websocket" and is_origin_check_enabled():
            reason = evaluate_ws_handshake(scope)
            if reason is not None:
                client = scope.get("client")
                logger.warning(
                    "WS origin check rejected handshake %s from %s: %s",
                    scope.get("path"),
                    client[0] if client else "unknown",
                    reason,
                )
                # Per the ASGI spec the server sends ``websocket.connect``
                # first; consume it, then close without accepting. Servers
                # surface this as a 403 handshake rejection (close code
                # 1008 = policy violation).
                await receive()
                await send({"type": "websocket.close", "code": 1008})
                return

        await self.app(scope, receive, send)
