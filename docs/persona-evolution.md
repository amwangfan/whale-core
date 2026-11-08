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


## Explicit maintenance

The owner-authenticated `POST /api/v1/life/maintenance` endpoint runs
`run_memory_maintenance` on demand. It reports deduplication and expiry
counts; it is not silently scheduled. This keeps destructive retention cleanup
under an explicit operator action until retention periods are reviewed.


## Studio management API

The following endpoints are protected by the normal owner Studio Bearer token:

- `GET /api/v1/studio/personas/{id}/evolution` lists active expression
  fields, pending/approved proposals and version history.
- `POST /api/v1/studio/personas/{id}/evolution/proposals` accepts
  `{"patch":{"tone":"..."},"rationale":"...","evidence_ids":["chat:..."]}`.
  Alternatively pass `experience_ids` and `principal_id` to validate
  selected experiences before proposing.
- `POST /api/v1/studio/personas/{id}/evolution/{proposal_id}/approve`
  approves a pending version proposal; `reject` denies it.
- `POST /api/v1/studio/personas/{id}/evolution/rollback` with
  `{"to_version":0}` restores a previous overlay while creating a new
  version entry. The original card is never rewritten.

The `{id}` segment may be `active` for the globally selected card.
These endpoints only change expression fields. No prompt can grant tool
permissions or change memory access scopes.
