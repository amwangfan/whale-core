"""WeQ 桌面内置 Web 服务的真实发送端口（W09 Phase 2 · 主账号身份=本机登录小号）。

通道说明：WeQ 对外部 MCP 刻意只读；本端口走的是**内置 Web 服务的用户界面同款
tRPC 接口**（`/_auth/login` + `/trpc/account.sendElements`），等价于用户自己在
浏览器里操作自己的 WeQ——token 由用户从 WeQ 设置复制注入（secrets），不破解、
不复用助手令牌、不绕过任何权限门。

前置与边界：
- 需要 WeQ 完整模式（QQ 在线 + 已授权 attach）；离线模式下 sendElements 会被
  WeQ 拒绝，本端口如实返回 False（账本记 unknown，不自动重试）；
- Web token 每次 WeQ 重启轮换，需要重新复制（长期方案等上游受控发送接口，
  见 docs/research-weq-deepseek-qq.md §6.3）；
- send 返回 True 仅表示 QQ 服务端受理（ok=true），不代表已读；
- 只做 direct_message 渠道、单一绑定对端（owner uid）；不群发、不换目标；
- token 不进日志与异常文本。

地址与目标校验（#15，凭据发出**之前**在构造期 fail closed）：
- 仅 http/https；回环地址（127.0.0.0/8、::1、localhost）允许 HTTP，
  **远程地址必须 HTTPS**（防 token 明文出网）；
- 拒绝 URL 内嵌凭据（user:pass@host）；可选 ``allowed_hosts`` 白名单再收紧；
- HTTP 重定向**一律不跟随**（3xx 直接失败）——防止登录凭据/会话 cookie
  被跳转到未授权主机；
- ``target_uid`` 必须是显式配置的固定 owner uid（``u_`` 前缀格式校验），
  发送目标永远来自构造配置，不从入站内容动态推导。
"""
from __future__ import annotations

import ipaddress
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Sequence

from whale_core.life.initiative import ProactiveDraft

MAX_TEXT_CHARS = 4000

# owner uid 形状（WeQ c2c uid 为 u_ 前缀）；只认显式配置的固定目标
UID_RE = re.compile(r"^u_[A-Za-z0-9_-]{2,62}$")
LOOPBACK_HOSTS = frozenset({"localhost", "::1"})


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def validate_base_url(raw: str, *, allowed_hosts: Sequence[str] | None = None) -> str:
    """校验 WeQ Web 服务地址；返回规范化（去尾斜杠）的 URL。不安全即 ValueError。

    规则：scheme 仅 http/https；不得内嵌凭据；回环允许 HTTP，远程必须 HTTPS；
    给了 allowed_hosts 则主机必须在白名单内。错误信息只含 scheme/host，不含凭据。
    """
    url = (raw or "").strip().rstrip("/")
    parts = urllib.parse.urlsplit(url)
    if parts.query or parts.fragment or "?" in url or "#" in url:
        raise ValueError("WEQ_WEB_URL must not include query or fragment")
    if parts.scheme not in ("http", "https"):
        raise ValueError(f"WEQ_WEB_URL scheme must be http/https, got {parts.scheme!r}")
    if parts.username is not None or parts.password is not None:
        raise ValueError("WEQ_WEB_URL must not embed credentials (user:pass@host form)")
    host = (parts.hostname or "").strip().lower()
    if not host:
        raise ValueError("WEQ_WEB_URL missing host")
    if parts.scheme == "http" and not _is_loopback(host):
        raise ValueError(
            f"remote WEQ_WEB_URL must use HTTPS, plaintext HTTP refused (host={host})"
        )
    if allowed_hosts:
        allow = {h.strip().lower() for h in allowed_hosts if h.strip()}
        if host not in allow:
            raise ValueError(f"WEQ_WEB_URL host {host!r} not in allowed_hosts")
    return url


