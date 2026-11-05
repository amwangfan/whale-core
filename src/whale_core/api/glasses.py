"""眼镜/手机轻量 API（2026-11-03 交接单 handoff-whale-core-glasses-chat.md）。

两个端点：

- ``POST /v1/chat``：眼镜/手机文字对话。``companion``（默认）走
  WhaleCompanion（人格 + 记忆召回 + 情节回写），与 QQ/电话共享同一份
  sqlite 记忆库——眼镜上聊的换个端能接上；``quick`` 走轻量模型
  （WHALE_QUICK_* / QQ_INTENT_*），**不走记忆库、不写长期记忆**。
- ``POST /v1/qq/reply``：QQ 快捷回复——把用户口述的文本发给**已配置的
  绑定对端**（目标不从请求推导，防误发）；走 WeQ 发送链与既有风控
  （被动额度/静默时段豁免口径与 runner 一致：主人当面 dictation 属被动延续）。

鉴权：``Authorization: Bearer <WHALE_GLASSES_TOKEN>``（phone 角色专用令牌，
与管理面 token 分离；常量时间比较）。无/错 token → 401。

部署纪律：只监听 127.0.0.1（对外暴露由反代/隧道层负责）；令牌只走环境变量；
本服务与 qq-bot runner 共享同一 sqlite（WAL，approve 子命令同款跨进程先例）。
"""
from __future__ import annotations

import argparse
import threading
import json
import os
import secrets
import sys
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MAX_BODY_BYTES = 64 * 1024

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



