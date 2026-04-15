"""Slash-command expansion for the advisory chat.

Users can type shortcuts such as ``/draft <teema>`` or ``/sources`` in the
chat input. This module turns such shortcuts into canonical Estonian prompts
that the LLM can reason about, and exposes autocomplete helpers for the UI.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SlashCommand:
    """A single slash command shortcut.

    Attributes:
        name: Command token used after the leading slash (lower-case).
        label: Short Estonian label shown in the UI autocomplete list.
        description: One-line Estonian help text.
        template: Canonical prompt with an ``{arg}`` placeholder. Commands
            that take no argument should not include ``{arg}``.
    """

    name: str
    label: str
    description: str
    template: str


COMMANDS: list[SlashCommand] = [
    SlashCommand(
        name="draft",
        label="Loo eelnõu",
        description="Alusta uue eelnõu koostamist antud teemal.",
        template="Aita mul luua eelnõu järgmisel teemal: {arg}",
    ),
    SlashCommand(
        name="compare",
        label="Võrdle sättega",
        description="Võrdle teksti kehtiva õigusega.",
        template="Võrdle järgnevat kehtiva õigusega: {arg}",
    ),
    SlashCommand(
        name="find-conflicts",
        label="Leia vastuolud",
        description="Leia vastuolud Euroopa Liidu õigusega.",
        template="Leia vastuolud EL õigusega järgnevas: {arg}",
    ),
    SlashCommand(
        name="explain",
        label="Selgita",
        description="Selgita sätet või mõistet lihtsas keeles.",
        template="Selgita lihtsas keeles: {arg}",
    ),
    SlashCommand(
        name="sources",
        label="Näita allikaid",
        description="Loetle allikad, millele eelmine vastus tugines.",
        template=("Millistele allikatele tugined eelmise vastuse puhul? Loetle need täpselt."),
    ),
]


_COMMANDS_BY_NAME: dict[str, SlashCommand] = {cmd.name: cmd for cmd in COMMANDS}


def expand(message: str) -> str:
    """Expand a leading slash command into its canonical Estonian prompt.

    If ``message`` starts with a known command (after stripping leading
    whitespace), the command token is replaced with its template. The rest
    of the message (everything after the first whitespace following the
    command) is substituted into ``{arg}``. Unknown commands and plain
    messages are returned unchanged.

    Matching on the command name is case-insensitive, but the user-supplied
    argument keeps its original casing.
    """

    if not message:
        return message

    stripped = message.lstrip()
    if not stripped.startswith("/"):
        return message

    # Split "/name" and the remainder on the first whitespace run.
    head, sep, tail = stripped[1:].partition(" ")
    # ``partition`` only splits on a single space; normalise by stripping
    # leading whitespace from the argument so that e.g. "/draft   foo"
    # yields arg "foo" rather than "  foo".
    name = head.lower()
    if name not in _COMMANDS_BY_NAME:
        return message

    cmd = _COMMANDS_BY_NAME[name]
    arg = tail.lstrip() if sep else ""

    if "{arg}" not in cmd.template:
        return cmd.template
    return cmd.template.format(arg=arg)


def match_prefix(prefix: str) -> list[SlashCommand]:
    """Return commands whose name starts with ``prefix`` (case-insensitive).

    An empty prefix returns all commands in declaration order. Used by the
    client-side autocomplete dropdown.
    """

    needle = prefix.lower()
    if not needle:
        return list(COMMANDS)
    return [cmd for cmd in COMMANDS if cmd.name.startswith(needle)]
