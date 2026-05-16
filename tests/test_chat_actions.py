"""Unit tests for the C1 outbound chat action links.

Two layers:

1. Pure-function tests for ``detect_entity_type``, ``extract_celex``
   and ``build_actions_for_uri`` — they cover the entity → action
   mapping and the URL builders without touching any FastHTML
   rendering.
2. Rendering tests for ``chat_actions_block`` — assert the
   collapsible ``<details class="chat-actions">`` markup, the
   Estonian summary label, and that the right URLs / form actions
   show up in the rendered HTML.
"""

from __future__ import annotations

import pytest

from app.chat.actions import (
    ACTIONS_BY_ENTITY_TYPE,
    ChatActionLink,
    build_actions_for_uri,
    chat_actions_block,
    collect_actions,
    detect_entity_type,
    extract_celex,
)

# ---------------------------------------------------------------------------
# detect_entity_type
# ---------------------------------------------------------------------------


class TestDetectEntityType:
    @pytest.mark.parametrize(
        "uri",
        [
            "estleg:KarS_p121",
            "estleg:AvTS_p35_lg1_p5",
            "https://data.riik.ee/ontology/estleg#Provision_1",
            "https://data.riik.ee/ontology/estleg#LegalProvision_42",
            "estleg:TsUS_p1_s1",
        ],
    )
    def test_recognises_provisions(self, uri: str):
        assert detect_entity_type(uri) == "provision"

    @pytest.mark.parametrize(
        "uri",
        [
            "estleg:EU_Dir_1",
            "https://data.riik.ee/ontology/estleg#EULegislation_GDPR",
            "estleg:32016R0679",
            "32016R0679",
            "estleg:CELEX_32016R0679",
        ],
    )
    def test_recognises_eu_acts(self, uri: str):
        assert detect_entity_type(uri) == "eu_act"

    @pytest.mark.parametrize(
        "uri",
        [
            "estleg:CourtDecision_1",
            "https://data.riik.ee/ontology/estleg#CourtDecision_2",
            "estleg:RKKK_3_1_1_63_15",
            "estleg:RKHK_3_3_1_4_13",
            "estleg:RKTK_2_19_5",
        ],
    )
    def test_recognises_court_decisions(self, uri: str):
        assert detect_entity_type(uri) == "court_decision"

    @pytest.mark.parametrize(
        "uri",
        [
            "",
            None,
            "estleg:Cluster_1",
            "estleg:Concept_42",
            "https://example.com/random-uri",
        ],
    )
    def test_unknown_falls_through(self, uri):
        assert detect_entity_type(uri) == "unknown"


# ---------------------------------------------------------------------------
# extract_celex
# ---------------------------------------------------------------------------


class TestExtractCelex:
    @pytest.mark.parametrize(
        "uri,expected",
        [
            ("estleg:32016R0679", "32016R0679"),
            ("32016R0679", "32016R0679"),
            ("estleg:CELEX_32019L0790", "32019L0790"),
            ("https://eur-lex.europa.eu/eli/dir/2016/0679", None),
            ("estleg:32016r0679", "32016R0679"),
        ],
    )
    def test_extracts_celex(self, uri: str, expected: str | None):
        assert extract_celex(uri) == expected

    def test_returns_none_for_non_eu_uri(self):
        assert extract_celex("estleg:KarS_p121") is None
        assert extract_celex("") is None
        assert extract_celex(None) is None  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Action map
# ---------------------------------------------------------------------------


class TestActionMap:
    def test_provision_maps_to_normi_mojuahel(self):
        actions = build_actions_for_uri("estleg:KarS_p121", label="KarS § 121")
        assert len(actions) == 1
        assert actions[0].label == "Käivita Normi mõjuahel"
        # Capability URL + URL-encoded sisend.
        assert actions[0].href.startswith("/analyysikeskus/normi-mojuahel?sisend=")
        # "§" is URL-encoded to %C2%A7.
        assert "KarS" in actions[0].href
        assert actions[0].method == "get"

    def test_provision_without_label_falls_back_to_local_name(self):
        actions = build_actions_for_uri("estleg:AvTS_p35")
        assert len(actions) == 1
        assert "AvTS_p35" in actions[0].href

    def test_eu_act_with_celex_maps_to_el_ulevott(self):
        actions = build_actions_for_uri("estleg:32016R0679", label="GDPR")
        assert len(actions) == 1
        assert actions[0].label == "Vaata EL ülevõttu"
        assert actions[0].href == "/analyysikeskus/el-ulevott?sisend=32016R0679"

    def test_eu_act_without_celex_yields_no_action(self):
        # EULegislation class hint but no CELEX → builder returns None →
        # no action link.
        actions = build_actions_for_uri("estleg:EU_Dir_1", label="EU Directive 1")
        assert actions == []

    def test_court_decision_maps_to_chat_seed_post(self):
        actions = build_actions_for_uri("estleg:CourtDecision_1", label="RKKK 3-1-1-63-15")
        assert len(actions) == 1
        link = actions[0]
        assert link.label == "Küsi seotud sätete kohta"
        assert link.href == "/chat/seed"
        assert link.method == "post"
        # The seed text mentions the decision label and asks about
        # interpreted provisions.
        seed_payload = dict(link.form_fields)
        assert "seed_text" in seed_payload
        assert "RKKK 3-1-1-63-15" in seed_payload["seed_text"]
        assert "tõlgendab" in seed_payload["seed_text"]

    def test_unknown_entity_yields_no_action(self):
        assert build_actions_for_uri("estleg:Cluster_1", label="Cluster") == []
        assert build_actions_for_uri("") == []

    def test_map_keys_are_the_three_expected_entity_types(self):
        # The plan calls out exactly three entity types — pin the map
        # so future additions are deliberate, not accidental.
        assert set(ACTIONS_BY_ENTITY_TYPE.keys()) == {
            "provision",
            "eu_act",
            "court_decision",
        }


