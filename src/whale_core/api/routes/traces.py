"""traces 面路由（issue #39）：/traces 诊断视图 + /activity 用户可读视图。

视图纪律：
- 诊断视图（/traces）：结构化字段（阶段/耗时/reason_code/receipt），
  供排查；span detail 写入前已脱敏，本层不再加码。
- 用户视图（/activity）：自然语言解释（发生了什么/为什么/结果如何），
  不暴露内部字段名与 ID 缩写以外的技术细节。
- 两类视图都不回显消息正文/原始 Prompt/密钥（账本层已不落正文）。
"""

from __future__ import annotations

import json

from whale_core.life.trace import TraceLedger, _trace_dict, explain


def _ledger(handler) -> TraceLedger:
    """按 store 路径惰性构建账本（与记忆库同目录的 trace.sqlite3）。"""
    ctx = handler.ctx
    led = getattr(ctx, "trace_ledger", None)
    if led is None:
        store_path = getattr(getattr(ctx, "store", None), "path", "") or ""
        if store_path and store_path != ":memory:":
            path = store_path.rsplit("/", 1)[0] + "/trace.sqlite3" \
                if "/" in store_path else "trace.sqlite3"
        else:
            path = ":memory:"
        led = TraceLedger(path)
        ctx.trace_ledger = led
    return led


def _send_json(handler, status: int, payload: dict) -> None:
    handler._send(status, json.dumps(payload).encode("utf-8"))


def _err(status: int, code: str, message: str) -> bytes:
    return json.dumps({"error": {"code": code, "message": message}}).encode()


def traces(handler, method: str, parts: list[str], query: dict) -> None:
    led = _ledger(handler)
    if len(parts) == 2 and parts[1] == "prune" and method == "POST":
        removed = led.prune()
        handler._send(200, json.dumps(
            {"result": {"pruned": removed}}).encode())
        return
    if len(parts) == 1 and method == "GET":
        def _f(name):
            return (query.get(name) or [""])[0].strip()
        try:
            limit = int(_f("limit") or 50)
            offset = int(_f("offset") or 0)
            since = float(_f("since") or 0)
            until = float(_f("until") or 0)
        except ValueError:
            handler._send(400, _err(400, "invalid_request",
                                    "limit/offset/since/until 必须是数字"))
            return
        rows, total = led.query(
            kind=_f("kind"), conversation_id=_f("conversation_id"),
            principal_id=_f("principal_id"), status=_f("status"),
            since=since, until=until, limit=limit, offset=offset)
        handler._send(200, json.dumps({"result": {
            "total": total,
            "offset": max(0, int(_f("offset") or 0)),
            "limit": limit,
            "traces": [
                {k: v for k, v in _trace_dict(t).items() if k != "spans"}
                for t in rows
            ],
        }}).encode())
        return
    if len(parts) == 2 and method == "GET":
        trace = led.get(parts[1])
        if trace is None:
            handler._send(404, _err(404, "not_found", "no such trace"))
            return
        handler._send(200, json.dumps(
            {"result": {"trace": _trace_dict(trace)}},
            ensure_ascii=False).encode())
        return
    handler._send(405, _err(405, "method_not_allowed", "use GET/POST"))


def activity(handler, method: str, parts: list[str], query: dict) -> None:
    """GET /activity：用户可读活动记录（自然语言，按时间倒序）。"""
    if method != "GET":
        handler._send(405, _err(405, "method_not_allowed", "use GET"))
        return
    led = _ledger(handler)
    try:
        limit = int((query.get("limit") or ["30"])[0] or 30)
        offset = int((query.get("offset") or ["0"])[0] or 0)
    except ValueError:
        handler._send(400, _err(400, "invalid_request",
                                "limit/offset 必须是数字"))
        return
    rows, total = led.query(limit=limit, offset=offset)
    items = []
    for t in rows:
        full = led.get(t.trace_id)
        if full is None:
            continue
        items.append({
            "trace_id": full.trace_id,
            "at": full.started_at,
            "status": full.status,
            "lines": explain(full),
        })
    handler._send(200, json.dumps({"result": {
        "total": total, "items": items}}, ensure_ascii=False).encode())