def validate_target_uid(uid: str) -> str:
    """校验固定目标 uid 形状；不回显取值（避免日志/异常带出身份材料）。"""
    value = (uid or "").strip()
    if not UID_RE.fullmatch(value):
        raise ValueError(
            "target_uid invalid: expect the explicitly configured owner uid (u_ prefix, "
            "[A-Za-z0-9_-], total length 4-64)"
        )
    return value


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """3xx 一律不跟随：redirect_request 返回 None → urllib 抛 HTTPError。

    登录 token 与会话 cookie 只会发给构造期校验过的 base_url 主机。
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


class WeQSendError(RuntimeError):
    """WeQ Web 通道失败（登录、HTTP、协议、被拒）。异常文本不含 token。"""


class WeQWebSendPort:
    """OutboundPort 实现：经 WeQ 内置 Web tRPC 发送 c2c 文本。

    opener 可注入以便离线测试；默认 urllib.request.urlopen。
    """

    def __init__(self, base_url: str, token: str, *, target_uid: str,
                 expected_account_uin: str | None = None,
                 expected_account_config_id: str | None = None,
                 allowed_hosts: Sequence[str] | None = None,
                 timeout: float = 15.0,
                 opener: Callable[..., object] | None = None) -> None:
        if not token:
            raise ValueError("token required (从 WeQ 设置 → WeQ 服务 → Web 与 Remote 复制)")
        # 安全校验先于任何凭据保存/发出（#15）：地址不合法直接构造失败
        self.base_url = validate_base_url(base_url, allowed_hosts=allowed_hosts)
        self.target_uid = validate_target_uid(target_uid)
        self.expected_account_uin = expected_account_uin
        self.expected_account_config_id = expected_account_config_id
        self.timeout = timeout
        # urlopen 的第二位置参数是 data 而非 timeout：必须包一层关键字传参。
        # 默认 opener 禁用重定向跟随（_NoRedirect），凭据不外流到未授权主机。
        if opener is not None:
            self._opener = opener
        else:
            no_redirect = urllib.request.build_opener(_NoRedirect)
            self._opener = lambda req, t: no_redirect.open(req, timeout=t)
        self.token = token
        self._cookie: str | None = None
        self.last_receipt: dict | None = None

    # ── 内部 HTTP ─────────────────────────────────────────────

    def _post(self, path: str, body: dict, *, auth: bool) -> tuple[int, dict | str, str]:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if auth:
            if not self._cookie:
                raise WeQSendError("no session (login first)")
            headers["Cookie"] = self._cookie
        request = urllib.request.Request(self.base_url + path, data=data,
                                         headers=headers, method="POST")
        try:
            with self._opener(request, self.timeout) as response:  # type: ignore[operator]
                raw = response.read()
                set_cookie = response.headers.get("Set-Cookie") if hasattr(response, "headers") else None
                status = getattr(response, "status", 200)
        except urllib.error.HTTPError as exc:
            try:
                raw = exc.read()
            except Exception:
                raw = b""
            status = exc.code
            set_cookie = None
        except (urllib.error.URLError, OSError, ValueError):
            raise WeQSendError("weq web unreachable") from None
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            parsed = raw.decode("utf-8", "replace")[:200]
        return status, parsed, set_cookie or ""

    def login(self) -> None:
        status, _body, set_cookie = self._post("/_auth/login", {"token": self.token}, auth=False)
        if status != 200 or "weq_session=" not in set_cookie:
            raise WeQSendError(f"weq web login failed (http {status})")
        # 只取会话 cookie 本体，不带其它属性
        for part in set_cookie.split(","):
            chunk = part.strip().split(";", 1)[0]
            if chunk.startswith("weq_session="):
                self._cookie = chunk
                return
        raise WeQSendError("weq web login returned no session cookie")

    def send_image(self, image_bytes: bytes, *, filename: str = "image.png",
                   width: int = 0, height: int = 0) -> bool:
        """发图片消息（/trpc/account.sendImage，weq-web 侧的媒体上传发送）。

        字节经 {type:'Buffer', data:[...]} JSON 形态传给 WeQ web（它落临时文件
        后走 NTV2 上传发送）。失败返回 False（调用方兜底成文本链接）。
        """
        if not image_bytes or len(image_bytes) > 20 * 1024 * 1024:
            return False
        payload = {
            "json": {
                "peerType": "c2c",
                "targetId": self.target_uid,
                "image": {"type": "Buffer", "data": list(image_bytes)},
                "fileName": filename[:100] or "image.png",
            }
        }
        if width > 0 and height > 0:
            payload["json"]["width"] = width
            payload["json"]["height"] = height
        if self.expected_account_uin:
            payload["json"]["expectedAccountUin"] = self.expected_account_uin
        if self.expected_account_config_id:
            payload["json"]["expectedAccountConfigId"] = self.expected_account_config_id
        try:
            if self._cookie is None:
                self.login()
            status, body, _ = self._post("/trpc/account.sendImage", payload, auth=True)
            if status == 401:
                self.login()
                status, body, _ = self._post("/trpc/account.sendImage", payload, auth=True)
        except WeQSendError:
            return False
        if status != 200 or not isinstance(body, dict):
            return False
        outcome = (((body.get("result") or {}).get("data") or {}).get("json"))
        return isinstance(outcome, dict) and bool(outcome.get("ok"))

    def send_voice(self, wav_bytes: bytes, *, filename: str = "voice.wav") -> bool:
        """发语音消息（/trpc/account.sendVoice，weq-web 侧 WAV→SILK 转换发送）。"""
        if not wav_bytes or len(wav_bytes) > 20 * 1024 * 1024:
            return False
        payload = {
            "json": {
                "peerType": "c2c",
                "targetId": self.target_uid,
                "wav": {"type": "Buffer", "data": list(wav_bytes)},
                "fileName": filename[:100] or "voice.wav",
            }
        }
        if self.expected_account_uin:
            payload["json"]["expectedAccountUin"] = self.expected_account_uin
        if self.expected_account_config_id:
            payload["json"]["expectedAccountConfigId"] = self.expected_account_config_id
        try:
            if self._cookie is None:
                self.login()
            status, body, _ = self._post("/trpc/account.sendVoice", payload, auth=True)
            if status == 401:
                self.login()
                status, body, _ = self._post("/trpc/account.sendVoice", payload, auth=True)
        except WeQSendError:
            return False
        if status != 200 or not isinstance(body, dict):
            return False
        outcome = (((body.get("result") or {}).get("data") or {}).get("json"))
        return isinstance(outcome, dict) and bool(outcome.get("ok"))

    # ── OutboundPort ─────────────────────────────────────────

    def send(self, draft: ProactiveDraft) -> bool:
        if draft.channel != "direct_message":
            return False
        text = draft.text
        if not text.strip() or len(text) > MAX_TEXT_CHARS:
            return False
        # 图文混排（owner 决策 2026-10-31）：文本里的图片链接拆出来真发图片，
        # 前后文字分段按序发送；图片下载失败兜底回文字链接（不静默丢内容）。
        parts = _split_image_parts(text)
        if any(kind == "image" for kind, _ in parts):
            return _send_mixed_impl(self, parts)
        return self._send_text_payload(text)

    def _send_text_payload(self, text: str) -> bool:
        payload = {
            "json": {
                "peerType": "c2c",
                "targetId": self.target_uid,
                "elements": [{"kind": "text", "textContent": text}],
            }
        }
        if self.expected_account_uin:
            payload["json"]["expectedAccountUin"] = self.expected_account_uin
        if self.expected_account_config_id:
            payload["json"]["expectedAccountConfigId"] = self.expected_account_config_id
        try:
            if self._cookie is None:
                self.login()
            status, body, _ = self._post("/trpc/account.sendElements", payload, auth=True)
            if status == 401:  # 会话过期：重登一次再试（仅此一次）
                self.login()
                status, body, _ = self._post("/trpc/account.sendElements", payload, auth=True)
        except WeQSendError:
            return False
        if status != 200 or not isinstance(body, dict):
            return False
        # tRPC + superjson: {"result":{"data":{"json":<SendMessageOutcome>}}}
        outcome = (((body.get("result") or {}).get("data") or {}).get("json"))
        if not isinstance(outcome, dict):
            return False
        self.last_receipt = outcome
        return bool(outcome.get("ok"))


# markdown 图片；模型有时写成不带 ! 的链接形态——也认（生产实证）
_MARKDOWN_IMG_RE = re.compile(r"!?\[[^\]]*\]\((https?://[^)\s]+\.(?:png|jpe?g|webp|gif)[^)\s]*)\)", re.IGNORECASE)
# HTML <img src="..."> 整标签识别（模型有时输出 HTML 而非 markdown——
# 生产实证 2026-10-31）：整段摘除，URL 单独发图，不留 HTML 残片。
_HTML_IMG_RE = re.compile(r"<img\b[^>]*?src=[\"'](https?://[^\"'\s]+\.(?:png|jpe?g|webp|gif)[^\"'\s]*)[\"'][^>]*>", re.IGNORECASE)
_IMAGE_URL_RE = re.compile(
    r"https?://[^\s)\]<>'\"]+\.(?:png|jpe?g|webp|gif)(?:\?[^\s)\]<>'\"]*)?",
    re.IGNORECASE)
_IMAGE_MAX_BYTES = 20 * 1024 * 1024


def _split_image_parts(text: str) -> list[tuple[str, str]]:
    """把文本拆成 [('text',...)|('image',url)] 按序分段。markdown 图片和裸图片
    链接都识别；链接本身从文本里摘除（图片单独发，不留重复链接）。"""
    spans: list[tuple[int, int, str]] = []
    for m in _HTML_IMG_RE.finditer(text):
        spans.append((m.start(), m.end(), m.group(1)))
    for m in _MARKDOWN_IMG_RE.finditer(text):
        if any(s <= m.start() < e for s, e, _u in spans):
            continue
        spans.append((m.start(), m.end(), m.group(1)))
    covered = [(s, e) for s, e, _ in spans]
    for m in _IMAGE_URL_RE.finditer(text):
        if any(s <= m.start() < e for s, e in covered):
            continue
        spans.append((m.start(), m.end(), m.group(0)))
    if not spans:
        return [("text", text)]
    spans.sort()
    parts: list[tuple[str, str]] = []
    cursor = 0
    for s, e, url in spans:
        before = text[cursor:s].strip()
        if before:
            parts.append(("text", before))
        parts.append(("image", url))
        cursor = e
    tail = text[cursor:].strip()
    if tail:
        parts.append(("text", tail))
    return parts


def _download_image(url: str, *, timeout: float = 30.0) -> bytes | None:
    """下载图片字节：仅 https、不跟重定向（防把凭据/会话带给第三方）、限 20MB。"""
    if not url.startswith("https://"):
        return None
    req = urllib.request.Request(url, headers={"User-Agent": "whale-core/0.1"})
    try:
        with urllib.request.build_opener(_NoRedirect()).open(req, timeout=timeout) as resp:
            data = resp.read(_IMAGE_MAX_BYTES + 1)
            if len(data) > _IMAGE_MAX_BYTES:
                return None
            return data
    except Exception:
        return None


class _MixedSend:
    """挂到 WeQWebSendPort 上：分段发送（text→sendElements, image→下载+sendImage）。"""


def _send_mixed_impl(self, parts: list[tuple[str, str]]) -> bool:
    ok_all = True
    for kind, content in parts:
        if kind == "text":
            if not self._send_text_payload(content):
                ok_all = False
        else:
            data = _download_image(content)
            if data and self.send_image(data, filename=content.rsplit("/", 1)[-1][:60] or "image.png"):
                continue
            # 图片失败兜底回文字链接（不丢内容）
            if not self._send_text_payload(content):
                ok_all = False
    return ok_all
