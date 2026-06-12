"""Analüüsikeskus directory page — ``GET /analyysikeskus`` (#720, #860).

The workflow directory: a card per capability whose ``target_url`` lives
under ``/analyysikeskus`` (live ones with an input form, planned ones with
a "Tulekul" badge), plus a "Hiljutised analüüsid" recent-activity table.

Patch where used (post-#860), e.g.::

  patch("app.analyysikeskus.routes._directory._get_recent_analyses")
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from fasthtml.common import *  # noqa: F403
from starlette.requests import Request

from app.analyysikeskus.routes._common import _MAX_RECENT_ANALYSES
from app.db import get_connection as _connect
from app.drafter.state_machine import STEP_LABELS_ET, Step
from app.impact.scoring import IMPACT_BAND_LABELS_ET, impact_band
from app.ui.capabilities import CAPABILITIES, Capability
from app.ui.data.data_table import Column, DataTable
from app.ui.layout import PageShell
from app.ui.primitives.badge import Badge
from app.ui.surfaces.card import Card, CardBody, CardHeader
from app.ui.theme import get_theme_from_request
from app.ui.time import format_tallinn

logger = logging.getLogger(__name__)


def _step_label(step_number: int) -> str:
    """Estonian label for a drafter step number, falling back to the bare number."""
    try:
        return STEP_LABELS_ET.get(Step(step_number), str(step_number))
    except (ValueError, TypeError):
        return str(step_number)


def _get_recent_analyses(user_id: str | None, org_id: str | None) -> list[dict[str, Any]]:
    """Return recent analysis activity for the directory page, newest first.

    Two small org-scoped raw-SQL queries — mirroring the try/except → log
    + return ``[]`` pattern from ``app/dashboard/service.py``'s widget
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
    ``app/dashboard/pages.py`` renders its empty section bodies).
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


_ANALYYSIKESKUS_INPUTS: dict[str, dict[str, str]] = {
    "moju-poliitikamottest": {
        "placeholder": (
            "Kirjelda poliitilist kavatsust vabas vormis — nt «Soovin lihtsustada "
            "puudega inimese toetuse taotlemist…»"
        ),
        "aria_label": "Poliitiline kavatsus vabas vormis",
        "examples": (
            "Näited: «Soovin lihtsustada puudega inimese toetuse taotlemist nii, "
            "et osa andmeid liiguks automaatselt Tervisekassast ja Töötukassast.»"
        ),
    },
    "normi-mojuahel": {
        "placeholder": (
            "Nt: AvTS § 35 · CELEX-number · eelnõu pealkiri · või kirjeldage muudatust"
        ),
        "aria_label": "Õiguslik viide või kirjeldus",
        "examples": "Näited: «Muudame AvTS § 35.» · «Kontrolli karistusseadustiku § 133 mõju.»",
    },
    "el-ulevott": {
        "placeholder": "Nt: CELEX-number · EL akti pealkiri · poliitikavaldkond",
        "aria_label": "CELEX-number, EL akti pealkiri või valdkond",
        "examples": "Näited: «Kontrolli AI määruse ülevõttu.» · «32016R0679»",
    },
    "sanktsioonid": {
        "placeholder": "Nt: KarS § 211 · KMS · CELEX-number · või kirjeldage säte",
        "aria_label": "Õiguslik viide või kirjeldus",
        "examples": "Näited: «KarS § 211» · «KMS § 30» · «32016R0679»",
    },
    "kohtupraktika": {
        "placeholder": "Nt: AvTS § 35 · KarS § 211 · CELEX-number · 3-1-1-63-15",
        "aria_label": "Säte, akt, CELEX-number või kohtuasja number",
        "examples": ("Näited: «AvTS § 35» · «KarS § 211» · «32016R0679» · «3-1-1-63-15»"),
    },
    "halduskoormus": {
        "placeholder": "Nt: KMS · TLS § 12 · eelnõu pealkiri · või kirjeldage akti",
        "aria_label": "Õigusakt, säte või eelnõu",
        "examples": ("Näited: «Töölepingu seadus» · «KMS» · «TLS § 12» · «32016R0679»"),
    },
    "padevused": {
        "placeholder": "Nt: Andmekaitse Inspektsioon · Maksu- ja Tolliamet",
        "aria_label": "Asutuse nimi",
        "examples": (
            "Näited: «Andmekaitse Inspektsioon» · «Tarbijakaitse ja Tehnilise Järelevalve Amet»"
        ),
    },
    "ajalugu": {
        "placeholder": ("Nt: AvTS § 35 · KMS · CELEX-number · või lahendinumber 3-2-1-100-15"),
        "aria_label": "Õiguslik viide või kirjeldus",
        "examples": "Näited: «AvTS § 35» · «KMS» · «3-2-1-100-15»",
    },
    "sarnasus": {
        "placeholder": "Nt: AvTS § 35 · CELEX-number · või kirjeldage sätte sisu",
        "aria_label": "Õiguslik viide või vaba tekst",
        "examples": "Näited: «AvTS § 35» · «menetlustähtaegade pikendamine»",
    },
}


