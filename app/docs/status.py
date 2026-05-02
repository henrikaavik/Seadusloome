"""Single source of truth for ``drafts.status`` (#625).

Before this module existed, draft status semantics were scattered across
seven sites:

    * ``app/docs/draft_model.VALID_STATUSES`` — the validation tuple.
    * ``app/docs/routes._STATUS_STAGES`` — pipeline order + Estonian copy.
    * ``app/docs/routes._STATUS_LABELS`` — value → Estonian label.
    * ``app/docs/routes._STATUS_VARIANT_MAP`` — value → ``BadgeVariant``.
    * ``app/docs/routes._STATUS_KEY_MAP`` — value → semantic CSS key.
    * Six raw ``UPDATE drafts SET status = '<literal>' ...`` writes
      across ``parse_handler`` / ``extract_handler`` / ``analyze_handler``
      / ``retry_handler`` / ``draft_model.update_draft_status``.

The duplication made it impossible to add a new status without grepping
the codebase, and the raw SQL writes meant a typo in any one site
silently fell through Postgres' ``CHECK`` constraint at runtime instead
of pyright at compile time.

This module collapses all of that into one tuple of ``DraftStatus``
records plus a typed :func:`update_draft_status` helper. Every read of
"the Estonian label", "the badge variant", "is this terminal", or
"what's the next stage in the happy path" goes through
:data:`STATUS_BY_VALUE`. Every write goes through
:func:`update_draft_status` so an unknown value raises ``ValueError``
in the application layer instead of waiting for the DB to reject it.

§4.2 status semantics decision (locked):
    Per the Eelnõud sprint plan §4.2, ``drafts.status`` is the canonical
    write path for now. The version-aware cutover (writing to
    ``draft_versions.status``) lands in #618 PR-B alongside the read
    cutover. This module deliberately writes ONLY to ``drafts`` so the
    sequencing is explicit and a bisect can pin the cutover point.

Cost-of-change rationale:
    The fixed signature ``update_draft_status(conn, draft_id, status,
    *, error_message=..., error_debug=..., extras=...)`` covers every
    current call site without forcing the helper to re-implement the
    half-dozen ad-hoc UPDATE shapes the handlers had grown. ``extras``
    keeps the rare two-column atomic writes (``parsed_text_encrypted``
    in the parse handler, ``entity_count`` in the extract handler) in
    a single UPDATE so we don't break the #626 atomicity guarantee.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from app.ui.primitives.badge import BadgeVariant


@dataclass(frozen=True)
class DraftStatus:
    """One row in the status SSOT.

    Attributes:
        value: The string written to ``drafts.status``. Matches the
            Postgres ``CHECK (status in (...))`` constraint in
            migration 005.
        label_et: The Estonian label rendered in the UI (status badge,
            stage tracker, filter checkboxes).
        badge_variant: The ``BadgeVariant`` used by ``app.ui.primitives.badge``
            so the colour stays in sync with the design system.
        is_terminal: ``True`` for statuses the pipeline never transitions
            out of on its own (``ready``, ``failed``). The polling /
            WebSocket layer uses this to decide when to stop pushing
            updates and when to freeze the elapsed-time label.
        successor: The next status in the happy-path pipeline, or
            ``None`` for terminal stages. ``failed`` returns ``None``
            because retry is an explicit user action, not part of the
            normal flow.
        order: Zero-based position in the stage tracker. ``failed`` uses
            ``99`` so it sorts after the happy path stages without
            colliding with them.
        css_key: Semantic key used by the status badge / tracker CSS so
            the existing ``draft-status-{key}`` and ``status-{key}``
            class names continue to work after the cutover.
    """

    value: str
    label_et: str
    badge_variant: BadgeVariant
    is_terminal: bool
    successor: str | None
    order: int
    css_key: str


# The canonical status table. Order matters: the happy path stages
# read top-to-bottom, with ``failed`` pinned to the bottom as the
# terminal-failure branch.
#
# Estonian labels are copied verbatim from the previous
# ``app/docs/routes.py`` ``_STATUS_STAGES`` + ``_STATUS_LABELS`` maps
# (and the filter-bar group) so the visible UI copy is unchanged.
DRAFT_STATUSES: tuple[DraftStatus, ...] = (
    DraftStatus(
        value="uploaded",
        label_et="Üles laaditud",
        badge_variant="default",
        is_terminal=False,
        successor="parsing",
        order=0,
        css_key="pending",
    ),
    DraftStatus(
        value="parsing",
        label_et="Töötlemine",
        badge_variant="primary",
        is_terminal=False,
        successor="extracting",
        order=1,
        css_key="running",
    ),
    DraftStatus(
        value="extracting",
        label_et="Olemite eraldamine",
        badge_variant="primary",
        is_terminal=False,
        successor="analyzing",
        order=2,
        css_key="running",
    ),
    DraftStatus(
        value="analyzing",
        label_et="Mõjude analüüs",
        badge_variant="primary",
        is_terminal=False,
        successor="ready",
        order=3,
        css_key="running",
    ),
    DraftStatus(
        value="ready",
        label_et="Valmis",
        badge_variant="success",
        is_terminal=True,
        successor=None,
        order=4,
        css_key="ok",
    ),
    DraftStatus(
        value="failed",
        label_et="Ebaõnnestus",
        badge_variant="danger",
        is_terminal=True,
        successor=None,
        order=99,
        css_key="failed",
    ),
)


# O(1) lookup by status value. Built once at import time.
STATUS_BY_VALUE: dict[str, DraftStatus] = {s.value: s for s in DRAFT_STATUSES}

# Tuple of valid status values for backwards-compatibility with the old
# ``app.docs.draft_model.VALID_STATUSES`` import path. New callers should
# prefer ``STATUS_BY_VALUE`` (membership check is identical, and a hit
# returns the full ``DraftStatus`` record with no extra lookup).
VALID_STATUSES: tuple[str, ...] = tuple(s.value for s in DRAFT_STATUSES)

# Frozen set of terminal statuses. Used by the pipeline handlers to
# decide whether to stamp ``processing_completed_at = now()`` or clear
# it back to NULL (#670 — the frozen completion timestamp).
TERMINAL_STATUSES: frozenset[str] = frozenset(s.value for s in DRAFT_STATUSES if s.is_terminal)

# Pipeline stage tuple kept in DB-write order. Excludes ``failed`` so
# the tracker UI renders left-to-right during normal operation; the
# failure branch is rendered separately by the routes layer.
PIPELINE_STAGES: tuple[DraftStatus, ...] = tuple(
    s for s in sorted(DRAFT_STATUSES, key=lambda d: d.order) if s.value != "failed"
)


def update_draft_status(
    conn: Any,
    draft_id: Any,
    status: str,
    error_message: str | None = None,
    *,
    error_debug: str | None = None,
    extras: Mapping[str, Any] | None = None,
    expected_status: str | None = None,
) -> bool:
    """Typed UPDATE that validates ``status`` against :data:`DRAFT_STATUSES`.

    §4.2 cutover: this writes ONLY to ``drafts``. The version-aware
    ``draft_versions`` write lands in #618 PR-B alongside the read
    cutover. The handler must NOT touch ``draft_versions`` here -- a
    bisect on the version cutover relies on this file containing zero
    references to the new table.

    Behaviour:
        * Always writes ``status``, ``updated_at = now()``,
          ``error_message`` and ``error_debug``. The two error columns
          default to ``NULL`` so any successful transition (parsing →
          extracting → analyzing → ready, or the retry reset uploaded)
          clears stale failure info from a prior attempt. The
          ``_mark_draft_failed`` paths pass explicit strings to set
          them. This matches the prior raw-SQL behaviour across all
          handlers (#625 §4.2).
        * Always stamps ``processing_completed_at`` -- ``now()`` for
          terminal statuses (``ready`` / ``failed``), ``NULL`` otherwise.
          This preserves the #670 frozen-completion-timestamp invariant
          without each caller having to remember it.
        * ``extras`` carries any additional column writes that must land
          in the SAME ``UPDATE`` for atomicity (e.g. ``entity_count``
          from the extract handler, ``parsed_text_encrypted`` from the
          parse handler). Keys are interpolated into the SQL so callers
          MUST only pass safe column names -- the only call sites in
          this package pass ``parsed_text_encrypted`` and
          ``entity_count``.
        * ``expected_status`` adds an ``AND status = %s`` predicate to
          the WHERE clause for optimistic-concurrency control (used by
          the retry path -- only flip ``failed`` -> ``uploaded`` when
          the draft is still ``failed`` at write time). The expected
          value is also validated against :data:`STATUS_BY_VALUE`.

    Args:
        conn: An open psycopg connection. The caller is responsible for
            ``commit()`` so this update can be batched into a larger
            transaction (matches the rest of ``draft_model``).
        draft_id: ``UUID`` or string. Coerced to ``str`` for psycopg.
        status: New status value. Must be in :data:`STATUS_BY_VALUE`
            or ``ValueError`` is raised before any SQL runs.
        error_message: User-facing Estonian message. ``None`` (default)
            clears the column to NULL; pass a string to set it. Kept
            positional so the legacy
            ``update_draft_status(conn, id, "failed", "Boom")`` call
            shape continues to work.
        error_debug: Raw technical detail for admin triage (#609).
            ``None`` (default) clears the column to NULL.
        extras: Additional ``column_name -> value`` writes that must
            land in the same UPDATE. ``None`` (default) when not needed.
        expected_status: Optimistic-concurrency guard. When supplied,
            the UPDATE only runs if the row is currently in this
            status; ``rowcount == 0`` means another writer beat us.

    Returns:
        ``True`` if a row was actually updated, ``False`` if the WHERE
        clause matched nothing. Mirrors the previous
        ``draft_model.update_draft_status`` contract.

    Raises:
        ValueError: ``status`` (or ``expected_status``) is not a known
            draft status.
    """
    if status not in STATUS_BY_VALUE:
        raise ValueError(f"Unknown draft status: {status!r}")
    if expected_status is not None and expected_status not in STATUS_BY_VALUE:
        raise ValueError(f"Unknown expected draft status: {expected_status!r}")

    # Build the SET clause in a deterministic order so test assertions
    # on the executed SQL string remain stable across runs. Always
    # writes the same fixed prefix; ``extras`` are appended in sorted
    # column-name order at the tail.
    set_parts: list[str] = [
        "status = %s",
        "error_message = %s",
        "error_debug = %s",
        "updated_at = now()",
    ]
    params: list[Any] = [status, error_message, error_debug]

    # #670: terminal transitions stamp the frozen completion timestamp;
    # non-terminal transitions clear it back to NULL so a retry path
    # (``failed`` -> ``uploaded``) doesn't carry stale completion time.
    if status in TERMINAL_STATUSES:
        set_parts.append("processing_completed_at = now()")
    else:
        set_parts.append("processing_completed_at = null")

    # ``extras`` columns are interpolated by name; values are bound as
    # parameters. Sorted for deterministic SQL output.
    if extras:
        for column in sorted(extras):
            set_parts.append(f"{column} = %s")
            params.append(extras[column])

    set_clause = ",\n            ".join(set_parts)

    # WHERE clause -- always ``id = %s`` plus an optional optimistic
    # ``status = %s`` predicate for the retry path.
    where_parts: list[str] = ["id = %s"]
    params.append(str(draft_id))
    if expected_status is not None:
        where_parts.append("status = %s")
        params.append(expected_status)
    where_clause = " and ".join(where_parts)

    # Note: deliberately writing to ``drafts`` only. Do NOT add a write
    # to ``draft_versions`` here -- that cutover is owned by #618 PR-B
    # and depends on the read path being version-aware first.
    sql = f"""
        update drafts
        set {set_clause}
        where {where_clause}
    """
    result = conn.execute(sql, tuple(params))
    return (result.rowcount or 0) > 0
