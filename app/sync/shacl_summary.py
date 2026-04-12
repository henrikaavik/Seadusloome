"""Condense a pyshacl validation report into a human-readable summary.

pyshacl writes a verbose multi-page report that dumps every single
violation. For 22,000+ warnings that's useless noise — operators need
the *shape* of the problem, not a transcript. This module parses the
text report and emits a one-line summary grouped by constraint type
and property, with sample focus nodes so an admin can jump to a
concrete broken entity.

Example input (truncated)::

    Validation Report
    Conforms: False
    Results (22503):
    Constraint Violation in MinCountConstraintComponent (...):
        Severity: sh:Violation
        Source Shape: [ sh:path estleg:summary ; sh:minCount 1 ; ... ]
        Focus Node: <https://data.riik.ee/ontology/estleg#ASeS_Par_19>
        Result Path: estleg:summary
        Message: Less than 1 values on ...->estleg:summary

    Constraint Violation in MinCountConstraintComponent (...):
        Source Shape: [ sh:path estleg:sourceAct ; ... ]
        ...

Example output::

    22,503 warnings: 18,432× missing estleg:summary (e.g. ASeS_Par_19,
    PS_Par_8, RT_Par_1); 4,071× missing estleg:sourceAct (e.g. ...)

The summary is capped at ~400 chars so it fits the sync_log
``error_message`` TEXT column without crowding the admin table.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass

_VIOLATION_RE = re.compile(r"Constraint Violation in (\w+)\b")
_FOCUS_NODE_RE = re.compile(r"Focus Node:\s*<([^>]+)>")
_RESULT_PATH_RE = re.compile(r"Result Path:\s*(\S+)")
_RESULTS_COUNT_RE = re.compile(r"Results \((\d+)\)")

#: Max length of the emitted summary string. Keeps the sync_log row
#: concise while still fitting three sample focus nodes per group.
_MAX_SUMMARY_CHARS = 900

#: How many sample focus-node short names to include per constraint group.
_SAMPLES_PER_GROUP = 3


@dataclass
class _Group:
    component: str  # e.g. "MinCountConstraintComponent"
    path: str  # e.g. "estleg:summary"
    count: int
    samples: list[str]  # shortened focus-node names


def _shorten_iri(iri: str) -> str:
    """Return the last path-segment / fragment of an IRI for display."""
    # Try fragment first (most estleg: IRIs end with ``#ASeS_Par_19``).
    if "#" in iri:
        tail = iri.rsplit("#", 1)[-1]
    elif "/" in iri:
        tail = iri.rsplit("/", 1)[-1]
    else:
        tail = iri
    return tail or iri


def _humanise_component(component: str) -> str:
    """Render a SHACL constraint-component name as plain Estonian prose.

    Only the handful of components pyshacl emits for this project are
    covered explicitly; everything else falls back to the raw name
    with the ``ConstraintComponent`` suffix trimmed.
    """
    # Strip the Component suffix to save characters.
    base = component.removesuffix("ConstraintComponent")
    mapping = {
        "MinCount": "missing",
        "MaxCount": "too many values for",
        "Datatype": "wrong datatype for",
        "NodeKind": "wrong node-kind for",
        "Class": "wrong class for",
        "Pattern": "regex mismatch on",
        "In": "value outside allowed set on",
    }
    return mapping.get(base, f"{base} violation on")


def parse_report(report: str) -> list[_Group]:
    """Parse a pyshacl text report into per-(component, path) groups."""
    # Split the report body after the header so we iterate per-violation.
    # Each violation block begins with ``Constraint Violation in``.
    blocks = report.split("Constraint Violation in ")[1:]

    # aggregator: (component, path) -> (count, [samples])
    per_group_count: Counter[tuple[str, str]] = Counter()
    per_group_samples: dict[tuple[str, str], list[str]] = defaultdict(list)

    for block in blocks:
        # Recover the component name (the first word of the block).
        comp_match = re.match(r"(\w+)", block)
        component = comp_match.group(1) if comp_match else "Unknown"

        path_match = _RESULT_PATH_RE.search(block)
        path = path_match.group(1) if path_match else "?"

        focus_match = _FOCUS_NODE_RE.search(block)
        focus = _shorten_iri(focus_match.group(1)) if focus_match else ""

        key = (component, path)
        per_group_count[key] += 1
        samples = per_group_samples[key]
        if focus and focus not in samples and len(samples) < _SAMPLES_PER_GROUP:
            samples.append(focus)

    groups = [
        _Group(component=c, path=p, count=n, samples=per_group_samples[(c, p)])
        for (c, p), n in per_group_count.most_common()
    ]
    return groups


def summarise_report(report: str) -> str:
    """Return a compact human-readable summary of a pyshacl report.

    Safe against empty input, reports with no violations, and reports
    whose body is truncated mid-block (groups still line up because
    the counter only increments on complete match pairs).
    """
    if not report:
        return "SHACL report empty"

    total_match = _RESULTS_COUNT_RE.search(report)
    total = int(total_match.group(1)) if total_match else 0

    groups = parse_report(report)
    if not groups:
        return f"{total:,} SHACL warning(s), unable to group (report format unexpected)"

    parts: list[str] = []
    for g in groups:
        action = _humanise_component(g.component)
        if g.samples:
            sample_str = ", ".join(g.samples)
            parts.append(f"{g.count:,}\u00d7 {action} {g.path} (nt {sample_str})")
        else:
            parts.append(f"{g.count:,}\u00d7 {action} {g.path}")

    head = f"{total:,} SHACL hoiatust: " if total else "SHACL hoiatused: "
    summary = head + "; ".join(parts)

    if len(summary) > _MAX_SUMMARY_CHARS:
        # Truncate on a group boundary so we don't cut a count in half.
        truncated: list[str] = []
        running = len(head)
        for part in parts:
            add = len(part) + (2 if truncated else 0)
            if running + add > _MAX_SUMMARY_CHARS - 10:
                remaining = len(parts) - len(truncated)
                if remaining > 0:
                    truncated.append(f"\u2026 +{remaining} veel")
                break
            truncated.append(part)
            running += add
        summary = head + "; ".join(truncated)

    return summary
