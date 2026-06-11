"""Tests for the Halduskoormus workflow (A2 v1).

Covers:

1. The deontic-bucket key folding in :func:`app.ontology.relations.norm_type_key`
   — canonical URI / prefixed name / literal alias inputs, and the
   ``"unknown"`` fallback.
2. The SPARQL helper layer in :mod:`app.analyysikeskus.burden` —
   row → :class:`BurdenRow` conversion, empty / dead-Jena paths, the
   act / provision / draft-delta query shapes, the per-row dedup +
   merge logic, and the dutyHolder top-N bucketing fallback.
3. The Estonian display-label helpers (``burden_label`` /
   ``burden_description``).
4. The route layer in :mod:`app.analyysikeskus.routes` — the
   ``/analyysikeskus/halduskoormus`` endpoint: the auth gate, the
   landing page (no ``sisend``), the resolved-provision happy path,
   the disambiguation branch, and the unresolved branch.
5. The capability is marked ``"live"`` in :data:`app.ui.capabilities.CAPABILITIES`.
6. Fixture-graph rdflib SPARQL — exercise the act-level burden query
   against the canonical TTL fixture to prove the SPARQL template is
   valid.

Tests follow the same shape as ``test_analyysikeskus_sanctions.py`` —
the SPARQL client / ReferenceResolver / RAG retriever are patched
*where used* (the patch-path contract).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from app.ontology.temporal_scope import TemporalScope

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "ontology_canonical.ttl"


# ---------------------------------------------------------------------------
# Shared URIs + row stubs
# ---------------------------------------------------------------------------

_TLS_URI = "https://data.riik.ee/ontology/estleg#toolepingu_seadus"
_TLS_P12_URI = "https://data.riik.ee/ontology/estleg#TLS-p12"

_NS = "https://data.riik.ee/ontology/estleg#"
_OBLIGATION_URI = f"{_NS}NormType_Obligation"
_RIGHT_URI = f"{_NS}NormType_Right"
_PERMISSION_URI = f"{_NS}NormType_Permission"
_PROHIBITION_URI = f"{_NS}NormType_Prohibition"


def _provision_row(
    *,
    provision: str = _TLS_P12_URI,
    label: str = "TLS § 12",
    act: str = _TLS_URI,
    act_label: str = "Töölepingu seadus",
    norm_type: str = _OBLIGATION_URI,
    duty_holder: str = "Tööandja",
) -> dict[str, str]:
    return {
        "provision": provision,
        "provisionLabel": label,
        "act": act,
        "actLabel": act_label,
        "normType": norm_type,
        "dutyHolder": duty_holder,
    }


# ---------------------------------------------------------------------------
# 1. norm_type_key — bucketing
# ---------------------------------------------------------------------------


class TestNormTypeKey:
    def test_canonical_individuals_resolve_to_each_bucket(self):
        from app.ontology.relations import norm_type_key

        assert norm_type_key(_OBLIGATION_URI) == "obligation"
        assert norm_type_key(_RIGHT_URI) == "right"
        assert norm_type_key(_PERMISSION_URI) == "permission"
        assert norm_type_key(_PROHIBITION_URI) == "prohibition"

    def test_prefixed_and_bare_local_name(self):
        from app.ontology.relations import norm_type_key

        assert norm_type_key("estleg:NormType_Obligation") == "obligation"
        assert norm_type_key("NormType_Prohibition") == "prohibition"

    def test_literal_estonian_aliases(self):
        from app.ontology.relations import norm_type_key

        assert norm_type_key("Kohustus") == "obligation"
        assert norm_type_key("õigus") == "right"
        assert norm_type_key("luba") == "permission"
        assert norm_type_key("keeld") == "prohibition"

    def test_literal_with_language_tag(self):
        from app.ontology.relations import norm_type_key

        # rdflib serialisations sometimes carry the @et tag.
        assert norm_type_key("Kohustus@et") == "obligation"

    def test_unknown_and_blank_inputs(self):
        from app.ontology.relations import norm_type_key

        assert norm_type_key("") == "unknown"
        assert norm_type_key("   ") == "unknown"
        assert norm_type_key("Suvaline jutt") == "unknown"


# ---------------------------------------------------------------------------
# 2. Estonian label / description helpers
# ---------------------------------------------------------------------------


class TestBurdenLabels:
    def test_known_labels(self):
        from app.analyysikeskus.burden import burden_label

        assert burden_label("obligation") == "Kohustused"
        assert burden_label("right") == "Õigused"
        assert burden_label("permission") == "Load"
        assert burden_label("prohibition") == "Keelud"
        assert burden_label("unknown") == "Liigitamata"

    def test_unknown_label_falls_back(self):
        from app.analyysikeskus.burden import burden_label

        assert burden_label("garbage") == "Liigitamata"

    def test_description_is_estonian(self):
        from app.analyysikeskus.burden import burden_description

        # Just smoke-check that every bucket gets a non-empty Estonian sentence.
        for key in ("obligation", "right", "permission", "prohibition", "unknown"):
            desc = burden_description(key)
            assert desc and isinstance(desc, str)
            assert len(desc) > 10


# ---------------------------------------------------------------------------
# 3. SPARQL helpers — list_burden_for_act / _for_provision / delta_for_draft
# ---------------------------------------------------------------------------


class TestListBurdenForAct:
    def test_returns_parsed_rows_with_counts(self):
        from app.analyysikeskus.burden import list_burden_for_act

        stub_client = MagicMock()
        stub_client.query.return_value = [
            _provision_row(provision=f"{_NS}P1", label="TLS § 1", norm_type=_OBLIGATION_URI),
            _provision_row(provision=f"{_NS}P2", label="TLS § 2", norm_type=_PROHIBITION_URI),
            _provision_row(
                provision=f"{_NS}P3",
                label="TLS § 3",
                norm_type=_RIGHT_URI,
                duty_holder="Töötaja",
            ),
        ]
        summary = list_burden_for_act(_TLS_URI, sparql_client=stub_client)
        assert summary.total == 3
        assert summary.counts["obligation"] == 1
        assert summary.counts["prohibition"] == 1
        assert summary.counts["right"] == 1
        assert summary.counts["permission"] == 0
        assert summary.counts["unknown"] == 0
        # dutyHolder buckets — both "Tööandja" (2 rows) and "Töötaja" (1).
        assert summary.duty_holder_counts.get("Tööandja") == 2
        assert summary.duty_holder_counts.get("Töötaja") == 1

    def test_blank_uri_short_circuits(self):
        from app.analyysikeskus.burden import list_burden_for_act

        stub_client = MagicMock()
        summary = list_burden_for_act("", sparql_client=stub_client)
        assert summary.total == 0
        stub_client.query.assert_not_called()

    def test_dead_jena_returns_empty_summary(self):
        from app.analyysikeskus.burden import list_burden_for_act

        stub_client = MagicMock()
        stub_client.query.side_effect = RuntimeError("jena down")
        summary = list_burden_for_act(_TLS_URI, sparql_client=stub_client)
        assert summary.total == 0
        # Counts dict still fully populated so the UI can render uniformly.
        for key in ("obligation", "prohibition", "permission", "right", "unknown"):
            assert summary.counts[key] == 0

    def test_dedup_merges_double_rows(self):
        """A single provision with two normativeType edges (canonical + literal)
        is merged into one row with the better (non-unknown) classification."""
        from app.analyysikeskus.burden import list_burden_for_act

        stub_client = MagicMock()
        stub_client.query.return_value = [
            # First row: literal-only (folds to "obligation" via alias)
            _provision_row(provision=f"{_NS}P1", norm_type="Kohustus", duty_holder=""),
            # Second row: canonical URI for the same provision
            _provision_row(provision=f"{_NS}P1", norm_type=_OBLIGATION_URI, duty_holder="Riik"),
        ]
        summary = list_burden_for_act(_TLS_URI, sparql_client=stub_client)
        assert summary.total == 1
        assert summary.rows[0].burden_key == "obligation"
        # dutyHolder should be carried forward from whichever row had one.
        assert summary.rows[0].duty_holder == "Riik"

    def test_passes_uri_input_as_uri_binding(self):
        """A URI-shaped input binds ``?actLit`` via ``uri_bindings`` so the
        SPARQL ``VALUES`` clause emits ``<URI>`` form — the canonical
        TTL fixture shape where ``estleg:sourceAct`` carries a URI object.
        """
        from app.analyysikeskus.burden import list_burden_for_act

        stub_client = MagicMock()
        stub_client.query.return_value = []
        list_burden_for_act(_TLS_URI, sparql_client=stub_client)
        kwargs = stub_client.query.call_args.kwargs
        assert kwargs.get("uri_bindings") == {"actLit": _TLS_URI}
        assert kwargs.get("bindings") is None

    def test_passes_literal_title_as_string_binding(self):
        """A literal-title input binds ``?actLit`` via ``bindings`` so the
        SPARQL ``VALUES`` clause emits ``"Title"`` (string) form — the
        prod shape where ``estleg:sourceAct`` is always a string literal
        (Wave 2 spike, 2026-05-18).
        """
        from app.analyysikeskus.burden import list_burden_for_act

        stub_client = MagicMock()
        stub_client.query.return_value = []
        list_burden_for_act("Töölepingu seadus", sparql_client=stub_client)
        kwargs = stub_client.query.call_args.kwargs
        assert kwargs.get("bindings") == {"actLit": "Töölepingu seadus"}
        assert kwargs.get("uri_bindings") is None

    def test_literal_title_rows_flow_through_workflow(self):
        """A mock prod-shaped row (``?act`` empty, ``?actLabel`` literal)
        flows correctly through the burden workflow — the Python that
        previously expected ``?act`` as a URI no longer crashes.

        In prod the ``sourceAct`` object is a string literal, so the
        SPARQL ``BIND`` clauses set ``?act = ""`` and ``?actLabel =
        <literal title>``. The :class:`BurdenRow` should carry the
        empty URI + the literal title without raising.
        """
        from app.analyysikeskus.burden import list_burden_for_act

        stub_client = MagicMock()
        # Prod-shaped row: no act URI, actLabel is the literal title.
        stub_client.query.return_value = [
            {
                "provision": f"{_NS}P1",
                "provisionLabel": "TLS § 12",
                "act": "",  # literal sourceAct in prod ⇒ empty URI
                "actLabel": "Töölepingu seadus",
                "normType": _OBLIGATION_URI,
                "dutyHolder": "Tööandja",
            }
        ]
        summary = list_burden_for_act("Töölepingu seadus", sparql_client=stub_client)
        assert summary.total == 1
        row = summary.rows[0]
        assert row.provision_uri == f"{_NS}P1"
        assert row.act_uri == ""  # literal mode ⇒ empty URI
        assert row.act_label == "Töölepingu seadus"
        assert row.burden_key == "obligation"
        assert summary.counts["obligation"] == 1


class TestListBurdenForProvision:
    def test_returns_single_row(self):
        from app.analyysikeskus.burden import list_burden_for_provision

        stub_client = MagicMock()
        stub_client.query.return_value = [_provision_row()]
        summary = list_burden_for_provision(_TLS_P12_URI, sparql_client=stub_client)
        assert summary.total == 1
        assert summary.rows[0].provision_uri == _TLS_P12_URI
        assert summary.rows[0].burden_key == "obligation"
        assert summary.counts["obligation"] == 1

    def test_blank_uri_short_circuits(self):
        from app.analyysikeskus.burden import list_burden_for_provision

        stub_client = MagicMock()
        summary = list_burden_for_provision("", sparql_client=stub_client)
        assert summary.total == 0
        stub_client.query.assert_not_called()

    def test_sparql_error_returns_empty(self):
        from app.analyysikeskus.burden import list_burden_for_provision

        stub_client = MagicMock()
        stub_client.query.side_effect = RuntimeError("jena down")
        summary = list_burden_for_provision(_TLS_P12_URI, sparql_client=stub_client)
        assert summary.total == 0


class TestBurdenDeltaForDraft:
    def test_empty_uri_yields_empty_delta(self):
        from app.analyysikeskus.burden import burden_delta_for_draft

        stub_client = MagicMock()
        delta = burden_delta_for_draft("", sparql_client=stub_client)
        assert delta.affected_count == 0
        assert delta.after is None
        assert delta.before.total == 0
        stub_client.query.assert_not_called()

    def test_aggregates_per_affected_provision(self):
        from app.analyysikeskus.burden import burden_delta_for_draft

        stub_client = MagicMock()
        # First call: affected-provisions list. Then one call per provision.
        stub_client.query.side_effect = [
            # Affected provisions (the draft touches P1 + P2)
            [{"provision": f"{_NS}P1"}, {"provision": f"{_NS}P2"}],
            # Burden for P1 — obligation
            [_provision_row(provision=f"{_NS}P1", norm_type=_OBLIGATION_URI)],
            # Burden for P2 — right
            [_provision_row(provision=f"{_NS}P2", norm_type=_RIGHT_URI)],
        ]
        delta = burden_delta_for_draft(f"{_NS}Draft_1", sparql_client=stub_client)
        assert delta.affected_count == 2
        assert delta.after is None  # v1: deferred to v2 after ontology #214
        assert delta.before.total == 2
        assert delta.before.counts["obligation"] == 1
        assert delta.before.counts["right"] == 1

    def test_dead_jena_on_affected_query_returns_empty(self):
        from app.analyysikeskus.burden import burden_delta_for_draft

        stub_client = MagicMock()
        stub_client.query.side_effect = RuntimeError("jena down")
        delta = burden_delta_for_draft(f"{_NS}Draft_1", sparql_client=stub_client)
        assert delta.affected_count == 0
        assert delta.before.total == 0


# ---------------------------------------------------------------------------
# 4. Top-N dutyHolder bucketing
# ---------------------------------------------------------------------------


class TestTopDutyHolders:
    def test_lumps_long_tail_into_muud(self):
        from app.analyysikeskus.burden import BurdenRow, top_duty_holders

        # 15 distinct dutyHolder literals, cap at 3 explicit → 12 lumped into "Muud".
        rows = [BurdenRow(provision_uri=f"P{i}", duty_holder=f"Actor_{i}") for i in range(15)]
        result = top_duty_holders(rows, limit=3)
        explicit_keys = [k for k in result if k not in ("Muud", "")]
        assert len(explicit_keys) == 3
        assert "Muud" in result
        assert result["Muud"] == 12

    def test_empty_dutyholder_kept_separately(self):
        from app.analyysikeskus.burden import BurdenRow, top_duty_holders

        rows = [
            BurdenRow(provision_uri="P1", duty_holder=""),
            BurdenRow(provision_uri="P2", duty_holder=""),
            BurdenRow(provision_uri="P3", duty_holder="Tööandja"),
        ]
        result = top_duty_holders(rows)
        assert result[""] == 2
        assert result["Tööandja"] == 1


# ---------------------------------------------------------------------------
# 5. Route smoke tests (test client end-to-end)
# ---------------------------------------------------------------------------


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


def _canned_resolved_provision_ref():
    from app.docs.entity_extractor import ExtractedRef
    from app.docs.reference_resolver import ResolvedRef

    return ResolvedRef(
        extracted=ExtractedRef(
            ref_text="TLS § 12",
            ref_type="provision",
            confidence=1.0,
            location={"source": "analyysikeskus_input"},
        ),
        entity_uri=_TLS_P12_URI,
        matched_label="TLS § 12 — Töölepingu seadus",
        match_score=1.0,
    )


def _canned_resolved_law_ref():
    """Build a ResolvedRef matching the real Wave 2 Step 2 resolver shape.

    Post-Wave-2 the resolver returns ``entity_uri=None`` for law-only
    refs and rides the canonical act title literal on ``partial_match``.
    The route picks that title up and routes to ``list_burden_for_act``.
    """
    from app.docs.entity_extractor import ExtractedRef
    from app.docs.reference_resolver import ResolvedRef

    return ResolvedRef(
        extracted=ExtractedRef(
            ref_text="TLS",
            ref_type="law",
            confidence=1.0,
            location={"source": "analyysikeskus_input"},
        ),
        entity_uri=None,
        matched_label="Töölepingu seadus",
        match_score=1.0,
        partial_match={
            "act_token": "TLS",
            "act_title": "Töölepingu seadus",
            "section": None,
        },
    )


def _canned_summary():
    from app.analyysikeskus.burden import BurdenRow, BurdenSummary

    rows = [
        BurdenRow(
            provision_uri=_TLS_P12_URI,
            provision_label="TLS § 12",
            act_uri=_TLS_URI,
            act_label="Töölepingu seadus",
            norm_type_uri=_OBLIGATION_URI,
            burden_key="obligation",
            duty_holder="Tööandja",
        )
    ]
    return BurdenSummary(
        counts={
            "obligation": 1,
            "right": 0,
            "permission": 0,
            "prohibition": 0,
            "unknown": 0,
        },
        rows=rows,
        duty_holder_counts={"Tööandja": 1},
        total=1,
        truncated=False,
    )


def test_burden_redirects_unauthenticated():
    from starlette.testclient import TestClient

    from app.main import app

    client = TestClient(app, follow_redirects=False)
    resp = client.get("/analyysikeskus/halduskoormus")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/auth/login"


@patch("app.auth.middleware._get_provider")
def test_burden_landing_renders_input_form(mock_provider: MagicMock):
    mock_provider.return_value = _stub_provider()
    client = _authed_client()
    resp = client.get("/analyysikeskus/halduskoormus")
    assert resp.status_code == 200
    body = resp.text
    assert "Halduskoormus" in body
    for heading in ("Sisend", "Ulatus", "Tulemused", "Tõendid", "Soovitatud tegevused"):
        assert heading in body, heading
    assert 'action="/analyysikeskus/halduskoormus"' in body
    assert "Hinda halduskoormust" in body


@patch("app.analyysikeskus.routes.list_burden_for_provision")
@patch("app.docs.reference_resolver.ReferenceResolver.resolve")
@patch("app.auth.middleware._get_provider")
def test_burden_resolved_provision_renders_full_result(
    mock_provider: MagicMock,
    mock_resolve: MagicMock,
    mock_list: MagicMock,
):
    mock_provider.return_value = _stub_provider()
    mock_resolve.return_value = [_canned_resolved_provision_ref()]
    mock_list.return_value = _canned_summary()

    client = _authed_client()
    resp = client.get("/analyysikeskus/halduskoormus?sisend=TLS+%C2%A7+12")
    assert resp.status_code == 200
    body = resp.text

    assert "Halduskoormus" in body
    for heading in ("Sisend", "Ulatus", "Tulemused", "Tõendid", "Soovitatud tegevused"):
        assert heading in body, heading
    # The resolved label appears in Sisend.
    assert "TLS § 12 — Töölepingu seadus" in body
    # The count grid shows the Kohustused bucket with count 1.
    assert "Kohustused" in body
    # The dutyHolder fallback column is labelled with the #214 reference.
    assert "Kohustatud isik (esialgne, vt #214)" in body
    # Tõendid row links to the provision URI in Õiguskaart.
    assert "/explorer?focus=" in body
    # Suggested actions include the "Tagasi" link.
    assert "Tagasi analüüsikeskusesse" in body

    mock_list.assert_called_once_with(_TLS_P12_URI, scope=TemporalScope.CURRENT)


@patch("app.analyysikeskus.routes.list_burden_for_act")
@patch("app.docs.reference_resolver.ReferenceResolver.resolve")
@patch("app.auth.middleware._get_provider")
def test_burden_resolved_law_uses_act_query(
    mock_provider: MagicMock,
    mock_resolve: MagicMock,
    mock_list_act: MagicMock,
):
    """Wave 2 Step 5: a partial-match law ref routes to list_burden_for_act(title).

    The Wave 2 Step 2 resolver returns ``entity_uri=None`` plus a
    ``partial_match`` payload carrying the literal act title for any
    law-only ref. The route's new dispatch picks that title up.
    """
    mock_provider.return_value = _stub_provider()
    mock_resolve.return_value = [_canned_resolved_law_ref()]
    mock_list_act.return_value = _canned_summary()

    client = _authed_client()
    # Use a §-ref input so parse_user_reference emits structured refs;
    # mock_resolve returns only the law ref so the route enters the act
    # branch (mirrors test_sanctions_resolved_law_uses_act_query).
    resp = client.get("/analyysikeskus/halduskoormus?sisend=TLS+%C2%A7+12")
    assert resp.status_code == 200
    # The route now passes the literal act title (from partial_match)
    # rather than a fake URI — matches the real resolver shape.
    mock_list_act.assert_called_once_with("Töölepingu seadus", scope=TemporalScope.CURRENT)


@patch("app.analyysikeskus.routes.list_burden_for_act")
@patch("app.analyysikeskus.routes._rag_candidates", return_value=[])
@patch("app.docs.reference_resolver.ReferenceResolver.resolve")
@patch("app.auth.middleware._get_provider")
def test_burden_bare_law_input_routes_to_for_act(
    mock_provider: MagicMock,
    mock_resolve: MagicMock,
    mock_rag: MagicMock,
    mock_list_act: MagicMock,
):
    """Wave 2 Step 5: a bare law sisend (``TLS``) routes to list_burden_for_act.

    Exercises the full path: parse_user_reference recognises ``TLS`` as
    a bare law abbreviation → resolver returns the partial-match shape
    → route picks the title from ``partial_match`` and calls
    ``list_burden_for_act("Töölepingu seadus")``. The route does NOT
    fall through to the unresolved/RAG path.
    """
    mock_provider.return_value = _stub_provider()
    mock_resolve.return_value = [_canned_resolved_law_ref()]
    mock_list_act.return_value = _canned_summary()

    client = _authed_client()
    # Bare law input — no § ref. The new bare-law branch in
    # parse_user_reference emits a single ``law`` ExtractedRef.
    resp = client.get("/analyysikeskus/halduskoormus?sisend=TLS")
    assert resp.status_code == 200
    body = resp.text

    # The act-level helper was called with the literal title.
    mock_list_act.assert_called_once_with("Töölepingu seadus", scope=TemporalScope.CURRENT)
    # The RAG fallback was NOT consulted.
    mock_rag.assert_not_called()
    # The "Ei tuvastanud" warning is absent — we resolved (partially).
    assert "Ei tuvastanud õiguslikku viidet" not in body


@patch("app.analyysikeskus.routes._rag_candidates", return_value=[])
@patch("app.docs.reference_resolver.ReferenceResolver.resolve", return_value=[])
@patch("app.auth.middleware._get_provider")
def test_burden_unresolved_input_shows_warning(
    mock_provider: MagicMock,
    mock_resolve: MagicMock,
    mock_rag: MagicMock,
):
    mock_provider.return_value = _stub_provider()
    client = _authed_client()
    resp = client.get("/analyysikeskus/halduskoormus?sisend=mingi+suvaline+jutt")
    assert resp.status_code == 200
    body = resp.text
    assert "Ei tuvastanud õiguslikku viidet" in body


@patch("app.analyysikeskus.routes.list_burden_for_provision")
@patch("app.docs.reference_resolver.ReferenceResolver.resolve")
@patch("app.auth.middleware._get_provider")
def test_burden_disambiguation_when_multiple_resolutions(
    mock_provider: MagicMock,
    mock_resolve: MagicMock,
    mock_list: MagicMock,
):
    """Two distinct URI-resolved refs ⇒ disambiguation card.

    Wave 2 Step 5: the route now prefers URI-resolved refs over
    partial-match refs when both are present (a §-ref input naturally
    produces one URI + one partial-match for the same act — they
    collapse to one). To force the disambiguation branch we have to
    mock TWO distinct URI-resolved refs.
    """
    from app.docs.entity_extractor import ExtractedRef
    from app.docs.reference_resolver import ResolvedRef

    other_uri = "https://data.riik.ee/ontology/estleg#TLS-p15"
    other_ref = ResolvedRef(
        extracted=ExtractedRef(
            ref_text="TLS § 15",
            ref_type="provision",
            confidence=1.0,
            location={"source": "analyysikeskus_input"},
        ),
        entity_uri=other_uri,
        matched_label="TLS § 15 — Töölepingu seadus",
        match_score=1.0,
    )

    mock_provider.return_value = _stub_provider()
    mock_resolve.return_value = [
        _canned_resolved_provision_ref(),
        other_ref,
    ]

    client = _authed_client()
    resp = client.get("/analyysikeskus/halduskoormus?sisend=TLS+%C2%A7+12")
    assert resp.status_code == 200
    body = resp.text
    assert "Sisend võib viidata mitmele üksusele" in body
    assert "TLS § 12 — Töölepingu seadus" in body
    assert "TLS § 15 — Töölepingu seadus" in body
    mock_list.assert_not_called()


# ---------------------------------------------------------------------------
# 6. Capability is marked live
# ---------------------------------------------------------------------------


def test_halduskoormus_capability_is_live():
    """The Capability dictionary entry must report ``status="live"``."""
    from app.ui.capabilities import CAPABILITIES

    cap = next(c for c in CAPABILITIES if c.slug == "halduskoormus")
    assert cap.status == "live"
    assert cap.target_url == "/analyysikeskus/halduskoormus"


def test_halduskoormus_has_input_metadata():
    """Live capabilities must have a ``_ANALYYSIKESKUS_INPUTS`` row."""
    from app.analyysikeskus.routes import _ANALYYSIKESKUS_INPUTS

    assert "halduskoormus" in _ANALYYSIKESKUS_INPUTS
    inputs = _ANALYYSIKESKUS_INPUTS["halduskoormus"]
    assert inputs["placeholder"]
    assert inputs["aria_label"]
    assert inputs["examples"]


# ---------------------------------------------------------------------------
# 7. Existing routes still register — no breakage
# ---------------------------------------------------------------------------


def test_existing_workflows_still_registered():
    """A2 only appends — the Normi / EL / Sanctions routes must still respond."""
    from starlette.testclient import TestClient

    from app.main import app

    client = TestClient(app, follow_redirects=False)
    for path in (
        "/analyysikeskus",
        "/analyysikeskus/normi-mojuahel",
        "/analyysikeskus/el-ulevott",
        "/analyysikeskus/sanktsioonid",
        "/analyysikeskus/halduskoormus",
    ):
        resp = client.get(path)
        assert resp.status_code == 303, path
        assert resp.headers["location"] == "/auth/login", path


# ---------------------------------------------------------------------------
# 8. Fixture-graph SPARQL — exercise the act-level template against rdflib
# ---------------------------------------------------------------------------


class TestActQueryAgainstFixture:
    """End-to-end against ``tests/fixtures/ontology_canonical.ttl``.

    Loads the fixture into an in-memory rdflib graph and runs the
    act-level burden SPARQL template against it — proves the template is
    valid SPARQL and the predicate names match what the canonical fixture
    populates after the A2 fixture extension.
    """

    def _load_graph(self):
        from rdflib import Graph

        g = Graph()
        g.parse(str(FIXTURE_PATH), format="turtle")
        return g

    def test_fixture_emits_three_provisions_for_act_1(self):
        from app.analyysikeskus.burden import _build_act_burden_query

        g = self._load_graph()
        # Inject the ``?actLit`` VALUES binding in URI form — the fixture
        # carries ``estleg:Provision_1 estleg:sourceAct estleg:Act_1``
        # (URI object), so the act-level burden query binds the act URI.
        # In prod the same query is run with a literal title binding;
        # see ``TestListBurdenForAct.test_passes_literal_title_as_string_binding``.
        query = _build_act_burden_query()
        last_brace = query.rfind("}")
        values_block = f"VALUES ?actLit {{ <{_NS}Act_1> }}\n"
        query = query[:last_brace] + "\n" + values_block + "\n" + query[last_brace:]

        raw_rows: Any = list(g.query(query))
        # The fixture has Provision_1 and Provision_3 with
        # ``estleg:sourceAct estleg:Act_1``; Provision_2 is on Act_2.
        provisions = {str(r[0]) for r in raw_rows}
        assert f"{_NS}Provision_1" in provisions
        assert f"{_NS}Provision_3" in provisions
        assert f"{_NS}Provision_2" not in provisions

    def test_fixture_normative_types_match_canonical_individuals(self):
        """Each fixture provision's normativeType resolves to a canonical bucket."""
        from app.analyysikeskus.burden import _build_act_burden_query
        from app.ontology.relations import norm_type_key

        g = self._load_graph()
        query = _build_act_burden_query()
        last_brace = query.rfind("}")
        values_block = f"VALUES ?actLit {{ <{_NS}Act_1> }}\n"
        query = query[:last_brace] + "\n" + values_block + "\n" + query[last_brace:]

        raw_rows: Any = list(g.query(query))
        by_provision: dict[str, str] = {}
        for r in raw_rows:
            provision = str(r[0])
            norm_type = str(r[4]) if r[4] is not None else ""
            by_provision[provision] = norm_type_key(norm_type)
        assert by_provision[f"{_NS}Provision_1"] == "obligation"
        assert by_provision[f"{_NS}Provision_3"] == "prohibition"

    def test_fixture_act_label_is_projected_from_rdfs_label(self):
        """When ``estleg:sourceAct`` carries a URI (fixture shape), the
        SPARQL ``BIND`` clauses should resolve ``?actLabel`` via the
        URI's ``rdfs:label`` rather than the URI's local-name.

        This is the canonical TTL fixture path. In prod ``sourceAct``
        is a literal, so ``?actLabel`` is the literal itself — covered
        by :class:`TestListBurdenForAct.test_literal_title_rows_flow_through_workflow`.
        """
        from app.analyysikeskus.burden import _build_act_burden_query

        g = self._load_graph()
        query = _build_act_burden_query()
        last_brace = query.rfind("}")
        values_block = f"VALUES ?actLit {{ <{_NS}Act_1> }}\n"
        query = query[:last_brace] + "\n" + values_block + "\n" + query[last_brace:]

        raw_rows: Any = list(g.query(query))
        # Every row should carry the fixture act label "Act 1 — fixture host"
        # and the URI string of Act_1 in the ?act binding.
        labels = {str(r[3]) for r in raw_rows if r[3] is not None}
        uris = {str(r[2]) for r in raw_rows if r[2] is not None}
        assert "Act 1 — fixture host" in labels
        assert f"{_NS}Act_1" in uris