# ---------------------------------------------------------------------------
# collect_actions
# ---------------------------------------------------------------------------


class TestCollectActions:
    def test_empty_context_returns_empty_list(self):
        assert collect_actions(None) == []
        assert collect_actions([]) == []

    def test_collects_one_action_per_unique_entity(self):
        rag_context = [
            {"source_uri": "estleg:KarS_p121", "title": "KarS § 121"},
            {"source_uri": "estleg:AvTS_p35", "title": "AvTS § 35"},
            {"source_uri": "estleg:32016R0679", "title": "GDPR"},
        ]
        actions = collect_actions(rag_context)
        labels = [a.label for a in actions]
        assert "Käivita Normi mõjuahel" in labels
        assert "Vaata EL ülevõttu" in labels
        # Two provision URIs → two distinct Normi mõjuahel links
        # (different sisend payloads).
        assert sum(1 for la in labels if la == "Käivita Normi mõjuahel") == 2

    def test_dedupes_identical_actions(self):
        # Same provision URI cited three times → one action link, not three.
        rag_context = [
            {"source_uri": "estleg:KarS_p121", "title": "KarS § 121"},
            {"source_uri": "estleg:KarS_p121", "title": "KarS § 121"},
            {"source_uri": "estleg:KarS_p121", "title": "KarS § 121"},
        ]
        actions = collect_actions(rag_context)
        assert len(actions) == 1

    def test_ignores_malformed_chunks(self):
        rag_context = [
            "not a dict",  # type: ignore[list-item]
            {"source_uri": ""},
            {"source_uri": None},
            {"source_uri": "estleg:KarS_p121", "title": "KarS § 121"},
        ]
        actions = collect_actions(rag_context)  # type: ignore[arg-type]
        assert len(actions) == 1
        assert actions[0].label == "Käivita Normi mõjuahel"


# ---------------------------------------------------------------------------
# Rendered HTML
# ---------------------------------------------------------------------------


def _render(node) -> str:
    """Render an FT node to its HTML string."""
    from fastcore.xml import to_xml

    return to_xml(node)


class TestChatActionsBlock:
    def test_empty_context_renders_nothing(self):
        # Empty string short-circuit so the bubble doesn't grow an
        # empty disclosure.
        assert chat_actions_block(None) == ""
        assert chat_actions_block([]) == ""

    def test_renders_details_disclosure_with_estonian_summary(self):
        rag = [{"source_uri": "estleg:KarS_p121", "title": "KarS § 121"}]
        html = _render(chat_actions_block(rag))
        # Collapsible disclosure with the C1 class.
        assert "<details " in html
        assert 'class="chat-actions"' in html
        # Open by default — the whole point of C1 is to surface the link.
        assert " open" in html
        assert "<summary>Tegevused (1)</summary>" in html
        # One link rendered with an arrow prefix and the Estonian label.
        assert "→ Käivita Normi mõjuahel" in html
        assert "/analyysikeskus/normi-mojuahel?sisend=" in html

    def test_renders_eu_action_with_celex(self):
        rag = [{"source_uri": "estleg:32016R0679", "title": "GDPR"}]
        html = _render(chat_actions_block(rag))
        assert "→ Vaata EL ülevõttu" in html
        assert "/analyysikeskus/el-ulevott?sisend=32016R0679" in html

    def test_renders_court_seed_as_post_form(self):
        rag = [{"source_uri": "estleg:CourtDecision_1", "title": "RKKK 3-1-1-63-15"}]
        html = _render(chat_actions_block(rag))
        # POST form with /chat/seed action and seed_text hidden input.
        assert 'action="/chat/seed"' in html
        assert 'method="post"' in html
        assert 'name="seed_text"' in html
        assert "→ Küsi seotud sätete kohta" in html
        # Hidden input value mentions the decision label.
        assert "RKKK 3-1-1-63-15" in html

    def test_mixed_context_renders_all_action_kinds(self):
        rag = [
            {"source_uri": "estleg:KarS_p121", "title": "KarS § 121"},
            {"source_uri": "estleg:32016R0679", "title": "GDPR"},
            {"source_uri": "estleg:CourtDecision_1", "title": "RKKK 3-1-1-63-15"},
        ]
        html = _render(chat_actions_block(rag))
        # All three action labels present.
        assert "Käivita Normi mõjuahel" in html
        assert "Vaata EL ülevõttu" in html
        assert "Küsi seotud sätete kohta" in html
        # Summary count matches.
        assert "Tegevused (3)" in html

    def test_unknown_entities_drop_silently(self):
        rag = [
            {"source_uri": "estleg:Cluster_1", "title": "Cluster"},
            {"source_uri": "estleg:Concept_42", "title": "Concept"},
        ]
        # No actions for unknown types → empty string.
        assert chat_actions_block(rag) == ""


class TestChatActionLinkDataclass:
    def test_is_frozen(self):
        link = ChatActionLink(label="x", href="/y", title="z")
        with pytest.raises(Exception):
            link.label = "changed"  # type: ignore[misc]
