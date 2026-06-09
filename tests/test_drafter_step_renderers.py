"""Tests for drafter step-5 clause-card citation rendering (#842, part 3).

The clause card (`_render_clause_card`) must render *verified* enriched
citations as in-app same-origin ``/explorer?focus=`` anchors and mark
*unverified* citations (enriched dicts that did not resolve, plus legacy
raw strings) as ``kontrollimata viide:`` non-links — so fabricated
citations never become clickable dead ``/explorer?search=`` links.
"""

from __future__ import annotations

import uuid

from fasthtml.common import to_xml

from app.drafter._step_renderers import _render_clause_card


def _clause_with_mixed_citations() -> dict:
    """Minimal valid step-5 clause whose citations mix all three forms."""
    return {
        "chapter": "1",
        "chapter_title": "Üldsätted",
        "paragraph": "§ 1",
        "title": "Reguleerimisala",
        "text": "Käesolev seadus reguleerib midagi.",
        "notes": "",
        "citations": [
            # Verified enriched dict → in-app /explorer?focus= anchor.
            {
                "text": "HKTS § 13",
                "verified": True,
                "label": "HKTS § 13",
                "resolved_uri": "https://data.riik.ee/ontology/estleg#HKTS_Par_13",
                "explorer_url": (
                    "/explorer?focus=https%3A%2F%2Fdata.riik.ee%2Fontology%2Festleg%23HKTS_Par_13"
                ),
            },
            # Unverified enriched dict → "kontrollimata viide:" Span, no link.
            {
                "text": "Väljamõeldud seadus § 99",
                "verified": False,
                "label": "Väljamõeldud seadus § 99",
                "resolved_uri": None,
                "explorer_url": None,
            },
            # Legacy raw string → coerced to unverified Span.
            "estleg:TsiviilS/par/3",
        ],
    }


def test_verified_citation_renders_in_app_focus_anchor():
    html = to_xml(_render_clause_card(_clause_with_mixed_citations(), uuid.uuid4(), 0))

    # The verified citation is a clickable /explorer?focus= anchor.
    assert "/explorer?focus=" in html
    # ...and it is in-app (no new-tab target).
    assert 'target="_blank"' not in html


def test_no_search_anchor_is_rendered():
    html = to_xml(_render_clause_card(_clause_with_mixed_citations(), uuid.uuid4(), 0))

    # Fabricated citations must NOT become /explorer?search= dead links.
    assert "/explorer?search=" not in html


def test_unverified_citations_are_marked_and_not_links():
    html = to_xml(_render_clause_card(_clause_with_mixed_citations(), uuid.uuid4(), 0))

    # Both the unverified dict and the legacy string get the marker text.
    assert html.count("kontrollimata viide:") == 2
    assert "kontrollimata viide: Väljamõeldud seadus § 99" in html
    assert "kontrollimata viide: estleg:TsiviilS/par/3" in html

    # The unverified marker text never sits inside an <a ...> href element —
    # the element wrapping each marker must be a <span ...>, not an anchor.
    for marker in (
        "kontrollimata viide: Väljamõeldud seadus § 99",
        "kontrollimata viide: estleg:TsiviilS/par/3",
    ):
        # Locate the element wrapping the marker; it must be a <span ...>,
        # not an <a ...> anchor.
        idx = html.index(marker)
        open_tag_start = html.rfind("<", 0, idx)
        open_tag = html[open_tag_start : html.index(">", open_tag_start) + 1]
        assert open_tag.startswith("<span")
        assert not open_tag.startswith("<a")
