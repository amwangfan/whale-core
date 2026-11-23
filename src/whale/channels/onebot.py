"""OneBot v11 backend（QQ 渠道的默认 backend）：HTTP API + 上报 webhook + 正向 WS。

QQ 语义（群模式/插话判断/气泡/通知）在 ``channels.qq.QQChannel``；本模块只负责
OneBot 线格式（CQ 码解析）、传输（HTTP/WS）与 webhook 鉴权。插件
``plugins/builtin/qq`` 托管的本渠道行为契约见 tests/test_plugins.py。

叠加 legacy weq_channel 的可移植纪律：端点构造期校验（``validate_endpoints``）——
只允许 http(s)/ws(s)，URL 不得内嵌凭据、不得带 query/fragment；凭据只走 secrets，
经 Authorization 头发出，永不出现在 URL、日志与异常文本里。
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import re
from collections import deque
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from ..kernel.errors import WhaleError
from ..kernel.http import http_client, status_error, transport_error
from ..kernel.ids import ulid
from .base import Sink
from .qq import (GROUP_GUIDE, JOIN_NOTE, JUDGE_SYSTEM, PRIVATE_GUIDE, NameDirectory, QQChannel,  # noqa: F401
                 QQEvent, _hard_chunks, clean_name, meaningful, plain, split_bubbles)

log = logging.getLogger("whale.channels.onebot")

SERVICE = "OneBot"
MAX_TEXT = 4000
MAX_BODY = 1_000_000
LOOPBACK = {"127.0.0.1", "::1", "localhost"}


# -- endpoint validation (ported from legacy weq_channel/weq_send.py) -------------------------
def validate_endpoints(api_base: str = "", ws_url: str = "") -> tuple[str, str]:
    """构造期校验入/出站端点；不安全即 WhaleError(invalid_params)，错误文本不含凭据。

    - scheme 只允许 http(s)（api_base）与 ws(s)（ws_url）；
    - 不得内嵌凭据（user:pass@host）；
    - api_base 不得带 query/fragment（token 只允许走 Authorization 头/secrets）；
    - 远程明文 http 只警告不拒绝：OneBot 协议端通常在同机/内网（tailnet）。
    """
    api_base = (api_base or "").strip().rstrip("/")
    ws_url = (ws_url or "").strip()
    if api_base:
        parts = urlsplit(api_base)
        if parts.scheme not in ("http", "https"):
            raise WhaleError("invalid_params", "api_base 必须是 http(s) 地址")
        if parts.username is not None or parts.password is not None:
            raise WhaleError("invalid_params", "api_base 不得内嵌凭据（token 请放在渠道密钥里）")
        if parts.query or parts.fragment:
            raise WhaleError("invalid_params", "api_base 不得带 query/fragment")
        if parts.scheme == "http" and (parts.hostname or "") not in LOOPBACK:
            log.info("onebot api_base uses plaintext http to a non-loopback host; keep it on a trusted network")
    if ws_url:
        parts = urlsplit(ws_url)
        if parts.scheme not in ("ws", "wss"):
            raise WhaleError("invalid_params", "ws_url 必须是 ws(s) 地址")
        if parts.username is not None or parts.password is not None:
            raise WhaleError("invalid_params", "ws_url 不得内嵌凭据（token 请放在渠道密钥里）")
    return api_base, ws_url


# -- OneBot 线格式：CQ 码解析 ------------------------------------------------------------------
_CQ = re.compile(r"\[CQ:([A-Za-z0-9_.\-]+)((?:,[^\]]*)?)\]")
_PLACEHOLDER = {"record": "[语音]", "video": "[视频]", "file": "[文件]", "json": "[卡片]", "xml": "[卡片]",
                "mface": "[表情]", "dice": "[骰子]", "rps": "[猜拳]", "forward": "[聊天记录]", "poke": "[戳一戳]"}


def cq_unescape(text: str) -> str:
    return text.replace("&#91;", "[").replace("&#93;", "]").replace("&#44;", ",").replace("&amp;", "&")


def parse_cq(raw: str) -> list[dict[str, Any]]:
    """CQ 码字符串 → OneBot 消息段数组。"""
    segs: list[dict[str, Any]] = []
    pos = 0
    for m in _CQ.finditer(raw):
        if m.start() > pos:
            segs.append({"type": "text", "data": {"text": cq_unescape(raw[pos:m.start()])}})
        data: dict[str, str] = {}
        for pair in m.group(2).lstrip(",").split(","):
            if "=" in pair:
                k, v = pair.split("=", 1)
                data[k.strip()] = cq_unescape(v)
        segs.append({"type": m.group(1), "data": data})
        pos = m.end()
    if pos < len(raw):
        segs.append({"type": "text", "data": {"text": cq_unescape(raw[pos:])}})
    return segs


@dataclass
class Parsed:
    text: str = ""
    images: list[str] = field(default_factory=list)
    mentions: list[str] = field(default_factory=list)
    reply_id: str = ""
    mentioned: bool = False


def parse_message(message: Any, bot_qq: str = "") -> Parsed:
    segs = parse_cq(message) if isinstance(message, str) else (message if isinstance(message, list) else [])
    out = Parsed()
    parts: list[str] = []
    for seg in segs:
        if not isinstance(seg, dict):
            continue
        kind = str(seg.get("type") or "")
        data = seg.get("data") if isinstance(seg.get("data"), dict) else {}
        if kind == "text":
            parts.append(str(data.get("text") or ""))
        elif kind == "at":
            qq = str(data.get("qq") or "")
            out.mentions.append(qq)
            if bot_qq and qq == bot_qq:
                out.mentioned = True
            elif qq == "all":
                parts.append("@全体成员 ")
            elif qq:
                parts.append(f"@{data.get('name') or qq} ")
        elif kind == "image":
            url = str(data.get("url") or "")
            if not url.startswith(("http://", "https://")):
                url = str(data.get("file") or "")
            if url.startswith(("http://", "https://")) and len(out.images) < 4:
                out.images.append(url)
        elif kind == "reply":
            out.reply_id = str(data.get("id") or "")
        elif kind in _PLACEHOLDER:
            parts.append(_PLACEHOLDER[kind])
    out.text = re.sub(r"[ \t]+", " ", "".join(parts)).strip()[:MAX_TEXT]
    return out


async def default_ws_connect(url: str, headers: dict[str, str]) -> Any:
    import websockets

    try:
        return websockets.connect(url, additional_headers=headers, max_size=8 * 1024 * 1024)
    except TypeError:  # websockets < 14
        return websockets.connect(url, extra_headers=headers, max_size=8 * 1024 * 1024)


class OneBotChannel(QQChannel):
    """QQ over OneBot v11：入站 webhook/正向 WS → 归一化 QQEvent → QQChannel.ingest。"""

    kind = "onebot"
    backend_name = "onebot"

    def __init__(self, app: Any, channel_id: str, config: dict[str, Any], *, token: str = "",
                 sink: Sink | None = None) -> None:
        super().__init__(app, channel_id, config, token=token, sink=sink)
        cfg = config
        self.api_base, self.ws_url = validate_endpoints(str(cfg.get("api_base") or ""),
                                                        str(cfg.get("ws_url") or ""))
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        self.http = http_client(self.app, timeout=15, headers=headers) if self.api_base else None
        self.ws: Any = None
        self.ws_connect = getattr(self.app, "ws_connect", None) or default_ws_connect  # injectable (tests)
        self.ws_task: asyncio.Task[Any] | None = None
        self.pending: dict[str, asyncio.Future[Any]] = {}
        self.sent_ids: deque[str] = deque(maxlen=500)
        self.closed = False

    @property
    def bot_qq(self) -> str:           # 兼容旧引用；权威字段是 QQChannel.bot_uin
        return self.bot_uin

    # -- lifecycle --------------------------------------------------------------------
    async def open(self) -> None:
        await super().open()           # qq.send_* 能力 + 主人通知出口
        sink = self.sink

        @sink.webhook(sink.hook_path)
        async def onebot(request: Request) -> Response:
            return await self.webhook(request)

        if self.ws_url:
            self.ws_task = asyncio.create_task(self.ws_loop(), name=f"onebot-ws:{self.id}")
        self.connected = self.http is not None or self.ws_url is not None

    async def close(self) -> None:
        self.closed = True
        for task in [self.ws_task, *self._tasks]:
            if task is not None and not task.done():
                task.cancel()
        if self.ws_task is not None:
            try:
                await self.ws_task
            except (asyncio.CancelledError, Exception):
                pass
        if self.http is not None:
            await self.http.aclose()
        self.connected = False

    def status(self) -> dict[str, Any]:
        return {**super().status(),
                "via": [x for x, on in (("http_api", self.http is not None), ("ws", self.ws_url is not None),
                                        ("webhook", True)) if on],
                "ws_connected": self.ws is not None}

    # -- OneBot actions（backend 发送面） ---------------------------------------------------------
    async def call(self, action: str, params: dict[str, Any], timeout: float = 15.0) -> Any:
        if self.http is not None:
            try:
                resp = await self.http.post(f"{self.api_base}/{action}", json=params, timeout=timeout)
            except Exception as exc:
                raise transport_error(SERVICE, exc) from None
            if resp.status_code >= 400:
                raise status_error(SERVICE, resp.status_code)
            try:
                body = resp.json()
            except ValueError:
                raise WhaleError("unavailable", f"{SERVICE} {action} 返回了非 JSON 内容") from None
        elif self.ws is not None:
            echo = ulid()
            fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
            self.pending[echo] = fut
            try:
                await self.ws.send(json.dumps({"action": action, "params": params, "echo": echo},
                                              ensure_ascii=False))
                body = await asyncio.wait_for(fut, timeout)
            except asyncio.TimeoutError:
                raise WhaleError("timeout", f"{SERVICE} {action} 超时") from None
            finally:
                self.pending.pop(echo, None)
        else:
            raise WhaleError("unavailable", "QQ 未连接：没有配置 HTTP API，正向 WS 也未连上", retryable=True)
        if not isinstance(body, dict):
            raise WhaleError("unavailable", f"{SERVICE} {action} 返回格式异常")
        retcode = body.get("retcode")
        if body.get("status") in ("ok", "async") or retcode in (0, 1):
            return body.get("data")
        why = body.get("wording") or body.get("message") or body.get("msg") or ""
        raise WhaleError("unavailable", f"{SERVICE} {action} 失败（retcode={retcode}）{why}"[:240],
                         details={"retcode": retcode})

    async def send(self, kind: str, target: str, text: str, reply_to: str = "") -> dict[str, Any]:
        """发送一条（可能拆成多个气泡的）消息；回执如实反映送到了几条。"""
        text = plain(text)
        bubbles = split_bubbles(text, group=kind == "group") if self.reply_split else _hard_chunks(text)
        action = "send_group_msg" if kind == "group" else "send_private_msg"
        key = "group_id" if kind == "group" else "user_id"
        receipt: dict[str, Any] = {"status": "ok", "sent": 0, "total": len(bubbles), "message_ids": [],
                                   "error": ""}
        if not bubbles:
            receipt.update(status="error", error="消息为空")
            return receipt
        for n, bubble in enumerate(bubbles):
            if n and self.bubble_delay:
                await asyncio.sleep(min(3.0, self.bubble_delay * (0.5 + len(bubble) / 40)))
            segs: list[dict[str, Any]] = []
            if n == 0 and reply_to:
                segs.append({"type": "reply", "data": {"id": reply_to}})
            segs.append({"type": "text", "data": {"text": bubble}})
            try:
                data = await self.call(action, {key: int(target), "message": segs})
            except WhaleError as exc:
                receipt["error"] = exc.message
                # 超时 = 协议端可能已发出：unknown，不重试；明确失败 = error
                receipt["status"] = "unknown" if exc.code == "timeout" else "error"
                break
            receipt["sent"] += 1
            mid = data.get("message_id") if isinstance(data, dict) else None
            if mid is not None:
                receipt["message_ids"].append(str(mid))
                self.sent_ids.append(str(mid))
        if receipt["status"] != "ok":
            self.log.warning("onebot %s to %s: %s (%d/%d bubbles)", action, target, receipt["status"],
                             receipt["sent"], receipt["total"])
        await self.app.bus.emit("qq.delivery", {"channel": self.id, "kind": kind, "target": target, **receipt})
        return receipt

    async def send_text(self, kind: str, target_uin: str, target_uid: str, text: str,
                        reply_to: str = "", at: dict[str, Any] | None = None) -> dict[str, Any]:
        return await self.send(kind, target_uin, text, reply_to)   # onebot 用 reply 元素，at 忽略

    async def send_image(self, kind: str, target: str, data: bytes, mime: str = "image/png") -> dict[str, Any]:
        """OneBot 图片段（base64，NapCat 类协议端支持）；回执三态与 send() 同口径。"""
        import base64

        action = "send_group_msg" if kind == "group" else "send_private_msg"
        key = "group_id" if kind == "group" else "user_id"
        segment = [{"type": "image", "data": {"file": "base64://" + base64.b64encode(data).decode()}}]
        receipt: dict[str, Any] = {"status": "ok", "sent": 0, "total": 1, "message_ids": [], "error": ""}
        try:
            res = await self.call(action, {key: int(target), "message": segment})
        except WhaleError as exc:
            # 超时 = 协议端可能已发出：unknown，不重试；明确失败 = error
            receipt.update(status="unknown" if exc.code == "timeout" else "error", error=exc.message)
            await self.app.bus.emit("qq.delivery", {"channel": self.id, "kind": f"{kind}:image",
                                                    "target": target, **receipt})
            return receipt
        receipt["sent"] = 1
        mid = res.get("message_id") if isinstance(res, dict) else None
        if mid is not None:
            receipt["message_ids"].append(str(mid))
        await self.app.bus.emit("qq.delivery", {"channel": self.id, "kind": f"{kind}:image",
                                                "target": target, **receipt})
        return receipt

    async def get_msg(self, message_id: str) -> dict[str, Any] | None:
        try:
            data = await self.call("get_msg", {"message_id": int(message_id) if message_id.lstrip("-").isdigit()
                                               else message_id}, timeout=5)
        except WhaleError:
            return None
        return data if isinstance(data, dict) else None

    async def quoted_text(self, ev: QQEvent) -> str:
        if not ev.reply_id:
            return ""
        msg = await self.get_msg(ev.reply_id)
        if not msg:
            return ""
        quoted = parse_message(msg.get("message") if msg.get("message") is not None
                               else msg.get("raw_message") or "", self.bot_uin).text
        return f"（引用：{quoted[:120]}）\n" if quoted else ""

    async def _fetch_name(self, kind: str, uid: str, gid: str = "") -> str:
        """NameDirectory 的 fetch：OneBot 只读查询，失败抛错由目录兜底为空。"""
        if kind == "group" and gid:
            data = await self.call("get_group_member_info", {"group_id": int(gid), "user_id": int(uid)},
                                   timeout=5)
            if isinstance(data, dict):
                return str(data.get("card") or data.get("nickname") or "")
            return ""
        data = await self.call("get_stranger_info", {"user_id": int(uid)}, timeout=5)
        return str(data.get("nickname") or "") if isinstance(data, dict) else ""

    async def _is_bot_message(self, message_id: str) -> bool:
        if not message_id:
            return False
        if message_id in self.sent_ids:
            return True
        msg = await self.get_msg(message_id)
        if not msg or not self.bot_uin:
            return False
        sender = msg.get("sender") if isinstance(msg.get("sender"), dict) else {}
        return str(sender.get("user_id") or msg.get("user_id") or "") == self.bot_uin

    # -- 入站归一化 ------------------------------------------------------------------------------
    async def handle_event(self, ev: Any) -> str:
        """处理一个 OneBot 事件，返回决策（便于观测与测试）。"""
        if not isinstance(ev, dict) or ev.get("post_type") != "message":
            return "ignored"
        self_id = str(ev.get("self_id") or "")
        if self_id:
            self.learn_bot_uin(self_id)
        uid = str(ev.get("user_id") or "")
        mtype = str(ev.get("message_type") or "")
        mid = str(ev.get("message_id") or "")
        message = ev.get("message")
        parsed = parse_message(message if message is not None else ev.get("raw_message") or "", self.bot_uin)
        sender = ev.get("sender") if isinstance(ev.get("sender"), dict) else {}
        nick = str(sender.get("card") or sender.get("nickname") or "")[:32]
        if mtype == "private":
            qev = QQEvent(kind="private", message_id=mid, uin=uid, nick=nick, text=parsed.text,
                          images=parsed.images, reply_id=parsed.reply_id)
        elif mtype == "group":
            reply_bot = False if parsed.mentioned else await self._is_bot_message(parsed.reply_id)
            qev = QQEvent(kind="group", message_id=mid, uin=uid, nick=nick, text=parsed.text,
                          images=parsed.images, group_id=str(ev.get("group_id") or ""),
                          mentioned=parsed.mentioned, reply_id=parsed.reply_id, reply_to_bot=reply_bot)
            try:
                qev.meta["sent_at"] = int(ev["time"]) * 1000   # OneBot ``time`` is Unix seconds
            except (KeyError, TypeError, ValueError):
                pass
        else:
            return "ignored"
        return await self.ingest(qev)

    # -- HTTP webhook -----------------------------------------------------------------------------
    def authorized(self, request: Request, body: bytes) -> bool:
        if not self.token:
            host = request.client.host if request.client else ""
            return host in LOOPBACK
        auth = request.headers.get("authorization", "")
        scheme, _, cred = auth.partition(" ")
        if scheme.lower() in ("bearer", "token") and hmac.compare_digest(cred.strip(), self.token):
            return True
        qtoken = request.query_params.get("access_token", "")
        if qtoken and hmac.compare_digest(qtoken, self.token):
            return True
        sig = request.headers.get("x-signature", "")
        if sig.startswith("sha1="):
            want = hmac.new(self.token.encode(), body, hashlib.sha1).hexdigest()
            return hmac.compare_digest(sig[5:], want)
        return False

    async def webhook(self, request: Request) -> Response:
        if request.method == "GET":
            return Response(status_code=204)
        body = await request.body()
        if len(body) > MAX_BODY:
            return JSONResponse({"ok": False, "error": {"code": "invalid_params", "message": "body too large"}},
                                status_code=413)
        if not self.authorized(request, body):
            return JSONResponse({"ok": False, "error": {"code": "unauthorized", "message": "invalid token"}},
                                status_code=401)
        try:
            event = json.loads(body or b"{}")
        except ValueError:
            return JSONResponse({"ok": False, "error": {"code": "invalid_params", "message": "bad JSON"}},
                                status_code=400)
        self.spawn(self.handle_event(event))  # 立即应答，协议端不等 Agent
        return Response(status_code=204)

    # -- forward WebSocket ------------------------------------------------------------------------
    def on_ws_frame(self, raw: Any) -> None:
        try:
            frame = json.loads(raw)
        except (TypeError, ValueError):
            return
        if not isinstance(frame, dict):
            return
        echo = frame.get("echo")
        if echo is not None and str(echo) in self.pending:
            fut = self.pending[str(echo)]
            if not fut.done():
                fut.set_result(frame)
        elif "post_type" in frame:
            self.spawn(self.handle_event(frame))

    async def ws_loop(self) -> None:
        delay = 1.0
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        while not self.closed:
            try:
                async with await self.ws_connect(self.ws_url, headers) as ws:
                    self.ws = ws
                    delay = 1.0
                    self.last_error = ""
                    self.log.info("onebot ws connected (%s)", self.id)
                    async for raw in ws:
                        self.on_ws_frame(raw)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = repr(exc)[:200]
                self.log.warning("onebot ws error: %s", self.last_error)
            finally:
                self.ws = None
                for fut in self.pending.values():
                    if not fut.done():
                        fut.set_exception(WhaleError("unavailable", "OneBot WS 断开"))
            if self.closed:
                break
            await asyncio.sleep(delay)
            delay = min(30.0, delay * 2)
