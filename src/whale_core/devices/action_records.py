"""Atomic device action state transitions shared by live receipts and retention."""
from __future__ import annotations

TERMINAL_STATES = frozenset({"succeeded", "failed", "timeout", "cancelled", "unknown"})


class ActionRecords:
    def __init__(self, store):
        self.store = store

    def receive(self, device_id: str, payload: dict, now: int) -> dict | None:
        action_id, status = payload["action_id"], payload["status"]
        if status not in TERMINAL_STATES | {"received"}:
            raise ValueError("invalid receipt state")
        if type(now) is not int or now < 0:
            raise ValueError("now must be a nonnegative integer")
        def update(tx):
            idem = tx.get(f"device:action-id:{device_id}:{action_id}")
            if not isinstance(idem, str):
                return None
            key = f"device:action:{device_id}:{idem}"
            record = tx.get(key)
            if not isinstance(record, dict) or record.get("action_id") != action_id:
                return None
            history_key = f"device:receipts:{device_id}:{action_id}"
            history = tx.get(history_key)
            history = history if isinstance(history, list) else []
            tx.put(history_key, (history + [{"status": status, "ts": now}])[-32:])
            estimated = (record.get("state") == "unknown"
                         and record.get("outcome_source") == "retention_timeout")
            if record.get("state") not in TERMINAL_STATES or (estimated and status in TERMINAL_STATES):
                record.update(state=status, updated_at=now)
                record.pop("outcome_source", None)
                if status in TERMINAL_STATES:
                    record["receipt"] = {key: payload[key] for key in (
                        "status", "task_id", "result", "error_code", "provider_ref", "started_at", "completed_at")
                        if key in payload}
                tx.put(key, record)
            return record
        return self.store.transact(update)

    def sweep(self, now: int, retention_ms: int, grace_ms: int, max_deletes: int) -> dict:
        for name, value in (("now", now), ("retention_ms", retention_ms), ("grace_ms", grace_ms)):
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a nonnegative integer")
        if type(max_deletes) is not int or max_deletes < 0:
            raise ValueError("max_deletes 必须为非负整数")
        scanned = deleted = marked = 0
        for key in self.store.keys():
            if not key.startswith("device:action:"):
                continue
            scanned += 1
            def process(tx):
                record = tx.get(key)  # re-read under the transaction, never trust a stale scan snapshot
                if not isinstance(record, dict):
                    return None
                try:
                    created, expires = int(record.get("created_at") or 0), int(record.get("expires_at") or 0)
                    terminal_at = int(record.get("updated_at", created))
                except (ValueError, TypeError, OverflowError):
                    return None
                state = record.get("state")
                if state in TERMINAL_STATES:
                    if max(created, terminal_at) + retention_ms >= now or deleted >= max_deletes:
                        return None
                    tx.delete(key)
                    device, action = record.get("device_id"), record.get("action_id")
                    if device and action:
                        index = f"device:action-id:{device}:{action}"
                        if tx.get(index) == key.split(":", 3)[3]:
                            tx.delete(index)
                            tx.delete(f"device:receipts:{device}:{action}")
                    return "deleted"
                if now > expires + grace_ms:
                    record.update(state="unknown", outcome_source="retention_timeout", updated_at=now)
                    tx.put(key, record)
                    return "marked"
                return None
            result = self.store.transact(process)
            deleted += result == "deleted"
            marked += result == "marked"
        for key in self.store.keys():
            if deleted >= max_deletes:
                break
            if not key.startswith("device:receipts:"):
                continue
            parts = key.split(":", 3)
            if len(parts) != 4:
                continue
            def remove_orphan(tx):
                if tx.get(f"device:action-id:{parts[2]}:{parts[3]}") is None:
                    tx.delete(key)
                    return 1
                return 0
            deleted += self.store.transact(remove_orphan)
        return {"scanned": scanned, "deleted": deleted, "marked_unknown": marked}
