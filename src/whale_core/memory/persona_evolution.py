"""Versioned, evidence-backed persona evolution.

Only expression fields may evolve. Proposals are inert until an owner approves
them; accepted overlays are versioned and can be rolled back. This layer never
changes tool permissions, device scopes, identity, or memory ACLs.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Mapping
from uuid import uuid4

from whale_core.memory.store import MemoryStore

PROPOSAL_PREFIX = "persona:evolution:proposal:"
ACTIVE_PREFIX = "persona:evolution:active:"
HISTORY_PREFIX = "persona:evolution:history:"
MAX_HISTORY = 100
MAX_EVIDENCE = 20

# These fields affect only the persona's expression. In particular, external
# prompts, tools, scopes, authorization, identity, and memory visibility cannot
# be changed through an evolution proposal.
ALLOWED_FIELDS = frozenset({
    "summary", "temperament", "tone", "relationship_style",
    "address_user_as", "style_rules", "extra_taboos",
})
_STRING_LIMITS = {
    "summary": 500, "temperament": 300, "tone": 300,
    "relationship_style": 500, "address_user_as": 80,
}


def _now(value: datetime | None = None) -> datetime:
    at = value or datetime.now(timezone.utc)
    return at.replace(tzinfo=timezone.utc) if at.tzinfo is None else at


def _validate_patch(patch: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(patch, Mapping) or not patch:
        raise ValueError("patch must be a non-empty object")
    unknown = set(patch) - ALLOWED_FIELDS
    if unknown:
        raise ValueError(f"unsupported persona fields: {sorted(unknown)}")
    clean: dict[str, Any] = {}
    for key, value in patch.items():
        if key in _STRING_LIMITS:
            if not isinstance(value, str):
                raise ValueError(f"{key} must be a string")
            clean[key] = value.strip()[:_STRING_LIMITS[key]]
        else:
            if not isinstance(value, (list, tuple)) or len(value) > 12:
                raise ValueError(f"{key} must be a list of at most 12 strings")
            if any(not isinstance(item, str) for item in value):
                raise ValueError(f"{key} items must be strings")
            clean[key] = [item.strip()[:300] for item in value if item.strip()]
    return clean


class PersonaEvolutionStore:
    """Owner-reviewed persona overlays with optimistic version checks."""

    def __init__(self, store: MemoryStore):
        self.store = store

    @staticmethod
    def _active_key(persona_id: str) -> str:
        return ACTIVE_PREFIX + persona_id

    @staticmethod
    def _history_key(persona_id: str) -> str:
        return HISTORY_PREFIX + persona_id

    @staticmethod
    def _proposal_key(proposal_id: str) -> str:
        return PROPOSAL_PREFIX + proposal_id

    def active_overlay(self, persona_id: str) -> tuple[dict[str, Any], int]:
        data = self.store.get(self._active_key(str(persona_id or "").strip())) or {}
        if not isinstance(data, dict):
            return {}, 0
        fields = data.get("fields") or {}
        try:
            clean = _validate_patch(fields) if fields else {}
        except (TypeError, ValueError):
            return {}, 0
        return clean, max(0, int(data.get("version") or 0))

    def propose(
        self, persona_id: str, patch: Mapping[str, Any], *,
        evidence_ids: list[str] | tuple[str, ...], rationale: str,
        proposed_by: str = "owner", proposal_id: str | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        pid = str(persona_id or "").strip()
        why = str(rationale or "").strip()
        if not pid or len(pid) > 120:
            raise ValueError("persona_id must be 1..120 characters")
        if not why:
            raise ValueError("rationale is required")
        clean = _validate_patch(patch)
        evidence = tuple(dict.fromkeys(
            str(item).strip()[:200] for item in evidence_ids if str(item).strip()))
        if not evidence or len(evidence) > MAX_EVIDENCE:
            raise ValueError(f"evidence_ids must contain 1..{MAX_EVIDENCE} entries")
        current = self.store.get(self._active_key(pid)) or {}
        current_version = max(0, int(current.get("version") or 0)) if isinstance(current, dict) else 0
        proposal_id = str(proposal_id or uuid4().hex)
        key = self._proposal_key(proposal_id)
        if self.store.get(key) is not None:
            raise ValueError("proposal_id already exists")
        at = _now(now).isoformat()
        proposal = {
            "proposal_id": proposal_id, "persona_id": pid, "patch": clean,
            "evidence_ids": list(evidence), "rationale": why[:1000],
            "proposed_by": str(proposed_by or "owner")[:120],
            "base_version": current_version, "state": "pending",
            "created_at": at, "updated_at": at,
        }
        self.store.put(key, proposal)
        return dict(proposal)

    def propose_from_experiences(
        self, persona_id: str, patch: Mapping[str, Any], *,
        experience_store, principal_id: str,
        experience_ids: list[str] | tuple[str, ...], rationale: str,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Create a proposal from selected trusted experiences, never candidates."""
        current = _now(now)
        available = {
            str(item.get("experience_id")): item
            for item in experience_store.list_for(principal_id)
        }
        evidence: list[str] = []
        for experience_id in dict.fromkeys(str(x) for x in experience_ids):
            item = available.get(experience_id)
            if item is None:
                raise KeyError(f"unknown experience: {experience_id}")
            source = str(item.get("source") or "")
            if source.startswith("candidate") or not (
                source == "receipt" or item.get("confirmed_by_user") is True
            ):
                raise PermissionError("untrusted candidate experience cannot drive evolution")
            ttl = float(item.get("ttl_s") or 0)
            if ttl > 0:
                recorded = datetime.fromisoformat(str(item.get("recorded_at")))
                if recorded.tzinfo is None:
                    recorded = recorded.replace(tzinfo=timezone.utc)
                if recorded + timedelta(seconds=ttl) <= current:
                    raise ValueError("expired experience cannot drive evolution")
            evidence.append(f"exp:{principal_id}:{experience_id}")
        return self.propose(
            persona_id, patch, evidence_ids=evidence, rationale=rationale,
            proposed_by=principal_id, now=current)

    def get_proposal(self, proposal_id: str) -> dict[str, Any] | None:
        data = self.store.get(self._proposal_key(str(proposal_id or "").strip()))
        return dict(data) if isinstance(data, dict) else None

    def list_proposals(self, persona_id: str | None = None, *,
                       state: str | None = None) -> tuple[dict[str, Any], ...]:
        prefix = PROPOSAL_PREFIX
        out = []
        for key in self.store.keys():
            if not key.startswith(prefix):
                continue
            item = self.store.get(key)
            if not isinstance(item, dict):
                continue
            if persona_id is not None and item.get("persona_id") != persona_id:
                continue
            if state is not None and item.get("state") != state:
                continue
            out.append(dict(item))
        return tuple(sorted(out, key=lambda item: str(item.get("created_at") or "")))

    def approve(self, proposal_id: str, *, now: datetime | None = None) -> dict[str, Any]:
        at = _now(now).isoformat()
        key = self._proposal_key(str(proposal_id or "").strip())

        def apply(tx):
            proposal = tx.get(key)
            if not isinstance(proposal, dict):
                raise KeyError(proposal_id)
            if proposal.get("state") != "pending":
                raise ValueError("proposal is no longer pending")
            pid = str(proposal["persona_id"])
            active_key, history_key = self._active_key(pid), self._history_key(pid)
            current = tx.get(active_key) or {"version": 0, "fields": {}}
            version = max(0, int(current.get("version") or 0))
            if version != int(proposal.get("base_version") or 0):
                raise ValueError("stale proposal: active persona changed")
            fields = dict(current.get("fields") or {})
            fields.update(_validate_patch(proposal["patch"]))
            history = list(tx.get(history_key) or [])
            if not history and version == 0:
                history.append({"version": 0, "fields": {}, "at": at,
                                "source": "initial"})
            new_version = version + 1
            active = {"version": new_version, "fields": fields,
                      "updated_at": at, "source_proposal_id": proposal["proposal_id"]}
            history.append({"version": new_version, "fields": fields,
                            "at": at, "source_proposal_id": proposal["proposal_id"]})
            tx.put(active_key, active)
            tx.put(history_key, history[-MAX_HISTORY:])
            proposal["state"] = "approved"
            proposal["applied_version"] = new_version
            proposal["updated_at"] = at
            tx.put(key, proposal)
            return dict(proposal)

        return self.store.transact(apply)

    def reject(self, proposal_id: str, *, reason: str = "",
               now: datetime | None = None) -> bool:
        key = self._proposal_key(str(proposal_id or "").strip())
        proposal = self.store.get(key)
        if not isinstance(proposal, dict) or proposal.get("state") != "pending":
            return False
        proposal["state"] = "rejected"
        proposal["rejection_reason"] = str(reason or "")[:500]
        proposal["updated_at"] = _now(now).isoformat()
        self.store.put(key, proposal)
        return True

    def history(self, persona_id: str) -> tuple[dict[str, Any], ...]:
        raw = self.store.get(self._history_key(str(persona_id or "").strip())) or []
        return tuple(dict(item) for item in raw if isinstance(item, dict))

    def rollback(self, persona_id: str, *, to_version: int | None = None,
                 now: datetime | None = None) -> dict[str, Any]:
        pid = str(persona_id or "").strip()
        if not pid:
            raise ValueError("persona_id required")
        at = _now(now).isoformat()
        active_key, history_key = self._active_key(pid), self._history_key(pid)

        def apply(tx):
            current = tx.get(active_key) or {"version": 0, "fields": {}}
            current_version = max(0, int(current.get("version") or 0))
            history = list(tx.get(history_key) or [])
            eligible = [item for item in history
                        if isinstance(item, dict)
                        and int(item.get("version", -1)) < current_version]
            if to_version is None:
                target = eligible[-1] if eligible else None
            else:
                target = next((item for item in history
                               if isinstance(item, dict)
                               and int(item.get("version", -1)) == int(to_version)), None)
                if target is not None and int(target.get("version", -1)) >= current_version:
                    target = None
            if target is None:
                raise ValueError("no earlier persona version to restore")
            fields = _validate_patch(target.get("fields") or {}) if target.get("fields") else {}
            new_version = current_version + 1
            active = {"version": new_version, "fields": fields,
                      "updated_at": at, "rollback_of": int(target["version"])}
            history.append({"version": new_version, "fields": fields,
                            "at": at, "rollback_of": int(target["version"])})
            tx.put(active_key, active)
            tx.put(history_key, history[-MAX_HISTORY:])
            return dict(active)

        return self.store.transact(apply)


__all__ = [
    "ALLOWED_FIELDS", "PersonaEvolutionStore", "PROPOSAL_PREFIX",
]