def install_request_location_intent_wrapper(orch, request_location: dict) -> None:
    """把「当前请求位置」注入 orch._intent_context；全生命周期只包装一次。

    ``request_location`` 是可变 dict，键 ``value`` 由每轮 companion_chat 写入。
    若每次请求都重新包装，闭包会嵌套捕获旧位置，导致东京→大阪仍含东京。
    """
    if orch is None or getattr(orch, "_glasses_loc_wrapped", False):
        return
    _orig_ctx = orch._intent_context

    def _intent_context_with_location(conversation_id, text, *, allowed_spaces=None):
        base = _orig_ctx(conversation_id, text, allowed_spaces=allowed_spaces)
        from whale_core.core.prompt_composer import format_location_context
        loc_line = format_location_context(
            location=request_location.get("value") or None)
        return (loc_line + ("\n" + base if base else ""))[:1500]

    orch._intent_context = _intent_context_with_location  # type: ignore
    orch._glasses_loc_wrapped = True  # type: ignore[attr-defined]


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
        return WhaleCompanion(store, brain_factory(),
                              curator=MemoryCurator(store))

    # 单 owner 通道：复用 companion/orchestrator，避免每请求重建搜索/shell 插件。
    # ThreadingHTTPServer 下用锁串行化一轮 handle，避免改 location / intent 上下文竞态。
    _orch_lock = threading.Lock()
    _cached: dict = {"companion": None, "orch": None, "quick": None}
    # 当前请求位置：由 companion_chat 每轮写入；intent 包装只装一次并读取此状态。
    _request_location: dict = {"value": ""}

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
        # SearchIntentRouter：规则未命中的闲聊默认 kind=chat 跳过意图 LLM；
        # 仅模糊写/媒体等 toolish 才会打 kimi；显式 search/shell 规则零 LLM。
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
                         or env.get("QQ_INTENT_API_KEY") or "").strip() or None)
        intent = SearchIntentRouter(
            model=intent_brain, enable_llm=enable_llm, tools=tools)
        return ConversationOrchestrator(
            store, companion, intent_model=intent, tools=tools, gateway=gateway)

    def companion_chat(text: str, conversation_id: str,
                       location: str = "") -> tuple[str, dict]:
        """返回 (reply, timing)；timing 含 intentMs/toolMs/replyMs/totalMs。"""
        from whale_core.core.runtime import InputEnvelope
        import time as _time
        t_all = _time.perf_counter()
        with _orch_lock:
            if _cached["companion"] is None:
                companion = _make_companion()
                companion.planning_hint = _GLASSES_LOCAL_LIFE_HINT
                _cached["companion"] = companion
                _cached["orch"] = _build_search_orchestrator(companion)
            companion = _cached["companion"]
            orch = _cached["orch"]
            brain = companion.model
            try:
                brain.default_location = location or None
            except Exception:
                pass
            if orch is not None:
                # 把位置摘要塞进意图上下文：SearchIntentRouter 本地生活规则会读
                # 「当前位置：」行；LLM 规划也能把地点当缺省。
                # 包装只安装一次；每轮只更新当前请求位置，避免层层嵌套。
                _request_location["value"] = location or ""
                install_request_location_intent_wrapper(orch, _request_location)
                result = orch.handle_envelope(InputEnvelope(
                    conversation_id=conversation_id, text=text,
                    source="glasses:phone", channel="glasses",
                    owner_id=cfg.owner_uin or cfg.owner_uid))
                timing = {
                    "intentMs": int(getattr(result, "intent_ms", 0) or 0),
                    "toolMs": int(getattr(result, "tool_ms", 0) or 0),
                    "replyMs": int(getattr(result, "reply_ms", 0) or 0),
                    "totalMs": int((_time.perf_counter() - t_all) * 1000),
                }
                return result.reply, timing
            # 无搜索 provider：退回纯陪伴（仍注入位置到 system）。
            reply = companion.chat(
                text, conversation_id=conversation_id, location=location or None)
            total = int((_time.perf_counter() - t_all) * 1000)
            return reply, {"intentMs": 0, "toolMs": 0, "replyMs": total, "totalMs": total}

    def quick_chat(text: str, location: str = "") -> tuple[str, dict]:
        import time as _time
        import inspect
        t0 = _time.perf_counter()
        with _orch_lock:
            if _cached["quick"] is None:
                _cached["quick"] = quick_brain_factory()
            brain = _cached["quick"]
            try:
                brain.default_location = location or None
            except Exception:
                pass
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
            "qq_send": qq_send, "prefs": prefs, "store": store}


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

        def _read_json(self) -> dict | None:
            lengths = self.headers.get_all("Content-Length", [])
            try:
                # No chunk decoder: reject ambiguous framing without reading it.
                if (self.headers.get("Transfer-Encoding") is not None
                        or len(lengths) != 1
                        or not lengths[0].isascii()
                        or not lengths[0].isdigit()):
                    raise ValueError("invalid body framing")
                length = int(lengths[0])
                if length <= 0 or length > MAX_BODY_BYTES:
                    raise ValueError("invalid body length")
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
                                           "capabilities": ["/v1/chat", "/v1/qq/reply"]})
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
            if self.path == "/v1/qq/reply":
                self._qq_reply()
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
                    reply, timing = delegates["companion_chat"](
                        text, conversation_id=f"glasses:{session_id}",
                        location=location)
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
                print(f"[glasses] mode={mode} session={session_id[:8]} "
                      f"latencyMs={latency_ms} intentMs={payload['intentMs']} "
                      f"toolMs={payload['toolMs']} replyMs={payload['replyMs']} "
                      f"loc={bool(location)} "
                      f"chars_in={len(text)} chars_out={len(reply or '')}",
                      flush=True)
                _json_response(self, 200, payload)
            except Exception as exc:
                # 内容错误不当网络错误暴露给镜片（交接单 §1）
                _json_response(self, 200, {"reply": None, "sessionId": session_id,
                                           "mode": mode,
                                           "error": f"{type(exc).__name__}"})

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
            "/v1/chat": {"post": {"summary": "眼镜/手机文字对话",
                                  "parameters": [],
                                  "responses": {"200": {"description": "reply"}},
                                  "security": [{"bearerAuth": []}]}},
            "/v1/qq/reply": {"post": {"summary": "QQ 快捷回复（固定绑定对端）",
                                      "responses": {"200": {"description": "ok"}},
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

    print(f"whale-glasses-api 监听 http://{args.host}:{args.port}"
          f"（/v1/chat /v1/qq/reply /v1/health /openapi.json）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if greeting_loop is not None:
            greeting_loop.stop_background()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
