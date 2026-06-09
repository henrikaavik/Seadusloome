"""Guard tests for the AI Advisory Chat system prompt.

Regression cover for the citation-URI hallucination bug: the prompt used
to instruct the model to "cite specific acts via their estleg: URIs",
which made it fabricate non-existent URIs such as
``https://data.riik.ee/ontology/estleg#HKTS_Par_13``. The prompt must now
tell the model to cite by law name + section and never invent URIs.
"""

from __future__ import annotations

from app.chat.system_prompt import build_system_prompt


class TestCitationRules:
    def test_does_not_order_url_citation(self):
        prompt = build_system_prompt()
        # The old blanket directive must be gone.
        assert "URI-de kaudu" not in prompt

    def test_forbids_inventing_uris(self):
        prompt = build_system_prompt()
        # An explicit "never construct/guess estleg: URIs" rule must exist.
        assert "KUNAGI" in prompt
        assert "estleg:" in prompt
        assert "konstrueeri" in prompt

    def test_prefers_name_and_paragraph_citation(self):
        prompt = build_system_prompt()
        # The model is told to cite human-readably (name + section).
        assert "§" in prompt
        assert "CELEX" in prompt

    def test_still_responds_in_estonian(self):
        prompt = build_system_prompt()
        assert "Vasta alati eesti keeles." in prompt

    def test_draft_context_still_appended(self):
        prompt = build_system_prompt(draft_context_id="abc123", impact_summary="Mingi kokkuvõte.")
        assert "abc123" in prompt
        assert "Mingi kokkuvõte." in prompt
