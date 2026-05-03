"""Pipeline-stage tracker rendering helpers (#704 PR-B extraction).

Pure rendering helpers extracted from ``app/docs/routes/__init__.py``.
The tracker walks :data:`_STATUS_STAGES` (#625 SSOT) and renders the
per-stage spinner / checkmark / failed states with the correct
semantic CSS classes.

Public surface (re-exported by ``app.docs.routes`` for back-compat):
    ``_status_tracker(draft)`` — renders the polling tracker Div used
    by both the detail page and the HTMX status fragment.

Constants and pure helpers live in :mod:`app.docs.routes._shared`;
the ``Alert`` / ``Button`` UI primitives are imported on demand.
"""

from __future__ import annotations

from typing import Any

from fasthtml.common import *  # noqa: F403

from app.docs.draft_model import Draft
from app.docs.routes._shared import (
    _STATUS_STAGES,
    _TYPICAL_STAGE_SECONDS,
    _elapsed_seconds,
    _format_elapsed,
    _format_elapsed_final,
    _is_status_polling_stale,
    _poll_interval_seconds,
    _processing_duration_seconds,
    _status_badge,
)
from app.docs.status import (
    TERMINAL_STATUSES as _TERMINAL_STATUSES,
)
from app.ui.primitives.button import Button
from app.ui.surfaces.alert import Alert


