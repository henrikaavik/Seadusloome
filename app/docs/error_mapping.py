"""Map pipeline exceptions to actionable Estonian user messages (#609).

The draft processing handlers (``parse_handler``, ``extract_handler``,
``analyze_handler``) used to dump the raw exception message into
``drafts.error_message``, which is the exact string surfaced to ministry
lawyers in the UI. Lines such as ``"Olemite eraldamine ebaõnnestus: "
"anthropic.BadRequestError: 'messages: at least one message is required"``
are unactionable — the user has no way to tell whether they should
re-upload, split the document, or wait and retry.

This module normalises every known failure mode into a *short, actionable*
Estonian message for the user and keeps the raw technical detail for the
admin/audit view (``drafts.error_debug``, added by migration 018).

Routes already read ``draft.error_message`` verbatim into the red Alert
component (see ``app/docs/routes.py:236``), so the Estonian string we
return here must be complete, human-grade prose — never a stack trace.

Extending
---------

When a new failure mode appears in the logs, add another branch to
:func:`map_failure_to_user_message`. Always:

1. Match by *exception class* first (the cheapest, most reliable check).
2. Fall back to string matching for library-agnostic sentinels
   (Tika error strings, SPARQL error substrings, etc) only when a
   dedicated exception class is not available.
3. Keep the Estonian message under ~180 chars so it fits in the UI
   alert without wrapping awkwardly.
4. Never leak a URI, file path, stack trace, or exception class name
   into the user-facing string.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Canonical Estonian messages
# ---------------------------------------------------------------------------
#
# Exposed at module scope so tests and other modules (e.g. notifications)
# can reference the exact strings without re-typing them. Each constant
# carries a short docstring-style comment explaining which failure mode
# it covers.

MSG_ENCRYPTED_PDF = "PDF on krüpteeritud või kaitstud. Eemaldage parool ja laadige uuesti."
"""PDF that Tika could not parse because it is password-protected."""

MSG_TIKA_CAPACITY = (
    "Fail on liiga keerukas automaatseks analüüsiks. Proovige jagada väiksemateks eelnõudeks."
)
"""Tika OOM, timeout, or generic 5xx — usually means the document is too large."""

MSG_SPARQL_BUSY = "Ontoloogia teenus on ajutiselt hõivatud. Proovige mõne minuti pärast uuesti."
"""Jena Fuseki timed out or returned a SPARQL/HTTP error."""

MSG_LLM_UNAVAILABLE = "AI teenus on ajutiselt kättesaamatu. Meeskond on teavitatud."
"""Claude rate limit, auth failure, or generic API error."""

MSG_FILE_MISSING = "Faili ei leitud. Laadige eelnõu uuesti üles."
"""Encrypted draft file missing on disk — usually storage was pruned."""

MSG_UNKNOWN = "Töötlemine ebaõnnestus tehnilisel põhjusel. Meeskond on teavitatud."
"""Catch-all for unknown errors. Admin sees the real message in error_debug."""


# Maximum length we persist to ``drafts.error_debug``. The raw stack-
# trace-adjacent message is not indexed, but keeping it unbounded would
# let pathological exceptions bloat the table.
_DEBUG_MAX_LEN = 2000


# Match Tika's own error strings for password-protected PDFs. Tika 2.x
# surfaces these as the exception message text via our ``TikaError``
# wrapper; we match on normalised lowercase substrings so we survive
# minor phrasing changes between Tika releases.
_ENCRYPTED_PDF_MARKERS = (
    "encrypted",
    "password",
    "pdfbox.encryption",
    "krüpteeritud",
)

_TIKA_CAPACITY_MARKERS = (
    "timeout",
    "timed out",
    "out of memory",
    "java heap",
    "outofmemoryerror",
    "read timed out",
)

_SPARQL_MARKERS = (
    "sparql",
    "fuseki",
    "queryexception",
    "queryparseexception",
)


def _contains_any(haystack: str, needles: tuple[str, ...]) -> bool:
    return any(n in haystack for n in needles)


def map_failure_to_user_message(exc: BaseException, stage: str) -> tuple[str, str]:
    """Return ``(user_message_et, debug_detail)`` for an exception.

    Args:
        exc: The raised exception. Any ``BaseException`` is acceptable so
            we can classify ``MemoryError`` / ``KeyboardInterrupt`` too.
        stage: One of ``"parse"``, ``"extract"``, ``"analyze"``,
            ``"export"``. Currently only used for debug context but
            reserved so future mappings can surface stage-specific
            guidance (e.g. "re-upload" vs "retry analysis").

    Returns:
        A tuple ``(user_message_et, debug_detail)``.
        - ``user_message_et``: the Estonian string to store in
          ``drafts.error_message`` and render to the user.
        - ``debug_detail``: the raw technical detail (truncated to
          :data:`_DEBUG_MAX_LEN` chars) for ``drafts.error_debug``.
    """
    # Debug detail: exception class name + message. Keeps admin-side
    # grep-ability without leaking the full traceback.
    raw_detail = f"[{stage}] {type(exc).__name__}: {exc}"
    debug_detail = raw_detail[:_DEBUG_MAX_LEN]

    # Lowercased copy of the message for substring matching. Tika
    # surfaces Java stack-trace heads in English, Anthropic errors are
    # English too — so ASCII lowercasing is sufficient.
    msg_lower = str(exc).lower()
    class_path = f"{type(exc).__module__}.{type(exc).__name__}".lower()

    # -- File missing ---------------------------------------------------
    if isinstance(exc, FileNotFoundError):
        return MSG_FILE_MISSING, debug_detail

    # -- Encrypted / password-protected PDF -----------------------------
    # Two signals: the Tika error string mentions encryption/password,
    # or parse_handler raised a dedicated "empty text" ValueError on
    # a .pdf file. The empty-text path is handled by the caller (it
    # passes a synthetic exception whose message contains
    # "empty text" + the content type); here we only need the Tika
    # signal so we don't double-classify other empty-text cases.
    if _contains_any(msg_lower, _ENCRYPTED_PDF_MARKERS):
        return MSG_ENCRYPTED_PDF, debug_detail

    # -- Stage-specific routing for HTTP timeouts ----------------------
    # ``requests.Timeout`` and ``httpx.TimeoutException`` both live in
    # library-specific modules — matching on the full class path lets
    # us classify them without importing the packages (stub-mode
    # deployments run without them). We check this BEFORE the generic
    # capacity check because the "timed out" substring would otherwise
    # route every HTTP timeout to the file-too-big message, which is
    # wrong during the analyze stage (those are Jena/Fuseki timeouts).
    is_http_timeout = (
        "requests.exceptions.timeout" in class_path or "httpx.timeoutexception" in class_path
    )
    if is_http_timeout and stage == "analyze":
        return MSG_SPARQL_BUSY, debug_detail

    # -- SPARQL / Fuseki busy ------------------------------------------
    if _contains_any(msg_lower, _SPARQL_MARKERS):
        return MSG_SPARQL_BUSY, debug_detail

    # -- Tika capacity (OOM / timeout) ---------------------------------
    if isinstance(exc, MemoryError):
        return MSG_TIKA_CAPACITY, debug_detail
    if _contains_any(msg_lower, _TIKA_CAPACITY_MARKERS) or _contains_any(
        class_path, ("timeout", "memoryerror")
    ):
        return MSG_TIKA_CAPACITY, debug_detail

    # -- LLM rate limit / auth / API errors ----------------------------
    # Anthropic errors inherit from ``anthropic.APIError``. We match on
    # class path so we don't need ``anthropic`` imported (stub mode
    # deployments do not install it).
    if "anthropic." in class_path:
        return MSG_LLM_UNAVAILABLE, debug_detail
    if _contains_any(
        msg_lower,
        (
            "rate limit",
            "rate_limit",
            "ratelimit",
            "invalid api key",
            "authentication",
            "401",
            "429",
        ),
    ):
        return MSG_LLM_UNAVAILABLE, debug_detail

    # -- Default --------------------------------------------------------
    logger.debug(
        "Unmapped failure in stage=%s class=%s — returning generic message",
        stage,
        class_path,
    )
    return MSG_UNKNOWN, debug_detail
