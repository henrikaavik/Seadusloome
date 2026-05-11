"""Smoke tests for the Õiguskaart (explorer) page shell (#714 / #718).

Renders ``explorer_page`` directly via ``to_xml()`` and asserts on the
control-bar copy — no TestClient, no DB.
"""

from __future__ import annotations

from fasthtml.common import to_xml
from starlette.requests import Request


def _req(query: str = "") -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/explorer",
            "query_string": query.encode(),
            "headers": [],
            "scheme": "http",
            "server": ("testserver", 80),
            "client": ("127.0.0.1", 12345),
            "auth": {
                "id": "u-1",
                "email": "u@example.ee",
                "full_name": "Test User",
                "role": "drafter",
                "org_id": "11111111-1111-1111-1111-111111111111",
            },
        }
    )


def test_explorer_page_uses_legal_language_controls():
    from app.explorer.pages import explorer_page

    html = to_xml(explorer_page(_req()))

    # New, plain-language / legal-work control labels (#714 / #718).
    assert "Vaate seaded" in html
    assert "Lähtesta paigutus" in html
    assert "Näita/peida seosenimed" in html
    assert "Rühmita liigi järgi" in html
    assert "Lähtesta vaade" in html

    # Old force-simulation vocabulary is gone from the control bar.
    assert "Taaskäivita simulatsioon" not in html
    assert "Lülita silte" not in html
    assert "Rühm. kategooria järgi" not in html


def test_explorer_page_is_branded_oiguskaart():
    from app.explorer.pages import explorer_page

    html = to_xml(explorer_page(_req()))
    assert "Õiguskaart" in html
    assert "Uurija" not in html


def test_explorer_page_timeline_is_clearly_labelled():
    from app.explorer.pages import explorer_page

    html = to_xml(explorer_page(_req()))
    assert "Ajaline vaade" in html
    assert "timeline-slider" in html
    # "Keelatud" was a confusing label for "no time filter active".
    assert "Keelatud" not in html


def test_explorer_page_focus_param_is_handed_to_js():
    from app.explorer.pages import explorer_page

    uri = "https://data.riik.ee/ontology/estleg#KarS_par_133"
    # ``focus`` arrives already URL-decoded in req.query_params.
    html = to_xml(explorer_page(_req(f"focus={uri}")))
    assert "window.__explorerFocus" in html
    assert uri in html
    # The "?draft=ID …" tip is suppressed when the user came here to
    # look at a specific entity.
    assert "?draft=ID" not in html
    # The back-link element is in the DOM (explorer.js unhides it).
    assert 'id="panel-back"' in html


def test_explorer_page_no_focus_keeps_the_draft_tip():
    from app.explorer.pages import explorer_page

    html = to_xml(explorer_page(_req()))
    assert "window.__explorerFocus" not in html
    assert "?draft=ID" in html
