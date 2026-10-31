"""plugins 面路由：/plugins 列表/详情/test/config/enable/disable/health。

从 api/server.py 拆分（issue #30），逻辑逐行等价；config 输出一律过
``PluginManager.config_view`` 脱敏，本层不回显任何 secret。
"""
from __future__ import annotations

import time

from whale_core.api.server import _error, _json_bytes


def plugins(handler, method: str, parts: list[str]) -> None:
    ctx = handler.ctx
    if ctx.manager is None:
        handler._send(503, _error("unavailable", "plugin manager not wired"))
        return
    mgr = ctx.manager
    if len(parts) == 1 and method == "GET":
        handler._send(200, _json_bytes({"result": {"plugins": mgr.status()}}))
        return
    if len(parts) == 2 and method == "GET":
        pid = parts[1]
        info = next((p for p in mgr.status() if p.get("plugin_id") == pid), None)
        if info is None:
            handler._send(404, _error("not_found", f"unknown plugin {pid!r}"))
            return
        info = dict(info)
        info["config"] = mgr.config_view(pid)  # 脱敏视图
        if pid in ('deepseek', 'local_model') and ctx.settings is not None:
            view = ctx.settings.view()['model']
            info['config'] = {'base_url': view['base_url'], 'model': view['model'],
                              'api_key': '***' if view['api_key_configured'] else ''}
            info['config_schema'] = {'properties': {'base_url': {'type': 'string'},
                'model': {'type': 'string'}, 'api_key': {'type': 'string', 'format': 'secret'}}}
        handler._send(200, _json_bytes({"result": info}))
        return
    if len(parts) == 3:
        pid, action = parts[1], parts[2]
        if action == "test" and method == "POST":
            plugin_test(handler, pid)
            return
        if action == "config" and method == "POST":
            body = handler._body_json()
            if body is None or not isinstance(body.get("config"), dict):
                handler._send(400, _error("bad_request", "body must contain config object"))
                return
            try:
                if pid in ('deepseek', 'local_model') and ctx.settings is not None:
                    ctx.settings.update({'model': {k: v for k, v in body['config'].items() if not (k == 'api_key' and v == '***')}})
                elif pid in getattr(mgr, '_factories', {}) and not mgr.config_view(pid):
                    handler._send(422, _error('invalid_config', 'configure official services on the settings page'))
                    return
                else:
                    mgr.patch_config(pid, body["config"])
                    row = next(p for p in mgr.status() if p['plugin_id'] == pid)
                    if row['enabled']:
                        mgr.disable(pid)
                        mgr.enable(pid)
            except KeyError:
                handler._send(404, _error("not_found", f"unknown plugin {pid!r}"))
                return
            except ValueError as exc:
                handler._send(422, _error("invalid_config", str(exc)[:200]))
                return
            ctx.log_event("plugin:config", pid)
            handler._send(200, _json_bytes({"result": {"plugin_id": pid, "saved": True}}))
            return
        if action in ("enable", "disable") and method == "POST":
            try:
                if action == "enable":
                    mgr.enable(pid)
                else:
                    mgr.disable(pid)
            except KeyError:
                handler._send(404, _error("not_found", f"unknown plugin {pid!r}"))
                return
            except Exception as exc:
                handler._send(500, _error("internal", f"{type(exc).__name__}"))
                return
            row = next((p for p in mgr.status() if p['plugin_id'] == pid), {})
            if action == 'enable' and not row.get('enabled'):
                handler._send(503, _error('unavailable', 'plugin could not start; check dependencies and configuration'))
                return
            if action == 'disable' and pid == 'voice_realtime' and ctx.voice_bridge is not None:
                ctx.voice_bridge.shutdown()
                ctx.voice_bridge = None
            ctx.log_event(f"plugin:{action}", pid)
            handler._send(200, _json_bytes({"result": {"plugin_id": pid, "status": action + "d"}}))
            return
        if action == "health" and method == "GET":
            handler._send(200, _json_bytes({"result": mgr.healthcheck(pid)}))
            return
    handler._send(404, _error("not_found", "unknown endpoint"))


def plugin_test(handler, pid: str) -> None:
    """POST /plugins/{id}/test：用户点击=同意的一次最小真实连通性测试。

    - provider 有 ``reply()``（llm.chat 类）→ 最小 ping（短超时）；
    - 否则回落 manager.healthcheck（不触网）。
    结果只含 ok/latency/错误分类名，**绝不含密钥与异常原文**。
    """
    mgr = handler.ctx.manager
    if mgr is None:
        handler._send(503, _error("unavailable", "plugin manager not wired"))
        return
    registry = getattr(mgr, "registry", None)
    provider = registry.provider_of(pid) if registry is not None else None
    if provider is None or not hasattr(provider, "reply"):
        health = mgr.healthcheck(pid)
        handler._send(200, _json_bytes({"result": {
            "plugin_id": pid, "ok": health.get("status") == "healthy",
            "latency_ms": None, "via": "healthcheck",
            "detail": health.get("status", "")}}))
        return
    started = time.monotonic()
    try:
        reply = provider.reply("ping")  # 最小调用；成本=一次短请求
        latency = int((time.monotonic() - started) * 1000)
        ok = isinstance(reply, str) and bool(reply)
        handler._send(200, _json_bytes({"result": {
            "plugin_id": pid, "ok": ok, "latency_ms": latency, "via": "ping"}}))
    except Exception as exc:
        latency = int((time.monotonic() - started) * 1000)
        kind = getattr(exc, "kind", type(exc).__name__)
        handler._send(200, _json_bytes({"result": {
            "plugin_id": pid, "ok": False, "latency_ms": latency,
            "via": "ping", "error_kind": str(kind)[:40]}}))
