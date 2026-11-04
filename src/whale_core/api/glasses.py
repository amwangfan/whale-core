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

    def companion_chat(text: str, conversation_id: str) -> str:
        from whale_core.core.companion import WhaleCompanion
        from whale_core.memory.curation import MemoryCurator
        companion = WhaleCompanion(store, brain_factory(),
                                   curator=MemoryCurator(store))
        # context=None：沿用全局活跃角色卡（persona:active_card），与
        # 控制台/兼容期同口径；会话历史与记忆按 glasses:<sessionId> 隔离连续。
        return companion.chat(text, conversation_id=conversation_id)

    def quick_chat(text: str) -> str:
        return quick_brain_factory().reply(
            text, persona="用一两句口语简明回答，不展开、不追问。")

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
            try:
                if mode == "quick":
                    reply = delegates["quick_chat"](text)
                else:
                    reply = delegates["companion_chat"](
                        text, conversation_id=f"glasses:{session_id}")
                if not (reply or "").strip():
                    _json_response(self, 200, {"reply": None, "sessionId": session_id,
                                               "mode": mode, "error": "empty reply"})
                    return
                _json_response(self, 200, {"reply": reply, "sessionId": session_id,
                                           "mode": mode})
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
    print(f"whale-glasses-api 监听 http://{args.host}:{args.port}"
          f"（/v1/chat /v1/qq/reply /v1/health /openapi.json）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
