"""Tests for the act-level EU-transposition query + runner (#723).

Covers :mod:`app.docs.impact.eu_transposition`:

* :func:`build_eu_transposition_query` — mentions the right predicates
  (``transposesDirective`` / ``transposedBy`` / ``transpositionStatus``
  / ``harmonisedWith``) and does **not** string-interpolate the EU act
  URI (it's bound via ``uri_bindings`` — the literal URI must not appear
  in the query body).
* :func:`run_eu_transposition` — with ``SparqlClient.query`` mocked:
  normalises raw rows into the workflow's dict shape, maps raw
  ``transpositionStatus`` literals to the four Estonian buckets,
  synthesises a ``puudub`` row when the EU act exists but no Estonian
  act transposes it, and returns ``[]`` on a query exception.
* :func:`normalise_transposition_status` — the status-mapping rules.

No live Jena: every test injects a ``MagicMock`` SparqlClient with
canned ``.query`` return values.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from app.docs.impact.eu_transposition import (
    build_eu_transposition_query,
    normalise_transposition_status,
    run_eu_transposition,
)

_GDPR_URI = "https://data.riik.ee/ontology/estleg#EU-32016R0679"
_AVTS_ACT_URI = "https://data.riik.ee/ontology/estleg#avaliku-teabe-seadus"
_AVTS_P35_URI = "https://data.riik.ee/ontology/estleg#AvTS-p35"


def _client_returning(rows: list[dict[str, Any]]) -> MagicMock:
    client = MagicMock()
    client.query.return_value = rows
    return client


# ---------------------------------------------------------------------------
# build_eu_transposition_query
# ---------------------------------------------------------------------------


def test_build_query_mentions_the_transposition_predicates():
    q = build_eu_transposition_query(_GDPR_URI)
    # Both transposition directions + the status literal + provision-level
    # harmonisation must all appear in the query body.
    assert "estleg:transposesDirective" in q
    assert "estleg:transposedBy" in q
    assert "estleg:transpositionStatus" in q
    assert "estleg:harmonisedWith" in q
    # It's an entity-centered query — pivots off ?euAct, no GRAPH wrapper.
    assert "?euAct" in q
    assert "GRAPH" not in q


def test_build_query_does_not_string_interpolate_the_uri():
    q = build_eu_transposition_query(_GDPR_URI)
    # The URI is bound via uri_bindings={"euAct": ...} (a VALUES clause
    # the SparqlClient appends + validates), NOT interpolated here — so
    # the literal URI must not appear in the static query body.
    assert _GDPR_URI not in q
    # ...and there's no leftover format placeholder either.
    assert "{eu_act_uri}" not in q
    assert "{}" not in q


def test_run_eu_transposition_binds_uri_not_interpolated():
    client = _client_returning([])
    run_eu_transposition(_GDPR_URI, sparql_client=client)
    # The runner must pass the URI as a URI binding, not bake it in.
    client.query.assert_called_once()
    _, kwargs = client.query.call_args
    assert kwargs.get("uri_bindings") == {"euAct": _GDPR_URI}
    sent_query = client.query.call_args.args[0]
    assert _GDPR_URI not in sent_query


# ---------------------------------------------------------------------------
# normalise_transposition_status
# ---------------------------------------------------------------------------


def test_status_mapping_buckets():
    # "complete"-ish → kaetud
    for raw in ("complete", "Transposed", "TÄIELIK", "kaetud", " full "):
        assert normalise_transposition_status(raw) == "kaetud", raw
    # "partial"-ish → osaline
    for raw in ("partial", "Osaline", "partially transposed"):
        assert normalise_transposition_status(raw) == "osaline", raw
    # anything else present → ebaselge
    for raw in ("pending", "unknown", "in progress", "xyz"):
        assert normalise_transposition_status(raw) == "ebaselge", raw
    # absent / blank → ebaselge
    assert normalise_transposition_status(None) == "ebaselge"
    assert normalise_transposition_status("") == "ebaselge"
    assert normalise_transposition_status("   ") == "ebaselge"


# ---------------------------------------------------------------------------
# run_eu_transposition — normalisation + status mapping
# ---------------------------------------------------------------------------


def test_run_eu_transposition_normalises_rows_and_maps_statuses():
    raw_rows = [
        {
            "euAct": _GDPR_URI,
            "euLabel": "Isikuandmete kaitse üldmäärus",
            "celex": "32016R0679",
            "eeAct": _AVTS_ACT_URI,
            "eeActLabel": "Avaliku teabe seadus",
            "status": "complete",
            "eeProvision": _AVTS_P35_URI,
            "eeProvisionLabel": "AvTS § 35",
        },
        {
            "euAct": _GDPR_URI,
            "euLabel": "Isikuandmete kaitse üldmäärus",
            "celex": "32016R0679",
            "eeAct": "https://data.riik.ee/ontology/estleg#rahapesu-seadus",
            "eeActLabel": "Rahapesu tõkestamise seadus",
            "status": "partial",
            # no harmonised provision on this row
        },
        {
            "euAct": _GDPR_URI,
            "euLabel": "Isikuandmete kaitse üldmäärus",
            "celex": "32016R0679",
            "eeAct": "https://data.riik.ee/ontology/estleg#mingi-seadus",
            "eeActLabel": "Mingi seadus",
            # no status literal on this row → ebaselge
        },
    ]
    rows = run_eu_transposition(_GDPR_URI, sparql_client=_client_returning(raw_rows))
    assert len(rows) == 3
    # Dict shape — every key present, the workflow contract.
    expected_keys = {
        "eu_act",
        "eu_label",
        "celex",
        "ee_act",
        "ee_act_label",
        "ee_provision",
        "ee_provision_label",
        "status",
        "authority",
        "authority_label",
    }
    for r in rows:
        assert set(r) == expected_keys
        assert r["eu_act"] == _GDPR_URI
        assert r["eu_label"] == "Isikuandmete kaitse üldmäärus"
        assert r["celex"] == "32016R0679"
        # No authority predicate is wired — always None.
        assert r["authority"] is None
        assert r["authority_label"] is None
    # Status mapping.
    assert rows[0]["status"] == "kaetud"
    assert rows[0]["ee_provision"] == _AVTS_P35_URI
    assert rows[0]["ee_provision_label"] == "AvTS § 35"
    assert rows[1]["status"] == "osaline"
    assert rows[1]["ee_provision"] is None
    assert rows[2]["status"] == "ebaselge"


def test_run_eu_transposition_synthesises_puudub_when_no_transposing_act():
    # Query returns nothing (the act exists per the resolver, but no
    # Estonian act transposes it) → exactly one synthesised "puudub" row.
    rows = run_eu_transposition(_GDPR_URI, sparql_client=_client_returning([]))
    assert len(rows) == 1
    r = rows[0]
    assert r["status"] == "puudub"
    assert r["eu_act"] == _GDPR_URI
    assert r["ee_act"] is None
    assert r["ee_provision"] is None
    assert r["authority"] is None


def test_run_eu_transposition_synthesises_puudub_when_only_act_naming_rows():
    # The query can return a row that names the EU act (label/CELEX) but
    # binds no ?eeAct (the UNION's right branch with no transposedBy
    # match). Still "puudub", but we keep the label/CELEX we saw.
    raw_rows = [
        {"euAct": _GDPR_URI, "euLabel": "Isikuandmete kaitse üldmäärus", "celex": "32016R0679"},
    ]
    rows = run_eu_transposition(_GDPR_URI, sparql_client=_client_returning(raw_rows))
    assert len(rows) == 1
    r = rows[0]
    assert r["status"] == "puudub"
    assert r["eu_label"] == "Isikuandmete kaitse üldmäärus"
    assert r["celex"] == "32016R0679"
    assert r["ee_act"] is None


def test_run_eu_transposition_blank_uri_short_circuits():
    client = MagicMock()
    assert run_eu_transposition("", sparql_client=client) == []
    assert run_eu_transposition("   ", sparql_client=client) == []
    client.query.assert_not_called()


def test_run_eu_transposition_returns_empty_on_query_exception():
    client = MagicMock()
    client.query.side_effect = RuntimeError("jena unreachable")
    assert run_eu_transposition(_GDPR_URI, sparql_client=client) == []


def test_run_eu_transposition_backfills_eu_label_onto_harmonisation_rows():
    # A harmonisation branch can leave ?euLabel / ?celex unbound on some
    # rows; the runner backfills them from the first row that had them.
    raw_rows = [
        {
            "euAct": _GDPR_URI,
            "euLabel": "Isikuandmete kaitse üldmäärus",
            "celex": "32016R0679",
            "eeAct": _AVTS_ACT_URI,
            "eeActLabel": "Avaliku teabe seadus",
            "status": "complete",
        },
        {
            # later row — harmonisation match, EU label/CELEX unbound
            "euAct": _GDPR_URI,
            "eeAct": _AVTS_ACT_URI,
            "eeActLabel": "Avaliku teabe seadus",
            "eeProvision": _AVTS_P35_URI,
            "eeProvisionLabel": "AvTS § 35",
            "status": "complete",
        },
    ]
    rows = run_eu_transposition(_GDPR_URI, sparql_client=_client_returning(raw_rows))
    assert len(rows) == 2
    for r in rows:
        assert r["eu_label"] == "Isikuandmete kaitse üldmäärus"
        assert r["celex"] == "32016R0679"
