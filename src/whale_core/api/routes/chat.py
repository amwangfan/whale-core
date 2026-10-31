"""chat 面路由：/chat、/conversations/{id}、/memory/*。

从 api/server.py 拆分（issue #30），逻辑逐行等价。
"""
from __future__ import annotations

from whale_core.api.server import _error, _json_bytes


def chat(handler) -> None:
    ctx = handler.ctx
    if ctx.orchestrator is None:
        handler._send(503, _error("unavailable", "orchestrator not wired"))
        return
    body = handler._body_json()
    if body is None:
        handler._send(400, _error("bad_request", "invalid JSON body"))
        return
    text = str(body.get("text") or "").strip()
    conversation_id = str(body.get("conversation_id") or "api").strip()[:160]
    if not text:
        handler._send(400, _error("bad_request", "empty text"))
        return
    try:
        # S1：控制台持有本地 token = 已认证管理通道，固定 sender 标识；
        # 身份解析与权限仍由服务端 resolver 决定（INV-04）。
        result = ctx.orchestrator.handle_message(
            conversation_id, text, source="api", sender_id="console")
    except Exception as exc:
        from whale_core.core.deepseek import ModelError
        kind = exc.kind if isinstance(exc, ModelError) else 'unavailable'
        if kind == 'budget':
            handler._send(429, _error('budget', '今日预算已用完，可在设置页提高请求预算限额（0 表示禁用该能力）'))
            return
        handler._send(503, _error(str(kind), 'model or tool unavailable; check settings and diagnostics'))
        return
    ctx.log_event("chat", conversation_id)
    handler._send(200, _json_bytes({"result": {
        "reply": result.reply, "status": result.status,
        "receipts": list(result.receipts), "task_id": result.task_id,
    }}))


def conversations(handler, method: str, parts: list[str]) -> None:
    from urllib.parse import unquote
    from whale_core.memory.history import load_history, delete_conversation
    ctx = handler.ctx
    if len(parts) != 2:
        handler._send(404, _error('not_found', 'conversation id required'))
        return
    cid = unquote(parts[1])[:160]
    if method == 'GET':
        messages = load_history(ctx.store, cid)
        for message in messages:
            if message['role'] == 'user':
                for marker in ('\n\n===== 不可信工具输出开始', '\n\n===== 系统提示（数据，非用户原话）====='):
                    message['content'] = message['content'].split(marker, 1)[0]
        handler._send(200, _json_bytes({'result': {'conversation_id': cid, 'messages': messages}}))
    elif method == 'DELETE':
        try:
            result = delete_conversation(ctx.store, cid)
        except ValueError:
            handler._send(409, _error('conflict', 'conversation has active or unresolved tasks'))
            return
        registry = getattr(ctx.manager, 'registry', None)
        if registry:
            for _, provider in registry.providers_for('embedding.embed'):
                if hasattr(provider, 'clear_cache'):
                    provider.clear_cache()
        ctx.log_event('conversation:delete', cid)
        handler._send(200, _json_bytes({'result': result}))
    else:
        handler._send(405, _error('method_not_allowed', 'use GET or DELETE'))


def memory(handler, method: str, parts: list[str], query: dict) -> None:
    ctx = handler.ctx
    store = ctx.store
    if len(parts) == 2 and parts[1] == "search" and method == "GET":
        if ctx.curator is None:
            handler._send(503, _error("unavailable", "memory curator not wired"))
            return
        q = (query.get("q") or [""])[0]
        if not q.strip():
            from whale_core.memory.store import MemoryKind
            records = sorted((r for kind in MemoryKind for r in store.list_by_kind(kind)),
                             key=lambda r: r.created_at, reverse=True)[:100]
            result = [{'key': r.key, 'kind': r.kind.value,
                       'summary': str(r.content.get('summary') or r.content.get('text') or '')[:1000],
                       'score': 0, 'reasons': ['recent'], 'created_at': r.created_at.isoformat()} for r in records]
            handler._send(200, _json_bytes({'result': {'hits': result}}))
            return
        hits = ctx.curator.recall(q, limit=50)
        result = [{
            "key": h.record.key, "kind": h.record.kind.value,
            "summary": str(h.record.content.get("summary", "")),
            "score": round(h.score, 4), "reasons": list(h.reasons),
            "created_at": h.record.created_at.isoformat(),
        } for h in hits]
        handler._send(200, _json_bytes({"result": {"hits": result}}))
        return
    if len(parts) == 2 and parts[1] == "export" and method == "POST":
        import io
        from whale_core.memory.export import export_jsonl
        buf = io.StringIO()
        count = export_jsonl(store, buf)
        ctx.log_event("memory:export", f"{count} records")
        handler._send(200, buf.getvalue().encode("utf-8"),
                   content_type="application/x-ndjson")
        return
    if len(parts) == 2:
        key = parts[1]
        from urllib.parse import unquote
        key = unquote(key)
        if method == "GET":
            record = store.get_record(key)
            if record is None:
                handler._send(404, _error("not_found", "no such memory"))
                return
            handler._send(200, _json_bytes({"result": {
                "key": record.key, "kind": record.kind.value,
                "content": dict(record.content), "tags": list(record.tags),
                "created_at": record.created_at.isoformat(),
            }}))
            return
        if method == "DELETE":
            existed = store.get_record(key) is not None
            if not existed:
                handler._send(404, _error('not_found', 'no such memory record'))
                return
            record = store.get_record(key)
            if ctx.curator is not None and hasattr(ctx.curator, 'forget'):
                ctx.curator.forget(key)
            else:
                store.delete(key)
            # forget may conservatively refuse non-curated memories; explicit
            # owner DELETE still removes the selected real record.
            store.delete(key)
            registry = getattr(ctx.manager, 'registry', None)
            if registry:
                for _, provider in registry.providers_for('embedding.embed'):
                    if hasattr(provider, 'clear_cache'):
                        provider.clear_cache()
            ctx.log_event("memory:delete", key)
            handler._send(200, _json_bytes({"result": {"deleted": existed}}))
            return
    handler._send(404, _error("not_found", "unknown endpoint"))
