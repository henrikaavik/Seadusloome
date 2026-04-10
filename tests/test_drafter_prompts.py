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
