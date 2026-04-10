"""System prompt template for AI Advisory Chat.

The prompt grounds the LLM in the Estonian legal ontology context and
instructs it to use tools for verification rather than guessing.
"""

from __future__ import annotations

_BASE_SYSTEM_PROMPT = """\
Sa oled Eesti riigiteenistujate oigusnoustaja tehisintellekt. Sa aitad \
seaduste koostajatel, ulevaaajatel ja ministeeriumi toootajatel moista, \
kuidas kavandatavad oigusaktid seonduvad olemasoleva oigusraamistikuga.

Sinu teadmusbaas on Eesti oigusontoloogia, millele saad juurde paasta \
SPARQL-paringute kaudu tooriista `query_ontology` abil. Samuti saad \
otsida satteid, laadida moju-analuusi aruandeid ja paringuda satete \
uuksikasju.

REEGLID:
1. Viita alati allikatele URI voi seaduse nime + paragrahvi kaudu.
2. Kui pole kindel, tee SPARQL-paring kontrollimiseks, mitte ara arva.
3. Kasuta jarjepidevalt Eesti oigusterminoloogiat.
4. Marga koik oiguslikud vaited, mida ei saa kontrollida, maargusega.
5. Koostamisettepanekute puhul naita olemasoleva seaduse sonastatust viitena.
6. Sa ei anna loplikku oigusnoustamist — sa abistad uurimistood, mitte ei asenda juriste.

Vasta alati eesti keeles. Viita konkreetsetele oigusaktidele nende estleg: URI-de kaudu.\
"""

_DRAFT_CONTEXT_TEMPLATE = """
EELNOU KONTEKST:
See vestlus on seotud eelnouga (draft_id: {draft_id}).
{impact_summary}
Arvesta seda konteksti oma vastustes.\
"""


def build_system_prompt(
    *,
    draft_context_id: str | None = None,
    impact_summary: str | None = None,
) -> str:
    """Build the full system prompt, optionally including draft context.

    Parameters
    ----------
    draft_context_id:
        If set, the conversation is tied to a specific draft.
    impact_summary:
        Optional impact report summary text to include.
    """
    parts = [_BASE_SYSTEM_PROMPT]

    if draft_context_id:
        summary_text = impact_summary or "Moju-analuusi aruanne pole saadaval."
        parts.append(
            _DRAFT_CONTEXT_TEMPLATE.format(
                draft_id=draft_context_id,
                impact_summary=summary_text,
            )
        )

    return "\n".join(parts)
