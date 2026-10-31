"""operations 面路由：/tasks、/approvals、/life、/events（SSE）。

从 api/server.py 拆分（issue #30），逻辑逐行等价。审批决策只授权后续
派发，**不发送任何消息**；兴趣反馈只做展示与调权，不触发探索。
"""
from __future__ import annotations

import time

from whale_core.api.server import SSE_REPLAY_LIMIT, _error, _json_bytes

_LIFE_DIARY_DAYS = 7
_LIFE_INTEREST_MAX = 20
_LIFE_TOPIC_MAX = 100


def tasks(handler, method: str, parts: list[str]) -> None:
    ctx = handler.ctx
    store = ctx.store
    if len(parts) == 1 and method == "GET":
        index = store.get("tasks:recent") or []
        task_list = []
        for task_id in reversed(index[-50:]):
            record = store.get(f"task:{task_id}")
            if isinstance(record, dict):
                task_list.append(record)
        handler._send(200, _json_bytes({"result": {"tasks": task_list}}))
        return
    if len(parts) == 2 and method == "GET":
        record = store.get(f"task:{parts[1]}")
        if not isinstance(record, dict):
            handler._send(404, _error("not_found", "no such task"))
            return
        handler._send(200, _json_bytes({"result": record}))
        return
    if len(parts) == 3 and method == 'POST' and parts[2] in ('cancel', 'decision', 'resume'):
        runtime = ctx.orchestrator
        if runtime is None:
            handler._send(503, _error('unavailable', 'runtime not wired'))
            return
        try:
            if parts[2] == 'cancel':
                result = runtime.cancel_task(parts[1])
            elif parts[2] == 'decision':
                result = runtime.decide_task(parts[1], str((handler._body_json() or {}).get('decision', '')))
            else:
                turn = runtime.resume_task(parts[1])
                result = {'reply': turn.reply, 'status': turn.status, 'receipts': list(turn.receipts), 'task_id': turn.task_id}
        except KeyError:
            handler._send(404, _error('not_found', 'no such task'))
            return
        except ValueError:
            handler._send(409, _error('conflict', 'task state does not allow this operation'))
            return
        ctx.log_event('task:' + parts[2], parts[1])
        handler._send(200, _json_bytes({'result': result}))
        return
    handler._send(404, _error("not_found", "unknown endpoint"))


def approvals(handler, method: str, parts: list[str]) -> None:
    with handler.ctx.lock:
        _approvals_locked(handler, method, parts)


def _approvals_locked(handler, method: str, parts: list[str]) -> None:
    """Decisions authorize later dispatch; they do NOT send a message."""
    ctx = handler.ctx
    store = ctx.store
    if len(parts) == 1 and method == "GET":
        pending = []
        if ctx.orchestrator is not None:
            for task in ctx.orchestrator.pending_tasks():
                pending.append({'kind': 'task', 'task_id': task['task_id'],
                                'draft_id': task['task_id'], 'text': task.get('goal', ''),
                                'status': task.get('status', ''), 'execution_status': 'not_dispatched'})
        for key in store.keys():
            if not key.startswith("life:draft:"):
                continue
            draft_id = key.split(":", 2)[2]
            draft = store.get(key)
            ledger = store.get("life:ledger:" + draft_id) or {}
            decision = store.get("life:decision:" + draft_id) or {}
            if not isinstance(draft, dict):
                continue
            status = ledger.get("status", "draft")
            if ledger.get("attempts", 0) > 0 or status in ("accepted", "unknown"):
                continue
            pending.append({
                "kind": "draft",
                "draft_id": draft.get("draft_id", draft_id),
                "text": str(draft.get("text", ""))[:200],
                "channel": draft.get("channel", ""),
                "status": status,
                "decision": decision.get("decision", ""),
                "execution_status": "not_dispatched",
            })
        handler._send(200, _json_bytes({"result": {"approvals": pending}}))
        return
    if len(parts) == 3 and parts[2] == "decision" and method == "POST":
        body = handler._body_json()
        decision = str((body or {}).get("decision") or "")
        if decision not in ("approve", "reject"):
            handler._send(400, _error("bad_request", "decision must be approve|reject"))
            return
        draft_id = parts[1]
        if not draft_id or not isinstance(store.get(f"life:draft:{draft_id}"), dict):
            handler._send(404, _error("not_found", "no such draft"))
            return
        ledger = store.get(f"life:ledger:{draft_id}") or {}
        if ledger.get("attempts", 0) > 0:
            handler._send(409, _error("conflict", "draft already attempted"))
            return
        key = f"life:decision:{draft_id}"
        existing = store.get(key) or {}
        if existing.get("decision"):
            if existing.get("decision") != decision:
                handler._send(409, _error("conflict", "draft decision already recorded"))
                return
            handler._send(200, _json_bytes({"result": {
                "draft_id": draft_id, "decision": decision,
                "execution_status": "not_dispatched",
            }}))
            return
        store.put(key, {"decision": decision, "at": time.time()})
        ctx.log_event(f"approval:{decision}", draft_id)
        handler._send(200, _json_bytes({"result": {
            "draft_id": draft_id, "decision": decision,
            "execution_status": "not_dispatched",
        }}))
        return
    handler._send(404, _error("not_found", "unknown endpoint"))


