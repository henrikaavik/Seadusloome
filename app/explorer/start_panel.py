"""Org-scoped data queries for the Õiguskaart **contextual start panel** (#754).

Issue #754 (epic #762, design doc ``docs/2026-05-12-oiguskaart-evidence-map.md``,
workstream A): ``/explorer`` with no ``?focus=`` / ``?draft=`` / ``?search=`` query
param no longer renders the cold 90k-entity category overview. Instead it shows a
small **start panel** inside the (otherwise empty) graph area — your bookmarks,
recent high-risk impact reports for your org, recent drafts you've touched, a
``Normi mõjuahel`` shortcut, a search box, and an explicit "Sirvi liikide kaupa"
(opt-in to today's overview).

This module is the *data* layer: three pure, individually-testable functions —
:func:`get_user_bookmarks`, :func:`get_high_risk_reports`,
:func:`get_recent_drafts` — each with a clean signature that can later be wrapped
as a REST endpoint or an MCP tool (see ``CLAUDE.md`` — "Internal service
functions"). They mirror the helper pattern in ``app/templates/dashboard.py``
(``_get_bookmarks`` / ``_get_high_risk_reports``) — every query is **org-scoped
at the SQL layer** (bookmarks are scoped by ``user_id``), wrapped in
``try/except`` → log + return ``[]`` so a DB hiccup degrades the panel to "empty"
rather than 500-ing the explorer.

Each result row carries an ``explorer_url`` (a relative ``/explorer?focus=…`` /
``/explorer?draft=…`` link) so the page module doesn't have to know the URL
contract — that keeps the ``?focus=`` / ``?draft=`` plumbing owned by one place.
"""

from __future__ import annotations

import logging
from typing import TypedDict
from urllib.parse import quote

from app.db import get_connection as _connect
from app.docs.impact.scoring import IMPACT_BAND_LABELS_ET, impact_band

logger = logging.getLogger(__name__)

# Per-section row caps — kept small so the panel stays a compact "where do I
# pick up" surface, not another dashboard. Mirrors the dashboard's widget caps.
_MAX_BOOKMARKS = 8
_MAX_HIGH_RISK = 6
_MAX_RECENT_DRAFTS = 6

# Bands considered "kõrge riskiga" for the start-panel section. ``impact_band``
# (the 51-80 / 81-100 tiers) is the single source of truth for the cut-off; the
# SQL pre-filter uses ``impact_score > 50`` as a cheap gate before the helper
# produces the user-facing label.
_HIGH_RISK_BANDS = frozenset({"high", "critical"})


# ---------------------------------------------------------------------------
# Result row shapes (TypedDicts — documentation + pyright, no runtime cost)
# ---------------------------------------------------------------------------


class BookmarkRow(TypedDict):
    """A single bookmark row for the start panel."""

    id: str
    entity_uri: str
    label: str
    explorer_url: str


class HighRiskReportRow(TypedDict):
    """A recent high-risk impact report (one row per draft, latest report)."""

    draft_id: str
    title: str
    impact_score: int
    band: str
    band_label: str
    conflict_count: int
    affected_count: int
    gap_count: int
    generated_at: object  # datetime | None — formatted by the page layer
    report_url: str
    explorer_url: str


class RecentDraftRow(TypedDict):
    """A draft the user has touched recently (org-scoped)."""

    draft_id: str
    title: str
    status: str
    updated_at: object  # datetime | None — formatted by the page layer
    detail_url: str
    explorer_url: str


# ---------------------------------------------------------------------------
# URL helpers — keep the ?focus= / ?draft= contract in one place
# ---------------------------------------------------------------------------


def _explorer_focus_url(entity_uri: str) -> str:
    """Return a ``/explorer?focus=<uri>`` link (URL-encoded URI).

    Mirrors :func:`app.docs.report_routes.explorer_focus_url` but lives here so
    ``app/explorer/`` does not import from ``app/docs/`` for a one-liner.
    """
    return f"/explorer?focus={quote(entity_uri, safe='')}"


