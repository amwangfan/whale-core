"""眼镜/手机轻量 API（2026-11-03 交接单 handoff-whale-core-glasses-chat.md）。

端点：

- ``POST /v1/chat``：眼镜/手机文字对话。``companion``（默认）走
  WhaleCompanion（人格 + 记忆召回 + 情节回写），与 QQ/电话共享同一份
  sqlite 记忆库——眼镜上聊的换个端能接上；``quick`` 走轻量模型
  （WHALE_QUICK_* / QQ_INTENT_*），**不走记忆库、不写长期记忆**。
- ``POST /v1/chat/followup``（2026-11-06）：并行链路第二条。``/v1/chat`` 第一条
  由回复模型立刻给出，意图模型后台判定；需要工具时第二条进会话收件板。
  请求 ``followup="poll"`` 的新客户端用本端点长轮询取；旧客户端（不带该字段）
  对规则可识别的工具类（天气/显式搜索等）或第一条已「先答应待会儿」时同步等待
  ≤WHALE_GLASSES_FOLLOWUP_SYNC_MS（默认 22s）后合并返回（``replies`` 分列两条），
  其余立即返回第一条。
- ``POST /v1/qq/reply``：QQ 快捷回复——把用户口述的文本发给**已配置的
  绑定对端**（目标不从请求推导，防误发）；走 WeQ 发送链与既有风控
  （被动额度/静默时段豁免口径与 runner 一致：主人当面 dictation 属被动延续）。
- ``POST /v1/result/deliver``：统一结果投递（ResultDelivery）——QQ 在线优先；
  QQ 离线走 Whale 原生通知（在线直发/离线补传），图片可继续回落手机图库。
  ``/v1/chat`` 请求体 ``deliverResult=true`` 时也会在回复后走同一路由。
- ``POST /v1/rayneo/ingest|query|control``：雷鸟眼镜全天转写上传 / 查询 / 删除。
  鉴权同 ``WHALE_GLASSES_TOKEN``。ingest 体上限 320 KiB，query/control 64 KiB。

鉴权：``Authorization: Bearer <WHALE_GLASSES_TOKEN>``（phone 角色专用令牌，
与管理面 token 分离；常量时间比较）。无/错 token → 401。

部署纪律：只监听 127.0.0.1（对外暴露由反代/隧道层负责）；令牌只走环境变量；
本服务与 qq-bot runner 共享同一 sqlite（WAL，approve 子命令同款跨进程先例）。
"""
from __future__ import annotations

import argparse
import re
import contextvars
import threading
import json
import os
import secrets
import sys
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MAX_BODY_BYTES = 64 * 1024
RAYNEO_INGEST_MAX_BYTES = 320 * 1024

def _load_env_file(path) -> dict:
    """解析 KEY=VALUE env 文件（# 注释/空行跳过；值可带引号）。"""
    out: dict = {}
    for raw in open(path, encoding="utf-8").read().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key:
            out[key] = value.strip().strip('"').strip("'")
    return out

MAX_TEXT_CHARS = 4000

# 最近一次客户端上报的位置（粗粒度摘要，禁止把高精度轨迹当长期记忆写别处）。
LOCATION_STORE_KEY = "glasses:last_location"

# companion 路径附加约束：外卖/本地生活只搜索建议，不下单不支付。
_GLASSES_LOCAL_LIFE_HINT = (
    "本地生活约束：可以搜索附近商家、评价与公开信息并给出建议或应用深链提示；"
    "不要声称已经下单、已经支付或正在代下单；本通道不支持自动点外卖/支付。"
)


def _coerce_coord(value) -> float | None:
    if value is None or value is False:
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if num != num:  # NaN
        return None
    return num


def summarize_location_fields(
    *,
    city: str = "",
    location: str = "",
    lat=None,
    lng=None,
) -> str:
    """把请求/存储里的位置字段收成一行摘要（城市优先，其次自由文本，再次经纬度）。"""
    city = (city or "").strip()[:80]
    location = (location or "").strip()[:120]
    lat_f = _coerce_coord(lat)
    lng_f = _coerce_coord(lng)
    parts: list[str] = []
    if city:
        parts.append(city)
    if location and location not in parts:
        parts.append(location)
    if lat_f is not None and lng_f is not None:
        # 保留约城市街区精度，避免把原始高精度坐标灌进提示词。
        parts.append(f"约 {lat_f:.4f},{lng_f:.4f}")
    return "；".join(parts)[:200]


_glasses_request_location: contextvars.ContextVar[str] = contextvars.ContextVar(
    "whale_glasses_request_location", default="")


def resolve_request_location(env: dict, store, body: dict | None) -> str:
    """位置解析优先级：请求体 → store 最近上报 → 环境默认 → 空（调用方写「未知」）。

    请求体可选字段：``location`` / ``city`` / ``lat`` / ``lng``。
    环境：``WHALE_DEFAULT_CITY`` 或 ``WHALE_HOME_CITY`` 或 ``WHALE_LOCATION``。
    """
    body = body or {}
    summary = summarize_location_fields(
        city=str(body.get("city") or ""),
        location=str(body.get("location") or ""),
        lat=body.get("lat"),
        lng=body.get("lng"),
    )
    if summary:
        try:
            store.put(LOCATION_STORE_KEY, {
                "summary": summary,
                "city": str(body.get("city") or "")[:80],
                "location": str(body.get("location") or "")[:120],
                "lat": _coerce_coord(body.get("lat")),
                "lng": _coerce_coord(body.get("lng")),
            })
        except Exception:
            pass
        return summary
    try:
        cached = store.get(LOCATION_STORE_KEY)
    except Exception:
        cached = None
    if isinstance(cached, dict):
        cached_summary = str(cached.get("summary") or "").strip()
        if cached_summary:
            return cached_summary[:200]
        cached_summary = summarize_location_fields(
            city=str(cached.get("city") or ""),
            location=str(cached.get("location") or ""),
            lat=cached.get("lat"),
            lng=cached.get("lng"),
        )
        if cached_summary:
            return cached_summary
    for key in ("WHALE_DEFAULT_CITY", "WHALE_HOME_CITY", "WHALE_LOCATION"):
        val = (env.get(key) or "").strip()
        if val:
            return val[:200]
    return ""


class GlassesConfig:

    def __init__(self, env: dict):
        self.token = (env.get("WHALE_GLASSES_TOKEN") or "").strip()
        self.store_path = (env.get("WHALE_GLASSES_STORE")
                           or env.get("WHALE_STORE") or "").strip()
        self.owner_uid = (env.get("OWNER_UID") or "").strip()
        self.owner_uin = (env.get("OWNER_QQ") or "").strip()
        self.weq_url = (env.get("WEQ_WEB_URL") or "").strip()
        self.weq_token = (env.get("WEQ_WEB_TOKEN") or "").strip()
        self.self_uin = (env.get("QQ_SELF_UIN") or "").strip()
        self.window_state = (env.get("QQ_FULLMODE_STATE_FILE")
                             or "/tmp/whale-fullmode-send.ts").strip()

    def missing(self) -> list[str]:
        out = []
        if not self.token:
            out.append("WHALE_GLASSES_TOKEN")
        if not self.store_path:
            out.append("WHALE_GLASSES_STORE")
        return out