def life(handler, method: str, parts: list[str]) -> None:
    """主动生活的只读面貌与兴趣反馈。

    兴趣台账与探索日记由 life.exploration（InterestTracker/ExplorationManager）
    持久化；本路由只做展示与调权，**不触发探索、不发送任何消息**——探索走
    /workflows/explore，草稿审批走 /approvals。
    """
    from dataclasses import asdict

    from whale_core.life.exploration import (
        DIARY_KEY_PREFIX,
        ExplorationManager,
        InterestTracker,
    )

    ctx = handler.ctx
    store = ctx.store
    if parts == ["life"] and method == "GET":
        days = sorted(
            k for k in store.keys() if k.startswith(DIARY_KEY_PREFIX)
        )[-_LIFE_DIARY_DAYS:]
        diary = []
        for key in days:
            for entry in store.get(key) or []:
                if isinstance(entry, dict):
                    diary.append({
                        "day": str(entry.get("day", "")),
                        "note": str(entry.get("note", "")),
                        "sources": [str(u) for u in entry.get("sources", ())],
                    })
        handler._send(200, _json_bytes({"result": {
            "interests": [asdict(i) for i in InterestTracker(store).topics()],
            "diary": diary,
        }}))
        return
    if parts == ["life", "interests"] and method == "POST":
        body = handler._body_json()
        topic = (body or {}).get("topic")
        if not isinstance(topic, str) or not topic.strip() \
                or len(topic) > _LIFE_TOPIC_MAX:
            handler._send(422, _error("bad_request", "topic must be 1..100 characters"))
            return
        with ctx.lock:
            tracker = InterestTracker(store)
            if (body or {}).get("remove") is True:
                result = {"removed": tracker.remove(topic)}
            else:
                known = tracker.as_interest_list()
                if len(known) >= _LIFE_INTEREST_MAX \
                        and topic.strip().lower() not in {t.lower() for t in known}:
                    handler._send(422, _error("bad_request", "at most 20 interests"))
                    return
                interest = tracker.add(topic)
                result = {"topic": interest.topic, "weight": interest.weight}
        ctx.log_event("life:interest", topic[:80])
        handler._send(200, _json_bytes({"result": result}))
        return
    if parts == ["life", "feedback"] and method == "POST":
        body = handler._body_json()
        topic, signal = (body or {}).get("topic"), (body or {}).get("signal")
        if not isinstance(topic, str) or not isinstance(signal, str):
            handler._send(422, _error("bad_request", "topic and signal required"))
            return
        from whale_core.life.discovery import DiscoveryPlanner
        manager = ExplorationManager(
            store, planner=DiscoveryPlanner(allowed_hosts=frozenset()))
        try:
            with ctx.lock:
                manager.feedback(topic, signal)
        except ValueError:
            handler._send(422, _error("bad_request", "unsupported feedback signal"))
            return
        ctx.log_event("life:feedback", f"{signal}:{topic[:80]}")
        handler._send(200, _json_bytes({"result": {"saved": True}}))
        return
    handler._send(404, _error("not_found", "unknown endpoint"))


def events_sse(handler) -> None:
    """Keep an authenticated SSE stream open, replaying buffered events."""
    ctx = handler.ctx
    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Connection", "keep-alive")
    handler.end_headers()
    # Snapshot by index; events are capped at 200, so a slow client may lose
    # old entries. No promise of durable event delivery.
    last = 0
    last_heartbeat = 0.0
    try:
        while not ctx.stopping.is_set():
            with ctx.lock:
                snapshot = list(ctx.events)
            if last == 0:
                batch = snapshot[-SSE_REPLAY_LIMIT:]
            else:
                batch = [event for event in snapshot if event.get("seq", 0) > last]
            for event in batch:
                handler.wfile.write(b"data: " + _json_bytes(event) + b"\n\n")
                last = max(last, int(event.get("seq", 0)))
            now = time.monotonic()
            if now - last_heartbeat >= 15.0 or (last_heartbeat == 0 and not batch):
                handler.wfile.write(b": ping\n\n")
                last_heartbeat = now
            if batch or last_heartbeat == now:
                handler.wfile.flush()
            time.sleep(0.5)
    except (BrokenPipeError, ConnectionResetError, OSError):
        return
