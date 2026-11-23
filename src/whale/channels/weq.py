"""WeQ backend（QQ 渠道的可选 backend）：现网 WeQ 桌面进程栈无缝接入。

入站：WeQ 桌面端把新消息 POST 到本渠道的 webhook（``/api/hooks/channel-<id>/inbound``，
载荷 ``{events:[...]}``，Bearer 恒定时间鉴权，未配置 token 拒绝启动 = fail closed；
至多一次投递由 QQChannel.ingest 的 msgId 去重兜底）。
出站：WeQ 内置 Web 服务的用户界面同款 tRPC（``/_auth/login`` + ``/trpc/account.sendElements``
等）——等价于用户自己操作自己的 WeQ；token 由用户从 WeQ 设置复制注入（secrets），不破解、
不复用助手令牌、不绕过任何权限门。

移植自 legacy ``plugins/weq_channel`` 的安全纪律（weq_send.py / wq_push.py）：
- 地址构造期校验：仅 http/https；回环允许 HTTP，**远程必须 HTTPS**；拒绝 URL 内嵌凭据；
  不得带 query/fragment；HTTP 重定向一律不跟随（凭据不外流到未授权主机）；
- 目标校验：私聊目标只认 ``u_`` 前缀 uid（来自入站事件/缓存映射，不从模型输出动态指定）；
  群目标必须在 allowed_groups 白名单内；
- 回执门闩：ok 必须带对应 sequence>0（假成功如实打回 error）；超时记 unknown 不重试；
- uid↔uin 同源映射入档（kv 缓存），capability 只按 uin 调用，无映射如实报错；
- 图片 rkey 会过期：入站 pic 候选链按活 rkey 重建（读 WeQ 配置目录，只读不写）；
  无活链就不传图，绝不伪造 URL。

完整模式窗口按需化（移植自 legacy ``full_mode_window.py`` 纪律，异步化）：
- 收消息与 attach 无关（webhook 推送），只有**发送**需要完整模式窗口在线；
- 发送前自动开窗：``setAutoAttachQq(true)`` → 轮询 ``getFullModeState.ready`` →
  grace 等收割 → 落 state_file；空闲超时自动关窗 ``setAutoAttachQq(false)``；
- 红线：开窗失败**不阻断**发送尝试（内层如实回错记 error/unknown，不重试）；
  token 不进日志/异常/state_file（文件里只有 epoch 秒）；开关以 state_file 为准；
- 服务启动即基线关窗（幂等）；每日开窗计数超阈值给一行警告。
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from ..kernel.errors import WhaleError
from ..kernel.http import http_client, is_private_http
from .base import Sink
from .qq import QQChannel, QQEvent, plain, split_bubbles

log = logging.getLogger("whale.channels.weq")

SERVICE = "WeQ"
MAX_BODY = 4_000_000
MAX_TEXT = 4000
UID_RE = re.compile(r"^u_[A-Za-z0-9_-]{2,62}$")

# 完整模式窗口（按需开关）：tRPC 路径与节奏参数
TRPC_FULL_MODE_STATE = "/trpc/bootstrap.getFullModeState"
TRPC_SET_AUTO_ATTACH = "/trpc/bootstrap.setAutoAttachQq"
WINDOW_READY_TIMEOUT_S = 25.0
WINDOW_GRACE_S = 4.0            # 冷启动首窗全量收割 ~4.7s，跑在就绪前会丢发
WINDOW_POLL_S = 1.0
WINDOW_CLOSER_TICK_S = 30.0
WINDOW_DAILY_OPEN_WARN = 40


def validate_weq_url(raw: str) -> str:
    """构造期校验 WeQ Web 服务地址；不安全即 WhaleError(invalid_params)，错误文本不含凭据。"""
    url = (raw or "").strip().rstrip("/")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise WhaleError("invalid_params", "weq_web_url 必须是 http(s) 地址")
    if parts.username is not None or parts.password is not None:
        raise WhaleError("invalid_params", "weq_web_url 不得内嵌凭据（token 请放在渠道密钥里）")
    if parts.query or parts.fragment:
        raise WhaleError("invalid_params", "weq_web_url 不得带 query/fragment")
    if parts.scheme == "http" and not is_private_http(url):
        raise WhaleError("invalid_params", "weq_web_url 远程地址必须 HTTPS（http 仅限本机/内网）")
    return url


def accept_send_outcome(outcome: Any, *, peer_type: str) -> dict[str, Any]:
    """发送回执门闩：ok 且对应 sequence>0 才算成功（假成功打回 error）。"""
    if not isinstance(outcome, dict):
        return {"ok": False, "error": "invalid-outcome"}
    out = dict(outcome)
    if not out.get("ok"):
        return out
    pt = str(peer_type or "").strip().lower()
    if pt == "group":
        ok = int(out.get("groupSequence") or 0) > 0
    else:
        ok = int(out.get("privateSequence") or 0) > 0
    if not ok:
        out["ok"] = False
        out["error"] = "sequence-missing"
    return out


# -- WeQ Web tRPC 出站端口 ---------------------------------------------------------------------------
class WeQSendPort:
    """WeQ 内置 Web tRPC 出站（异步 httpx；``app.http_transport`` 可注入，测试零网络）。"""

    def __init__(self, app: Any, base_url: str, token: str, *, expected_account_uin: str = "",
                 expected_account_config_id: str = "") -> None:
        if not token:
            raise WhaleError("invalid_params", "weq access_token 未配置（fail closed）")
        self.base_url = validate_weq_url(base_url)
        self.token = token
        self.expected_account_uin = expected_account_uin
        self.expected_account_config_id = expected_account_config_id
        self.http = http_client(app, timeout=15, follow_redirects=False)
        self._cookie = ""

    def _binding(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.expected_account_uin:
            payload["json"]["expectedAccountUin"] = self.expected_account_uin
        if self.expected_account_config_id:
            payload["json"]["expectedAccountConfigId"] = self.expected_account_config_id
        return payload

    async def _login(self) -> None:
        try:
            resp = await self.http.post(self.base_url + "/_auth/login", json={"token": self.token})
        except Exception as exc:
            raise WhaleError("unavailable", f"{SERVICE} Web 无法连接", retryable=True) from None
        cookie = resp.headers.get("set-cookie", "")
        m = re.search(r"weq_session=[^;,\s]+", cookie)
        if resp.status_code != 200 or not m:
            raise WhaleError("unauthorized", f"{SERVICE} Web 登录失败（HTTP {resp.status_code}），请检查 token")
        self._cookie = m.group(0)

    async def _post(self, path: str, payload: dict[str, Any], *, retry_login: bool = True,
                    bind: bool = True) -> Any:
        if not self._cookie:
            await self._login()
        headers = {"Cookie": self._cookie}
        try:
            resp = await self.http.post(self.base_url + path,
                                        json=self._binding(payload) if bind else payload, headers=headers)
        except WhaleError:
            raise
        except Exception as exc:
            kind = "timeout" if "Timeout" in type(exc).__name__ else "unavailable"
            raise WhaleError(kind, f"{SERVICE} Web 请求失败", retryable=True) from None
        if resp.status_code == 401 and retry_login:
            await self._login()
            return await self._post(path, payload, retry_login=False, bind=bind)
        if resp.status_code >= 400:
            raise WhaleError("unavailable", f"{SERVICE} Web 返回 HTTP {resp.status_code}", retryable=resp.status_code >= 500)
        try:
            body = resp.json()
        except ValueError:
            raise WhaleError("unavailable", f"{SERVICE} Web 返回了非 JSON 内容") from None
        return (((body or {}).get("result") or {}).get("data") or {}).get("json")

    async def _get(self, path: str, *, retry_login: bool = True) -> Any:
        """tRPC query（GET，无入参）；401 重登一次，与 _post 同款。错误文本不含 token。"""
        if not self._cookie:
            await self._login()
        try:
            resp = await self.http.get(self.base_url + path, headers={"Cookie": self._cookie})
        except WhaleError:
            raise
        except Exception as exc:
            kind = "timeout" if "Timeout" in type(exc).__name__ else "unavailable"
            raise WhaleError(kind, f"{SERVICE} Web 请求失败", retryable=True) from None
        if resp.status_code == 401 and retry_login:
            await self._login()
            return await self._get(path, retry_login=False)
        if resp.status_code >= 400:
            raise WhaleError("unavailable", f"{SERVICE} Web 返回 HTTP {resp.status_code}",
                             retryable=resp.status_code >= 500)
        try:
            body = resp.json()
        except ValueError:
            raise WhaleError("unavailable", f"{SERVICE} Web 返回了非 JSON 内容") from None
        return (((body or {}).get("result") or {}).get("data") or {}).get("json")

    # -- 完整模式窗口原语（只管调用，开关策略在 FullModeWindow/WeQChannel） ----------------------
    async def full_mode_state(self) -> dict[str, Any]:
        """getFullModeState → {"allowed": bool, "ready": bool?}（ready 是新字段，旧 build 可能缺）。"""
        data = await self._get(TRPC_FULL_MODE_STATE)
        return data if isinstance(data, dict) else {}

    async def set_auto_attach(self, enabled: bool) -> None:
        """setAutoAttachQq（运行时即改即生效）；bootstrap.* 不带账号绑定字段。"""
        await self._post(TRPC_SET_AUTO_ATTACH, {"json": {"enabled": bool(enabled)}}, bind=False)

    # -- sends（目标只接受宿主注入的 uid/白名单群，详见模块 docstring） --------------------------------
    async def send_c2c(self, uid: str, text: str) -> bool:
        if not UID_RE.fullmatch(uid or ""):
            return False
        text = (text or "").strip()
        if not text or len(text) > MAX_TEXT:
            return False
        payload = {"json": {"peerType": "c2c", "targetId": uid,
                            "elements": [{"kind": "text", "textContent": text}]}}
        outcome = await self._post("/trpc/account.sendElements", payload)
        return bool(accept_send_outcome(outcome, peer_type="c2c").get("ok"))

    async def send_group_text(self, group_code: str, text: str, *, at_uid: str = "",
                              at_uin: str = "", at_name: str = "") -> bool:
        text = (text or "").strip()
        if not text or len(text) > MAX_TEXT:
            return False
        elements: list[dict[str, Any]] = []
        if at_uid or at_uin:
            at_el: dict[str, Any] = {"kind": "at"}
            if at_uid:
                at_el["atTargetUid"] = at_uid
            elif at_uin:
                try:
                    at_el["atTargetUin"] = int(at_uin)
                except (TypeError, ValueError):
                    at_el = {}
            if at_el:
                at_el["textContent"] = f"@{at_name or at_uid or at_uin} "
                elements.append(at_el)
        elements.append({"kind": "text", "textContent": text})
        payload = {"json": {"peerType": "group", "targetId": group_code, "elements": elements}}
        outcome = await self._post("/trpc/account.sendElements", payload)
        return bool(accept_send_outcome(outcome, peer_type="group").get("ok"))

    async def member_briefs(self, group_code: str = "", uins: list[str] | None = None) -> list[dict[str, Any]]:
        """只读：uin → 群名片/昵称（失败回空，调用方回落 QQ 号，不阻断主链路）。"""
        uins = [str(u) for u in (uins or []) if str(u).strip().isdigit()][:50]
        if not uins:
            return []
        # tRPC query 只能 GET
        q = json.dumps({"json": {"groupCode": str(group_code), "uins": uins, "uids": []}}, ensure_ascii=False)
        if not self._cookie:
            await self._login()
        try:
            resp = await self.http.get(self.base_url + "/trpc/account.memberBriefs",
                                       params={"input": q}, headers={"Cookie": self._cookie})
        except Exception:
            return []
        if resp.status_code != 200:
            return []
        try:
            data = (((resp.json() or {}).get("result") or {}).get("data") or {}).get("json")
        except ValueError:
            return []
        members = data.get("members") if isinstance(data, dict) else None
        return [m for m in members if isinstance(m, dict)] if isinstance(members, list) else []

    async def close(self) -> None:
        await self.http.aclose()


# -- 完整模式窗口（legacy full_mode_window.py 的异步移植） ------------------------------------------------
class FullModeWindow:
    """按需完整模式窗口：发送前开窗、空闲关窗。

    - ``open()``：setAutoAttachQq(true) → 轮询 getFullModeState 至 ready → grace 等收割 →
      落 state_file（当前 epoch 秒）；
    - ``close()``：setAutoAttachQq(false) → 清 state_file。幂等；
    - ``touch()``/``last_send_epoch()``：滑动续期与跨进程协调都只靠 state_file（纯本地文件）。
    红线：token 不进日志/异常/state_file；失败一律 WhaleError 上抛，由调用方决定不阻断。
    """

    def __init__(self, port: WeQSendPort, *, state_file: Path,
                 ready_timeout_s: float = WINDOW_READY_TIMEOUT_S, grace_s: float = WINDOW_GRACE_S,
                 poll_interval_s: float = WINDOW_POLL_S) -> None:
        self._port = port
        self.state_file = Path(state_file)
        self.ready_timeout_s = float(ready_timeout_s)
        self.grace_s = float(grace_s)
        self.poll_interval_s = float(poll_interval_s)

    async def is_ready(self) -> bool:
        """allowed=策略门（autoAttachQq 写入即真）；ready=SSO 已登记此刻真可发（旧 build 缺省回落 allowed）。"""
        state = await self._port.full_mode_state()
        if not state.get("allowed"):
            return False
        ready = state.get("ready")
        return bool(ready) if ready is not None else True

    async def open(self) -> None:
        await self._port.set_auto_attach(True)
        deadline = time.monotonic() + self.ready_timeout_s
        while not await self.is_ready():
            if time.monotonic() >= deadline:
                raise WhaleError("timeout", f"{SERVICE} 完整模式窗口就绪超时", retryable=True)
            await asyncio.sleep(self.poll_interval_s)
        await asyncio.sleep(self.grace_s)
        self.touch()

    async def close(self) -> None:
        await self._port.set_auto_attach(False)
        try:
            self.state_file.unlink()
        except FileNotFoundError:
            pass

    def touch(self) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.state_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="ascii") as fh:
            fh.write(str(int(time.time())))

    def last_send_epoch(self) -> float:
        """读 state_file（不存在/损坏 → 0，不抛）。"""
        try:
            return float(self.state_file.read_text(encoding="ascii").strip())
        except (OSError, ValueError):
            return 0.0


# -- 入站归一化（legacy wq_push.sse_message_to_incoming 的 v3 移植） -------------------------------------
def weq_event_to_qq(ev: Any, *, bot_uin: str, bot_uid: str, accept_group: bool,
                    allowed_groups: frozenset[str]) -> QQEvent | None:
    """WeQ 推送事件 → QQEvent；不收/解析不了的一律 None（宁缺勿错）。"""
    if not isinstance(ev, dict) or ev.get("type") != "message":
        return None
    sender_uin = str(ev.get("senderUin") or "")
    if not sender_uin or sender_uin == bot_uin:
        return None                       # 防自激回路
    msg_id = str(ev.get("msgId") or "")
    if not msg_id:
        return None
    chat_type = ev.get("chatType")
    conv_id = str(ev.get("convId") or "")
    if chat_type == "c2c":
        kind, gid = "private", ""
    elif chat_type == "group" and accept_group and conv_id:
        if allowed_groups and conv_id not in allowed_groups:
            return None
        kind, gid = "group", conv_id
    else:
        return None
    sender_uid = str(ev.get("senderUid") or "")
    text = str(ev.get("text") or "")[:MAX_TEXT]
    mentions: list[str] = []
    reply_id = ""
    reply_to_bot = False
    images: list[str] = []
    elements = ev.get("elements")
    if isinstance(elements, list):
        for el in elements:
            if not isinstance(el, dict):
                continue
            data = el.get("data") if isinstance(el.get("data"), dict) else {}
            if el.get("type") == "at" and data.get("atTargetUid"):
                mentions.append(str(data["atTargetUid"]))
            elif el.get("type") == "reply":
                reply_id = str(data.get("origMsgId") or "")
                reply_to_bot = (str(data.get("origSenderUid") or "") == bot_uid
                                or str(data.get("origSenderUin") or "") == bot_uin)
            elif el.get("type") == "pic":
                url = str(data.get("originalUrl") or "")
                if url.startswith("https://") and len(images) < 4:
                    images.append(url[:2000])
    mentioned = bool(bot_uid) and bot_uid in mentions
    return QQEvent(kind=kind, message_id=msg_id, uin=sender_uin, uid=sender_uid, text=text,
                   images=images, group_id=gid, mentioned=mentioned, reply_id=reply_id,
                   reply_to_bot=reply_to_bot, meta={"send_time": str(ev.get("sendTime") or "")})


class WeQChannel(QQChannel):
    """QQ over WeQ 桌面进程：HTTP 推送入站 + tRPC 出站。"""

    kind = "weq"
    backend_name = "weq"

    def __init__(self, app: Any, channel_id: str, config: dict[str, Any], *, token: str = "",
                 web_token: str = "", sink: Sink | None = None) -> None:
        super().__init__(app, channel_id, config, token=token, sink=sink)
        cfg = config
        # 两个 token 分开：access_token=入站推送 Bearer 校验；web_token=WeQ Web tRPC 登录
        #（缺省回落同一个，兼容单 token 部署）
        self.web_token = web_token or token
        self.weq_web_url = str(cfg.get("weq_web_url") or "").strip()
        self.bot_uid = str(cfg.get("bot_uid") or "").strip()
        self.accept_group = bool(cfg.get("accept_group", False))
        self.port: WeQSendPort | None = None
        self._expected_uin = str(cfg.get("expected_account_uin") or "").strip()
        self._expected_cfg = str(cfg.get("expected_account_config_id") or "").strip()
        # 按需完整模式窗口（默认开：发送前自动开窗、空闲自动关窗；=false 回退旧的直发行为）
        self.fullmode_ondemand = bool(cfg.get("fullmode_ondemand", True))
        self.fullmode_idle_min = float(cfg.get("fullmode_idle_min", 10.0))
        self._window_ready_timeout = float(cfg.get("fullmode_ready_timeout_s", WINDOW_READY_TIMEOUT_S))
        self._window_grace = float(cfg.get("fullmode_grace_s", WINDOW_GRACE_S))
        self._window_state_path = str(cfg.get("fullmode_state_file") or "").strip()
        self.window: FullModeWindow | None = None
        self._window_lock = asyncio.Lock()
        self._closer_task: asyncio.Task[Any] | None = None
        self.win_opens = 0
        self.win_closes = 0
        self.win_open_errors = 0
        self._win_daily_date = ""
        self._win_daily_opens = 0

    # -- lifecycle -------------------------------------------------------------------------
    async def open(self) -> None:
        if not self.weq_web_url:
            raise WhaleError("invalid_params", "weq 渠道缺少 weq_web_url")
        if not self.token or not self.web_token:
            raise WhaleError("invalid_params", "weq 渠道缺少 access_token（fail closed）")
        self.port = WeQSendPort(self.app, self.weq_web_url, self.web_token,
                                expected_account_uin=self._expected_uin,
                                expected_account_config_id=self._expected_cfg)
        await super().open()           # qq.send_* 能力 + 主人通知出口
        sink = self.sink

        @sink.webhook(sink.hook_path)
        async def weq_inbound(request: Request) -> Response:
            return await self.webhook(request)

        self.connected = True
        if self.fullmode_ondemand:
            self.window = FullModeWindow(self.port, state_file=self._window_state_file(),
                                         ready_timeout_s=self._window_ready_timeout,
                                         grace_s=self._window_grace)
            try:
                await self.window.close()       # 启动基线关窗（幂等，跨进程定基线）
            except WhaleError as exc:
                self.log.warning("weq 窗口基线关窗失败（closer 将兜底）：%s", exc.message)
            self._closer_task = self.spawn(self._window_closer_loop())

    def _window_state_file(self) -> Path:
        if self._window_state_path:
            return Path(self._window_state_path)
        data_dir = getattr(self.app, "data_dir", None)
        if data_dir:
            return Path(data_dir) / "fullmode_window.ts"
        return Path(tempfile.gettempdir()) / f"whale-fullmode-{self.id}.ts"

    async def close(self) -> None:
        if self._closer_task is not None:
            self._closer_task.cancel()
            self._closer_task = None
        if self.window is not None and self.window.state_file.exists():
            try:
                await self.window.close()       # 停机把窗带上（幂等；失败下次启动基线兜底）
            except WhaleError:
                pass
        if self.port is not None:
            await self.port.close()
        self.connected = False

    def status(self) -> dict[str, Any]:
        s = {**super().status(), "via": ["webhook", "trpc"], "accept_group": self.accept_group}
        if self.fullmode_ondemand:
            s["fullmode"] = {"ondemand": True, "idle_min": self.fullmode_idle_min,
                             "opens": self.win_opens, "closes": self.win_closes,
                             "open_errors": self.win_open_errors}
        return s

    # -- 完整模式窗口（发送前开、空闲关；开窗失败不阻断发送） --------------------------------------------
    async def _ensure_window(self) -> None:
        """发送前确保窗口在线。开窗失败不阻断发送（红线）：内层如实回错，记 open_errors。"""
        if self.window is None:
            return
        async with self._window_lock:
            try:
                if await self.window.is_ready():
                    return
                await self.window.open()
            except (WhaleError, OSError) as exc:
                self.win_open_errors += 1
                msg = exc.message if isinstance(exc, WhaleError) else repr(exc)[:120]
                self.log.warning("weq 开窗失败（不阻断发送，按既有语义如实记账）：%s", msg)
                return
            self.win_opens += 1
            today = time.strftime("%Y-%m-%d")
            if today != self._win_daily_date:
                self._win_daily_date, self._win_daily_opens = today, 0
            self._win_daily_opens += 1
            self.log.info("weq 完整模式窗口 open（第%d次，今日第%d次）", self.win_opens, self._win_daily_opens)
            if self._win_daily_opens == WINDOW_DAILY_OPEN_WARN + 1:
                self.log.warning("weq 今日开窗超过 %d 次，建议调大 fullmode_idle_min",
                                 WINDOW_DAILY_OPEN_WARN)

    async def _window_closer_loop(self) -> None:
        while True:
            await asyncio.sleep(WINDOW_CLOSER_TICK_S)
            try:
                await self._window_closer_step(time.time())
            except WhaleError as exc:
                self.log.warning("weq 关窗失败（下周期重试）：%s", exc.message)

    async def _window_closer_step(self, now: float) -> bool:
        """窗口开着（state_file 在）且空闲超阈值 → 关窗。窗口状态以 state_file 为准。"""
        window = self.window
        if window is None or not window.state_file.exists():
            return False
        idle_s = self.fullmode_idle_min * 60
        if now - window.last_send_epoch() <= idle_s:
            return False
        async with self._window_lock:
            if now - window.last_send_epoch() <= idle_s:    # 复查：send 可能刚 touch
                return False
            await window.close()
            self.win_closes += 1
            self.log.info("weq 完整模式窗口 close（第%d次，空闲超 %.0fmin）",
                          self.win_closes, self.fullmode_idle_min)
        return True

    # -- uid ↔ uin 映射（入站学习 + kv 缓存；capability 只按 uin 调用） --------------------------------
    def _learn_identity(self, ev: QQEvent) -> None:
        self._learn_uid(ev.uin, ev.uid)

    def _learn_uid(self, uin: str, uid: str) -> None:
        if uin and uid:
            try:
                self.app.db.kv_set("channel_uidmap", uin, {"uid": uid, "at": time.time()})
            except Exception:
                pass

    def _uid_for(self, uin: str) -> str:
        try:
            rec = self.app.db.kv_get("channel_uidmap", uin)
        except Exception:
            rec = None
        return str(rec.get("uid") or "") if isinstance(rec, dict) else ""

    # -- backend 发送面 --------------------------------------------------------------------------------
    async def send_text(self, kind: str, target_uin: str, target_uid: str, text: str,
                        reply_to: str = "", at: dict[str, Any] | None = None) -> dict[str, Any]:
        if self.port is None:
            return {"status": "error", "sent": 0, "total": 0, "message_ids": [], "error": "WeQ 未连接"}
        text = plain(text)
        bubbles = split_bubbles(text, group=kind == "group") if self.reply_split else [text]
        receipt: dict[str, Any] = {"status": "ok", "sent": 0, "total": len(bubbles), "message_ids": [],
                                   "error": ""}
        if not bubbles or not bubbles[0]:
            receipt.update(status="error", error="消息为空")
            return receipt
        if kind == "private":
            uid = target_uid or self._uid_for(target_uin)
            if not uid:
                receipt.update(status="error", error=f"无 {target_uin} 的 uid 映射（对方先开口一次即学习）")
                return receipt
        await self._ensure_window()     # 平时离线、发时开窗；开窗失败不阻断（红线）
        try:
            for n, bubble in enumerate(bubbles):
                if n and self.bubble_delay:
                    await asyncio.sleep(min(3.0, self.bubble_delay * (0.5 + len(bubble) / 40)))
                try:
                    if kind == "group":
                        at_kw = {"at_uid": str(at.get("uid") or ""), "at_uin": str(at.get("uin") or ""),
                                 "at_name": str(at.get("name") or "")} if (n == 0 and at) else {}
                        ok = await self.port.send_group_text(target_uin, bubble, **at_kw)
                    else:
                        ok = await self.port.send_c2c(uid, bubble)
                except WhaleError as exc:
                    receipt["error"] = exc.message
                    receipt["status"] = "unknown" if exc.code == "timeout" else "error"
                    break
                if not ok:
                    receipt.update(status="error", error="WeQ 回执未带有效 sequence（未确认送达）")
                    break
                receipt["sent"] += 1
        finally:
            if self.window is not None:
                try:
                    self.window.touch()         # 成败都续期：失败发送也算窗口活跃
                except OSError as exc:
                    self.log.warning("weq 窗口 touch 失败（空闲计时可能失真）：%s", exc)
        if receipt["status"] != "ok":
            self.log.warning("weq %s to %s: %s (%d/%d bubbles)", kind, target_uin, receipt["status"],
                             receipt["sent"], receipt["total"])
        await self.app.bus.emit("qq.delivery", {"channel": self.id, "kind": kind,
                                                "target": target_uin, **receipt})
        return receipt

    async def _fetch_name(self, kind: str, uin: str, gid: str = "") -> str:
        if self.port is None or kind != "group" or not gid:
            return ""
        rows = await self.port.member_briefs(gid, uins=[uin])
        for row in rows:
            if str(row.get("uin") or "") == uin:
                return str(row.get("card") or row.get("nick") or "")
        return ""

    # -- 入站 webhook（WeQ 推送） ----------------------------------------------------------------------
    def authorized(self, request: Request) -> bool:
        auth = request.headers.get("authorization", "")
        scheme, _, cred = auth.partition(" ")
        return bool(self.token) and scheme.lower() == "bearer" and bool(cred.strip()) \
            and hmac.compare_digest(cred.strip(), self.token)

    async def webhook(self, request: Request) -> Response:
        if request.method == "GET":
            return Response(status_code=204)
        body = await request.body()
        if len(body) > MAX_BODY:
            return JSONResponse({"ok": False, "error": {"code": "invalid_params", "message": "body too large"}},
                                status_code=413)
        if not self.authorized(request):
            return JSONResponse({"ok": False, "error": {"code": "unauthorized", "message": "invalid token"}},
                                status_code=401)
        try:
            payload = json.loads(body or b"{}")
        except ValueError:
            return JSONResponse({"ok": False, "error": {"code": "invalid_params", "message": "bad JSON"}},
                                status_code=400)
        events = payload.get("events") if isinstance(payload, dict) else None
        if not isinstance(events, list):
            return JSONResponse({"ok": False, "error": {"code": "invalid_params", "message": "events missing"}},
                                status_code=400)
        accepted = 0
        for raw in events:
            qev = weq_event_to_qq(raw, bot_uin=self.bot_uin, bot_uid=self.bot_uid,
                                  accept_group=self.accept_group,
                                  allowed_groups=frozenset(self.allowed_groups))
            if qev is None:
                continue
            self.spawn(self.ingest(qev))     # 立即 ACK，协议端不等 Agent（ingest 内学习 uid 映射）
            accepted += 1
        return JSONResponse({"accepted": accepted})
