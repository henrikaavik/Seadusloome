"""VTK (vabariigi valitsuse korralduse eelanaluus) workflow variant.

The VTK is a standardised pre-analysis document required by Estonian
government procedure before legislation can proceed. It has a fixed
5-chapter structure (unlike full_law which generates a structure via
LLM in Step 4).

This module provides:
    - ``VTK_STRUCTURE`` — the fixed chapter/section outline
    - ``is_vtk_section`` — check if a section title has a VTK-specific prompt
    - ``get_vtk_prompt`` — retrieve the VTK-specific prompt for a section

The actual VTK_STRUCTURE and VTK_SECTION_PROMPTS are defined in
:mod:`app.drafter.prompts` to keep all prompt text in one place.
This module re-exports them and adds helper logic.
"""

from __future__ import annotations

from app.drafter.prompts import VTK_SECTION_PROMPTS, VTK_STRUCTURE

# Re-export for convenience
__all__ = ["VTK_SECTION_PROMPTS", "VTK_STRUCTURE", "get_vtk_prompt", "is_vtk_section"]


def is_vtk_section(section_title: str) -> bool:
    """Return True if *section_title* has a dedicated VTK prompt."""
    return section_title in VTK_SECTION_PROMPTS


def get_vtk_prompt(section_title: str) -> str | None:
    """Return the VTK-specific prompt for *section_title*, or None."""
    return VTK_SECTION_PROMPTS.get(section_title)
