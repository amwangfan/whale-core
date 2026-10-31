"""system 面路由：health / status / settings / diagnostics / providers test。

从 api/server.py 拆分（issue #30），逻辑逐行等价；鉴权与错误信封见主模块。
"""
from __future__ import annotations

import time

from whale_core.api.server import _error, _json_bytes


def health(handler) -> None:
    handler._send(200, _json_bytes({"result": {"ok": True}}))


def status(handler) -> None:
    from whale_core.memory.store import MemoryKind
    ctx = handler.ctx
    store = ctx.store
    plugins = {"loaded": 0, "enabled": 0}
    if ctx.manager is not None:
        plugin_status = ctx.manager.status()
        plugins["loaded"] = len(plugin_status)
        plugins["enabled"] = sum(1 for p in plugin_status if p.get("enabled"))
    drafts = sum(1 for k in store.keys() if k.startswith("life:draft:"))
    result = {
        "ok": True,
        "version": ctx.version,
        "profile": ctx.profile,
        "uptime_s": int(time.time() - ctx.started_at),
        "plugins": plugins,
        "memory": {
            "episodes": len(store.list_by_kind(MemoryKind.EPISODE)),
            "preferences": len(store.list_by_kind(MemoryKind.PREFERENCE)),
        },
        "drafts_pending": drafts,
    }
    handler._send(200, _json_bytes({"result": result}))


def settings(handler, method: str) -> None:
    ctx = handler.ctx
    controller = ctx.settings
    if controller is None:
        handler._send(503, _error('unavailable', 'settings not wired'))
        return
    if method == 'GET':
        handler._send(200, _json_bytes({'result': controller.view()}))
    elif method == 'POST':
        body = handler._body_json()
        if body is None:
            handler._send(400, _error('bad_request', 'invalid settings object'))
            return
        try:
            controller.update(body)
        except (ValueError, TypeError):
            handler._send(422, _error('invalid_config', 'invalid settings; check endpoint, profile and field types'))
            return
        ctx.log_event('settings:saved')
        handler._send(200, _json_bytes({'result': controller.view()}))
    else:
        handler._send(405, _error('method_not_allowed', 'use GET or POST'))


def diagnostics(handler) -> None:
    from whale_core.core.diagnostics import provider_health_report
    ctx = handler.ctx
    checks = [{'name': 'api', 'status': 'ok', 'detail': 'Authenticated API is running'},
              {'name': 'memory', 'status': 'ok', 'detail': type(ctx.store).__name__}]
    if ctx.settings:
        view = ctx.settings.view()
        checks.append({'name': 'model', 'status': 'ok' if view['profile'] == 'local' or view['model']['api_key_configured'] else 'fail',
                       'detail': 'Connection test is separate; configuration does not prove reachability'})
        checks.extend({'name': 'configuration', 'status': 'warn', 'detail': n} for n in view['notes'] if '[warn]' in n)
    import shutil
    checks.append({'name': 'shell sandbox', 'status': 'ok' if shutil.which('bwrap') and shutil.which('prlimit') else 'warn',
                   'detail': 'Linux bubblewrap/prlimit required; actual namespace availability checked before execution'})
    summary = {k: sum(c['status'] == k for c in checks) for k in ('ok', 'warn', 'fail')}
    handler._send(200, _json_bytes({'result': {'checks': checks, 'summary': summary,
                                         'providers': provider_health_report(ctx.store),
                                         'worker': ctx.worker.status() if ctx.worker else None}}))


def provider_test(handler) -> None:
    from whale_core.plugins.model_provider import resolve_chat_model
    registry = getattr(handler.ctx.manager, 'registry', None)
    model = resolve_chat_model(registry) if registry else None
    if model is None:
        handler._send(200, _json_bytes({'result': {'ok': False, 'latency_ms': 0, 'error_kind': 'config'}}))
        return
    start = time.monotonic()
    try:
        reply = model.reply('Reply with OK.')
        result = {'ok': isinstance(reply, str) and bool(reply), 'latency_ms': int((time.monotonic()-start)*1000)}
    except Exception as exc:
        result = {'ok': False, 'latency_ms': int((time.monotonic()-start)*1000),
                  'error_kind': getattr(exc, 'kind', 'unavailable')}
    handler._send(200, _json_bytes({'result': result}))
