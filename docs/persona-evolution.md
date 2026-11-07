# Persona evolution and memory lifecycle

Persona cards remain user-authored source material. Core stores evolution as a
separate, versioned overlay rather than rewriting an imported card.

## Proposal and approval

- Only expression fields are mutable: summary, temperament, tone,
  relationship style, user address, style rules and expression taboos.
- A proposal must carry evidence references and a rationale.
- Trusted execution receipts and explicit user feedback can be selected as
  evidence through `propose_from_experiences`; untrusted candidate experiences
  and expired experiences are rejected.
- Proposals do not affect runtime prompts until explicitly approved.
- Approval uses an optimistic base-version check. A stale proposal cannot
  overwrite a newer active persona.
- Every approved version is retained. Rollback creates a new version whose
  expression fields match an earlier version.
- Tool permissions, device scopes, identity, vault ACLs and memory visibility
  are not valid evolution fields.

The runtime's `build_effective_persona` applies the active overlay to the
selected card. The resulting snapshot includes `evolution_version` and a
content hash, so the same renderer used by chat and Studio reflects the
accepted evolution.

## Memory lifecycle

Curated episodic memories already use evidence, consent, importance, confidence
and half-life decay; stable preferences do not decay. Pending facts now receive
an explicit expiry and can be removed by `expire_pending_facts`. Confirmation
and correction preserve the source memory space, so promotion cannot silently
move a fact into a broader ACL domain.
