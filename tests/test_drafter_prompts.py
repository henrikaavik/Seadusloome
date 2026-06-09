"""Tests for drafter prompt templates.

Validates that:
    - Each prompt template has the expected placeholders
    - VTK section prompts cover all VTK structure sections
    - Prompt formatting does not crash with typical inputs
"""

from __future__ import annotations

from app.drafter.prompts import (
    CLARIFY_PROMPT,
    DRAFT_PROMPT,
    STRUCTURE_PROMPT,
    VTK_SECTION_PROMPTS,
    VTK_STRUCTURE,
)

# Every prompt template that can instruct the model to emit citations.
_ALL_CITATION_PROMPTS = {
    "CLARIFY_PROMPT": CLARIFY_PROMPT,
    "STRUCTURE_PROMPT": STRUCTURE_PROMPT,
    "DRAFT_PROMPT": DRAFT_PROMPT,
    **{f"VTK[{title}]": prompt for title, prompt in VTK_SECTION_PROMPTS.items()},
}


class TestClarifyPrompt:
    def test_has_intent_placeholder(self):
        assert "{intent}" in CLARIFY_PROMPT

    def test_has_laws_placeholder(self):
        assert "{laws}" in CLARIFY_PROMPT

    def test_format_succeeds(self):
        result = CLARIFY_PROMPT.format(
            intent="Test kavatsus",
            laws="- Law 1\n- Law 2",
        )
        assert "Test kavatsus" in result
        assert "Law 1" in result


class TestStructurePrompt:
    def test_has_intent_placeholder(self):
        assert "{intent}" in STRUCTURE_PROMPT

    def test_has_clarifications_placeholder(self):
        assert "{clarifications}" in STRUCTURE_PROMPT

    def test_has_similar_laws_placeholder(self):
        assert "{similar_laws}" in STRUCTURE_PROMPT

    def test_format_succeeds(self):
        result = STRUCTURE_PROMPT.format(
            intent="Test intent",
            clarifications="Q1: question\nA1: answer",
            similar_laws="- Law A",
        )
        assert "Test intent" in result


class TestDraftPrompt:
    def test_has_required_placeholders(self):
        assert "{chapter_title}" in DRAFT_PROMPT
        assert "{chapter_number}" in DRAFT_PROMPT
        assert "{section_title}" in DRAFT_PROMPT
        assert "{paragraph}" in DRAFT_PROMPT
        assert "{intent}" in DRAFT_PROMPT
        assert "{relevant_research}" in DRAFT_PROMPT

    def test_format_succeeds(self):
        result = DRAFT_PROMPT.format(
            chapter_title="Uldsatted",
            chapter_number="1. peatukk",
            section_title="Reguleerimisala",
            paragraph="par 1",
            intent="Test intent",
            relevant_research="- Provision 1",
        )
        assert "Uldsatted" in result
        assert "par 1" in result


class TestVtkSectionPrompts:
    def test_covers_all_vtk_sections(self):
        """Every section in VTK_STRUCTURE has a matching prompt."""
        for chapter in VTK_STRUCTURE["chapters"]:
            for section in chapter["sections"]:
                title = section["title"]
                assert title in VTK_SECTION_PROMPTS, (
                    f"VTK section '{title}' has no prompt template"
                )

    def test_vtk_structure_has_5_chapters(self):
        assert len(VTK_STRUCTURE["chapters"]) == 5

    def test_each_prompt_has_intent_placeholder(self):
        for title, prompt in VTK_SECTION_PROMPTS.items():
            assert "{intent}" in prompt, f"VTK prompt for '{title}' missing {{intent}} placeholder"

    def test_each_prompt_has_relevant_research_placeholder(self):
        for title, prompt in VTK_SECTION_PROMPTS.items():
            assert "{relevant_research}" in prompt, (
                f"VTK prompt for '{title}' missing {{relevant_research}} placeholder"
            )

    def test_each_prompt_formats_without_error(self):
        for title, prompt in VTK_SECTION_PROMPTS.items():
            # All prompts should accept these kwargs (some may not use all)
            try:
                prompt.format(
                    intent="Test intent",
                    clarifications="Q: test\nA: test",
                    relevant_research="- finding 1",
                )
            except KeyError as e:
                # Some prompts might not have {clarifications}, that's OK
                if "clarifications" not in str(e):
                    raise


