"""Estonian-language display labels for draft-related classifications.

Shared between the HTML report routes and the .docx exporter so both
surfaces stay in sync when labels change.
"""

from __future__ import annotations

# Map ontology class IRI local names → Estonian display labels.
TYPE_LABELS_ET: dict[str, str] = {
    "EnactedLaw": "Kehtiv seadus",
    "DraftLegislation": "Eelnõu",
    "CourtDecision": "Kohtulahend",
    "EULegislation": "EL õigusakt",
    "EUCourtDecision": "EL kohtulahend",
    "Provision": "Säte",
    "TopicCluster": "Teemaklaster",
}
