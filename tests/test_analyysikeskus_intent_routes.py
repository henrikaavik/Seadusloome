"""Route-level integration tests for the intent → impact flow (#814 Phase 2b).

Exercises the three new endpoints end-to-end via :class:`TestClient`:

    GET  /analyysikeskus/moju-poliitikamottest          — intake form
    POST /analyysikeskus/moju-poliitikamottest/extract  — confirmation panel
    POST /analyysikeskus/moju-poliitikamottest/analyze  — aggregated result

The LLM extractor, reference resolver, and Jena layer are all patched
*where used* (the patch-path contract — patch where the symbol is
imported, not where it's defined). No real network calls; deterministic
canned responses drive every code path.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch


def _authed_user() -> dict[str, Any]:
    return {
        "id": "33333333-3333-3333-3333-333333333333",
        "email": "kasutaja@seadusloome.ee",
        "full_name": "Test Kasutaja",
        "role": "drafter",
        "org_id": "11111111-1111-1111-1111-111111111111",
    }


def _stub_provider() -> MagicMock:
    provider = MagicMock()
    provider.get_current_user.return_value = _authed_user()
    return provider


def _authed_client(*, raise_server_exceptions: bool = True):
    from starlette.testclient import TestClient

    client = TestClient(
        __import__("app.main", fromlist=["app"]).app,
        follow_redirects=False,
        raise_server_exceptions=raise_server_exceptions,
    )
    client.cookies.set("access_token", "stub-token")
    return client


# ---------------------------------------------------------------------------
# Canned LLM + resolver responses
# ---------------------------------------------------------------------------


def _canned_intent_candidates():
    """Two canned :class:`IntentCandidate` rows the route handler renders."""
    from app.analyysikeskus.intent_extractor import IntentCandidate

    return [
        IntentCandidate(
            ref_text="PISTS § 4",
            ref_type="provision",
            confidence=0.9,
            reasoning="Põhinorm puudega inimese toetuse maksmise kohta.",
        ),
        IntentCandidate(
            ref_text="sotsiaalhoolekande seadus",
            ref_type="law",
            confidence=0.5,
            reasoning="Seos sotsiaaltoetuste üldise raamistikuga.",
        ),
    ]


_PISTS_URI = "https://data.riik.ee/ontology/estleg#PISTS_Par_4"
_SHS_URI = "https://data.riik.ee/ontology/estleg#sotsiaalhoolekande-seadus"


def _canned_resolved_refs():
    """Canned resolver output for the two candidates above."""
    from app.docs.entity_extractor import ExtractedRef
    from app.docs.reference_resolver import ResolvedRef

    return [
        ResolvedRef(
            extracted=ExtractedRef(
                ref_text="PISTS § 4",
                ref_type="provision",
                confidence=0.9,
            ),
            entity_uri=_PISTS_URI,
            matched_label="PISTS § 4",
            match_score=1.0,
        ),
        ResolvedRef(
            extracted=ExtractedRef(
                ref_text="sotsiaalhoolekande seadus",
                ref_type="law",
                confidence=0.5,
            ),
            entity_uri=_SHS_URI,
            matched_label="Sotsiaalhoolekande seadus",
            match_score=0.9,
        ),
    ]


def _canned_findings(affected: int = 2, conflicts: int = 1, gaps: int = 0):
    from app.impact.analyzer import ImpactFindings

    return ImpactFindings(
        affected_entities=[
            {"uri": f"e-{i}", "label": f"Üksus {i}", "type": "Provision"} for i in range(affected)
        ],
        conflicts=[
            {"conflicting_entity": f"c-{i}", "conflicting_label": f"Konflikt {i}", "reason": "x"}
            for i in range(conflicts)
        ],
        gaps=[{"label": f"gap-{i}"} for i in range(gaps)],
        affected_count=affected,
        conflict_count=conflicts,
        gap_count=gaps,
    )


# ---------------------------------------------------------------------------
# Unauthenticated requests redirect to login
# ---------------------------------------------------------------------------


def test_intent_routes_redirect_unauthenticated():
    from starlette.testclient import TestClient

    from app.main import app

    client = TestClient(app, follow_redirects=False)
    for path in (
        "/analyysikeskus/moju-poliitikamottest",
        "/analyysikeskus/moju-poliitikamottest/extract",
        "/analyysikeskus/moju-poliitikamottest/analyze",
    ):
        resp = client.get(path) if path.endswith("moju-poliitikamottest") else client.post(path)
        assert resp.status_code == 303, path
        assert resp.headers["location"] == "/auth/login", path


# ---------------------------------------------------------------------------
# Step 1 — GET intake form
# ---------------------------------------------------------------------------


@patch("app.analyysikeskus.routes._directory._get_recent_analyses", return_value=[])
@patch("app.auth.middleware._get_provider")
def test_intent_intake_form_renders(mock_provider: MagicMock, mock_recent: MagicMock):
    mock_provider.return_value = _stub_provider()
    client = _authed_client()
    resp = client.get("/analyysikeskus/moju-poliitikamottest")
    assert resp.status_code == 200
    body = resp.text

    # The page chrome / workflow title.
    assert "Analüüsi poliitikamõttest" in body
    # The four intake form fields are present.
    assert "Mida soovid muuta või lisada?" in body
    assert 'name="intent"' in body
    # Intent is a textarea, not a single-line input.
    assert "<textarea" in body
    # Target-group chips — chip group label includes the explicit
    # "scope metadata only, doesn't influence search" disclaimer so users
    # don't expect chip selection to silently shape candidates (#822 P2-2).
    assert "Sihtrühm" in body
    assert "ei mõjuta kandidaatide otsingut" in body
    assert "Lapsed" in body
    assert "Puuetega inimesed" in body
    # Affected-area chips with the same disclaimer.
    assert "Mõjutatud valdkonnad" in body
    assert "Sotsiaalhoolekanne" in body
    # Known-refs optional text input.
    assert "Teadaolevad õiguslikud viited (valikuline)" in body
    assert 'name="known_refs"' in body
    # The submit button copy.
    assert "Otsi mõjutatud sätteid" in body
    # The HTMX target wiring.
    assert "moju-poliitikamottest-result" in body
    assert 'hx-post="/analyysikeskus/moju-poliitikamottest/extract"' in body
    # Estonian copy throughout — no raw English placeholder leaks.
    assert "Poliitiline kavatsus" in body


# ---------------------------------------------------------------------------
# Step 1 — capability card uses the new route
# ---------------------------------------------------------------------------


@patch("app.analyysikeskus.routes._directory._get_recent_analyses", return_value=[])
@patch("app.auth.middleware._get_provider")
def test_analyysikeskus_directory_links_to_new_intent_route(
    mock_provider: MagicMock, mock_recent: MagicMock
):
    mock_provider.return_value = _stub_provider()
    client = _authed_client()
    resp = client.get("/analyysikeskus")
    assert resp.status_code == 200
    body = resp.text

    # The capability card is visible and links to the new route — NOT to
    # the directory page anymore.
    assert "Analüüsi poliitikamõttest" in body
    assert 'href="/analyysikeskus/moju-poliitikamottest"' in body
    # The "Tulekul" badge is gone for this card (the entry is live now).
    # We can't assert the badge absence globally (other planned entries
    # use it) — but we can assert this card's link is "Alusta analüüsi →"
    # which only the live intent card renders.
    assert "Alusta analüüsi →" in body


# ---------------------------------------------------------------------------
# Step 2 — POST /extract — happy path
# ---------------------------------------------------------------------------


@patch("app.analyysikeskus.routes._directory._get_recent_analyses", return_value=[])
@patch("app.analyysikeskus.intent_analysis.extract_intent_candidates")
@patch("app.docs.reference_resolver.ReferenceResolver.resolve")
@patch("app.auth.middleware._get_provider")
def test_intent_extract_renders_confirmation_panel(
    mock_provider: MagicMock,
    mock_resolve: MagicMock,
    mock_extract: MagicMock,
    mock_recent: MagicMock,
):
    mock_provider.return_value = _stub_provider()
    mock_extract.return_value = _canned_intent_candidates()
    mock_resolve.return_value = _canned_resolved_refs()

    client = _authed_client()
    resp = client.post(
        "/analyysikeskus/moju-poliitikamottest/extract",
        data={
            "intent": "Soovin lihtsustada puudega inimese toetuse taotlemist.",
            "target_groups": ["Puuetega inimesed", "Eakad"],
            "affected_areas": ["Sotsiaalhoolekanne"],
            "known_refs": "",
        },
    )
    assert resp.status_code == 200
    body = resp.text

    # The confirmation panel headline.
    assert "Süsteem leidis järgmised kandidaadid" in body
    # The applied chip selections surface in the summary.
    assert "Puuetega inimesed" in body
    assert "Sotsiaalhoolekanne" in body
    # Each candidate's resolved label + ref_type + confidence shows.
    assert "PISTS § 4" in body
    assert "säte" in body
    # The reasoning text (smaller font).
    assert "Põhinorm puudega inimese toetuse" in body
    # The high-confidence row (>= 0.7) is pre-checked; the low one is not.
    # Pre-check is shown via the "checked" attribute on the checkbox.
    assert 'checked="checked"' in body
    # The submit button copy includes the count of pre-checked rows
    # (PISTS § 4 with confidence 0.9 is the only one above the threshold).
    assert "Käivita mõjuanalüüs (1 kinnitatud sätet)" in body
    # The form posts to /analyze.
    assert 'hx-post="/analyysikeskus/moju-poliitikamottest/analyze"' in body
    # The URI hidden inputs carry the resolved URI per candidate.
    assert _PISTS_URI in body
    # Each candidate row carries a label hidden input.
    assert "label_0" in body
    assert "label_1" in body
    # The LLM was called exactly once with the full intent.
    mock_extract.assert_called_once()
    args, kwargs = mock_extract.call_args
    assert "puudega inimese" in args[0]


# ---------------------------------------------------------------------------
# Step 2 — empty intent skips the LLM
# ---------------------------------------------------------------------------


@patch("app.analyysikeskus.routes._directory._get_recent_analyses", return_value=[])
@patch("app.analyysikeskus.intent_analysis.extract_intent_candidates")
@patch("app.auth.middleware._get_provider")
def test_intent_extract_empty_intent_shows_validation_no_llm(
    mock_provider: MagicMock,
    mock_extract: MagicMock,
    mock_recent: MagicMock,
):
    mock_provider.return_value = _stub_provider()
    client = _authed_client()
    resp = client.post(
        "/analyysikeskus/moju-poliitikamottest/extract",
        data={"intent": "   "},
    )
    assert resp.status_code == 200
    body = resp.text

    # Friendly Estonian validation message.
    assert "Palun sisesta poliitiline kavatsus" in body
    # The LLM extractor was NOT called.
    mock_extract.assert_not_called()


# ---------------------------------------------------------------------------
# Step 2 — zero LLM candidates → empty-state warning + back link
# ---------------------------------------------------------------------------


@patch("app.analyysikeskus.routes._directory._get_recent_analyses", return_value=[])
@patch("app.analyysikeskus.intent_analysis.extract_intent_candidates")
@patch("app.docs.reference_resolver.ReferenceResolver.resolve")
@patch("app.auth.middleware._get_provider")
def test_intent_extract_zero_candidates_shows_empty_state(
    mock_provider: MagicMock,
    mock_resolve: MagicMock,
    mock_extract: MagicMock,
    mock_recent: MagicMock,
):
    mock_provider.return_value = _stub_provider()
    mock_extract.return_value = []
    mock_resolve.return_value = []

    client = _authed_client()
    resp = client.post(
        "/analyysikeskus/moju-poliitikamottest/extract",
        data={"intent": "midagi ebamäärast"},
    )
    assert resp.status_code == 200
    body = resp.text

    # Empty-state messaging + back-link affordance.
    assert "ei suutnud sellest kavatsusest kandidaate" in body
    assert "Tagasi sisestuse juurde" in body


# ---------------------------------------------------------------------------
# Step 2 — known refs from the manual input get added as candidates
# ---------------------------------------------------------------------------


@patch("app.analyysikeskus.routes._directory._get_recent_analyses", return_value=[])
@patch("app.analyysikeskus.intent_analysis.extract_intent_candidates")
@patch("app.docs.reference_resolver.ReferenceResolver.resolve")
@patch("app.auth.middleware._get_provider")
def test_intent_extract_includes_manual_known_refs(
    mock_provider: MagicMock,
    mock_resolve: MagicMock,
    mock_extract: MagicMock,
    mock_recent: MagicMock,
):
    from app.docs.entity_extractor import ExtractedRef
    from app.docs.reference_resolver import ResolvedRef

    mock_provider.return_value = _stub_provider()
    mock_extract.return_value = []  # No LLM candidates.

    # Capture what the resolver receives.
    captured: list[ExtractedRef] = []

    def _capture(refs: list[ExtractedRef]) -> list[ResolvedRef]:
        captured.extend(refs)
        return [
            ResolvedRef(
                extracted=r,
                entity_uri=f"https://data.riik.ee/ontology/estleg#{r.ref_text.replace(' ', '_')}",
                matched_label=r.ref_text,
                match_score=1.0,
            )
            for r in refs
        ]

    mock_resolve.side_effect = _capture

    client = _authed_client()
    resp = client.post(
        "/analyysikeskus/moju-poliitikamottest/extract",
        data={
            "intent": "mingi kavatsus",
            "known_refs": "AvTS § 35, KarS § 121",
        },
    )
    assert resp.status_code == 200
    body = resp.text

    # The two manually entered refs were passed through to the resolver.
    ref_texts = {r.ref_text for r in captured}
    assert "AvTS § 35" in ref_texts
    assert "KarS § 121" in ref_texts
    # Both surface in the confirmation panel.
    assert "AvTS § 35" in body
    assert "KarS § 121" in body


# ---------------------------------------------------------------------------
# Step 3 — POST /analyze with 0 confirmed URIs returns empty state
# ---------------------------------------------------------------------------


@patch("app.analyysikeskus.routes._directory._get_recent_analyses", return_value=[])
@patch("app.analyysikeskus.intent_analysis.run_adhoc_impact_analysis")
@patch("app.auth.middleware._get_provider")
def test_intent_analyze_zero_confirmed_returns_empty_state(
    mock_provider: MagicMock,
    mock_adhoc: MagicMock,
    mock_recent: MagicMock,
):
    mock_provider.return_value = _stub_provider()
    client = _authed_client()
    resp = client.post(
        "/analyysikeskus/moju-poliitikamottest/analyze",
        data={"intent": "midagi", "confirmed": []},
    )
    assert resp.status_code == 200
    body = resp.text

    # Friendly empty state with the back link.
    assert "Mõjuanalüüsi käivitamiseks kinnita vähemalt üks säte" in body
    assert "Tagasi sisestuse juurde" in body
    # The per-URI analyser was never called.
    mock_adhoc.assert_not_called()


# ---------------------------------------------------------------------------
# Step 3 — POST /analyze with N confirmed URIs renders the full result
# ---------------------------------------------------------------------------


@patch("app.analyysikeskus.routes._directory._get_recent_analyses", return_value=[])
@patch("app.analyysikeskus.intent_analysis.run_adhoc_impact_analysis")
@patch("app.auth.middleware._get_provider")
def test_intent_analyze_runs_per_uri_and_renders_result(
    mock_provider: MagicMock,
    mock_adhoc: MagicMock,
    mock_recent: MagicMock,
):
    from app.analyysikeskus.adhoc_analysis import AdhocAnalysisResult

    mock_provider.return_value = _stub_provider()

    # Each URI returns its own canned result.
    results = {
        _PISTS_URI: AdhocAnalysisResult(
            findings=_canned_findings(affected=3, conflicts=1, gaps=0),
            score=70,
            graph_uri="g-pists",
        ),
        _SHS_URI: AdhocAnalysisResult(
            findings=_canned_findings(affected=2, conflicts=0, gaps=1),
            score=40,
            graph_uri="g-shs",
        ),
    }
    mock_adhoc.side_effect = lambda uri, **_: results[uri]

    client = _authed_client()
    resp = client.post(
        "/analyysikeskus/moju-poliitikamottest/analyze",
        data={
            "intent": "Soovin lihtsustada puudega toetuse taotlemist.",
            "target_groups": ["Puuetega inimesed"],
            "affected_areas": ["Sotsiaalhoolekanne"],
            "confirmed": ["0", "1"],
            "uri_0": _PISTS_URI,
            "label_0": "PISTS § 4",
            "uri_1": _SHS_URI,
            "label_1": "Sotsiaalhoolekande seadus",
        },
    )
    assert resp.status_code == 200
    body = resp.text

    # The five result-shell cards are present in the fragment.
    for heading in ("Sisend", "Ulatus", "Tulemused", "Tõendid", "Soovitatud tegevused"):
        assert heading in body, heading
    # The Sisend block echoes the intent + confirmed labels.
    assert "Soovin lihtsustada" in body
    assert "PISTS § 4" in body
    assert "Sotsiaalhoolekande seadus" in body
    # The Ulatus block lists the applied chips.
    assert "Puuetega inimesed" in body
    assert "Sotsiaalhoolekanne" in body
    # Per-URI traceability: each confirmed URI has its own "Mõjuahel
    # sätte X analüüsist" sub-heading.
    assert "Mõjuahel sätte PISTS § 4 analüüsist" in body
    assert "Mõjuahel sätte Sotsiaalhoolekande seadus analüüsist" in body
    # The headline totals sum across URIs.
    assert "5 mõjutatud üksust" in body  # 3 + 2
    assert "1 konflikti" in body
    assert "1 lünka" in body
    assert "üle 2 kinnitatud sätte" in body
    # Recommended actions include the cross-links the design doc names.
    assert "Küsi nõustajalt" in body
    assert "/chat/new" in body
    assert "Ava õiguskaart" in body
    assert "Ava Koostaja" in body
    assert "Laadi üles eelnõu" in body
    # The per-URI analyser was called once per confirmed URI.
    assert mock_adhoc.call_count == 2
    called_uris = {c.args[0] for c in mock_adhoc.call_args_list}
    assert called_uris == {_PISTS_URI, _SHS_URI}


# ---------------------------------------------------------------------------
# Step 3 — per-URI traceability headings include every confirmed source
# ---------------------------------------------------------------------------


@patch("app.analyysikeskus.routes._directory._get_recent_analyses", return_value=[])
@patch("app.analyysikeskus.intent_analysis.run_adhoc_impact_analysis")
@patch("app.auth.middleware._get_provider")
def test_intent_analyze_traceability_per_source(
    mock_provider: MagicMock,
    mock_adhoc: MagicMock,
    mock_recent: MagicMock,
):
    from app.analyysikeskus.adhoc_analysis import AdhocAnalysisResult

    mock_provider.return_value = _stub_provider()
    mock_adhoc.return_value = AdhocAnalysisResult(
        findings=_canned_findings(),
        score=50,
        graph_uri="g",
    )

    client = _authed_client()
    # Three confirmed sources — every one gets its own heading.
    resp = client.post(
        "/analyysikeskus/moju-poliitikamottest/analyze",
        data={
            "intent": "midagi",
            "confirmed": ["0", "1", "2"],
            "uri_0": "uri-A",
            "label_0": "AvTS § 35",
            "uri_1": "uri-B",
            "label_1": "KarS § 121",
            "uri_2": "uri-C",
            "label_2": "TLS § 12",
        },
    )
    assert resp.status_code == 200
    body = resp.text

    # Each confirmed source maps to a "Mõjuahel sätte X analüüsist" sub-heading.
    for label in ("AvTS § 35", "KarS § 121", "TLS § 12"):
        assert f"Mõjuahel sätte {label} analüüsist" in body


# ---------------------------------------------------------------------------
# Step 3 — confirmed row with no URI is silently dropped
# ---------------------------------------------------------------------------


@patch("app.analyysikeskus.routes._directory._get_recent_analyses", return_value=[])
@patch("app.analyysikeskus.intent_analysis.run_adhoc_impact_analysis")
@patch("app.auth.middleware._get_provider")
def test_intent_analyze_drops_confirmed_rows_without_uri(
    mock_provider: MagicMock,
    mock_adhoc: MagicMock,
    mock_recent: MagicMock,
):
    from app.analyysikeskus.adhoc_analysis import AdhocAnalysisResult

    mock_provider.return_value = _stub_provider()
    mock_adhoc.return_value = AdhocAnalysisResult(
        findings=_canned_findings(),
        score=50,
        graph_uri="g",
    )

    client = _authed_client()
    # Row 0 is confirmed but has no uri_0 hidden input (e.g. unresolved).
    # Row 1 is confirmed with a URI.
    resp = client.post(
        "/analyysikeskus/moju-poliitikamottest/analyze",
        data={
            "intent": "midagi",
            "confirmed": ["0", "1"],
            # uri_0 deliberately omitted.
            "label_0": "Unresolved ref",
            "uri_1": "uri-B",
            "label_1": "KarS § 121",
        },
    )
    assert resp.status_code == 200
    body = resp.text

    # Only the row with a real URI was sent to the analyser.
    assert mock_adhoc.call_count == 1
    assert mock_adhoc.call_args_list[0].args[0] == "uri-B"
    # The unresolved label does NOT appear as a "Mõjuahel sätte ..." heading.
    assert "Mõjuahel sätte Unresolved ref analüüsist" not in body
    assert "Mõjuahel sätte KarS § 121 analüüsist" in body


# ---------------------------------------------------------------------------
# #822 review follow-ups (P2 caps + P3 prefill)
# ---------------------------------------------------------------------------


@patch("app.analyysikeskus.routes._directory._get_recent_analyses", return_value=[])
@patch("app.auth.middleware._get_provider")
def test_intake_form_prefills_intent_from_sisend_query_param(
    mock_provider: MagicMock,
    mock_recent: MagicMock,
):
    """#822 P3: the capability-card / global-search helpers append
    ?sisend=<example> to /analyysikeskus/* deep-links. The intake page
    must read it and prefill the textarea, otherwise clicking the
    dashboard "Näide:" affordance lands on a blank form."""
    mock_provider.return_value = _stub_provider()
    client = _authed_client()
    sisend = "Soovin lihtsustada puudega inimese toetuse taotlemist."
    resp = client.get(
        "/analyysikeskus/moju-poliitikamottest",
        params={"sisend": sisend},
    )
    assert resp.status_code == 200
    body = resp.text
    # The textarea value attribute carries the prefill.
    assert sisend in body
    # Belt-and-braces: an empty sisend doesn't crash + renders blank textarea.
    resp_blank = client.get(
        "/analyysikeskus/moju-poliitikamottest",
        params={"sisend": ""},
    )
    assert resp_blank.status_code == 200


@patch("app.analyysikeskus.routes._directory._get_recent_analyses", return_value=[])
@patch("app.analyysikeskus.intent_analysis.extract_intent_candidates")
@patch("app.docs.reference_resolver.ReferenceResolver.resolve")
@patch("app.auth.middleware._get_provider")
def test_extract_caps_manual_known_refs_to_max_intent_known_refs(
    mock_provider: MagicMock,
    mock_resolve: MagicMock,
    mock_extract: MagicMock,
    mock_recent: MagicMock,
):
    """#822 P2-1: an unbounded comma-separated known_refs list would
    fan out into N resolver SPARQL lookups. Cap at
    ``_MAX_INTENT_KNOWN_REFS`` (10)."""
    from app.analyysikeskus.routes import _MAX_INTENT_KNOWN_REFS
    from app.docs.entity_extractor import ExtractedRef
    from app.docs.reference_resolver import ResolvedRef

    mock_provider.return_value = _stub_provider()
    mock_extract.return_value = []  # No LLM candidates.

    captured: list[ExtractedRef] = []

    def _capture(refs: list[ExtractedRef]) -> list[ResolvedRef]:
        captured.extend(refs)
        return [
            ResolvedRef(
                extracted=r,
                entity_uri=f"uri-{i}",
                matched_label=r.ref_text,
                match_score=1.0,
            )
            for i, r in enumerate(refs)
        ]

    mock_resolve.side_effect = _capture

    # 25 manually entered refs — well above the cap.
    flood = ", ".join(f"REF{n}" for n in range(25))

    client = _authed_client()
    resp = client.post(
        "/analyysikeskus/moju-poliitikamottest/extract",
        data={"intent": "mingi kavatsus", "known_refs": flood},
    )
    assert resp.status_code == 200
    # Resolver received at most the cap, not all 25.
    assert len(captured) <= _MAX_INTENT_KNOWN_REFS, (
        f"Expected at most {_MAX_INTENT_KNOWN_REFS} refs to reach the "
        f"resolver, got {len(captured)}"
    )


@patch("app.analyysikeskus.routes._directory._get_recent_analyses", return_value=[])
@patch("app.analyysikeskus.intent_analysis.extract_intent_candidates")
@patch("app.docs.reference_resolver.ReferenceResolver.resolve")
@patch("app.auth.middleware._get_provider")
def test_extract_manual_refs_win_when_llm_fills_the_cap(
    mock_provider: MagicMock,
    mock_resolve: MagicMock,
    mock_extract: MagicMock,
    mock_recent: MagicMock,
):
    """#822 PR review P2 (round 2): if the LLM returns _MAX_INTENT_CANDIDATES
    rows, a previous revision truncated post-append and silently dropped
    every manual known_ref. Explicit user input must reach the resolver
    even when the LLM list is full — manual refs win over inferred ones.
    """
    from app.analyysikeskus.intent_extractor import IntentCandidate
    from app.analyysikeskus.routes import _MAX_INTENT_CANDIDATES
    from app.docs.entity_extractor import ExtractedRef
    from app.docs.reference_resolver import ResolvedRef

    mock_provider.return_value = _stub_provider()

    # LLM returns exactly _MAX_INTENT_CANDIDATES candidates (12) — would
    # have completely displaced the manual ref under the old behaviour.
    mock_extract.return_value = [
        IntentCandidate(
            ref_text=f"LLM{n}",
            ref_type="provision",
            confidence=0.5 + (n * 0.01),  # ascending confidence
            reasoning=f"LLM kandidaat #{n}.",
        )
        for n in range(_MAX_INTENT_CANDIDATES)
    ]

    captured: list[ExtractedRef] = []

    def _capture(refs: list[ExtractedRef]) -> list[ResolvedRef]:
        captured.extend(refs)
        return [
            ResolvedRef(
                extracted=r,
                entity_uri=f"uri-{r.ref_text}",
                matched_label=r.ref_text,
                match_score=1.0,
            )
            for r in refs
        ]

    mock_resolve.side_effect = _capture

    client = _authed_client()
    resp = client.post(
        "/analyysikeskus/moju-poliitikamottest/extract",
        data={
            "intent": "mingi kavatsus",
            "known_refs": "Manual § 1, Manual § 2",
        },
    )
    assert resp.status_code == 200
    body = resp.text

    ref_texts = {r.ref_text for r in captured}
    # The manual refs MUST have reached the resolver.
    assert "Manual § 1" in ref_texts, f"Manual ref 1 dropped — captured: {sorted(ref_texts)}"
    assert "Manual § 2" in ref_texts, f"Manual ref 2 dropped — captured: {sorted(ref_texts)}"
    # Combined count still respects the overall cap.
    assert len(captured) <= _MAX_INTENT_CANDIDATES
    # The lowest-confidence LLM rows are the ones that got squeezed out.
    assert "LLM0" not in ref_texts  # lowest confidence — bumped by manual
    # Highest-confidence LLM rows survive.
    assert f"LLM{_MAX_INTENT_CANDIDATES - 1}" in ref_texts
    # The confirmation panel renders the manual refs so the user can see them.
    assert "Manual § 1" in body
    assert "Manual § 2" in body


@patch("app.analyysikeskus.routes._directory._get_recent_analyses", return_value=[])
@patch("app.analyysikeskus.intent_analysis.extract_intent_candidates")
@patch("app.docs.reference_resolver.ReferenceResolver.resolve")
@patch("app.auth.middleware._get_provider")
def test_extract_manual_refs_capped_when_overflowing_total(
    mock_provider: MagicMock,
    mock_resolve: MagicMock,
    mock_extract: MagicMock,
    mock_recent: MagicMock,
):
    """Manual refs are themselves capped at _MAX_INTENT_KNOWN_REFS (10).
    If the user crams more than the cap into the comma list, only the
    first 10 reach the resolver (no LLM rows fit at all in this case
    because the manual list already meets the global cap)."""
    from app.analyysikeskus.intent_extractor import IntentCandidate
    from app.analyysikeskus.routes import _MAX_INTENT_KNOWN_REFS
    from app.docs.entity_extractor import ExtractedRef
    from app.docs.reference_resolver import ResolvedRef

    mock_provider.return_value = _stub_provider()
    mock_extract.return_value = [
        IntentCandidate(
            ref_text="LLM_extra",
            ref_type="law",
            confidence=0.9,
            reasoning="LLM kandidaat.",
        )
    ]

    captured: list[ExtractedRef] = []

    def _capture(refs: list[ExtractedRef]) -> list[ResolvedRef]:
        captured.extend(refs)
        return [
            ResolvedRef(
                extracted=r,
                entity_uri=f"uri-{r.ref_text}",
                matched_label=r.ref_text,
                match_score=1.0,
            )
            for r in refs
        ]

    mock_resolve.side_effect = _capture

    # 15 manual refs > _MAX_INTENT_KNOWN_REFS (10).
    flood = ", ".join(f"Manual{n}" for n in range(15))

    client = _authed_client()
    resp = client.post(
        "/analyysikeskus/moju-poliitikamottest/extract",
        data={"intent": "mingi kavatsus", "known_refs": flood},
    )
    assert resp.status_code == 200

    manual_in_captured = [r for r in captured if r.ref_text.startswith("Manual")]
    assert len(manual_in_captured) <= _MAX_INTENT_KNOWN_REFS
    # The first N manual entries are taken in order (split-on-comma order).
    captured_texts = {r.ref_text for r in manual_in_captured}
    assert "Manual0" in captured_texts
    assert "Manual9" in captured_texts
    assert "Manual10" not in captured_texts  # past the cap


@patch("app.analyysikeskus.routes._directory._get_recent_analyses", return_value=[])
@patch("app.analyysikeskus.intent_analysis.run_adhoc_impact_analysis")
@patch("app.auth.middleware._get_provider")
def test_analyze_caps_confirmed_uris_to_max_intent_confirmed_uris(
    mock_provider: MagicMock,
    mock_adhoc: MagicMock,
    mock_recent: MagicMock,
):
    """#822 P2-1: each confirmed URI triggers a per-URI Jena impact
    run. An unbounded POST would fan out N Jena roundtrips. Cap at
    ``_MAX_INTENT_CONFIRMED_URIS`` (10)."""
    from app.analyysikeskus.adhoc_analysis import AdhocAnalysisResult
    from app.analyysikeskus.routes import _MAX_INTENT_CONFIRMED_URIS

    mock_provider.return_value = _stub_provider()
    mock_adhoc.return_value = AdhocAnalysisResult(
        findings=_canned_findings(),
        score=50,
        graph_uri="g",
    )

    # 25 confirmed rows posted — well above the cap.
    data: dict[str, Any] = {
        "intent": "midagi",
        "confirmed": [str(i) for i in range(25)],
    }
    for i in range(25):
        data[f"uri_{i}"] = f"uri-{i}"
        data[f"label_{i}"] = f"label-{i}"

    client = _authed_client()
    resp = client.post(
        "/analyysikeskus/moju-poliitikamottest/analyze",
        data=data,
    )
    assert resp.status_code == 200
    # The analyser was invoked at most the cap, not all 25.
    assert mock_adhoc.call_count <= _MAX_INTENT_CONFIRMED_URIS, (
        f"Expected at most {_MAX_INTENT_CONFIRMED_URIS} per-URI runs, got {mock_adhoc.call_count}"
    )