class TestCitationGuidanceIsHumanReadable:
    """Guard against regressing to fabricated estleg: pseudo-URI citations (#842).

    The drafter must instruct the model to cite provisions human-readably
    (law name + section / CELEX / case number), never to construct estleg:
    identifiers. Downstream (``app/drafter/citations.py``) resolves these
    human-readable strings against the ontology.
    """

    def test_no_estleg_pseudo_uri_in_any_prompt(self):
        """No prompt may demonstrate or instruct the bracketed estleg: form."""
        for name, prompt in _ALL_CITATION_PROMPTS.items():
            assert "[estleg:" not in prompt, (
                f"{name} still contains a fabricated '[estleg:...]' citation example"
            )

    def test_no_par_slash_path_in_any_prompt(self):
        """The old ``/par/N`` pseudo-path must not appear anywhere."""
        for name, prompt in _ALL_CITATION_PROMPTS.items():
            assert "/par/" not in prompt, f"{name} still contains a '/par/' pseudo-URI path"

    def test_draft_prompt_uses_human_readable_citations(self):
        """DRAFT_PROMPT cites by law name + section symbol, not a pseudo-URI."""
        assert "[estleg:" not in DRAFT_PROMPT
        assert "/par/" not in DRAFT_PROMPT
        # Human-readable guidance: section symbol + a named example law.
        assert "§" in DRAFT_PROMPT
        assert "Halduskoostoo seadus § 13" in DRAFT_PROMPT
        # CELEX / case-number guidance is present (the literal phrase may be
        # line-wrapped, so assert on stable single tokens).
        assert "CELEX" in DRAFT_PROMPT
        assert "court decisions by case" in DRAFT_PROMPT

    def test_draft_prompt_forbids_inventing_identifiers(self):
        """The 'never invent identifiers' instruction must be present."""
        assert "NEVER construct, guess, or invent estleg: identifiers" in DRAFT_PROMPT

    def test_draft_prompt_example_citations_are_human_readable(self):
        """The JSON ``citations`` example shows resolvable human-readable strings."""
        # The exact example values written into the prompt.
        assert '"citations": ["Halduskoostoo seadus § 13", "32016R0679"]' in DRAFT_PROMPT
        # Sanity: neither legacy scheme prefix appears in the example line.
        assert "estleg:ActName" not in DRAFT_PROMPT
        assert "eu:DirectiveNumber" not in DRAFT_PROMPT

    def test_every_vtk_prompt_carries_citation_guardrail(self):
        """Each VTK section prompt must carry the same human-readable citation
        guardrail as DRAFT_PROMPT (#843): VTK generation uses these prompts, so
        they must instruct the model to cite human-readably and never invent
        ``estleg:`` identifiers.
        """
        for title, prompt in VTK_SECTION_PROMPTS.items():
            # The 'never invent identifiers' instruction is present.
            assert "NEVER construct, guess, or invent estleg:" in prompt, (
                f"VTK prompt for '{title}' is missing the citation guardrail"
            )
            # Human-readable section symbol is shown.
            assert "§" in prompt, f"VTK prompt for '{title}' is missing the '§' symbol"
            # Neither fabricated pseudo-URI form may appear.
            assert "[estleg:" not in prompt, (
                f"VTK prompt for '{title}' contains a fabricated '[estleg:...]' citation"
            )
            assert "/par/" not in prompt, (
                f"VTK prompt for '{title}' contains a '/par/' pseudo-URI path"
            )