def _json_response(handler: BaseHTTPRequestHandler, status: int, payload: dict) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    if handler.close_connection:
        handler.send_header("Connection", "close")
    handler.end_headers()
    handler.wfile.write(body)



def install_request_location_intent_wrapper(orch, request_location=None) -> None:
    """把「当前请求位置」注入 orch._intent_context；全生命周期只包装一次。

    位置来自 ``_glasses_request_location`` ContextVar（每请求 set），避免
    同进程重叠请求互相覆盖共享 dict；``request_location`` 参数保留兼容旧调用，
    若传入则仅在 ContextVar 为空时回落读取其 ``value``。
    """
    if orch is None or getattr(orch, "_glasses_loc_wrapped", False):
        return
    _orig_ctx = orch._intent_context

    def _intent_context_with_location(conversation_id, text, *, allowed_spaces=None):
        base = _orig_ctx(conversation_id, text, allowed_spaces=allowed_spaces)
        from whale_core.core.prompt_composer import format_location_context
        loc = _glasses_request_location.get() or ""
        if not loc and isinstance(request_location, dict):
            loc = str(request_location.get("value") or "")
        loc_line = format_location_context(location=loc or None)
        return (loc_line + ("\n" + base if base else ""))[:1500]

    orch._intent_context = _intent_context_with_location  # type: ignore
    orch._glasses_loc_wrapped = True  # type: ignore[attr-defined]


class FollowupBoard:
    """并行链路第二条的会话级收件板（眼镜 /v1/chat 非流式，第二条靠轮询/合并取）。

    每轮 open 一个 turnId；后台意图/工具/跟进结束时 settle：
    ``ready``（有第二条）或 ``none``（不需要工具，本轮结束）。内存态、有上限。
    """

    def __init__(self, cap: int = 128):
        self._cap = cap
        self._lock = threading.Lock()
        self._items: dict = {}

    def open(self, session_key: str) -> str:
        turn_id = uuid.uuid4().hex[:16]
        with self._lock:
            self._items[turn_id] = {"session": session_key, "status": "pending",
                                    "reply": "", "timing": {},
                                    "event": threading.Event()}
            while len(self._items) > self._cap:
                self._items.pop(next(iter(self._items)))
        return turn_id

    def settle(self, turn_id: str, status: str, reply: str = "",
               timing: dict | None = None) -> None:
        with self._lock:
            item = self._items.get(turn_id)
            if item is None:
                return
            item.update(status=status, reply=reply or "", timing=dict(timing or {}))
            item["event"].set()

    def wait(self, turn_id: str, timeout_s: float) -> dict | None:
        with self._lock:
            item = self._items.get(turn_id)
        if item is None:
            return None
        if timeout_s > 0:
            item["event"].wait(timeout_s)
        with self._lock:
            return {k: v for k, v in item.items() if k != "event"}

    def latest(self, session_key: str) -> str:
        with self._lock:
            for turn_id in reversed(list(self._items)):
                if self._items[turn_id]["session"] == session_key:
                    return turn_id
        return ""


from whale_core.core.runtime.turn_orchestration import (  # noqa: E402
    reply_promises_followup as first_reply_promises_followup)


def _rule_plan_is_task(orch, text: str, location: str) -> bool:
    """零 LLM 规则探针（天气/显式搜索/附近/显式 shell）：仅用于旧客户端是否同步等第二条。"""
    try:
        from whale_core.core.intent_router import SearchIntentRouter
        tools = getattr(getattr(orch, "intent_model", None), "tools", None)
        probe = SearchIntentRouter(model=None, enable_llm=False, tools=tools)
        ctx = f"当前位置：{location}" if location else ""
        return json.loads(probe.plan(text or "", context=ctx)).get("kind") == "task"
    except Exception:
        return False


