"""Tests for the drafter citation resolver pipeline (issue #842)."""

from __future__ import annotations

from types import SimpleNamespace

from app.drafter.citations import (
    _classify,
    coerce_citation,
    resolve_citations,
    unverified_label,
)


class _FakeResolver:
    """Returns a ResolvedRef-shaped object per ref, keyed by ref_text.

    ``mapping`` maps an expected ``ref_text`` to an ``entity_uri`` (or None
    for "act resolved but provision missing" / unresolved).
    """

    def __init__(self, mapping: dict[str, str | None], *, raises: bool = False):
        self.mapping = mapping
        self.raises = raises
        self.seen: list[tuple[str, str]] = []

    def resolve(self, refs):
        if self.raises:
            raise RuntimeError("jena down")
        out = []
        for r in refs:
            self.seen.append((r.ref_type, r.ref_text))
            uri = self.mapping.get(r.ref_text)
            out.append(
                SimpleNamespace(
                    entity_uri=uri,
                    matched_label=(f"label::{uri}" if uri else None),
                )
            )
        return out


# --------------------------------------------------------------------------
# _classify
# --------------------------------------------------------------------------


class TestClassify:
    def test_legacy_estleg_pseudo_uri_normalised_to_provision(self):
        assert _classify("estleg:TsiviilS/par/3") == ("provision", "TsiviilS § 3")

    def test_legacy_estleg_with_brackets(self):
        assert _classify("[estleg:HKTS/par/13]") == ("provision", "HKTS § 13")

    def test_legacy_eu_pseudo_uri(self):
        rt, _ = _classify("eu:2016-679/art/6")
        assert rt == "eu_act"

    def test_human_paragraph_is_provision(self):
        assert _classify("Halduskoostöö seadus § 13") == (
            "provision",
            "Halduskoostöö seadus § 13",
        )

    def test_celex_is_eu_act(self):
        assert _classify("32016R0679")[0] == "eu_act"

    def test_case_number_is_court_decision(self):
        assert _classify("3-2-1-100-15")[0] == "court_decision"

    def test_bare_name_is_law(self):
        assert _classify("Karistusseadustik")[0] == "law"


# --------------------------------------------------------------------------
# resolve_citations
# --------------------------------------------------------------------------


class TestResolveCitations:
    def test_empty_and_none(self):
        assert resolve_citations(None) == []
        assert resolve_citations([]) == []

    def test_verified_citation_gets_uri_and_explorer_link(self):
        uri = "https://data.riik.ee/ontology/estleg#HKTS_Par_13"
        resolver = _FakeResolver({"HKTS § 13": uri})
        out = resolve_citations(["HKTS § 13"], resolver=resolver)
        assert len(out) == 1
        c = out[0]
        assert c["verified"] is True
        assert c["resolved_uri"] == uri
        assert c["label"] == f"label::{uri}"
        assert c["explorer_url"] == "/explorer?focus=" + (
            "https%3A%2F%2Fdata.riik.ee%2Fontology%2Festleg%23HKTS_Par_13"
        )

    def test_unresolved_citation_is_unverified(self):
        resolver = _FakeResolver({"HKTS § 99": None})
        out = resolve_citations(["HKTS § 99"], resolver=resolver)
        c = out[0]
        assert c["verified"] is False
        assert c["resolved_uri"] is None
        assert c["explorer_url"] is None
        assert c["text"] == "HKTS § 99"

    def test_fabricated_pseudo_uri_resolved_via_normalised_form(self):
        # Legacy fabricated id -> normalised to "TsiviilS § 3" before resolve.
        resolver = _FakeResolver({"TsiviilS § 3": None})
        out = resolve_citations(["estleg:TsiviilS/par/3"], resolver=resolver)
        assert resolver.seen == [("provision", "TsiviilS § 3")]
        assert out[0]["verified"] is False

    def test_resolver_failure_fails_open_all_unverified(self):
        resolver = _FakeResolver({}, raises=True)
        out = resolve_citations(["HKTS § 13", "KarS § 5"], resolver=resolver)
        assert [c["verified"] for c in out] == [False, False]
        assert all(c["explorer_url"] is None for c in out)

    def test_order_preserved_across_mixed_results(self):
        resolver = _FakeResolver({"A § 1": "uri:a", "B § 2": None, "C § 3": "uri:c"})
        out = resolve_citations(["A § 1", "B § 2", "C § 3"], resolver=resolver)
        assert [c["text"] for c in out] == ["A § 1", "B § 2", "C § 3"]
        assert [c["verified"] for c in out] == [True, False, True]

    def test_already_enriched_dict_passes_through(self):
        enriched = {
            "text": "X § 1",
            "resolved_uri": "uri:x",
            "label": "X paragrahv 1",
            "verified": True,
            "explorer_url": "/explorer?focus=uri%3Ax",
        }
        resolver = _FakeResolver({})
        out = resolve_citations([enriched], resolver=resolver)
        assert out[0]["verified"] is True
        assert out[0]["resolved_uri"] == "uri:x"
        # Resolver was never asked about an already-enriched dict.
        assert resolver.seen == []


# --------------------------------------------------------------------------
# coerce_citation  (read path — never re-resolves)
# --------------------------------------------------------------------------


class TestCoerceCitation:
    def test_legacy_string_is_unverified(self):
        c = coerce_citation("estleg:Foo/par/1")
        assert c["verified"] is False
        assert c["text"] == "estleg:Foo/par/1"
        assert c["explorer_url"] is None

    def test_enriched_dict_preserved(self):
        c = coerce_citation({"text": "X § 1", "resolved_uri": "uri:x", "verified": True})
        assert c["verified"] is True
        assert c["explorer_url"] == "/explorer?focus=uri%3Ax"

    def test_dict_without_uri_is_unverified(self):
        c = coerce_citation({"text": "X § 1", "verified": True})
        assert c["verified"] is False
        assert c["explorer_url"] is None


def test_unverified_label():
    assert unverified_label("HKTS § 13") == "kontrollimata viide: HKTS § 13"
