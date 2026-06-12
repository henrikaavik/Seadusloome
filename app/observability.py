"""Sentry integration and PII scrubbing for error tracking.

Initializes the Sentry SDK with the ``SENTRY_DSN`` environment variable.
When the DSN is unset (local dev), initialization is skipped silently.

Call ``init_sentry()`` once at application startup — before ``fast_app()``
creates the ASGI app — so that the Starlette integration can wrap the
app and capture unhandled exceptions automatically.
"""

from __future__ import annotations

import logging
import re
import subprocess
from typing import Any

from app import config

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Request / URL scrubbing (issue #846)
# ---------------------------------------------------------------------------
#
# With ``traces_sample_rate=0.1`` roughly 10% of requests ship to Sentry
# SaaS as transaction events carrying the full request URL — including
# ``/auth/reset/<token_hex(32)>`` clicks and ``?token=`` signed report
# downloads. The helpers below redact bearer credentials from URLs,
# query strings, headers, cookies and form bodies while keeping the
# route context (host, path, benign params) intact so events stay
# debuggable. They are shared by ``before_send`` (error events),
# ``before_send_transaction`` (performance events) and the breadcrumb /
# span / stack-frame scrubbers so all egress paths use one
# implementation.

# Placeholder for values redacted inside free-form strings (paths,
# query-param values). Dict-key redaction keeps the pre-existing
# ``[Redacted]`` marker for backwards compatibility.
_REDACTED_VALUE = "[redacted]"
_REDACTED_KEY = "[Redacted]"

# Path segments following these route prefixes are single-use bearer
# credentials (password-reset tokens are ``secrets.token_hex(32)``).
_TOKEN_PATH_RE = re.compile(r"(/auth/reset/)[^/?#\s]+")

# Any path segment of 32+ hex chars is token-shaped — UUIDs keep their
# dashes and Estonian slugs are not hex, so this never hits a segment
# we need for debugging. Catch-all for future token-in-path routes.
_HEX_SEGMENT_RE = re.compile(r"(/)[0-9a-fA-F]{32,}(?=[/?#.,\s]|$)")

# ``name=value`` pairs in query strings / form bodies / log lines.
_QUERY_PAIR_RE = re.compile(r"(^|[?&;\s])([A-Za-z0-9_.\-]+)=([^&;#\s]+)")

# Names whose values are credentials when they appear as a query
# param, form field, header, breadcrumb-data key or stack-frame local.
# Suffix matching requires a separator (``api_key``, ``x-auth-token``)
# or exact match (``token``) so benign names like ``status_code`` or
# ``monkey`` survive. Plural forms (``tokens``, ``keys``…) are covered
# because bulk-credential collections are a real payload shape.
_SENSITIVE_NAME_RE = re.compile(
    r"(?i)(?:^|[._\-])"
    r"(?:tokens?|secrets?|passwords?|passwd|pwd|keys?|apikeys?|auth"
    r"|authorization|sessions?|sessionid|signatures?|sig|jwt|bearer"
    r"|otp|credentials?)$"
)

# Query-param-only sensitive names: OAuth/OIDC ``code`` + ``state`` and
# the single-use chat-seed token. Kept out of the shared regex so a
# stack-frame local called ``state`` or ``code`` is not nuked.
_SENSITIVE_PARAMS_EXACT = frozenset({"code", "state", "seed"})

# Dict-key-only sensitive names (headers, frame vars, form fields).
_SENSITIVE_KEYS_EXACT = frozenset(
    {
        "email",
        "full_name",
        "isikukood",
        "cookie",
        "set-cookie",
        "x-csrftoken",
        "x-csrf-token",
        "x-xsrf-token",
    }
)


def _is_sensitive_param(name: str) -> bool:
    """True when a query/form param *name* carries a credential."""
    lowered = name.lower()
    return lowered in _SENSITIVE_PARAMS_EXACT or bool(_SENSITIVE_NAME_RE.search(lowered))


def _is_sensitive_key(name: str) -> bool:
    """True when a dict key (header, frame var, body field) is sensitive."""
    lowered = name.lower()
    return lowered in _SENSITIVE_KEYS_EXACT or bool(_SENSITIVE_NAME_RE.search(lowered))


def _redact_sensitive_params(text: str) -> str:
    """Redact values of sensitive ``name=value`` pairs inside *text*."""

    def _sub(match: re.Match[str]) -> str:
        if _is_sensitive_param(match.group(2)):
            return f"{match.group(1)}{match.group(2)}={_REDACTED_VALUE}"
        return match.group(0)

    return _QUERY_PAIR_RE.sub(_sub, text)


def _scrub_text(value: str) -> str:
    """Scrub one free-form string: token paths, query params, PII regexes.

    This is the single implementation shared by request URLs, query
    strings, headers, form bodies, breadcrumb messages/data, span
    descriptions and stack-frame locals — for both error events and
    transaction events.
    """
    from app.llm.scrubber import scrub_prompt

    value = _TOKEN_PATH_RE.sub(rf"\g<1>{_REDACTED_VALUE}", value)
    value = _HEX_SEGMENT_RE.sub(rf"\g<1>{_REDACTED_VALUE}", value)
    value = _redact_sensitive_params(value)
    return scrub_prompt(value)


# Containers nested deeper than this are replaced wholesale: we cannot
# verify they are clean, so fail closed. Sentry's own serializer
# truncates well before this depth, so real telemetry is unaffected.
_MAX_SCRUB_DEPTH = 20


def _deep_scrub(value: Any, _depth: int = 0, _seen: set[int] | None = None) -> Any:
    """Recursively scrub *value*: dicts, lists, tuples and strings.

    Review finding on #846: a shallow helper only rewrote *top-level*
    string values, so nested JSON-like payloads (request bodies,
    breadcrumb/span data, stack-frame locals) still shipped tokens and
    Estonian PII verbatim. This walks the whole structure:

    - ``str`` values run through :func:`_scrub_text`.
    - Dict entries whose key matches the sensitive-name rules are
      redacted wholesale, regardless of the value's type or depth.
    - Dicts and lists are mutated in place (and returned) so shared
      references inside cyclic payloads observe the scrubbed content;
      tuples are immutable and therefore rebuilt.
    - An ``id()``-based visited guard stops infinite recursion on
      self-referential structures. Skipping a revisit is sound because
      mutable containers are scrubbed in place exactly once. Tuples
      are deliberately *not* guarded (a reference cycle cannot consist
      of tuples alone) so a tuple shared between two branches is still
      rebuilt scrubbed at each site.
    - Containers nested beyond ``_MAX_SCRUB_DEPTH`` are replaced with
      the redaction marker — fail closed rather than ship unverified
      data.
    """
    if isinstance(value, str):
        return _scrub_text(value)
    if not isinstance(value, (dict, list, tuple)):
        return value
    if _depth >= _MAX_SCRUB_DEPTH:
        return _REDACTED_KEY
    if _seen is None:
        _seen = set()

    if isinstance(value, tuple):
        return tuple(_deep_scrub(item, _depth + 1, _seen) for item in value)

    if id(value) in _seen:
        return value
    _seen.add(id(value))

    if isinstance(value, dict):
        for key in list(value.keys()):
            if isinstance(key, str) and _is_sensitive_key(key):
                value[key] = _REDACTED_KEY
            else:
                value[key] = _deep_scrub(value[key], _depth + 1, _seen)
        return value

    for index in range(len(value)):
        value[index] = _deep_scrub(value[index], _depth + 1, _seen)
    return value


def _scrub_request(event: dict[str, Any]) -> None:
    """Scrub ``event["request"]`` — url, query_string, headers, cookies, data."""
    request = event.get("request")
    if not isinstance(request, dict):
        return

    url = request.get("url")
    if isinstance(url, str):
        request["url"] = _scrub_text(url)

    query_string = request.get("query_string")
    if isinstance(query_string, str):
        request["query_string"] = _scrub_text(query_string)

    # Cookies are session credentials wholesale (JWT cookie auth) —
    # there is no debugging value in any individual cookie.
    if request.get("cookies"):
        request["cookies"] = _REDACTED_KEY

    headers = request.get("headers")
    if isinstance(headers, dict):
        # Authorization / Cookie / X-Api-Key → redacted via the key
        # check; Referer and friends get URL scrubbing via the value
        # branch so token-bearing referrers don't slip through.
        _deep_scrub(headers)

    # Body can be a form dict, nested JSON structure, list-of-dicts or
    # a raw string — deep-scrub handles every shape.
    data = request.get("data")
    if data is not None:
        request["data"] = _deep_scrub(data)


def _scrub_breadcrumbs(event: dict[str, Any]) -> None:
    """Scrub breadcrumb messages and data (httpx/log crumbs carry URLs)."""
    crumbs = event.get("breadcrumbs")
    if isinstance(crumbs, dict):
        values = crumbs.get("values", [])
    elif isinstance(crumbs, list):  # older SDK shape
        values = crumbs
    else:
        values = []
    for breadcrumb in values:
        if not isinstance(breadcrumb, dict):
            continue
        data = breadcrumb.get("data")
        if data is not None:
            breadcrumb["data"] = _deep_scrub(data)
        message = breadcrumb.get("message")
        if isinstance(message, str):
            breadcrumb["message"] = _scrub_text(message)


def _scrub_spans(event: dict[str, Any]) -> None:
    """Scrub span descriptions/data — outbound httpx URLs in transactions."""
    spans = event.get("spans")
    if not isinstance(spans, list):
        return
    for span in spans:
        if not isinstance(span, dict):
            continue
        description = span.get("description")
        if isinstance(description, str):
            span["description"] = _scrub_text(description)
        data = span.get("data")
        if data is not None:
            span["data"] = _deep_scrub(data)


def _scrub_stack_vars(event: dict[str, Any]) -> None:
    """Scrub captured frame locals — the classic Sentry leak vector.

    The SDK captures local variables by default, so any token /
    isikukood / IBAN held in a variable at crash time ships with the
    event. Covers both ``exception.values[*]`` and the
    ``threads.values[*]`` equivalent, in dict-with-``values`` and
    bare-list protocol shapes.
    """
    for section in ("exception", "threads"):
        info = event.get(section)
        if isinstance(info, dict):
            entries = info.get("values", [])
        elif isinstance(info, list):
            entries = info
        else:
            continue
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            stacktrace = entry.get("stacktrace")
            if not isinstance(stacktrace, dict):
                continue
            frames = stacktrace.get("frames", [])
            if not isinstance(frames, list):
                continue
            for frame in frames:
                if not isinstance(frame, dict):
                    continue
                local_vars = frame.get("vars")
                if local_vars is not None:
                    frame["vars"] = _deep_scrub(local_vars)


def _scrub_logentry(event: dict[str, Any]) -> None:
    """Scrub log-derived events — providers log reset links verbatim."""
    logentry = event.get("logentry")
    if not isinstance(logentry, dict):
        return
    for field in ("message", "formatted"):
        value = logentry.get(field)
        if isinstance(value, str):
            logentry[field] = _scrub_text(value)
    params = logentry.get("params")
    if params is not None:
        logentry["params"] = _deep_scrub(params)


def _get_git_sha() -> str:
    """Return a short Git commit hash for Sentry release tagging.

    Reads ``GIT_SHA`` from the environment first (set by CI/CD or
    Dockerfile ``ARG``).  Falls back to ``git rev-parse --short HEAD``
    for local development.  Returns ``"unknown"`` when neither works.
    """
    sha = config.env_str("GIT_SHA")
    if sha:
        return sha
    try:
        return (
            subprocess.check_output(  # noqa: S603, S607
                ["git", "rev-parse", "--short", "HEAD"],
                stderr=subprocess.DEVNULL,
            )
            .decode()
            .strip()
        )
    except Exception:
        return "unknown"


def _scrub_pii(event: dict[str, Any], hint: dict[str, Any]) -> dict[str, Any] | None:
    """Remove PII and bearer credentials from Sentry events before send.

    Registered as both ``before_send`` (error events) and
    ``before_send_transaction`` (performance events) so the two egress
    paths share one implementation (issue #846). Strips ``user``
    context entirely, scrubs the request envelope (URL, query string,
    headers, cookies, body), deep-scrubs every arbitrary structure the
    protocol carries (breadcrumb data, span data, ``extra``,
    ``contexts``, exception/thread stack-frame locals — at any nesting
    depth), and runs every free-form string through
    :func:`app.llm.scrubber.scrub_prompt` so emails, phone numbers,
    UUIDs, Estonian isikukoodid, EE IBANs and secret-like tokens are
    redacted with the same regex set the LLM egress path uses
    (NFR §7.1).
    """
    # Remove top-level user context so emails/names never reach Sentry.
    event.pop("user", None)

    _scrub_request(event)
    _scrub_breadcrumbs(event)
    _scrub_spans(event)
    _scrub_stack_vars(event)
    _scrub_logentry(event)

    # Free-form attachment points: ``extra`` is arbitrary user data,
    # ``contexts`` carries trace/runtime metadata whose identifiers
    # (32-hex trace_id, 16-hex span_id) survive _scrub_text untouched.
    for section in ("extra", "contexts"):
        value = event.get(section)
        if isinstance(value, (dict, list)):
            _deep_scrub(value)

    return event


def init_sentry() -> None:
    """Initialize Sentry SDK if ``SENTRY_DSN`` is set.

    Safe to call unconditionally — when the DSN is empty or missing,
    the function returns immediately without importing ``sentry_sdk``.
    """
    dsn = config.env_str("SENTRY_DSN")
    if not dsn:
        logger.debug("SENTRY_DSN not set — Sentry disabled")
        return

    import sentry_sdk

    sentry_sdk.init(
        dsn=dsn,
        traces_sample_rate=0.1,
        release=_get_git_sha(),
        environment=config.get_app_env(),
        before_send=_scrub_pii,  # type: ignore[arg-type]  # Sentry stubs define Event as TypedDict
        # Transactions sample real request URLs (reset links, signed
        # download tokens) — scrub them with the same function (#846).
        before_send_transaction=_scrub_pii,  # type: ignore[arg-type]
    )
    logger.info("Sentry initialized (release=%s)", _get_git_sha())
