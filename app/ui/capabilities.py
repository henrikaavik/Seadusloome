"""Authoritative capability dictionary for Seadusloome (B3).

This module is the **single source of truth** for every "what can I do" entry
in the system — every workflow, every primary action a ministry lawyer can
take. Per the plan in ``docs/2026-05-15-ontology-six-use-cases-plan.md``
(section 5 → Direction B → B3), it backs:

* The ``/chat`` InfoBox (replaces a hand-coded prose list).
* The ``/analyysikeskus`` directory page (cards generated from the dict).
* The future global search bar (B1) — capability dropdown.
* The future Töölaud "Mida soovid teha?" capability map (B2).

The point of centralisation: when a new workflow ships (say, A1 Sanctions),
it appears in every surface automatically and is described in the same words.
Today the system uses different wording for the same capability in different
places ("Normi mõjuahel" in Analüüsikeskus vs. "find impact of a provision"
in chat suggestions); a single dictionary fixes that.

Design notes
------------
* **Stable slugs.** ``slug`` is the URL-safe identifier used by callers
  (search dropdown, capability cards, tests). Treat it as a public API:
  do not rename without auditing every consumer.
* **Estonian-first.** ``canonical_name_et`` and ``one_line_description_et``
  are user-facing; they must read naturally to a ministry lawyer.
* **Lucide icon names.** ``icon`` is a Lucide icon slug — the icon system
  (see ``app.ui.primitives.icon``) renders these. Stick to common ones so
  the bundle stays small.
* **Status lifecycle.** ``"live"`` (shipped), ``"planned"`` (in the current
  plan, not yet wired), ``"deferred"`` (acknowledged scope, no eta).
* **Use-case bucket.** ``use_case_from_section_2`` references the six
  ministry-lawyer use cases enumerated in section 2 of the plan; it lets
  B2's capability map group cards by user intent.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Capability:
    """One discoverable workflow / primary action.

    Frozen so callers can safely hash / cache it; field order matches how the
    capability map will render (name + description above the fold, icon and
    URL drive the affordance, the rest is filter metadata).

    Args:
        slug: URL-safe stable identifier (diacritic-clean). Treated as a
            public API by B1's search dropdown and tests.
        canonical_name_et: User-facing Estonian name. Imperative phrasing
            preferred ("Käivita Normi mõjuahel" over "Normi mõjuahel").
        one_line_description_et: Single-sentence Estonian description for
            the capability card / search dropdown row.
        icon: Lucide icon slug (e.g. ``"search"``, ``"git-branch"``).
        target_url: Where the capability lives. ``/`` -prefixed app paths
            for in-app workflows; for actions that need a seed value (e.g.
            ``/chat/seed``), callers pass the seed in a separate field.
        example_input: Optional example query / input to show below the
            card. ``None`` if the workflow has no free-text entry.
        use_case_from_section_2: 1-6 — which of the six ministry-lawyer
            use cases this capability serves. Drives B2 grouping.
        mobile_visible: Whether this capability should surface in the
            mobile capability map / search results. Default ``True``;
            heavy graph workflows can opt out.
        requires_role: Optional list of RBAC roles that may see this
            capability. ``None`` means everyone authenticated.
        status: ``"live"`` | ``"planned"`` | ``"deferred"``. Drives the
            "Tulekul" badge on planned entries.
    """

    slug: str
    canonical_name_et: str
    one_line_description_et: str
    icon: str
    target_url: str
    example_input: str | None
    use_case_from_section_2: int
    mobile_visible: bool = True
    requires_role: list[str] | None = None
    status: str = "live"


# ---------------------------------------------------------------------------
# The authoritative capability list
# ---------------------------------------------------------------------------
#
# Order matters: B1's search dropdown shows the top N matches in this order
# when scores tie; B2's capability map renders cards top-to-bottom. Keep
# "live" capabilities first within each use-case group so an empty system
# still feels populated.
#
# When adding a new capability:
#   1. Pick a stable slug — lowercase, ascii-only, hyphenated.
#   2. Choose ``use_case_from_section_2`` from sect. 2 of the plan doc.
#   3. Start ``status="planned"``; flip to ``"live"`` when the route is wired.
#   4. Add a row to ``tests/test_capabilities.py`` if the new entry needs
#      special coverage (the uniqueness/status invariants run automatically).
CAPABILITIES: list[Capability] = [
    Capability(
        slug="globaalne-otsing",
        canonical_name_et="Otsi sätet, akti või mõistet",
        one_line_description_et=(
            "Leia konkreetne säte, õigusakt, kohtulahend, EL akt või mõiste ühest otsingust."
        ),
        icon="search",
        target_url="/",
        example_input="AvTS § 35",
        use_case_from_section_2=1,
        status="planned",
    ),
    Capability(
        slug="noustaja",
        canonical_name_et="Küsi Nõustajalt",
        one_line_description_et=(
            "Esita vabas vormis küsimus Eesti õiguse kohta — Nõustaja "
            "vastab ontoloogiale ja RAG-ile tuginedes."
        ),
        icon="message-circle",
        target_url="/chat/new",
        example_input="Mida tähendab AvTS § 35 lg 1 punkt 5?",
        use_case_from_section_2=1,
    ),
    Capability(
        slug="oiguskaart",
        canonical_name_et="Sirvi Õiguskaarti",
        one_line_description_et=(
            "Vaata õigussüsteemi visuaalse kaardina — leia sätte naabrid, viited ja kontekst."
        ),
        icon="map",
        target_url="/explorer",
        example_input=None,
        use_case_from_section_2=2,
    ),
    Capability(
        slug="eelnou-impact",
        canonical_name_et="Analüüsi eelnõu mõju",
        one_line_description_et=(
            "Laadi üles eelnõu (.docx / .pdf) ja vaata, mida see kehtivas "
            "õiguses muudab — konfliktid, mõjutatud sätted, lüngad."
        ),
        icon="file-text",
        target_url="/drafts",
        example_input=None,
        use_case_from_section_2=3,
    ),
    Capability(
        slug="normi-mojuahel",
        canonical_name_et="Käivita Normi mõjuahel",
        one_line_description_et=(
            "Vaata, mida sätte muudatus mõjutab — viitavad sätted, seotud "
            "eelnõud ja Riigikohtu praktika."
        ),
        icon="git-branch",
        target_url="/analyysikeskus/normi-mojuahel",
        example_input="AvTS § 35",
        use_case_from_section_2=3,
    ),
    Capability(
        slug="el-ulevott",
        canonical_name_et="Kontrolli EL ülevõttu",
        one_line_description_et=(
            "Vaata, kas Eesti õigus katab EL direktiivi või määruse — "
            "transponeerimise tabel ja katmata kohad."
        ),
        icon="globe",
        target_url="/analyysikeskus/el-ulevott",
        example_input="32016R0679",
        use_case_from_section_2=4,
    ),
    Capability(
        slug="kohtupraktika",
        canonical_name_et="Otsi kohtupraktikat sätte kohta",
        one_line_description_et=(
            "Leia kõik Riigikohtu ja EL Kohtu lahendid, mis tõlgendavad konkreetset sätet."
        ),
        icon="gavel",
        target_url="/analyysikeskus/kohtupraktika",
        example_input="KarS § 211",
        use_case_from_section_2=5,
        status="planned",
    ),
    Capability(
        slug="sanktsioonid",
        canonical_name_et="Vaata sanktsioonide indeksit",
        one_line_description_et=(
            "Leia kõik karistused ja meetmed konkreetses aktis või "
            "võrdle sanktsioone sarnastes aktides."
        ),
        icon="scale",
        target_url="/analyysikeskus/sanktsioonid",
        example_input="KarS § 211",
        use_case_from_section_2=5,
    ),
    Capability(
        slug="halduskoormus",
        canonical_name_et="Hinda halduskoormust",
        one_line_description_et=(
            "Loenda eelnõu uued kohustused, keelud, õigused ja load — VTK halduskoormuse hinnang."
        ),
        icon="trending-up",
        target_url="/analyysikeskus/halduskoormus",
        example_input=None,
        use_case_from_section_2=3,
        status="planned",
    ),
    Capability(
        slug="padevused",
        canonical_name_et="Kaardista pädevusi",
        one_line_description_et=(
            "Vaata, millised volitused on antud millisele asutusele — "
            "kattuvused ja lüngad pädevusalades."
        ),
        icon="users",
        target_url="/analyysikeskus/padevused",
        example_input="Andmekaitse Inspektsioon",
        use_case_from_section_2=6,
        status="planned",
    ),
    Capability(
        slug="ajalugu",
        canonical_name_et="Vaata ajaloolist kehtivust",
        one_line_description_et=(
            "Vaata sätte sõnastust ja kehtivust ajateljel — millal kehtis milline redaktsioon."
        ),
        icon="clock",
        target_url="/analyysikeskus/ajalugu",
        example_input="AvTS § 35",
        use_case_from_section_2=2,
        status="planned",
    ),
    Capability(
        slug="sarnasus",
        canonical_name_et="Otsi sarnaseid sätteid",
        one_line_description_et=(
            "Leia sõnastuselt või sisult sarnased sätted teistes aktides — "
            "kasulik koostamisel ja võrdluseks."
        ),
        icon="copy",
        target_url="/analyysikeskus/sarnasus",
        example_input="AvTS § 35",
        use_case_from_section_2=3,
    ),
    Capability(
        slug="el-tahtajad",
        canonical_name_et="Jälgi EL ülevõtu tähtaegasid",
        one_line_description_et=(
            "Vaata, millised EL direktiivid lähenevad ülevõtu tähtajale "
            "ja kus on Eesti poole töö pooleli."
        ),
        icon="calendar",
        target_url="/dashboard",
        example_input=None,
        use_case_from_section_2=4,
        status="planned",
    ),
]


# ---------------------------------------------------------------------------
# Lookup helpers
# ---------------------------------------------------------------------------
#
# Kept as module-level functions (not methods on Capability) so callers
# can compose them naturally — e.g. ``[c for c in live_capabilities() if ...]``.
# Each helper returns a *new* list so callers can safely mutate.


def get_capability(slug: str) -> Capability | None:
    """Return the capability with ``slug`` (or ``None`` if no match).

    Linear scan over ``CAPABILITIES`` — the list is small enough (< 50)
    that an index would be premature.
    """
    for cap in CAPABILITIES:
        if cap.slug == slug:
            return cap
    return None


def live_capabilities() -> list[Capability]:
    """Return capabilities with ``status == "live"`` — shipped today."""
    return [c for c in CAPABILITIES if c.status == "live"]


def planned_capabilities() -> list[Capability]:
    """Return capabilities with ``status == "planned"`` — in the plan,
    not yet wired."""
    return [c for c in CAPABILITIES if c.status == "planned"]


def capabilities_for_use_case(n: int) -> list[Capability]:
    """Return all capabilities serving section-2 use case ``n`` (1-6)."""
    return [c for c in CAPABILITIES if c.use_case_from_section_2 == n]


def mobile_capabilities() -> list[Capability]:
    """Return capabilities flagged ``mobile_visible=True`` — for the
    collapsed mobile capability map."""
    return [c for c in CAPABILITIES if c.mobile_visible]
