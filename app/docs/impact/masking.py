"""Cross-org masking for impact-report conflict rows (#844 A3b + remediation).

The cross-draft arm of the conflict query (:data:`app.docs.impact.queries.
CONFLICTS`) reports "another draft already references this section". That
"other draft" can belong to a **different organisation**, and its URI /
``rdfs:label`` carry the foreign draft's identity (the graph URI embeds
the draft UUID; the label is the draft title). Surfacing either across an
org boundary leaks pre-publication draft metadata.

Two entry points share one masking rule:

* :func:`mask_conflict_rows` — applied **at detection time** in
  :meth:`app.docs.impact.analyzer.ImpactAnalyzer._detect_conflicts`, so a
  freshly-generated report never *persists* a foreign org's draft URI in
  the first place.

* :func:`mask_stored_conflict_rows` — applied **at render time** to rows
  read back from a persisted ``impact_reports.report_data`` blob. Reports
  generated before this fix already contain foreign draft URIs; this
  guard scrubs them on the way to every render surface (Analüüsikeskus
  Tõendid, the .docx export, the explorer draft subgraph).

The masking mirrors :func:`app.docs.similarity.list_similar_drafts_for_view`:
a cross-org row keeps its *shape* and its non-identifying fields (so the
count of conflicts is honest) but its identifying fields
(``conflicting_entity`` URI + ``conflicting_label`` title) are blanked and
a neutral Estonian label is substituted, with ``masked=True`` flagged.

Only **draft-graph** conflict rows are subject to org ownership — the
secondary court-decision arm (``interpretsLaw`` / ``interpretedBy``)
points at public Riigikohus decisions and is always safe to show.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from typing import Any

from app.ontology.scoping import draft_id_from_uri, is_adhoc_graph_uri

logger = logging.getLogger(__name__)

# Estonian placeholder shown in place of a foreign org's draft title. The
# count of conflicts stays accurate; only the identity is withheld.
_MASKED_CONFLICT_LABEL = "Teise asutuse eelnõu (juurdepääs piiratud)"


def _conflict_row_draft_id(row: dict[str, Any]) -> str | None:
    """Return the draft UUID a conflict *row* points at, or ``None``.

    A cross-draft conflict row's ``conflicting_entity`` is the other
    draft's subject IRI (``…/drafts/<uuid>#self``); some legacy rows may
    only carry ``draft_ref`` or the raw ``other_graph`` projection.
    Court-decision rows (the secondary arm) return ``None`` — they are
    public and never masked. Adhoc probe rows also return ``None`` here
    because they are stripped entirely upstream
    (:func:`drop_adhoc_conflict_rows`), never masked.
    """
    for key in ("conflicting_entity", "other_graph", "otherGraph"):
        val = str(row.get(key) or "").strip()
        if val:
            did = draft_id_from_uri(val)
            if did is not None:
                return did
    return None


def drop_adhoc_conflict_rows(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop conflict rows whose other-graph is an ephemeral adhoc probe.

    Defence in depth on top of the SPARQL ``!STRSTARTS(…adhoc…)`` filter
    (A3c): if a row ever arrives carrying an adhoc-namespaced
    ``conflicting_entity`` / ``other_graph`` (e.g. from a legacy persisted
    report generated before the query fix), it is silently removed — an
    adhoc probe is never a real conflict.
    """
    out: list[dict[str, Any]] = []
    for row in rows:
        candidate = (
            str(row.get("conflicting_entity") or "").strip()
            or str(row.get("other_graph") or "").strip()
            or str(row.get("otherGraph") or "").strip()
        )
        if candidate and is_adhoc_graph_uri(candidate):
            continue
        out.append(row)
    return out