def build_delegates(env: dict, *, brain_factory=None, quick_brain_factory=None,
                    send_port_factory=None):
    """装配委派对象（测试注入假件；生产用真实件）。

    返回 dict：companion_chat(text, conversation_id) / quick_chat(text) /
    qq_send(text) -> (ok, receipt) / prefs()。
    """
    from whale_core.life.initiative import ProactivePrefs, load_prefs
    from whale_core.memory.sqlite_store import SqliteMemoryStore

    cfg = GlassesConfig(env)
    store = SqliteMemoryStore(cfg.store_path)

    if brain_factory is None:
        from whale_core.core.deepseek import DeepSeekCompanion

        def brain_factory():
            return DeepSeekCompanion(
                model=(env.get("DEEPSEEK_MODEL") or "").strip() or None,
                base_url=(env.get("DEEPSEEK_BASE_URL") or "").strip() or None,
                api_key=(env.get("DEEPSEEK_API_KEY") or "").strip() or None)

    if quick_brain_factory is None:
        def quick_brain_factory():
            from whale_core.core.deepseek import DeepSeekCompanion
            # 轻量档：显式 WHALE_QUICK_* 优先，其次意图角色（QQ_INTENT_*）
            return DeepSeekCompanion(
                model=(env.get("WHALE_QUICK_MODEL") or env.get("QQ_INTENT_MODEL")
                       or "").strip() or None,
                base_url=(env.get("WHALE_QUICK_BASE_URL") or env.get("QQ_INTENT_BASE_URL")
                          or "").strip() or None,
                api_key=(env.get("WHALE_QUICK_API_KEY") or env.get("QQ_INTENT_API_KEY")
                         or "").strip() or None)

    def _make_companion():
        from whale_core.core.companion import WhaleCompanion
        from whale_core.memory.curation import MemoryCurator

        def _deliver_exploration(draft, conversation_id):
            evidence = list(draft.get("evidence") or ())
            text = str(draft.get("text") or "").strip()
            if evidence:
                text += "\n来源：" + evidence[0]
            result = deliver_result(
                [{"kind": "text", "text": text}],
                conversation=conversation_id,
                summary="exploration:" + str(draft.get("draft_id") or "")[:80])
            # A logged stub is not a successful delivery. Only a QQ send or
            # Whale notification accepted by the Core gateway resolves "要".
            result["ok"] = any(
                item.get("ok") and item.get("channel") in
                {"qq_c2c", "qq_group", "whale_notification"}
                for item in result.get("receipts", ())
                if isinstance(item, dict))
            return result

        return WhaleCompanion(
            store, brain_factory(), curator=MemoryCurator(store),
            exploration_delivery=_deliver_exploration)

    # 单 owner 通道：复用 companion/orchestrator，避免每请求重建搜索/shell 插件。
    # ThreadingHTTPServer 下用锁串行化一轮 handle，避免改 location / intent 上下文竞态。
    _orch_lock = threading.Lock()
    _cached: dict = {"companion": None, "orch": None, "quick": None}
    # 当前请求位置：由 companion_chat 每轮写入；intent 包装只装一次并读取此状态。
    _request_location: dict = {"value": ""}
    _last_chat_ms = {"value": 0}

    def mark_chat(now_ms: int | None = None) -> None:
        import time as _time
        _last_chat_ms["value"] = int(now_ms if now_ms is not None else _time.time() * 1000)

    def last_chat_ms() -> int:
        return int(_last_chat_ms["value"] or 0)

    rayneo_store = None
    try:
        from whale_core.api.rayneo_http import raw_ttl_days, rayneo_store_path
        from whale_core.memory.rayneo_context import RayneoContextStore
        rayneo_store = RayneoContextStore(
            rayneo_store_path(env), raw_ttl_days=raw_ttl_days(env))
    except Exception as exc:  # noqa: BLE001 — 转写库失败不阻止主对话
        print(f"[rayneo] store not opened: {type(exc).__name__}", flush=True)

    def _build_search_orchestrator(companion):
        """与 QQ runner 同款：搜索 +（可选）主人 shell + IntentRouter。

        眼镜/手机通道经 WHALE_GLASSES_TOKEN 鉴权，视为主人身份：personal 档下
        可挂 shell_sandbox（与 QQ owner 路径同 ACL）。无搜索且无 shell 时返回 None。
        """
        has_search = bool(
            env.get("TAVILY_API_KEY") or env.get("BRAVE_API_KEY")
            or env.get("WHALE_SEARCH_PROVIDER")
            or env.get("WHALE_DEEPSEEK_SEARCH_API_KEY"))
        has_weather = bool(
            (env.get("QWEATHER_API_KEY") or "").strip()
            and (env.get("QWEATHER_API_HOST") or "").strip())
        shell_on = env.get("WHALE_SHELL_ENABLED", "").strip() == "1"
        if not has_search and not has_weather and not shell_on:
            return None
        from whale_core.core.intent_router import SearchIntentRouter
        from whale_core.core.runtime import (
            ConversationOrchestrator, ToolRegistry, runtime_shell_grant_verifier)
        from whale_core.core.runtime.turn_orchestration import (
            apply_parallel_env, intent_brain_max_tokens, intent_brain_timeout,
            intent_skip_chitchat)
        from whale_core.plugins.registry import PluginRegistry
        from whale_core.plugins.gateway import PluginGateway
        from whale_core.plugins.policy import PluginActionPolicy

        plugin_registry = PluginRegistry()
        tools = ToolRegistry()
        fallback_search = None
        if has_search:
            from search_web.web_fetch import register_web_fetch
            from search_web.web_search import register_web_search, select_provider
            provider = select_provider(env)
            register_web_search(plugin_registry, tools, provider=provider)
            register_web_fetch(plugin_registry, tools)

            def fallback_search(query: str, _provider=provider) -> str:
                results = _provider.search(str(query or ""), limit=5)
                if not results:
                    return "没有找到结果"
                return "\n".join(
                    f"{r.title} — {r.snippet}（{r.url}）" for r in results[:5])

        if has_weather:
            from qweather.weather import register_qweather
            register_qweather(
                plugin_registry, tools, env=env,
                fallback_search=fallback_search)

        shell_auto_actions: frozenset[str] = frozenset()
        if shell_on:
            from pathlib import Path as _Path
            from whale_core.plugins.shell_tool import register_shell, SHELL_AUTO_PAIR
            from whale_core.cloud.shell_policy import ShellPolicy, ShellTier
            ws = _Path(cfg.store_path).parent / "workspace"
            register_shell(plugin_registry, tools, ws, env=env)
            if ShellPolicy.from_env(env).tier is ShellTier.PERSONAL:
                shell_auto_actions = frozenset({SHELL_AUTO_PAIR})

        gateway = PluginGateway(
            plugin_registry,
            PluginActionPolicy(plugin_registry, auto_approve_actions=shell_auto_actions),
            # 本服务 HTTP 层已用 WHALE_GLASSES_TOKEN 鉴权，视为主人手机/眼镜通道。
            owner_verifier=lambda _intent: True,
            approval_verifier=lambda _intent: True,
            grant_verifier=runtime_shell_grant_verifier(store),
        )
        # Shell 审操 / 模糊写意图：优先 WHALE_SHELL_MODEL，其次眼镜意图，再次 QQ 意图。
        # 端点与 Key 一律沿用 QQ_INTENT_*（AgentScope），只换模型名环境变量。
        # SearchIntentRouter：显式 search/天气/shell 规则零 LLM；其余（含寒暄）
        # 交 kimi 判定——并行链路下在后台跑，不拖慢第一条。
        intent_model_id = (
            (env.get("WHALE_SHELL_MODEL")
             or env.get("WHALE_GLASSES_INTENT_MODEL")
             or env.get("QQ_INTENT_MODEL")
             or "").strip())
        intent_brain = None
        enable_llm = bool(intent_model_id)
        if enable_llm:
            from whale_core.core.deepseek import DeepSeekCompanion
            intent_brain = DeepSeekCompanion(
                model=intent_model_id,
                base_url=(env.get("WHALE_GLASSES_INTENT_BASE_URL")
                          or env.get("QQ_INTENT_BASE_URL") or "").strip() or None,
                api_key=(env.get("WHALE_GLASSES_INTENT_API_KEY")
                         or env.get("QQ_INTENT_API_KEY") or "").strip() or None,
                max_tokens=intent_brain_max_tokens(env),
                timeout=intent_brain_timeout(env))
        intent = SearchIntentRouter(
            model=intent_brain, enable_llm=enable_llm, tools=tools,
            skip_clear_chitchat=intent_skip_chitchat(env))
        orch = ConversationOrchestrator(
            store, companion, intent_model=intent, tools=tools, gateway=gateway)
        apply_parallel_env(orch, env)
        return orch

    followups = FollowupBoard()
    parallel_on = (env.get("WHALE_PARALLEL_REPLY", "1") or "1").strip() != "0"
    # 旧客户端（不带 followup=poll）收不到异步第二条，策略：
    # smart（默认）= 规则可识别的工具类 或 第一条已「先答应待会儿」→ 同步等第二条合并；
    # rules = 仅规则工具类；intent = 一律等意图判定；none = 不等。
    legacy_policy = (env.get("WHALE_GLASSES_LEGACY_FOLLOWUP") or "smart").strip().lower()
    try:
        legacy_wait_s = max(0.0, float(env.get("WHALE_GLASSES_FOLLOWUP_SYNC_MS") or 22000) / 1000)
    except ValueError:
        legacy_wait_s = 22.0

    def followup_poll(conversation_id: str, turn_id: str = "",
                      wait_ms: int = 0) -> dict:
        turn_id = turn_id or followups.latest(conversation_id)
        item = followups.wait(turn_id, max(0, min(int(wait_ms or 0), 25000)) / 1000) \
            if turn_id else None
        if item is None or item.get("session") != conversation_id:
            return {"status": "unknown", "turnId": turn_id or None}
        out = {"status": item["status"], "turnId": turn_id}
        if item["status"] == "ready":
            out["reply"] = item["reply"]
        out.update(item.get("timing") or {})
        return out

    def companion_chat(text: str, conversation_id: str,
                       location: str = "", followup_mode: str = "") -> tuple[str, dict]:
        """返回 (reply, timing)；timing 含 intentMs/toolMs/replyMs/totalMs。"""
        from whale_core.core.runtime import InputEnvelope
        import time as _time
        mark_chat()
        t_all = _time.perf_counter()
        # 眼镜侧「做一张简单结果图发我」：规则短路 → 本地图 → ResultDelivery。
        try:
            from whale_core.life.do_request import match_do_request, execute_do_request
            req = match_do_request(text or "")
            if req is not None and req.kind == "result_image":
                out = execute_do_request(
                    req,
                    deliverer=lambda items, summary: deliver_result(
                        list(items), summary=summary or ""),
                    owner_uid=cfg.owner_uid)
                total = int((_time.perf_counter() - t_all) * 1000)
                reply = out.get("reply") or "好了。"
                timing = {"intentMs": 0, "toolMs": 0, "replyMs": total,
                          "totalMs": total, "doRequest": out.get("kind"),
                          "delivery": out.get("delivery")}
                return reply, timing
        except Exception as exc:
            print(f"[glasses] do_request short-circuit failed: {type(exc).__name__}",
                  flush=True)
        first = None
        turn_id = ""
        wait = False
        # 每请求位置进 ContextVar，重叠请求互不覆盖（不再靠共享 dict）。
        _glasses_request_location.set(location or "")
        with _orch_lock:
            if _cached["companion"] is None:
                companion = _make_companion()
                companion.planning_hint = _GLASSES_LOCAL_LIFE_HINT
                _cached["companion"] = companion
                _cached["orch"] = _build_search_orchestrator(companion)
            companion = _cached["companion"]
            orch = _cached["orch"]
            if orch is not None:
                # 包装只安装一次；位置读 ContextVar。
                install_request_location_intent_wrapper(orch, _request_location)
        # 锁外跑 LLM：同会话连说时 B 能在 A 生成中启动，并靠 history 早落盘看见 A。
        brain = companion.model
        try:
            brain.default_location = location or None
        except Exception:
            pass
        if orch is None:
            reply = companion.chat(
                text, conversation_id=conversation_id, location=location or None)
            total = int((_time.perf_counter() - t_all) * 1000)
            return reply, {"intentMs": 0, "toolMs": 0, "replyMs": total,
                           "totalMs": total}
        envelope = InputEnvelope(
            conversation_id=conversation_id, text=text,
            source="glasses:phone", channel="glasses",
            owner_id=cfg.owner_uin or cfg.owner_uid)
        if not (parallel_on and hasattr(orch, "handle_envelope_parallel")):
            result = orch.handle_envelope(envelope)
            timing = {
                "intentMs": int(getattr(result, "intent_ms", 0) or 0),
                "toolMs": int(getattr(result, "tool_ms", 0) or 0),
                "replyMs": int(getattr(result, "reply_ms", 0) or 0),
                "totalMs": int((_time.perf_counter() - t_all) * 1000),
            }
            return result.reply, timing
        # 2026-11-06 并行链路：DS 第一条不等意图；Kimi 后台判定，
        # 需要工具时第二条进 FollowupBoard（轮询取 / 旧客户端同步合并）。
        turn_id = followups.open(conversation_id)

        def _settled(route, out, _tid=turn_id):
            if route == "task" and out is not None and (out.reply or "").strip():
                followups.settle(_tid, "ready", reply=out.reply, timing={
                    "intentMs": int(out.intent_ms or 0),
                    "toolMs": int(out.tool_ms or 0),
                    "followupMs": int(out.reply_ms or 0)})
            else:
                followups.settle(_tid, "none")

        first = orch.handle_envelope_parallel(envelope, on_settled=_settled)
        wait_reason = ""
        if followup_mode != "poll" and legacy_policy != "none":
            if legacy_policy == "intent":
                wait_reason = "intent"
            elif _rule_plan_is_task(orch, text, location):
                wait_reason = "rule"
            elif (legacy_policy == "smart"
                  and first_reply_promises_followup(first.reply)):
                wait_reason = "promise"
        wait = bool(wait_reason)
        # 锁外等待第二条：不阻塞下一轮请求进入。
        timing = {"intentMs": 0, "toolMs": 0,
                  "replyMs": int(first.reply_ms or 0), "followupMs": 0}
        reply = first.reply
        item = followups.wait(turn_id, legacy_wait_s if wait else 0) or {}
        status = item.get("status", "pending")
        merged = bool(wait and status == "ready" and (item.get("reply") or "").strip())
        if merged:
            # 旧客户端兼容：同步等到第二条后合并返回（replies 分开给出）。
            reply = ((reply or "").rstrip() + "\n" + item["reply"].strip()).strip()
            timing["replies"] = [first.reply, item["reply"]]
        timing.update(item.get("timing") or {})
        timing["followup"] = {"turnId": turn_id, "status": status, "merged": merged,
                              "waited": wait_reason or None}
        timing["totalMs"] = int((_time.perf_counter() - t_all) * 1000)
        return reply, timing

    def quick_chat(text: str, location: str = "", persona: str | None = None) -> tuple[str, dict]:
        import time as _time
        import inspect
        mark_chat()
        t0 = _time.perf_counter()
        with _orch_lock:
            if _cached["quick"] is None:
                _cached["quick"] = quick_brain_factory()
            brain = _cached["quick"]
            try:
                brain.default_location = location or None
            except Exception:
                pass
            # 2026-11-11：persona 可被调用方覆盖（投递交代用人格卡软提示）
            if persona is None:
                persona = (
                    "用一两句口语简明回答，不展开、不追问。\n"
                    + _GLASSES_LOCAL_LIFE_HINT)
            kwargs = {"persona": persona}
            try:
                if "location" in inspect.signature(brain.reply).parameters:
                    kwargs["location"] = location or None
            except (TypeError, ValueError):
                pass
            reply = brain.reply(text, **kwargs)
        total = int((_time.perf_counter() - t0) * 1000)
        return reply, {"intentMs": 0, "toolMs": 0, "replyMs": total, "totalMs": total}

    if send_port_factory is None:
        def send_port_factory():
            from weq_channel.full_mode_window import FullModeWindow, WindowedSendPort
            from weq_channel.weq_send import WeQWebSendPort
            port = WeQWebSendPort(cfg.weq_url, cfg.weq_token,
                                  target_uid=cfg.owner_uid,
                                  expected_account_uin=cfg.self_uin or None)
            if os.environ.get("QQ_FULLMODE_ONDEMAND", "").strip() == "1":
                window = FullModeWindow(cfg.weq_url, cfg.weq_token,
                                        state_file=cfg.window_state)
                port = WindowedSendPort(port, window)
            return port

    def qq_send(text: str) -> tuple[bool, str]:
        port = send_port_factory()
        ok = port.send_c2c(cfg.owner_uid, text) if hasattr(port, "send_c2c") else False
        return bool(ok), ("accepted" if ok else "send_failed")

    def _whale_push(items, conversation: str, summary: str):
        """QQ 离线时向已配对设备提交原生通知；无在线网关时先入本地补传队列。"""
        from whale_core.devices.registry import DeviceRegistry
        from whale_core.life.result_delivery import DeliveryReceipt
        from whale_core.notifications.gateway import NotificationGateway
        from whale_core.notifications.models import NotificationCategory, NotificationTarget

        texts = [str(item.text or "").strip() for item in items
                 if getattr(item, "kind", "") == "text" and str(item.text or "").strip()]
        body = "\n".join(texts)[:2000]
        if not body:
            body = str(summary or "").strip()[:2000]
        if not body:
            return []
        try:
            devices = [meta for meta in DeviceRegistry(store).list_devices()
                       if meta.get("status") == "active" and meta.get("device_id")]
        except Exception as exc:
            return [DeliveryReceipt(
                channel="whale_notification", ok=False,
                detail=f"device_registry:{type(exc).__name__}", qq_online=False)]
        if not devices:
            return []

        api_base = (env.get("WHALE_API") or env.get("WHALE_API_URL")
                    or "http://127.0.0.1:8080").strip().rstrip("/")
        api_token = (env.get("WHALE_API_TOKEN") or "").strip()
        target = NotificationTarget("chat", str(conversation or "")[:128])
        group_key = ("explore:" + str(conversation or "owner"))[:128]
        gateway = NotificationGateway(store)
        receipts = []
        for meta in devices:
            device_id = str(meta["device_id"])
            accepted = False
            ambiguous = False
            detail = ""
            if api_token:
                try:
                    import urllib.error
                    import urllib.request
                    from urllib.parse import quote

                    class _NoRedirect(urllib.request.HTTPRedirectHandler):
                        def redirect_request(self, req, fp, code, msg, headers, newurl):
                            return None

                    payload = json.dumps({
                        "category": NotificationCategory.CHAT_MESSAGE,
                        "title": "鲸鱼有一条新发现",
                        "body": body,
                        "target": target.to_payload(),
                        "group_key": group_key,
                        "collapse_key": group_key,
                    }, ensure_ascii=False).encode("utf-8")
                    request = urllib.request.Request(
                        f"{api_base}/api/v1/devices/{quote(device_id, safe='')}/notify",
                        data=payload, method="POST",
                        headers={"Authorization": f"Bearer {api_token}",
                                 "Content-Type": "application/json"})
                    opener = urllib.request.build_opener(_NoRedirect())
                    with opener.open(request, timeout=4) as response:
                        accepted = int(response.status) == 200
                        detail = "core_gateway_accepted" if accepted else f"http_{response.status}"
                except urllib.error.HTTPError as exc:
                    # An explicit HTTP rejection (not a committed notification)
                    # may use the shared SQLite fallback. 5xx other than the
                    # known pre-dispatch 503 is treated as ambiguous.
                    detail = f"core_gateway:http_{exc.code}"
                    ambiguous = exc.code >= 500 and exc.code != 503
                except Exception as exc:
                    # A timeout/read failure may occur AFTER the gateway queued
                    # the event. Never invent a second event ID on this path.
                    detail = f"core_gateway:unknown_{type(exc).__name__}"
                    ambiguous = True
            if not accepted and not ambiguous:
                try:
                    import asyncio
                    notification_id = asyncio.run(gateway.notify(
                        device_id, NotificationCategory.CHAT_MESSAGE,
                        "鲸鱼有一条新发现", body, target=target,
                        group_key=group_key, collapse_key=group_key))
                    record = gateway.queue.get_delivery(device_id, notification_id)
                    accepted = bool(notification_id and record is not None)
                    detail = ("queued_for_device_sync" if accepted else
                              detail or "local_queue_failed")
                except Exception as exc:
                    detail = f"local_queue:{type(exc).__name__}"
                    accepted = False
            receipts.append(DeliveryReceipt(
                channel="whale_notification", ok=accepted,
                detail=detail or ("accepted" if accepted else "unavailable"),
                qq_online=False))
        return receipts

    def deliver_result(items, *, conversation: str = "", summary: str = "",
                       prefer_owner_dm: bool = True) -> dict:
        """统一投递：QQ online → WeQ；offline → Whale 通知/补传队列，再回落图库。"""
        from whale_core.life.result_delivery import (
            DeliveryItem, ResultDelivery, summarize_receipts)
        port = send_port_factory()
        delivery = ResultDelivery(
            port, owner_uid=cfg.owner_uid, prefer_owner_dm=prefer_owner_dm,
            whale_push=_whale_push)
        norm = []
        for it in items or ():
            if isinstance(it, DeliveryItem):
                norm.append(it)
                continue
            if not isinstance(it, dict):
                continue
            kind = str(it.get("kind") or "text").strip()
            if kind == "image":
                raw = it.get("image_bytes") or b""
                if isinstance(raw, str):
                    import base64
                    try:
                        raw = base64.b64decode(raw)
                    except Exception:
                        raw = b""
                norm.append(DeliveryItem(
                    kind="image", image_bytes=raw,
                    filename=str(it.get("filename") or "result.png")[:80]))
            elif kind == "audio":
                # 2026-11-11：语音产物投递（WAV 字节 → send_voice_to）
                raw = it.get("audio_bytes") or b""
                if isinstance(raw, str):
                    import base64
                    try:
                        raw = base64.b64decode(raw)
                    except Exception:
                        raw = b""
                norm.append(DeliveryItem(
                    kind="audio", audio_bytes=raw,
                    filename=str(it.get("filename") or "voice.wav")[:80]))
            else:
                norm.append(DeliveryItem(kind="text", text=str(it.get("text") or "")))
        receipts = delivery.deliver(
            norm, conversation=conversation, summary=summary)
        return summarize_receipts(receipts)

    def prefs() -> ProactivePrefs:
        from datetime import time as dtime
        base = ProactivePrefs(
            enabled=True, allowed_channels=frozenset({"direct_message"}),
            auto_send=True,
            daily_limit=int(env.get("DAILY_LIMIT", "10")),
            quiet_start=dtime(int(env.get("QUIET_START", "22"))),
            quiet_end=dtime(int(env.get("QUIET_END", "7"))),
            passive_daily_limit=int(env.get("PASSIVE_DAILY_LIMIT", "200")))
        return load_prefs(store, base)

    return {"companion_chat": companion_chat, "quick_chat": quick_chat,
            "followup_poll": followup_poll,
            "qq_send": qq_send, "deliver_result": deliver_result,
            "prefs": prefs, "store": store,
            "rayneo_store": rayneo_store,
            "last_chat_ms": last_chat_ms, "mark_chat": mark_chat,
            "env": env}


