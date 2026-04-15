"""Tests for :mod:`app.chat.slash` — slash-command expansion."""

from __future__ import annotations

from app.chat.slash import COMMANDS, SlashCommand, expand, match_prefix


def test_draft_expands_with_argument() -> None:
    assert (
        expand("/draft uus andmekaitseseadus")
        == "Aita mul luua eelnõu järgmisel teemal: uus andmekaitseseadus"
    )


def test_unknown_command_returns_message_unchanged() -> None:
    assert expand("/unknown foo") == "/unknown foo"


def test_no_slash_returns_message_unchanged() -> None:
    assert expand("tere, kuidas läheb?") == "tere, kuidas läheb?"


def test_empty_message_returns_empty() -> None:
    assert expand("") == ""


def test_sources_returns_static_template() -> None:
    expected = "Millistele allikatele tugined eelmise vastuse puhul? Loetle need täpselt."
    assert expand("/sources") == expected
    # Trailing argument is ignored for static templates.
    assert expand("/sources palun") == expected


def test_empty_argument_still_expands() -> None:
    assert expand("/draft") == "Aita mul luua eelnõu järgmisel teemal: "
    assert expand("/draft ") == "Aita mul luua eelnõu järgmisel teemal: "


def test_command_name_is_case_insensitive_arg_preserves_case() -> None:
    assert (
        expand("/DRAFT Uus AndmekaitseSeadus")
        == "Aita mul luua eelnõu järgmisel teemal: Uus AndmekaitseSeadus"
    )
    assert (
        expand("/Find-Conflicts §5 rakendus")
        == "Leia vastuolud EL õigusega järgnevas: §5 rakendus"
    )


def test_leading_whitespace_tolerated() -> None:
    assert expand("   /explain §12") == "Selgita lihtsas keeles: §12"


def test_compare_and_find_conflicts_templates() -> None:
    assert expand("/compare KarS §199") == "Võrdle järgnevat kehtiva õigusega: KarS §199"
    assert (
        expand("/find-conflicts GDPR art 5") == "Leia vastuolud EL õigusega järgnevas: GDPR art 5"
    )


def test_match_prefix_draft() -> None:
    results = match_prefix("dr")
    names = [cmd.name for cmd in results]
    assert "draft" in names
    # Only commands starting with "dr" are returned.
    assert all(name.startswith("dr") for name in names)


def test_match_prefix_empty_returns_all() -> None:
    results = match_prefix("")
    assert results == COMMANDS
    assert len(results) == len(COMMANDS)


def test_match_prefix_is_case_insensitive() -> None:
    assert [cmd.name for cmd in match_prefix("DR")] == [cmd.name for cmd in match_prefix("dr")]


def test_match_prefix_no_matches() -> None:
    assert match_prefix("zzz-nope") == []


def test_commands_are_frozen_dataclasses() -> None:
    cmd = COMMANDS[0]
    assert isinstance(cmd, SlashCommand)
    try:
        cmd.name = "mutated"  # type: ignore[misc]
    except Exception:
        return
    raise AssertionError("SlashCommand should be frozen")