def _planned_workflow_card(cap: Capability) -> Any:
    """A directory entry for a not-yet-wired Analüüsikeskus workflow.

    Renders with a ``Tulekul`` badge in the header and no input form — the
    point is to advertise the workflow's existence (so it appears in the
    sidebar of the directory and is discoverable), not to expose a dead form.
    """
    return Card(
        CardHeader(
            H3(  # noqa: F405
                cap.canonical_name_et,
                " ",
                Badge("Tulekul", variant="warning"),
                cls="card-title",
            )
        ),
        CardBody(
            P(cap.one_line_description_et),  # noqa: F405
            Small(  # noqa: F405
                "See töövoog avaneb peagi.",
                cls="muted-text",
            ),
        ),
        cls="analyysikeskus-card analyysikeskus-card--planned",
    )


def _intent_workflow_card(cap: Capability, *, examples: str) -> Any:
    """Directory entry for the policy-intent workflow (#814 Phase 2b).

    The intent flow's intake form needs a *multi-line* textarea plus chip
    selectors, which don't fit the single-``sisend`` GET form the other
    workflow cards use. Render a compact link-style card instead — the
    primary action navigates to ``/analyysikeskus/moju-poliitikamottest``
    where the full intake form lives.
    """
    return Card(
        CardHeader(H3(cap.canonical_name_et, cls="card-title")),  # noqa: F405
        CardBody(
            P(cap.one_line_description_et),  # noqa: F405
            A(  # noqa: F405
                "Alusta analüüsi →",
                href=cap.target_url,
                cls="btn btn-primary",
            ),
            Small(examples, cls="muted-text"),  # noqa: F405
        ),
    )


def _capability_card(cap: Capability) -> Any:
    """Render a Capability as a directory entry — full card or "Tulekul" card."""
    if cap.status != "live":
        return _planned_workflow_card(cap)
    inputs = _ANALYYSIKESKUS_INPUTS.get(cap.slug)
    if inputs is None:
        # A live capability that's wired but has no input-form metadata — fall
        # back to the planned-card layout so the user can still see it (and the
        # developer notices the missing entry).
        return _planned_workflow_card(cap)
    # The policy-intent workflow has a richer intake form (textarea + chip
    # selectors) than the single ``sisend`` text input every other workflow
    # uses — render a link-style card that hands the user straight to the
    # full intake form instead of trying to cram everything onto the card.
    if cap.slug == "moju-poliitikamottest":
        return _intent_workflow_card(cap, examples=inputs["examples"])
    return _workflow_card(
        title=cap.canonical_name_et,
        purpose=cap.one_line_description_et,
        action=cap.target_url,
        input_name="sisend",
        input_placeholder=inputs["placeholder"],
        input_aria_label=inputs["aria_label"],
        examples=inputs["examples"],
    )


def analyysikeskus_page(req: Request):
    """GET /analyysikeskus — the legal-analysis workflow directory.

    The card list is generated from :data:`app.ui.capabilities.CAPABILITIES`,
    filtered to entries whose ``target_url`` lives under ``/analyysikeskus``.
    Live capabilities (``Normi mõjuahel``, ``EL ülevõtt``) render with their
    input form; planned ones (``Kohtupraktika``, ``Sanktsioonid``, ...) render
    as a compact "Tulekul" card. The order matches the dict's order — see
    ``app/ui/capabilities.py`` for the canonical ordering rules.
    """
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

    # Pull every capability whose target lives under /analyysikeskus —
    # preserves the canonical order from the dict (live first within each
    # use case).
    workflow_caps = [c for c in CAPABILITIES if c.target_url.startswith("/analyysikeskus")]
    workflow_cards = [_capability_card(c) for c in workflow_caps]

    return PageShell(
        *header,
        *workflow_cards,
        _recent_analyses_card(recent),
        title="Analüüsikeskus",
        user=auth,
        theme=theme,
        active_nav="/analyysikeskus",
    )