def _persona_card_text(store) -> str:
    """取当前人格卡文本（软提示用）：绑定优先，回退名字为 Enxi 的卡。"""
    if store is None:
        return ""
    try:
        for key in store.keys():
            if str(key).startswith("bind:persona:"):
                bind = store.get(key)
                pid = str(bind.get("persona_id") or "") if isinstance(bind, dict) else ""
                if pid:
                    card = store.get(f"persona:card:{pid}")
                    if isinstance(card, dict):
                        text = str(card.get("external_prompt") or "").strip()
                        if text:
                            return text
        for key in store.keys():
            if str(key).startswith("persona:card:"):
                card = store.get(key)
                if isinstance(card, dict) and str(card.get("name") or "") == "Enxi":
                    text = str(card.get("external_prompt") or "").strip()
                    if text:
                        return text
    except Exception:
        pass
    return ""


def _delivery_note(result: dict, items: list, say=None) -> str:
    """投递后的眼镜侧文字交代（2026-11-11 owner 决策：不静默）。

    模型自主拟一句（say=轻量脑 callable），失败/空/超长回落固定文案。
    """
    kinds = {str(it.get("kind") or "text") for it in items if isinstance(it, dict)}
    if "audio" in kinds:
        what, fallback = "一段语音", "语音发过去啦，听听看~"
    elif "image" in kinds:
        what, fallback = "一张图", "图发你手机上了，看看~"
    elif "file" in kinds:
        what, fallback = "一个文件", "文件放你 QQ 上了，回头翻~"
    else:
        what, fallback = "话", "说了哦，看看手机~"
    if not result.get("ok"):
        what, fallback = "", "刚才没发出去——你手机上 QQ 可能不在线，我记着了，等它回来再补给你~"
    if say is not None:
        try:
            # 2026-11-11 owner 决策：软提示——只陈述事实，口吻由人格卡塑造，
            # 不在提示词里教语气/给示例句。
            if result.get("ok"):
                prompt = (f"刚才你把{what}发到对方 QQ 了。跟对方交代一声"
                          "（别复述内容本身）。只回这一句。")
            else:
                prompt = ("刚才要发到对方 QQ 的东西没发出去（可能 QQ 不在线），"
                          "之后会补。跟对方交代一声。只回这一句。")
            out = str(say(prompt) or "").strip().strip("「」\"'")
            # 护栏：非空、不超过 40 字、不带引号包裹才用模型的；否则回落
            if out and len(out) <= 40 and "\n" not in out:
                return out
        except Exception:
            pass
    return fallback


