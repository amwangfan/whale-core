# Optional personas and prompt assembly

Whale Core no longer ships character cards, a preselected personality, third-party
card catalog entries, or a default character identity. New installations start with
no active card. User-created or imported cards in the memory store are preserved,
but their activation remains explicit. Tracked exported cards are no longer
shipped with the repository.

## Runtime ordering

1. **Host contract** (`core/prompt_composer.py`): neutral when unconfigured,
   accurate responses, persona consistency when selected, no authorization changes.
2. **Selected expression card**: name, summary, temperament, relationship,
   style rules and example dialogue are rendered through EffectivePersona;
   external text is labeled as context and subject to a character limit.
3. **Recall context**: up to eight relevant history memories, quoted as JSON
   and bounded separately. These are background evidence, not instructions.
4. **Conversation history and current user message**: original chat roles stay
   separate from system data. The current request is not inferred from memories.

This is a conservative subset of SillyTavern Prompt Manager and World Info:
prompt ordering, distinct character and memory scopes, explicit user choice.
Dynamic keyword lorebook activation, depth-specific injection and fully
compatible Character Card V2 post-history fields remain *future work*;
this change does not pretend to implement them.

## Migration

Existing `persona:card:<id>` records are **not deleted**. If an old custom card
is still selected in `persona:active_card`, it continues working. If you
want a neutral assistant, clear that key through a controlled data migration
or choose a new card. Old `persona:default` continuity snapshots alone
no longer activate a personality. A missing binding gracefully falls back
to the selected global card, or to neutral behavior.

`WHALE_PERSONA_CARD` can still explicitly select an existing user-installed card.
`BUILTIN_CARDS` and `AIGENGTU_CATALOG` remain empty deprecated exports
for source compatibility; no card content or catalog ships with core.

## Prompt security

Do not confuse a selected roleplay prompt with authorization. Tool requests,
ShellPolicy and owner identity are verified outside model text. Quoting or
delimiting untrusted character content reduces accidents but cannot guarantee
LLM resistance to malicious instructions. Keep auditing at the execution boundary.

References:
- https://docs.sillytavern.app/usage/prompts/prompt-manager/
- https://docs.sillytavern.app/usage/core-concepts/worldinfo/
- https://github.com/malfoyslastname/character-card-spec-v2/blob/main/spec_v2.md