def _status_tracker(draft: Draft):
    """Render the 6-stage horizontal status tracker.

    Wrapped in a polling Div so HTMX can refresh it every 3 seconds
    until the draft reaches a terminal state OR the polling timeout
    elapses (#457). After the timeout we drop the polling attributes
    and surface a yellow alert nudging the user to check the admin
    dashboard so they don't sit on the page forever.
    """
    items: list = []
    current_index = -1
    for idx, (key, _) in enumerate(_STATUS_STAGES):
        if key == draft.status:
            current_index = idx
            break

    elapsed = _elapsed_seconds(draft)

    for idx, (key, label) in enumerate(_STATUS_STAGES):
        classes = ["draft-stage"]
        is_active = False
        if draft.status == "failed":
            # On failure every stage past the last successful one is dim.
            classes.append("draft-stage-idle")
        elif current_index >= 0 and idx < current_index:
            classes.append("draft-stage-done")
        elif current_index >= 0 and idx == current_index:
            classes.append("draft-stage-active")
            is_active = True
        else:
            classes.append("draft-stage-idle")

        # #606: render elapsed time + typical range under the active
        # stage. The elapsed value is bumped client-side by a small
        # inline interval so the user sees a live ticker without any
        # extra HTMX polls.
        # #657: the live ticker is ONLY attached when the draft is in
        # a non-terminal state. For ``ready`` we render a frozen
        # "Analüüsitud N min" label without the ``.draft-stage-elapsed``
        # class so the client-side tick loop skips it. ``failed`` falls
        # through to the idle branch above — no ticker is ever attached
        # to a failed draft because no stage is marked active.
        extras: list = []
        if is_active and elapsed is not None and draft.status not in _TERMINAL_STATUSES:
            extras.append(
                Span(  # noqa: F405
                    _format_elapsed(elapsed),
                    cls="draft-stage-elapsed",
                    data_elapsed_seconds=str(elapsed),
                )
            )
            typical = _TYPICAL_STAGE_SECONDS.get(key)
            if typical is not None:
                low, high = typical
                low_min, high_min = low // 60 or 1, high // 60 or 1
                # Prefer a "N-M min" display; fall back to seconds when
                # the lower bound is sub-minute (e.g. parsing 10s-60s).
                if low < 60:
                    range_text = f"tüüpiline aeg {low}s-{high // 60 or 1} min"
                else:
                    range_text = f"tüüpiline aeg {low_min}-{high_min} min"
                extras.append(
                    Span(  # noqa: F405
                        range_text,
                        cls="draft-stage-typical muted-text",
                    )
                )
        elif is_active and draft.status == "ready":
            # #657: render a frozen "Analüüsitud" label on the "Valmis"
            # stage. Deliberately NOT ``.draft-stage-elapsed`` so the
            # client-side ticker leaves it alone. The displayed
            # duration is the total pipeline time (upload -> ready),
            # not "time since completion".
            duration = _processing_duration_seconds(draft)
            if duration is not None:
                extras.append(
                    Span(  # noqa: F405
                        _format_elapsed_final(duration),
                        cls="draft-stage-done-label muted-text",
                    )
                )

        items.append(
            Li(  # noqa: F405
                Span(str(idx + 1), cls="draft-stage-number", aria_hidden="true"),  # noqa: F405
                Span(label, cls="draft-stage-label"),  # noqa: F405
                *extras,
                cls=" ".join(classes),
            )
        )

    tracker = Ol(*items, cls="draft-status-tracker", aria_label="Töötluse staatus")  # noqa: F405

    # Build the poll attributes only while the draft is still
    # progressing AND we haven't blown the polling timeout (#457).
    polling_stale = _is_status_polling_stale(draft)
    poll_attrs: dict[str, Any] = {}
    if draft.status not in _TERMINAL_STATUSES and not polling_stale:
        poll_attrs = {
            "hx_get": f"/drafts/{draft.id}/status",
            # #607: exponential-ish polling backoff. Freshly created
            # drafts poll every 3s so the tracker feels responsive, but
            # as the wall-clock elapses we slow down to avoid hammering
            # the server for genuinely-slow pipelines. The upper bound
            # is still the _POLLING_TIMEOUT_SECONDS budget above.
            "hx_trigger": f"every {_poll_interval_seconds(draft)}s",
            "hx_target": "this",
            "hx_swap": "outerHTML",
        }

    header = Div(  # noqa: F405
        Span("Staatus:", cls="draft-status-label-text"),  # noqa: F405
        _status_badge(draft.status),
        cls="draft-status-header",
    )

    children: list = [header, tracker]
    if draft.status == "failed" and draft.error_message:
        # #656: surface a "Proovi uuesti" retry button alongside the
        # red error banner so the user never has to re-upload the
        # original file just to re-run the pipeline. The button posts
        # to /drafts/{id}/retry which resets the draft back to
        # ``uploaded`` and re-enqueues parse_draft. HTMX drives the
        # submit so the action stays on the page; an HX-Redirect
        # bounces the browser back to the detail page once the reset
        # commits.
        children.append(
            Alert(
                Div(
                    P(draft.error_message, cls="draft-failed-message"),  # noqa: F405
                    Form(  # noqa: F405
                        Button(
                            "Proovi uuesti",
                            type="submit",
                            variant="primary",
                            size="md",
                        ),
                        method="post",
                        action=f"/drafts/{draft.id}/retry",
                        enctype="application/x-www-form-urlencoded",
                        hx_post=f"/drafts/{draft.id}/retry",
                        hx_target="body",
                        hx_swap="outerHTML",
                        cls="inline-form draft-retry-form",
                    ),
                    cls="draft-failed-body",
                ),
                variant="danger",
                title="Töötlemine ebaõnnestus",
            )
        )
    elif polling_stale and draft.status not in _TERMINAL_STATUSES:
        # The pipeline has been running longer than the polling
        # timeout. Stop the auto-poll and replace the old admin-
        # dashboard dead-end (#606) with an actionable pair: a manual
        # "Kontrolli uuesti kohe" button that fires one immediate
        # /status fetch, plus a short guidance line telling the user
        # when to escalate.
        children.append(
            Alert(
                Div(
                    P(  # noqa: F405
                        "Töötlemine võtab oodatust kauem aega. "
                        "Kui analüüs on kauem kui 10 minutit peatunud, "
                        "võtke ühendust meeskonnaga.",
                        cls="stale-guidance",
                    ),
                    Button(
                        "Kontrolli uuesti kohe",
                        type="button",
                        variant="secondary",
                        size="sm",
                        hx_get=f"/drafts/{draft.id}/status",
                        hx_target=f"#draft-status-{draft.id}",
                        hx_swap="outerHTML",
                    ),
                    cls="stale-repoll",
                ),
                variant="warning",
                title="Töötlemine venib",
            )
        )

    # #606: inline ticker that increments the ".draft-stage-elapsed"
    # span once per second so users see a live "1:40 möödas" counter
    # without extra HTMX polls. The script runs on every HTMX swap
    # because HTMX executes inline scripts inside swapped fragments.
    # The window-level interval id is reused across swaps to avoid
    # stacking multiple timers.
    #
    # #657: two bug-fixes applied here.
    #   1. Format: past 60 minutes the old ``M:SS`` template emitted
    #      unreadable three-digit minute counts ("8835:14"). Switch to
    #      ``H:MM:SS möödas`` once the raw counter clears one hour.
    #   2. Stop condition: when no ``.draft-stage-elapsed`` nodes
    #      remain (terminal swap — the ``ready``/``failed`` status
    #      fragment no longer renders one), clearInterval so the
    #      timer is not left dangling on the window.
    if any("draft-stage-elapsed" in str(child) for child in children):
        children.append(
            Script(  # noqa: F405
                "(function () {\n"
                "  if (window.__draftElapsedTimer) "
                "clearInterval(window.__draftElapsedTimer);\n"
                "  function pad(n) { return n < 10 ? '0' + n : '' + n; }\n"
                "  function format(raw) {\n"
                "    if (raw >= 3600) {\n"
                "      var h = Math.floor(raw / 3600);\n"
                "      var m = Math.floor((raw % 3600) / 60);\n"
                "      var s = raw % 60;\n"
                "      return h + ':' + pad(m) + ':' + pad(s) + ' möödas';\n"
                "    }\n"
                "    var mm = Math.floor(raw / 60);\n"
                "    var ss = raw % 60;\n"
                "    return mm + ':' + pad(ss) + ' möödas';\n"
                "  }\n"
                "  function tick() {\n"
                "    var nodes = document.querySelectorAll('.draft-stage-elapsed');\n"
                "    if (nodes.length === 0) {\n"
                "      clearInterval(window.__draftElapsedTimer);\n"
                "      window.__draftElapsedTimer = null;\n"
                "      return;\n"
                "    }\n"
                "    nodes.forEach(function (el) {\n"
                "      var raw = parseInt(el.getAttribute('data-elapsed-seconds'), 10);\n"
                "      if (isNaN(raw)) return;\n"
                "      raw += 1;\n"
                "      el.setAttribute('data-elapsed-seconds', raw);\n"
                "      el.textContent = format(raw);\n"
                "    });\n"
                "  }\n"
                "  window.__draftElapsedTimer = setInterval(tick, 1000);\n"
                "})();\n"
            )
        )

    # #604: announce stage transitions to screen readers. ``polite``
    # avoids interrupting the user mid-task; ``aria_atomic=false`` means
    # assistive tech reads just the changed node instead of the whole
    # tracker. The existing failed-state Alert still carries
    # ``role="alert"`` for the more urgent announcement.
    # #608: marker for the WS push listener. ``draft-status.js`` finds
    # this attribute on DOMContentLoaded, opens a WebSocket to
    # /ws/drafts/status, subscribes to ``draft.id`` and swaps the
    # tracker on every status event. The hx-* polling attributes above
    # are deliberately preserved so the page degrades to 3s polling if
    # the WS is unavailable. The marker is dropped once the draft
    # reaches a terminal status — at that point the JS will already
    # have closed the WS and there's nothing more to push.
    ws_attrs: dict[str, Any] = {}
    if draft.status not in _TERMINAL_STATUSES:
        ws_attrs = {
            "data_draft_status_ws": "1",
            "data_draft_id": str(draft.id),
        }

    return Div(  # noqa: F405
        *children,
        id=f"draft-status-{draft.id}",
        cls="draft-status-wrapper",
        aria_live="polite",
        aria_atomic="false",
        **poll_attrs,
        **ws_attrs,
    )
