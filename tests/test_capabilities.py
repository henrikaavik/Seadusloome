"""Tests for the B3 capability dictionary (``app/ui/capabilities.py``).

The capability dict is the single source of truth for every "what can I do"
entry across Seadusloome (chat InfoBox, Analüüsikeskus directory, future B1
search bar, future B2 dashboard map). The invariants below lock the contract
so a regression somewhere in the dict can't silently break consumers:

* **Slug uniqueness** — slugs are public-API identifiers used by B1's
  dropdown and tests; a collision would silently shadow capabilities.
* **Use-case coverage** — every section-2 use case (1-6) must have at
  least one capability, otherwise B2's "Mida soovid teha?" grid has an
  empty row.
* **Status whitelist** — anything outside ``{live, planned, deferred}``
  breaks the "Tulekul" badge logic and the live/planned helpers.
* **Diacritic-clean slugs** — slugs must be ASCII (no ``õ``/``ä``/``ü``/...);
  Estonian diacritics live in the user-facing fields only.

The helpers (:func:`get_capability`, :func:`live_capabilities`,
:func:`planned_capabilities`, :func:`capabilities_for_use_case`,
:func:`mobile_capabilities`) get a brief smoke check each.
"""

from __future__ import annotations

from app.ui.capabilities import (
    CAPABILITIES,
    capabilities_for_use_case,
    get_capability,
    live_capabilities,
    mobile_capabilities,
    planned_capabilities,
)

_VALID_STATUSES = {"live", "planned", "deferred"}
_VALID_USE_CASES = {1, 2, 3, 4, 5, 6}


# ---------------------------------------------------------------------------
# Invariants
# ---------------------------------------------------------------------------


def test_slug_uniqueness():
    """No two capabilities share a slug — slugs are public-API identifiers."""
    slugs = [c.slug for c in CAPABILITIES]
    assert len(slugs) == len(set(slugs)), (
        f"Duplicate slugs detected: {[s for s in slugs if slugs.count(s) > 1]}"
    )


def test_every_use_case_covered():
    """Each of the six section-2 use cases (1-6) has at least one capability.

    B2's dashboard map groups cards by use case; an empty row would render
    a blank section, which looks broken.
    """
    seen = {c.use_case_from_section_2 for c in CAPABILITIES}
    missing = _VALID_USE_CASES - seen
    assert not missing, f"Use cases without any capability: {sorted(missing)}"


def test_use_case_values_are_in_range():
    """Every use_case_from_section_2 value is 1-6 (the section-2 buckets)."""
    for cap in CAPABILITIES:
        assert cap.use_case_from_section_2 in _VALID_USE_CASES, (
            f"{cap.slug}: invalid use_case_from_section_2={cap.use_case_from_section_2}"
        )


def test_status_values_are_whitelisted():
    """Every status is in the {live, planned, deferred} whitelist."""
    for cap in CAPABILITIES:
        assert cap.status in _VALID_STATUSES, (
            f"{cap.slug}: invalid status={cap.status!r} (allowed: {sorted(_VALID_STATUSES)})"
        )


def test_slugs_are_diacritic_clean():
    """Slugs are ASCII — no Estonian diacritics or unicode lookalikes."""
    for cap in CAPABILITIES:
        assert cap.slug.isascii(), f"Slug {cap.slug!r} contains non-ASCII characters"
        # Defensive: catch zero-width / control characters too.
        for ch in cap.slug:
            assert ch.isalnum() or ch == "-", (
                f"Slug {cap.slug!r}: forbidden character {ch!r} (slugs must be [a-z0-9-]+)"
            )


def test_required_fields_are_populated():
    """Canonical name and one-line description are non-empty for every entry."""
    for cap in CAPABILITIES:
        assert cap.canonical_name_et.strip(), f"{cap.slug}: empty canonical_name_et"
        assert cap.one_line_description_et.strip(), f"{cap.slug}: empty one_line_description_et"
        assert cap.icon.strip(), f"{cap.slug}: empty icon"
        assert cap.target_url.startswith("/"), (
            f"{cap.slug}: target_url must be an app path (got {cap.target_url!r})"
        )


# ---------------------------------------------------------------------------
# Helper smoke tests
# ---------------------------------------------------------------------------


def test_get_capability_hits_and_misses():
    """``get_capability`` returns the matching entry or ``None``."""
    cap = get_capability("noustaja")
    assert cap is not None
    assert cap.slug == "noustaja"
    assert get_capability("does-not-exist") is None


def test_live_and_planned_partition_the_dict():
    """``live`` + ``planned`` + ``deferred`` covers every entry exactly once."""
    live = live_capabilities()
    planned = planned_capabilities()
    deferred = [c for c in CAPABILITIES if c.status == "deferred"]
    assert len(live) + len(planned) + len(deferred) == len(CAPABILITIES)
    # No overlap between live and planned.
    assert not (set(c.slug for c in live) & set(c.slug for c in planned))


def test_live_capabilities_returns_only_live():
    """Every entry from :func:`live_capabilities` has ``status == "live"``."""
    for cap in live_capabilities():
        assert cap.status == "live", f"{cap.slug}: leaked into live_capabilities()"


def test_planned_capabilities_returns_only_planned():
    """Every entry from :func:`planned_capabilities` has ``status == "planned"``."""
    for cap in planned_capabilities():
        assert cap.status == "planned", f"{cap.slug}: leaked into planned_capabilities()"


def test_capabilities_for_use_case_filters_by_bucket():
    """``capabilities_for_use_case(n)`` returns exactly the entries with that bucket."""
    for n in _VALID_USE_CASES:
        bucket = capabilities_for_use_case(n)
        for cap in bucket:
            assert cap.use_case_from_section_2 == n
        # Sanity: bucket count matches direct filtering.
        expected = [c for c in CAPABILITIES if c.use_case_from_section_2 == n]
        assert len(bucket) == len(expected)


def test_capabilities_for_use_case_unknown_bucket_returns_empty():
    """Out-of-range use-case numbers return an empty list (not an error)."""
    assert capabilities_for_use_case(0) == []
    assert capabilities_for_use_case(99) == []


def test_mobile_capabilities_is_a_subset():
    """Every mobile capability is in the master list (no leaks)."""
    mobile_slugs = {c.slug for c in mobile_capabilities()}
    all_slugs = {c.slug for c in CAPABILITIES}
    assert mobile_slugs.issubset(all_slugs)
    # Default-True invariant: with no opt-outs, mobile == all.
    for cap in CAPABILITIES:
        if cap.mobile_visible:
            assert cap.slug in mobile_slugs


def test_capability_is_frozen_dataclass():
    """``Capability`` is frozen — callers can hash/cache it safely."""
    cap = CAPABILITIES[0]
    try:
        cap.slug = "mutated"  # type: ignore[misc]
    except Exception:
        pass
    else:
        raise AssertionError("Capability is supposed to be frozen but allowed mutation")