def make_handler(delegates):
    from whale_core.life.initiative import ProactiveDraft, ProactiveOutbox

    class GlassesHandler(BaseHTTPRequestHandler):
        server_version = "whale-glasses/1.0"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # 静音默认访问日志（不含正文纪律）
            pass

        def _authorized(self) -> bool:
            token = getattr(self.server, "glasses_token", "")
            header = self.headers.get("Authorization", "")
            return (header.startswith("Bearer ")
                    and secrets.compare_digest(header[7:].strip(), token))

        def _read_json(self, max_bytes: int = MAX_BODY_BYTES) -> dict | None:
            self._body_too_large = False
            self._body_nbytes = 0
            lengths = self.headers.get_all("Content-Length", [])
            try:
                # No chunk decoder: reject ambiguous framing without reading it.
                if (self.headers.get("Transfer-Encoding") is not None
                        or len(lengths) != 1
                        or not lengths[0].isascii()
                        or not lengths[0].isdigit()):
                    raise ValueError("invalid body framing")
                length = int(lengths[0])
                self._body_nbytes = length
                if length <= 0:
                    raise ValueError("invalid body length")
                if length > max_bytes:
                    self._body_too_large = True
                    self.close_connection = True
                    return None
                raw = self.rfile.read(length)
                if len(raw) != length:
                    raise ValueError("incomplete body")
                data = json.loads(raw)
                if not isinstance(data, dict):
                    raise ValueError("expected json object")
                return data
            except (ValueError, UnicodeDecodeError):
                # An unread body must never become the next HTTP request.
                self.close_connection = True
                return None

        def do_GET(self):
            if self.path == "/v1/health":
                _json_response(self, 200, {"ok": True,
                                           "capabilities": ["/v1/chat", "/v1/chat/followup", "/v1/qq/reply", "/v1/result/deliver",
                                                            "/v1/rayneo/ingest", "/v1/rayneo/query", "/v1/rayneo/control"]})
                return
            if self.path == "/openapi.json":
                _json_response(self, 200, _openapi())
                return
            self.close_connection = True
            _json_response(self, 404, {"error": "not found"})

        def do_POST(self):
            if not self._authorized():
                self.close_connection = True
                _json_response(self, 401, {"error": "unauthorized"})
                return
            if self.path == "/v1/chat":
                self._chat()
                return
            if self.path == "/v1/chat/followup":
                self._chat_followup()
                return
            if self.path == "/v1/qq/reply":
                self._qq_reply()
                return
            if self.path == "/v1/result/deliver":
                self._result_deliver()
                return
            if self.path == "/v1/rayneo/ingest":
                self._rayneo_ingest()
                return
            if self.path == "/v1/rayneo/query":
                self._rayneo_query()
                return
            if self.path == "/v1/rayneo/control":
                self._rayneo_control()
                return
            self.close_connection = True
            _json_response(self, 404, {"error": "not found"})

        def _chat(self):
            body = self._read_json()
            if body is None:
                _json_response(self, 400, {"error": "invalid json body"})
                return
            text = str(body.get("text") or "").strip()
            if not text:
                _json_response(self, 400, {"error": "text required"})
                return
            text = text[:MAX_TEXT_CHARS]
            mode = str(body.get("mode") or "companion").strip()
            if mode not in ("companion", "quick"):
                _json_response(self, 400, {"error": "mode must be companion|quick"})
                return
            session_id = str(body.get("sessionId") or "").strip() or uuid.uuid4().hex
            session_id = session_id[:64]
            location = resolve_request_location(
                getattr(self.server, "glasses_env", {}) or {},
                delegates["store"], body)
            try:
                if mode == "quick":
                    reply, timing = delegates["quick_chat"](text, location=location)
                else:
                    extra = {}
                    if body.get("followup") in ("poll", True, 1, "1", "true", "yes"):
                        # 新客户端：第一条立即返回，第二条走 /v1/chat/followup 轮询。
                        extra["followup_mode"] = "poll"
                    reply, timing = delegates["companion_chat"](
                        text, conversation_id=f"glasses:{session_id}",
                        location=location, **extra)
                latency_ms = int(timing.get("totalMs") or 0)
                if not (reply or "").strip():
                    _json_response(self, 200, {"reply": None, "sessionId": session_id,
                                               "mode": mode, "error": "empty reply",
                                               "latencyMs": latency_ms,
                                               "intentMs": timing.get("intentMs", 0),
                                               "toolMs": timing.get("toolMs", 0),
                                               "replyMs": timing.get("replyMs", 0)})
                    return
                payload = {"reply": reply, "sessionId": session_id, "mode": mode}
                # 调试可观测：不回传坐标原文，只回是否已知位置；分阶段耗时便于体感核对。
                payload["locationKnown"] = bool(location)
                payload["latencyMs"] = latency_ms
                payload["intentMs"] = int(timing.get("intentMs") or 0)
                payload["toolMs"] = int(timing.get("toolMs") or 0)
                payload["replyMs"] = int(timing.get("replyMs") or 0)
                if "followup" in timing:
                    # 并行链路：第二条状态（pending=后台还在判/查；none=无第二条；
                    # ready+merged=旧客户端已合并进 reply）。
                    payload["followup"] = timing["followup"]
                    payload["followupMs"] = int(timing.get("followupMs") or 0)
                    if timing.get("replies"):
                        payload["replies"] = list(timing["replies"])
                # 眼镜/手机「做完再汇总投递」：deliverResult=true 时走统一路由。
                if body.get("deliverResult") in (True, 1, "1", "true", "yes"):
                    try:
                        delivery = delegates["deliver_result"](
                            [{"kind": "text", "text": reply}],
                            summary=f"glasses:{mode}:{session_id[:8]}")
                        payload["delivery"] = delivery
                    except Exception as exc:
                        payload["delivery"] = {
                            "ok": False, "error": type(exc).__name__}
                print(f"[glasses] mode={mode} session={session_id[:8]} "
                      f"latencyMs={latency_ms} intentMs={payload['intentMs']} "
                      f"toolMs={payload['toolMs']} replyMs={payload['replyMs']} "
                      f"followupMs={payload.get('followupMs', 0)} "
                      f"followup={(payload.get('followup') or {}).get('status', '-')} "
                      f"wait={(payload.get('followup') or {}).get('waited') or '-'} "
                      f"loc={bool(location)} "
                      f"chars_in={len(text)} chars_out={len(reply or '')}"
                      f"{' deliver=' + str(payload.get('delivery', {}).get('ok')) if 'delivery' in payload else ''}",
                      flush=True)
                _json_response(self, 200, payload)
            except Exception as exc:
                # 内容错误不当网络错误暴露给镜片（交接单 §1）
                _json_response(self, 200, {"reply": None, "sessionId": session_id,
                                           "mode": mode,
                                           "error": f"{type(exc).__name__}"})

        def _chat_followup(self):
            """并行链路第二条：``{sessionId, turnId?, waitMs?}`` → status/reply（长轮询≤25s）。"""
            body = self._read_json()
            if body is None:
                _json_response(self, 400, {"error": "invalid json body"})
                return
            session_id = str(body.get("sessionId") or "").strip()[:64]
            if not session_id:
                _json_response(self, 400, {"error": "sessionId required"})
                return
            poll = delegates.get("followup_poll")
            if poll is None:
                _json_response(self, 200, {"status": "unsupported"})
                return
            try:
                wait_ms = int(body.get("waitMs") or 0)
            except (TypeError, ValueError):
                wait_ms = 0
            out = poll(f"glasses:{session_id}", str(body.get("turnId") or "")[:32],
                       wait_ms=wait_ms)
            out["sessionId"] = session_id
            _json_response(self, 200, out)

        def _qq_reply(self):
            body = self._read_json()
            if body is None:
                _json_response(self, 400, {"error": "invalid json body"})
                return
            text = str(body.get("text") or "").strip()
            if not text:
                _json_response(self, 400, {"error": "text required"})
                return
            if len(text) > MAX_TEXT_CHARS:
                _json_response(self, 400, {"error": "text too long"})
                return
            store = delegates["store"]
            outbox = ProactiveOutbox(store)
            draft_id = f"glasses-reply:{uuid.uuid4().hex[:12]}"
            target = f"private:{getattr(self.server, 'owner_uin', '')}"
            outbox.propose(ProactiveDraft(
                draft_id=draft_id, text=text,
                reason="眼镜快捷回复（主人口述 dictation，当面发起）",
                evidence=("glasses:/v1/qq/reply",), channel="direct_message",
                target_conversation=target))
            # 风控与 runner 同口径：被动 lane（当面 dictation=reactive_inbound，
            # 静默豁免），被动日额度是防跑飞保险丝；不绕过、不自动重试。
            verdict = outbox.dispatch(
                draft_id, delegates["prefs"](), now=datetime.now(timezone.utc),
                port=_DirectPort(delegates["qq_send"]),
                response_origin="reactive_inbound")
            if verdict.status == "accepted":
                _json_response(self, 200, {"ok": True, "receipt": "sent"})
            else:
                _json_response(self, 200, {"ok": False, "error": verdict.status,
                                           "reason": verdict.reason})

        def _result_deliver(self):
            """POST /v1/result/deliver — 文字/图片统一投递。

            body: {
              text?: str,
              summary?: str,
              images?: [{filename, data_base64}],
              audio?: {filename, data_base64},   # 2026-11-11：语音（WAV）投递
              conversation?: "private:uid"|"group:code",  # 默认 owner 私聊
              preferOwnerDm?: bool  # 默认 true
            }
            """
            import base64
            body = self._read_json()
            if body is None:
                _json_response(self, 400, {"error": "invalid json body"})
                return
            items = []
            text = str(body.get("text") or "").strip()
            if text:
                items.append({"kind": "text", "text": text[:MAX_TEXT_CHARS]})
            for img in body.get("images") or []:
                if not isinstance(img, dict):
                    continue
                raw_b64 = str(img.get("data_base64") or "")
                try:
                    raw = base64.b64decode(raw_b64) if raw_b64 else b""
                except Exception:
                    raw = b""
                if not raw:
                    continue
                items.append({
                    "kind": "image",
                    "image_bytes": raw,
                    "filename": str(img.get("filename") or "result.png")[:80],
                })
            aud = body.get("audio")
            if isinstance(aud, dict):
                raw_b64 = str(aud.get("data_base64") or "")
                try:
                    raw = base64.b64decode(raw_b64) if raw_b64 else b""
                except Exception:
                    raw = b""
                if raw:
                    items.append({
                        "kind": "audio",
                        "audio_bytes": raw,
                        "filename": str(aud.get("filename") or "voice.wav")[:80],
                    })
            if not items:
                _json_response(self, 400, {"error": "text, images or audio required"})
                return
            prefer = body.get("preferOwnerDm", True)
            if prefer in (False, 0, "0", "false", "no"):
                prefer = False
            else:
                prefer = True
            try:
                result = delegates["deliver_result"](
                    items,
                    conversation=str(body.get("conversation") or ""),
                    summary=str(body.get("summary") or "")[:200],
                    prefer_owner_dm=prefer)
                # 2026-11-11 owner 决策：媒体/操作投递后，眼镜侧要给一句
                # 交代——模型自主拟，口吻由人格卡软提示塑造；失败回落固定文案。
                def _say(prompt, _store=delegates.get("store")):
                    return delegates["quick_chat"](
                        prompt, persona=_persona_card_text(_store))[0]
                result["note"] = _delivery_note(
                    result, items,
                    say=_say if delegates.get("quick_chat") else None)
                _json_response(self, 200, result)
            except Exception as exc:
                _json_response(self, 200, {"ok": False, "error": type(exc).__name__})

        def _rayneo_read(self, max_bytes: int):
            body = self._read_json(max_bytes=max_bytes)
            if body is None:
                if getattr(self, "_body_too_large", False):
                    _json_response(self, 413, {"error": "body too large"})
                else:
                    _json_response(self, 400, {"error": "invalid request"})
                return None
            return body

        def _rayneo_ingest(self):
            from whale_core.api.rayneo_http import INGEST_MAX_BYTES, handle_ingest
            body = self._rayneo_read(INGEST_MAX_BYTES)
            if body is None:
                return
            store = delegates.get("rayneo_store")
            if store is None:
                _json_response(self, 503, {"error": "store_unavailable"})
                return
            env = delegates.get("env") or getattr(self.server, "glasses_env", {}) or {}
            try:
                status, payload = handle_ingest(
                    body, store=store, env=env,
                    body_bytes=int(getattr(self, "_body_nbytes", 0) or 0))
            except Exception:
                _json_response(self, 503, {"error": "store_unavailable"})
                return
            _json_response(self, status, payload)

        def _rayneo_query(self):
            from whale_core.api.rayneo_http import QUERY_MAX_BYTES, handle_query
            body = self._rayneo_read(QUERY_MAX_BYTES)
            if body is None:
                return
            env = delegates.get("env") or getattr(self.server, "glasses_env", {}) or {}
            try:
                status, payload = handle_query(
                    body, rayneo_store=delegates.get("rayneo_store"),
                    memory_store=delegates.get("store"), env=env,
                    body_bytes=int(getattr(self, "_body_nbytes", 0) or 0))
            except Exception:
                _json_response(self, 200, {
                    "ok": True, "source": "rayneo", "instruction_eligible": False,
                    "query_disabled": False, "items": [], "derived": [],
                    "error_class": "timeout"})
                return
            _json_response(self, status, payload)

        def _rayneo_control(self):
            from whale_core.api.rayneo_http import CONTROL_MAX_BYTES, handle_control
            body = self._rayneo_read(CONTROL_MAX_BYTES)
            if body is None:
                return
            env = delegates.get("env") or getattr(self.server, "glasses_env", {}) or {}
            store = delegates.get("rayneo_store")
            if store is None:
                _json_response(self, 503, {"error": "store_unavailable"})
                return
            try:
                status, payload = handle_control(
                    body, rayneo_store=store,
                    memory_store=delegates.get("store"), env=env)
            except Exception:
                _json_response(self, 503, {"error": "store_unavailable"})
                return
            _json_response(self, status, payload)

    class _DirectPort:
        def __init__(self, send):
            self._send = send

        def send(self, draft) -> bool:
            ok, _ = self._send(draft.text)
            return ok

    return GlassesHandler