def _explorer_draft_url(draft_id: str) -> str:
    """Return a ``/explorer?draft=<id>`` link for a draft's impact overlay."""
    return f"/explorer?draft={quote(str(draft_id), safe='')}"


# ---------------------------------------------------------------------------
# Section 1: Sinu järjehoidjad
# ---------------------------------------------------------------------------


def get_user_bookmarks(user_id: str | None, *, limit: int = _MAX_BOOKMARKS) -> list[BookmarkRow]:
    """Return the user's own bookmarks, newest first.

    Scoped by ``user_id`` — bookmarks are personal (the ``bookmarks`` table has
    no ``org_id`` column; the per-user scope *is* the access control). Returns
    ``[]`` for a missing ``user_id`` or on any DB error.

    Args:
        user_id: The current user's id (string UUID). ``None`` → ``[]``.
        limit: Maximum rows to return.

    Returns:
        A list of :class:`BookmarkRow`, each with an ``explorer_url`` that
        focuses the bookmarked entity.
    """
    if not user_id:
        return []
    try:
        with _connect() as conn:
            rows = conn.execute(
                """
                SELECT id, entity_uri, label
                FROM bookmarks
                WHERE user_id = %s
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (user_id, limit),
            ).fetchall()
    except Exception:
        logger.exception("start_panel: failed to fetch bookmarks for user %s", user_id)
        return []
    out: list[BookmarkRow] = []
    for r in rows:
        entity_uri = str(r[1])
        out.append(
            {
                "id": str(r[0]),
                "entity_uri": entity_uri,
                "label": (r[2] or entity_uri),
                "explorer_url": _explorer_focus_url(entity_uri),
            }
        )
    return out


# ---------------------------------------------------------------------------
# Section 2: Hiljutised kõrge riskiga leiud
# ---------------------------------------------------------------------------


def get_high_risk_reports(
    org_id: str | None, *, limit: int = _MAX_HIGH_RISK
) -> list[HighRiskReportRow]:
    """Return recent High/Critical impact reports for the org, newest first.

    Org-scoped via ``drafts.org_id`` in the JOIN. Picks the *latest* report per
    draft first, then filters by ``impact_score > 50`` — so a draft re-analysed
    from high risk down to medium/low drops off the list instead of clinging to
    its stale high-risk row (same logic as the dashboard's "Kõrge riskiga leiud"
    widget). Returns ``[]`` for a missing ``org_id`` or on any DB error.

    Args:
        org_id: The current user's organisation id. ``None`` → ``[]``.
        limit: Maximum rows to return.

    Returns:
        A list of :class:`HighRiskReportRow` (one per draft). Each row has a
        ``report_url`` (``/drafts/<id>/report``) and an ``explorer_url``
        (``/explorer?draft=<id>``).
    """
    if not org_id:
        return []
    try:
        with _connect() as conn:
            rows = conn.execute(
                """
                WITH latest_report AS (
                    SELECT DISTINCT ON (ir.draft_id)
                           ir.draft_id, ir.impact_score, ir.conflict_count,
                           ir.affected_count, ir.gap_count, ir.generated_at
                    FROM impact_reports ir
                    JOIN drafts d ON d.id = ir.draft_id
                    WHERE d.org_id = %s
                    ORDER BY ir.draft_id, ir.generated_at DESC
                )
                SELECT lr.draft_id, d.title, lr.impact_score,
                       lr.conflict_count, lr.affected_count, lr.gap_count,
                       lr.generated_at
                FROM latest_report lr
                JOIN drafts d ON d.id = lr.draft_id
                WHERE lr.impact_score > 50
                ORDER BY lr.generated_at DESC
                LIMIT %s
                """,
                (org_id, limit),
            ).fetchall()
    except Exception:
        logger.exception("start_panel: failed to fetch high-risk reports for org %s", org_id)
        return []
    out: list[HighRiskReportRow] = []
    for r in rows:
        draft_id = str(r[0])
        score = int(r[2] or 0)
        band = impact_band(score)
        # Belt-and-braces: the SQL ``> 50`` gate already excludes low/medium,
        # but if the band cut-offs ever move, keep the section honest.
        if band not in _HIGH_RISK_BANDS:
            continue
        out.append(
            {
                "draft_id": draft_id,
                "title": r[1] or "Pealkirjata eelnõu",
                "impact_score": score,
                "band": band,
                "band_label": IMPACT_BAND_LABELS_ET[band],
                "conflict_count": int(r[3] or 0),
                "affected_count": int(r[4] or 0),
                "gap_count": int(r[5] or 0),
                "generated_at": r[6],
                "report_url": f"/drafts/{draft_id}/report",
                "explorer_url": _explorer_draft_url(draft_id),
            }
        )
    return out


# ---------------------------------------------------------------------------
# Section 3: Sinu hiljutised eelnõud
# ---------------------------------------------------------------------------


def get_recent_drafts(
    org_id: str | None, *, limit: int = _MAX_RECENT_DRAFTS
) -> list[RecentDraftRow]:
    """Return drafts the org has touched recently, most recently updated first.

    Org-scoped via ``drafts.org_id``. Ordered by ``updated_at`` so a draft that
    was just edited / re-analysed surfaces at the top. The status comes from the
    ``drafts`` table directly (the simple column — the start panel only needs a
    human label, not the version-backed pipeline state). Returns ``[]`` for a
    missing ``org_id`` or on any DB error.

    Args:
        org_id: The current user's organisation id. ``None`` → ``[]``.
        limit: Maximum rows to return.

    Returns:
        A list of :class:`RecentDraftRow`. Each row has a ``detail_url``
        (``/drafts/<id>``) and an ``explorer_url`` (``/explorer?draft=<id>`` —
        "Ava mõjukaart").
    """
    if not org_id:
        return []
    try:
        with _connect() as conn:
            rows = conn.execute(
                """
                SELECT id, title, status, updated_at
                FROM drafts
                WHERE org_id = %s
                ORDER BY updated_at DESC NULLS LAST
                LIMIT %s
                """,
                (org_id, limit),
            ).fetchall()
    except Exception:
        logger.exception("start_panel: failed to fetch recent drafts for org %s", org_id)
        return []
    out: list[RecentDraftRow] = []
    for r in rows:
        draft_id = str(r[0])
        out.append(
            {
                "draft_id": draft_id,
                "title": r[1] or "Pealkirjata eelnõu",
                "status": str(r[2] or ""),
                "updated_at": r[3],
                "detail_url": f"/drafts/{draft_id}",
                "explorer_url": _explorer_draft_url(draft_id),
            }
        )
    return out


# ---------------------------------------------------------------------------
# Bundle — one call for the page layer
# ---------------------------------------------------------------------------


class StartPanelData(TypedDict):
    """Everything the start panel renders, fetched in one go."""

    bookmarks: list[BookmarkRow]
    high_risk_reports: list[HighRiskReportRow]
    recent_drafts: list[RecentDraftRow]


def load_start_panel_data(user_id: str | None, org_id: str | None) -> StartPanelData:
    """Fetch all three start-panel sections for the given user/org.

    Convenience wrapper so the page handler makes a single call. Each underlying
    query independently degrades to ``[]`` on error, so this never raises.

    Args:
        user_id: The current user's id (for personal bookmarks).
        org_id: The current user's organisation id (for org-scoped reports/drafts).

    Returns:
        A :class:`StartPanelData` dict.
    """
    return {
        "bookmarks": get_user_bookmarks(user_id),
        "high_risk_reports": get_high_risk_reports(org_id),
        "recent_drafts": get_recent_drafts(org_id),
    }