def _mask_one(row: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of *row* with the foreign draft identity blanked."""
    masked = dict(row)
    masked["conflicting_entity"] = ""
    masked["conflicting_label"] = _MASKED_CONFLICT_LABEL
    # Drop any raw graph projection so the foreign UUID can't leak via it.
    masked.pop("other_graph", None)
    masked.pop("otherGraph", None)
    masked["masked"] = True
    return masked


def mask_conflict_rows(
    rows: Sequence[dict[str, Any]],
    owned_draft_ids: Iterable[str],
) -> list[dict[str, Any]]:
    """Mask conflict rows pointing at drafts outside *owned_draft_ids*.

    Args:
        rows: Conflict rows (already adhoc-stripped) as produced by the
            analyzer / read from a stored report. Each is a plain dict
            with ``conflicting_entity`` / ``conflicting_label`` keys.
        owned_draft_ids: The set of draft UUIDs the *viewing* org owns.
            A cross-draft row whose target draft UUID is in this set is
            kept verbatim; any other cross-draft row is masked. Court /
            public rows (no resolvable draft UUID) are always kept.

    Returns:
        A new list, same length and order as *rows*, with foreign
        cross-draft rows masked. The conflict *count* is preserved so the
        impact score and "N konflikti" summary stay truthful — only the
        foreign identity is withheld.
    """
    owned = {str(d).strip() for d in owned_draft_ids if str(d).strip()}
    out: list[dict[str, Any]] = []
    for row in rows:
        did = _conflict_row_draft_id(row)
        if did is None:
            # Public (court-decision) row — always safe.
            out.append(dict(row))
            continue
        if did in owned:
            out.append(dict(row))
        else:
            out.append(_mask_one(row))
    return out


# ---------------------------------------------------------------------------
# DB-backed org lookup (used by both detection-time and render-time paths)
# ---------------------------------------------------------------------------


def fetch_owned_draft_ids(conn: Any, org_id: str) -> set[str]:
    """Return the set of draft UUIDs owned by *org_id* (lowercased str).

    A single indexed query against ``drafts``. Returns an empty set on
    any error or when *org_id* is falsy — the caller then masks *every*
    cross-draft conflict row, which is the safe failure mode.
    """
    if not org_id:
        return set()
    try:
        rows = conn.execute(
            "SELECT id::text FROM drafts WHERE org_id = %s",
            (str(org_id),),
        ).fetchall()
    except Exception:
        logger.warning("fetch_owned_draft_ids failed for org=%s", org_id, exc_info=True)
        return set()
    return {str(r[0]).strip() for r in rows if r and r[0]}


def mask_stored_conflict_rows(
    rows: Sequence[dict[str, Any]],
    *,
    viewer_org_id: str | None,
    conn: Any | None = None,
) -> list[dict[str, Any]]:
    """Adhoc-strip + cross-org-mask conflict rows read from a stored report.

    The render-time counterpart to :func:`mask_conflict_rows`, used where
    a persisted ``impact_reports.report_data`` blob is prepared for
    display (Analüüsikeskus Tõendid, .docx export, explorer subgraph).
    Reports written before #844 contain foreign draft URIs; this scrubs
    them every time the report is rendered, so no migration is required.

    Args:
        rows: The ``conflicts`` list from a parsed report dict.
        viewer_org_id: The viewing user's org. When ``None`` (or no
            ``conn`` is available) every cross-draft row is masked — the
            safe default for an unauthenticated / org-less context.
        conn: Optional open DB connection used to look up the viewer's
            owned draft UUIDs. When omitted, a fresh connection is taken
            via :func:`app.db.get_connection`.

    Returns:
        A new, adhoc-stripped, cross-org-masked list of conflict rows.
    """
    stripped = drop_adhoc_conflict_rows(rows or [])
    if not stripped:
        return []
    if not viewer_org_id:
        # No viewer org → mask every cross-draft row.
        return mask_conflict_rows(stripped, owned_draft_ids=set())

    if conn is not None:
        owned = fetch_owned_draft_ids(conn, str(viewer_org_id))
    else:
        from app.db import get_connection

        try:
            with get_connection() as own_conn:
                owned = fetch_owned_draft_ids(own_conn, str(viewer_org_id))
        except Exception:
            logger.warning(
                "mask_stored_conflict_rows: could not open DB for org=%s; masking all",
                viewer_org_id,
                exc_info=True,
            )
            owned = set()
    return mask_conflict_rows(stripped, owned_draft_ids=owned)


__all__ = [
    "drop_adhoc_conflict_rows",
    "mask_conflict_rows",
    "mask_stored_conflict_rows",
    "fetch_owned_draft_ids",
]