def _openapi() -> dict:
    return {
        "openapi": "3.0.0",
        "info": {"title": "whale-glasses", "version": "1.0.0"},
        "paths": {
            "/v1/chat": {"post": {"summary": "眼镜/手机文字对话（第一条秒回；"
                                             "body.followup=\"poll\" 时第二条走 /v1/chat/followup）",
                                  "parameters": [],
                                  "responses": {"200": {"description": "reply"}},
                                  "security": [{"bearerAuth": []}]}},
            "/v1/chat/followup": {"post": {
                "summary": "取并行链路第二条：{sessionId, turnId?, waitMs≤25000} → "
                           "status pending|ready|none|unknown, reply",
                "responses": {"200": {"description": "followup"}},
                "security": [{"bearerAuth": []}]}},
            "/v1/qq/reply": {"post": {"summary": "QQ 快捷回复（固定绑定对端）",
                                      "responses": {"200": {"description": "ok"}},
                                      "security": [{"bearerAuth": []}]}},
            "/v1/result/deliver": {"post": {
                "summary": "统一结果投递（QQ online / Whale 原生通知 / 手机图库）",
                "responses": {"200": {"description": "delivery receipts"}},
                "security": [{"bearerAuth": []}]}},
            "/v1/rayneo/ingest": {"post": {"summary": "雷鸟转写批量上传",
                                           "security": [{"bearerAuth": []}]}},
            "/v1/rayneo/query": {"post": {"summary": "雷鸟转写与记忆查询",
                                          "security": [{"bearerAuth": []}]}},
            "/v1/rayneo/control": {"post": {"summary": "雷鸟转写删除/开关",
                                            "security": [{"bearerAuth": []}]}},
            "/v1/health": {"get": {"summary": "存活与能力"}},
        },
        "components": {"securitySchemes": {
            "bearerAuth": {"type": "http", "scheme": "bearer"}}},
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="whale-glasses-api")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8191)
    parser.add_argument("--secrets", default=None,
                        help="env 文件路径（KEY=VALUE 行）")
    args = parser.parse_args(argv)

    env = dict(os.environ)
    if args.secrets:
        env.update(_load_env_file(args.secrets))
    cfg = GlassesConfig(env)
    missing = cfg.missing()
    if missing:
        print(f"错误：缺少配置 {', '.join(missing)}", file=sys.stderr)
        return 2

    delegates = build_delegates(env)
    handler = make_handler(delegates)
    server = ThreadingHTTPServer((args.host, args.port), handler)
    server.glasses_token = cfg.token  # type: ignore[attr-defined]
    server.owner_uin = cfg.owner_uin  # type: ignore[attr-defined]
    server.glasses_env = env  # type: ignore[attr-defined]

    greeting_loop = None
    try:
        from whale_core.life.proactive_greeting import (
            ProactiveGreetingConfig, ProactiveGreetingLoop)
        from whale_core.core.deepseek import DeepSeekCompanion
        gcfg = ProactiveGreetingConfig.from_env(env)
        if gcfg.enabled:
            judge = DeepSeekCompanion(
                model=(env.get("QQ_INTENT_MODEL") or "").strip() or None,
                base_url=(env.get("QQ_INTENT_BASE_URL") or gcfg.judge_base_url
                          ).strip() or gcfg.judge_base_url,
                api_key=(env.get("QQ_INTENT_API_KEY") or "").strip() or None)
            reply_model = DeepSeekCompanion(
                model=(env.get("DEEPSEEK_MODEL") or "").strip() or None,
                base_url=(env.get("DEEPSEEK_BASE_URL") or "").strip() or None,
                api_key=(env.get("DEEPSEEK_API_KEY") or "").strip() or None)

            def _send(draft: str) -> bool:
                ok, _ = delegates["qq_send"](draft)
                if not ok:
                    print("[proactive] send failed", flush=True)
                return bool(ok)

            def _situation() -> str:
                """从 InnerState + 近期活动拼情境；失败则空串（判断端默认谨慎）。"""
                try:
                    from whale_core.life.state import InnerStateStore
                    store = delegates.get("store")
                    if store is None:
                        return ""
                    g = InnerStateStore(store).load()
                    bits = []
                    if g.style and g.style != "neutral":
                        bits.append(f"风格={g.style}")
                    if g.interests:
                        bits.append("兴趣=" + "、".join(g.interests[-3:]))
                    if g.unfinished_topics:
                        bits.append("未完=" + "；".join(g.unfinished_topics[-2:]))
                    return "；".join(bits)
                except Exception:
                    return ""

            greeting_loop = ProactiveGreetingLoop(
                config=gcfg, judge=judge, reply_model=reply_model, send=_send,
                situation_provider=_situation,
                store=delegates.get("store"))
            if greeting_loop.start_background():
                print(f"proactive greeting loop on interval={gcfg.interval_s}s "
                      f"dry_run={gcfg.dry_run} cooldown={gcfg.cooldown_s}s "
                      f"quota={gcfg.daily_quota}", flush=True)
    except Exception as exc:  # noqa: BLE001 — 问候循环失败不阻止主服务
        print(f"[proactive] not started: {type(exc).__name__}", flush=True)

    summarizer = None
    try:
        from whale_core.api.rayneo_http import env_flag
        if env_flag(env, "WHALE_RAYNEO_SUMMARY", False) and delegates.get("rayneo_store") is not None:
            from whale_core.life.rayneo_summarizer import RayneoSummarizer
            summarizer = RayneoSummarizer.from_env(
                env, delegates["rayneo_store"], delegates["store"],
                last_chat_ms=delegates.get("last_chat_ms"))
            if summarizer.start_background():
                print("[rayneo] summarizer started", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[rayneo] summarizer not started: {type(exc).__name__}", flush=True)

    print(f"whale-glasses-api 监听 http://{args.host}:{args.port}"
          f"（/v1/chat /v1/chat/followup /v1/qq/reply /v1/result/deliver "
          f"/v1/rayneo/ingest /v1/rayneo/query /v1/rayneo/control /v1/health /openapi.json）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if greeting_loop is not None:
            greeting_loop.stop_background()
        if summarizer is not None:
            summarizer.stop_background()
        try:
            rs = delegates.get("rayneo_store")
            if rs is not None:
                rs.close()
        except Exception:
            pass
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
