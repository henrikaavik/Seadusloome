"""Analüüsikeskus routes (#714).

The Analüüsikeskus is the legal-analysis workflow hub — the design
rationale lives in ``docs/2026-05-11-ministry-lawyer-ui-structure.md``.
This module hosts:

    GET  /analyysikeskus                         — workflow directory (#720)
    GET  /analyysikeskus/normi-mojuahel          — Normi mõjuahel stub (#722 fills it in)
    GET  /analyysikeskus/el-ulevott              — EL ülevõtt stub (#723 fills it in)

Only the two workflows with backing ontology data today (``Normi
mõjuahel`` and ``EL ülevõtt ja harmoneerimine``) are wired here; the
other six Section-7 workflows are deferred to a follow-up epic and get
no placeholder cards in the meantime.

Auth is handled by the global ``auth_before`` middleware — none of these
paths are in ``SKIP_PATHS`` so an unauthenticated request is redirected
to ``/auth/login`` before any handler runs.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from fasthtml.common import *  # noqa: F403
from starlette.requests import Request
from starlette.responses import RedirectResponse

from app.analyysikeskus.result_shell import analysis_result_shell
from app.db import get_connection as _connect
from app.docs.impact.scoring import IMPACT_BAND_LABELS_ET, impact_band
from app.drafter.state_machine import STEP_LABELS_ET, Step
from app.ui.data.data_table import Column, DataTable
from app.ui.layout import PageShell
from app.ui.primitives.button import Button  # noqa: E402  (re-import after wildcard)
from app.ui.primitives.input import Input
from app.ui.surfaces.alert import Alert
from app.ui.surfaces.card import Card, CardBody, CardHeader
from app.ui.theme import get_theme_from_request
from app.ui.time import format_tallinn

logger = logging.getLogger(__name__)

# How many "Hiljutised analüüsid" rows to surface (newest first, merged
# across impact reports + drafter sessions). Kept small so the directory
# page stays dense-but-calm.
_MAX_RECENT_ANALYSES = 10

# Stub copy reused by both workflow result pages until #722 / #723 land
# the real computation.
_RESULTS_STUB_TEXT = "Selle töövoo tulemuste arvutus on koostamisel — tulekul."
_EVIDENCE_STUB_TEXT = (
    "Tõendid (allikad, seosed, kuupäevad, lingid) kuvatakse siin, kui tulemused on arvutatud."
)

# Static action set every stub workflow ends with — no LLM-generated
# recommendations (that's Phase D). ``{label, href}`` dicts.
_STUB_ACTIONS: list[dict[str, str]] = [
    {"label": "Küsi nõustajalt", "href": "/chat/new"},
    {"label": "Tagasi analüüsikeskusesse", "href": "/analyysikeskus"},
]


# ---------------------------------------------------------------------------
# DB helper — "Hiljutised analüüsid"
# ---------------------------------------------------------------------------


def _step_label(step_number: int) -> str:
    """Estonian label for a drafter step number, falling back to the bare number."""
    try:
        return STEP_LABELS_ET.get(Step(step_number), str(step_number))
    except (ValueError, TypeError):
        return str(step_number)


def _get_recent_analyses(user_id: str | None, org_id: str | None) -> list[dict[str, Any]]:
    """Return recent analysis activity for the directory page, newest first.

    Two small org-scoped raw-SQL queries — mirroring the try/except → log
    + return ``[]`` pattern from ``app/templates/dashboard.py``'s widget
    loaders — merged into one list capped at :data:`_MAX_RECENT_ANALYSES`:

    * **Impact reports** — the latest report per draft for the org (via a
      ``DISTINCT ON (ir.draft_id)`` CTE, then re-sorted by ``generated_at``
      DESC). Each row links to ``/drafts/{id}/report`` and carries the
      draft title, the risk-band label, and the ``generated_at`` timestamp.
    * **Drafter sessions** — the current user's active/completed sessions
      for the org. Each row links to ``/drafter/{id}`` and carries the
      "Koostaja — {N}. samm" label and ``updated_at``.

    Returns a list of dicts shaped::

        {"kind": "report"|"session", "href": str, "title": str,
         "detail": str, "when": datetime|None, "_sort": datetime}

    ``[]`` on any DB error so the directory page degrades to the empty
    state rather than 500.
    """
    out: list[dict[str, Any]] = []

    # --- recent impact reports (org-scoped) ---------------------------------
    if org_id:
        try:
            with _connect() as conn:
                rows = conn.execute(
                    """
                    WITH latest_report AS (
                        SELECT DISTINCT ON (ir.draft_id)
                               ir.draft_id, ir.impact_score, ir.generated_at
                        FROM impact_reports ir
                        JOIN drafts d ON d.id = ir.draft_id
                        WHERE d.org_id = %s
                        ORDER BY ir.draft_id, ir.generated_at DESC
                    )
                    SELECT lr.draft_id, d.title, lr.impact_score, lr.generated_at
                    FROM latest_report lr
                    JOIN drafts d ON d.id = lr.draft_id
                    ORDER BY lr.generated_at DESC
                    LIMIT %s
                    """,
                    (org_id, _MAX_RECENT_ANALYSES),
                ).fetchall()
            for r in rows:
                score = int(r[2] or 0)
                band_label = IMPACT_BAND_LABELS_ET[impact_band(score)]
                out.append(
                    {
                        "kind": "report",
                        "href": f"/drafts/{r[0]}/report",
                        "title": r[1] or "Pealkirjata eelnõu",
                        "detail": f"Mõjuaruanne — {band_label}",
                        "when": r[3],
                        "_sort": r[3],
                    }
                )
        except Exception:
            logger.exception("Failed to fetch recent impact reports for org %s", org_id)

    # --- recent drafter sessions (user + org scoped) ------------------------
    if user_id and org_id:
        try:
            with _connect() as conn:
                rows = conn.execute(
                    """
                    SELECT id, current_step, updated_at
                    FROM drafting_sessions
                    WHERE user_id = %s AND org_id = %s
                      AND status IN ('active', 'completed')
                    ORDER BY updated_at DESC
                    LIMIT %s
                    """,
                    (user_id, org_id, _MAX_RECENT_ANALYSES),
                ).fetchall()
            for r in rows:
                step_num = int(r[1] or 1)
                out.append(
                    {
                        "kind": "session",
                        "href": f"/drafter/{r[0]}",
                        "title": "Koostaja eelnõu",
                        "detail": f"Koostaja — {step_num}. samm: {_step_label(step_num)}",
                        "when": r[2],
                        "_sort": r[2],
                    }
                )
        except Exception:
            logger.exception("Failed to fetch recent drafter sessions for user %s", user_id)

    # Merge newest-first; rows with a missing timestamp sink to the bottom.
    def _key(item: dict[str, Any]) -> datetime:
        ts = item.get("_sort")
        if isinstance(ts, datetime):
            return ts if ts.tzinfo is not None else ts.replace(tzinfo=UTC)
        return datetime.min.replace(tzinfo=UTC)

    out.sort(key=_key, reverse=True)
    return out[:_MAX_RECENT_ANALYSES]


# ---------------------------------------------------------------------------
# Directory page (#720)
# ---------------------------------------------------------------------------


def _workflow_card(
    *,
    title: str,
    purpose: str,
    action: str,
    input_name: str,
    input_placeholder: str,
    input_aria_label: str,
    examples: str,
) -> Any:
    """One compact workflow entry — a card with a one-line purpose + a GET form.

    Not a marketing card: dense, scan-friendly, task-first (per the design
    doc's "Analüüsikeskus" section). The form does a plain ``GET`` to the
    workflow route with a single ``sisend`` text input + an "Alusta
    analüüsi" submit; example inputs sit below in muted text.
    """
    return Card(
        CardHeader(H3(title, cls="card-title")),  # noqa: F405
        CardBody(
            P(purpose),  # noqa: F405
            Form(  # noqa: F405
                Input(
                    input_name,
                    type="text",
                    placeholder=input_placeholder,
                    aria_label=input_aria_label,
                    cls="analyysikeskus-input",
                ),
                Button("Alusta analüüsi", type="submit", variant="primary"),
                method="get",
                action=action,
                cls="analyysikeskus-workflow-form",
            ),
            Small(examples, cls="muted-text"),  # noqa: F405
        ),
    )


def _recent_analyses_card(items: list[dict[str, Any]]) -> Any:
    """The "Hiljutised analüüsid" card — a DataTable of recent activity.

    Empty → a single muted "Veel pole analüüse." row (consistent with how
    ``app/templates/dashboard.py`` renders its empty section bodies).
    """
    if not items:
        return Card(
            CardHeader(H3("Hiljutised analüüsid", cls="card-title")),  # noqa: F405
            CardBody(P("Veel pole analüüse.", cls="muted-text")),  # noqa: F405
        )

    columns = [
        Column(
            key="title",
            label="Analüüs",
            sortable=False,
            render=lambda r: A(r["title"], href=r["href"], cls="table-link"),  # noqa: F405
        ),
        Column(key="detail", label="Tüüp", sortable=False),
        Column(
            key="when",
            label="Muudetud",
            sortable=False,
            render=lambda r: format_tallinn(r["when"]),
        ),
    ]
    rows = [
        {"title": it["title"], "href": it["href"], "detail": it["detail"], "when": it["when"]}
        for it in items
    ]
    return Card(
        CardHeader(H3("Hiljutised analüüsid", cls="card-title")),  # noqa: F405
        CardBody(DataTable(columns=columns, rows=rows)),
    )


def analyysikeskus_page(req: Request):
    """GET /analyysikeskus — the legal-analysis workflow directory."""
    auth = req.scope.get("auth") or None
    theme = get_theme_from_request(req)
    user_id = auth.get("id") if auth else None
    org_id = auth.get("org_id") if auth else None

    recent = _get_recent_analyses(user_id, org_id)

    # Compact header — no marketing hero, no InfoBox banner.
    header = (
        H1("Analüüsikeskus", cls="page-title"),  # noqa: F405
        P(  # noqa: F405
            "Õigusliku analüüsi töövood ühes kohas. Vali töövoog, sisesta "
            "õiguslik viide või küsimus.",
            cls="page-subtitle",
        ),
    )

    normi_card = _workflow_card(
        title="Normi mõjuahel",
        purpose=(
            "Vaata, mida muudatus mõjutab — millised sätted viitavad muudetavale "
            "paragrahvile, millised eelnõud puudutavad sama teemat ja milline "
            "Riigikohtu praktika on seotud."
        ),
        action="/analyysikeskus/normi-mojuahel",
        input_name="sisend",
        input_placeholder=(
            "Nt: AvTS § 35 · CELEX-number · eelnõu pealkiri · või kirjeldage muudatust"
        ),
        input_aria_label="Õiguslik viide või kirjeldus",
        examples=("Näited: «Muudame AvTS § 35.» · «Kontrolli karistusseadustiku § 133 mõju.»"),
    )

    el_card = _workflow_card(
        title="EL ülevõtt ja harmoneerimine",
        purpose=(
            "Kontrolli, kas Eesti õigus katab EL kohustuse — millised Eesti sätted "
            "on EL aktiga seotud ja kus on katmata kohad."
        ),
        action="/analyysikeskus/el-ulevott",
        input_name="sisend",
        input_placeholder="Nt: CELEX-number · EL akti pealkiri · poliitikavaldkond",
        input_aria_label="CELEX-number, EL akti pealkiri või valdkond",
        examples="Näited: «Kontrolli AI määruse ülevõttu.» · «32016R0679»",
    )

    return PageShell(
        *header,
        normi_card,
        el_card,
        _recent_analyses_card(recent),
        title="Analüüsikeskus",
        user=auth,
        theme=theme,
        active_nav="/analyysikeskus",
    )


# ---------------------------------------------------------------------------
# Stub workflow routes (#722 Normi mõjuahel, #723 EL ülevõtt fill these in)
# ---------------------------------------------------------------------------


def _workflow_stub_response(req: Request, *, workflow_title: str):
    """Shared body for the two stub workflow GET routes.

    Reads the ``sisend`` query param; a blank value redirects back to the
    directory (303). Otherwise renders :func:`analysis_result_shell` with
    the echoed input, the "koostamisel" placeholder in ``Tulemused``, a
    stub ``Tõendid`` line, and the static :data:`_STUB_ACTIONS` set.
    """
    sisend = (req.query_params.get("sisend") or "").strip()
    if not sisend:
        return RedirectResponse(url="/analyysikeskus", status_code=303)

    auth = req.scope.get("auth") or None
    theme = get_theme_from_request(req)

    return analysis_result_shell(
        workflow_title=workflow_title,
        input_summary=P(f"Sisestasite: «{sisend}»"),  # noqa: F405
        results_block=Alert(_RESULTS_STUB_TEXT, variant="info"),
        evidence_block=P(_EVIDENCE_STUB_TEXT, cls="muted-text"),  # noqa: F405
        actions=_STUB_ACTIONS,
        user=auth,
        theme=theme,
    )


def normi_mojuahel_page(req: Request):
    """GET /analyysikeskus/normi-mojuahel?sisend=<text> — Normi mõjuahel (stub)."""
    return _workflow_stub_response(req, workflow_title="Normi mõjuahel")


def el_ulevott_page(req: Request):
    """GET /analyysikeskus/el-ulevott?sisend=<text> — EL ülevõtt ja harmoneerimine (stub)."""
    return _workflow_stub_response(req, workflow_title="EL ülevõtt ja harmoneerimine")


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------


def register_analyysikeskus_routes(rt) -> None:  # type: ignore[no-untyped-def]
    """Register Analüüsikeskus routes on the FastHTML route decorator *rt*."""
    rt("/analyysikeskus", methods=["GET"])(analyysikeskus_page)
    rt("/analyysikeskus/normi-mojuahel", methods=["GET"])(normi_mojuahel_page)
    rt("/analyysikeskus/el-ulevott", methods=["GET"])(el_ulevott_page)
