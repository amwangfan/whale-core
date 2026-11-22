"""模型角色管理面 REST（2026-11-22 控制台重构 · 裁决①②③，Tob 批准）。

端点（统一 {result}/{error}；双认证由 server 层完成）：
- GET  /models/registry            注册表条目 + 角色绑定视图（只读）
- POST /models/registry            upsert 条目 {name, provider, model, effort, base_url?}
- POST /models/roles/{role}        绑定角色→条目 {name}；{name:null|""} 解绑
- POST /models/roles/{role}/test   逐角色连通测试（按注册表装配该角色的脑，
                                   真实一次短回复探活；失败回 error_kind）

密钥不出域：测试只报 ok/latency/error_kind，永不回显凭据；审计只记角色名。
"""

from __future__ import annotations

import time

from whale_core.api.server import _error, _json_bytes


def dispatch_models(handler, method: str, parts: list[str], query: dict | None = None) -> None:
    send = handler._send
    ctx = handler.ctx

    # GET /models/registry —— 注册表 + 角色绑定视图（只读）
    if parts == ["models", "registry"] and method == "GET":
        from whale_core.life.model_registry import get_roles, registry_entries
        send(200, _json_bytes({"result": {
            "entries": registry_entries(ctx.store),
            "roles": get_roles(ctx.store, ctx_env(ctx)),
        }}))
        return

    # POST /models/registry —— upsert 条目
    if parts == ["models", "registry"] and method == "POST":
        body = handler._body_json()
        if body is None:
            send(400, _error("bad_request", "JSON body required"))
            return
        from whale_core.life.model_registry import upsert_entry
        try:
            entry = upsert_entry(ctx.store, body)
        except ValueError as exc:
            send(400, _error("bad_request", str(exc)))
            return
        ctx.log_event("models:registry_upsert", str(entry.get("name", "")))
        send(200, _json_bytes({"result": entry}))
        return

    # POST /models/roles/{role} —— 绑定/解绑角色
    if len(parts) == 3 and parts[:2] == ["models", "roles"] and method == "POST":
        role = parts[2]
        body = handler._body_json()
        if body is None:
            send(400, _error("bad_request", "JSON body required"))
            return
        from whale_core.life.model_registry import ROLE_NAMES, set_role
        if role not in ROLE_NAMES:
            send(404, _error("not_found", f"未知模型角色 {role}（可选：{', '.join(ROLE_NAMES)}）"))
            return
        name = body.get("name")
        name = str(name).strip() if name is not None else None
        if not set_role(ctx.store, role, name or None):
            send(400, _error("bad_request", "条目不在注册表内"))
            return
        ctx.log_event("models:role_bind", f"{role}→{name or '(unbound)'}")
        send(200, _json_bytes({"result": {"role": role, "name": name}}))
        return

    # POST /models/roles/{role}/test —— 逐角色连通测试
    if len(parts) == 4 and parts[:2] == ["models", "roles"] and parts[3] == "test" \
            and method == "POST":
        role = parts[2]
        from whale_core.life.model_registry import ROLE_NAMES, get_roles, resolve_brain
        if role not in ROLE_NAMES:
            send(404, _error("not_found", f"未知模型角色 {role}（可选：{', '.join(ROLE_NAMES)}）"))
            return
        env = ctx_env(ctx)
        name = get_roles(ctx.store, env).get(role)
        if not name:
            send(200, _json_bytes({"result": {
                "ok": False, "latency_ms": 0, "error_kind": "unbound",
                "role": role}}))
            return
        brain = None
        try:
            brain = resolve_brain(name, env=env, store=ctx.store)
        except Exception:  # noqa: BLE001
            brain = None
        if brain is None:
            send(200, _json_bytes({"result": {
                "ok": False, "latency_ms": 0, "error_kind": "config",
                "role": role, "entry": name}}))
            return
        start = time.monotonic()
        try:
            reply = brain.reply("Reply with OK.")
            result = {"ok": isinstance(reply, str) and bool(reply),
                      "latency_ms": int((time.monotonic() - start) * 1000),
                      "role": role, "entry": name}
        except Exception as exc:  # noqa: BLE001 — 不回显内部细节
            result = {"ok": False,
                      "latency_ms": int((time.monotonic() - start) * 1000),
                      "error_kind": getattr(exc, "kind", "unavailable"),
                      "role": role, "entry": name}
        ctx.log_event("models:role_test", role)
        send(200, _json_bytes({"result": result}))
        return

    send(404, _error("not_found", "unknown endpoint"))


def ctx_env(ctx) -> dict:
    """装配期 env（SettingsController 持有），无则空——resolve_brain 的 env 链按此回落。"""
    settings = getattr(ctx, "settings", None)
    env = getattr(settings, "env", None)
    return dict(env) if isinstance(env, dict) else {}
