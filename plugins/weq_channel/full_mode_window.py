"""发送窗口按需完整模式管理器（full-mode window）。

动机：WeQ 完整模式（attach 在线 + nt_helper 每 ~25min 周期 harvest，~58 次/日
真实腾讯侧发包）7×24 常开没有必要——**收消息走 SSE 本地库监听，与 attach 无关**，
只有「发送」需要完整模式。本模块把完整模式改为**按需**：

- 草稿实发前自动开窗：`setAutoAttachQq(true)` → 轮询 `getFullModeState` 就绪 →
  grace 等待收割完成 → 落 state_file；
- 空闲超时自动关窗：`setAutoAttachQq(false)`，掐断周期 harvest 循环；
- 平时完整模式离线，收侧（SSE/inbox/工作流）零改动、不受影响。

语义红线（任务书 §2）：
- 不改变发送结果语义：开窗失败**不阻断**发送尝试；内层 port 返回 False 照旧
  记 unknown、不自动重试；
- token 不进日志/异常/回执/state_file（文件里只有 epoch 秒数字）；
- 跨进程协调只靠 state_file：`approve` 子进程开的窗，runner 主进程的 closer
  也能关；runner 启动即 close() 定基线（幂等）；
- 频繁开关防护：每次 send 滑动续期（touch）；每日开窗计数 >40 给一行日志警告。

已知坑（2026-10-30 实测）：UI/旧流程若调过 ``account.requestFullMode``，会在
weq-web 进程内留下 sticky 的 manualRequests 记录——``fullModeAllowed`` 因此恒真，
``setAutoAttachQq(false)`` 关不掉周期收割。WeQ 没有清除该记录的 tRPC 接口，
只能靠**重启 weq-web**（内存态清空；autoEnter 会自动重开账号）。本模块上线前
已重启清场；之后请勿在 UI 里手动「启用完整功能」，否则窗口管理失效直到下次重启。
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

from .weq_send import _NoRedirect, validate_base_url

STATE_FILE_DEFAULT = "/opt/whale/state/fullmode_window.ts"
LOGIN_PATH = "/_auth/login"
TRPC_FULL_MODE_STATE = "/trpc/bootstrap.getFullModeState"
TRPC_SET_AUTO_ATTACH = "/trpc/bootstrap.setAutoAttachQq"
HTTP_TIMEOUT_S = 15.0
CLOSER_TICK_S = 30.0
DAILY_OPEN_WARN_THRESHOLD = 40


class FullModeWindowError(RuntimeError):
    """开窗/关窗/就绪探测失败（登录、HTTP、超时）。异常文本不含 token。"""


class FullModeWindow:
    """管完整模式开关与就绪探测。opener 可注入供离线测试（模式同 weq_send）。"""

    def __init__(self, base_url: str, token: str, *, state_file: str | Path,
                 ready_timeout_s: float = 25.0, grace_s: float = 4.0,
                 poll_interval_s: float = 1.0,
                 opener: Callable[..., object] | None = None) -> None:
        if not token:
            raise ValueError("token required (WEQ_WEB_TOKEN)")
        # 地址安全校验与发送端口同款（回环 http / 远程必须 https，拒绝内嵌凭据）
        self.base_url = validate_base_url(base_url)
        self.token = token
        self.state_file = Path(state_file)
        self.ready_timeout_s = float(ready_timeout_s)
        self.grace_s = float(grace_s)
        self.poll_interval_s = float(poll_interval_s)
        if opener is not None:
            self._opener = opener
        else:
            no_redirect = urllib.request.build_opener(_NoRedirect)
            self._opener = lambda req, t: no_redirect.open(req, timeout=t)
        self._cookie: str | None = None

    # ── 内部 HTTP（错误一律 FullModeWindowError，文本不含 token）─────────

    def _request(self, path: str, body: dict | None, *, auth: bool = True):
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers = {}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if auth:
            if not self._cookie:
                raise FullModeWindowError("no session (login first)")
            headers["Cookie"] = self._cookie
        request = urllib.request.Request(self.base_url + path, data=data, headers=headers,
                                         method="POST" if body is not None else "GET")
        try:
            with self._opener(request, HTTP_TIMEOUT_S) as response:  # type: ignore[operator]
                raw = response.read()
                set_cookie = response.headers.get("Set-Cookie") if hasattr(response, "headers") else None
                status = getattr(response, "status", 200)
        except urllib.error.HTTPError as exc:
            status = exc.code
            try:
                raw = exc.read()
            except Exception:
                raw = b""
            set_cookie = None
        except (urllib.error.URLError, OSError, ValueError):
            raise FullModeWindowError("weq web unreachable") from None
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            parsed = raw.decode("utf-8", "replace")[:200]
        return status, parsed, set_cookie or ""

    def _login(self) -> None:
        status, _body, set_cookie = self._request(LOGIN_PATH, {"token": self.token}, auth=False)
        if status != 200 or "weq_session=" not in set_cookie:
            raise FullModeWindowError(f"weq web login failed (http {status})")
        for part in set_cookie.split(","):
            chunk = part.strip().split(";", 1)[0]
            if chunk.startswith("weq_session="):
                self._cookie = chunk
                return
        raise FullModeWindowError("weq web login returned no session cookie")

    def _call(self, path: str, body: dict | None = None):
        """tRPC 调用（401 重登一次，与 weq_send 同款）；返回 result.data.json。"""
        if self._cookie is None:
            self._login()
        status, parsed, _ = self._request(path, body)
        if status == 401:  # 会话过期：重登一次再试（仅此一次）
            self._login()
            status, parsed, _ = self._request(path, body)
        if status != 200 or not isinstance(parsed, dict):
            raise FullModeWindowError(f"trpc {path} failed (http {status})")
        if "error" in parsed:
            raise FullModeWindowError(f"trpc {path} returned error")
        return ((parsed.get("result") or {}).get("data") or {}).get("json")

    # ── 开关与就绪探测 ────────────────────────────────────────────

    def is_allowed(self) -> bool:
        """getFullModeState.allowed（策略门：autoAttachQq=true 写入即真）。"""
        result = self._call(TRPC_FULL_MODE_STATE)
        return bool((result or {}).get("allowed"))

    def is_ready(self) -> bool:
        """getFullModeState.ready（SSO 会话已登记，此刻真可发）。

        ready 字段是 2026-11-01 才加的：旧 weq-web 返回里没有它，
        缺失时回落 allowed（行为同旧版，向后兼容）。
        见 docs/fullmode-ready-field-20261101.md：allowed 只是策略门，
        冷启动首次开窗全量收割 ~4.7s，跑在就绪前会丢发。
        """
        result = self._call(TRPC_FULL_MODE_STATE)
        if not isinstance(result, dict) or not result.get("allowed"):
            return False
        ready = result.get("ready")
        return bool(ready) if ready is not None else True

    def open(self) -> None:
        """开窗：置 autoAttachQq=true → 轮询就绪（ready 位）→ grace → 落 state_file。"""
        self._call(TRPC_SET_AUTO_ATTACH, {"json": {"enabled": True}})
        deadline = time.time() + self.ready_timeout_s
        while not self.is_ready():
            if time.time() >= deadline:
                raise FullModeWindowError("full mode not ready in time")
            time.sleep(self.poll_interval_s)
        time.sleep(self.grace_s)
        self.touch()

    def close(self) -> None:
        """关窗：置 autoAttachQq=false → 清 state_file。幂等，可重复调用。"""
        self._call(TRPC_SET_AUTO_ATTACH, {"json": {"enabled": False}})
        try:
            self.state_file.unlink()
        except FileNotFoundError:
            pass

    def touch(self) -> None:
        """滑动续期：只更新 state_file 时间戳（纯本地文件，无 HTTP）。"""
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


class WindowedSendPort:
    """包装任意 OutboundPort：发送前确保完整模式窗口在线，空闲自动关窗。

    生产环境 inner=WeQWebSendPort。计数器 opens/closes/sends/open_errors 供观测。
    """

    def __init__(self, inner, window: FullModeWindow, *, idle_min: float = 10.0) -> None:
        self._inner = inner
        self.window = window
        self.idle_min = float(idle_min)
        self._lock = threading.Lock()
        self._open = False
        self._closer_started = False
        self.opens = 0
        self.closes = 0
        self.sends = 0
        self.open_errors = 0
        self._daily_date = ""
        self._daily_opens = 0

    # ── OutboundPort ─────────────────────────────────────────────

    def send(self, draft) -> bool:
        """开窗（需要时）→ 委托内层发送 → touch 续期 → 透传内层返回值。

        开窗失败不阻断发送（红线 1）：内层如实返回 False → 账本 unknown，不重试。
        """
        with self._lock:
            self.sends += 1
            try:
                if not self._window_open():
                    self._open_window()
            except FullModeWindowError as exc:
                self.open_errors += 1
                self._open = False
                print(f"[窗口] 开窗失败（不阻断发送，按既有语义如实记账）：{exc}")
            try:
                return self._inner.send(draft)
            finally:
                # 无论开窗/发送成败都续期：失败发送也算窗口活跃；开窗半吊子态
                # （set(true) 成功但轮询超时）也由 closer 据此超时纠正关窗。
                try:
                    self.window.touch()
                except OSError as exc:
                    print(f"[窗口] touch 失败（空闲计时可能失真）：{exc}")

    # ── 窗口管理 ────────────────────────────────────────────────

    def _window_open(self) -> bool:
        """以服务端真实状态为准（跨进程安全）：探测 getFullModeState.ready。"""
        try:
            self._open = self.window.is_ready()
        except FullModeWindowError:
            self._open = False
        return self._open

    def _open_window(self) -> None:
        self.window.open()
        self._open = True
        self.opens += 1
        today = time.strftime("%Y-%m-%d")
        if today != self._daily_date:
            self._daily_date = today
            self._daily_opens = 0
        self._daily_opens += 1
        print(f"[窗口] open (第{self.opens}次，今日第{self._daily_opens}次)")
        if self._daily_opens == DAILY_OPEN_WARN_THRESHOLD + 1:
            print(f"[窗口] [!] 今日开窗超过 {DAILY_OPEN_WARN_THRESHOLD} 次，"
                  f"建议调大 QQ_FULLMODE_IDLE_MIN")

    def close(self) -> None:
        """关窗（closer 与启动基线调用）。失败向上抛 FullModeWindowError。"""
        try:
            self.window.close()
        finally:
            self._open = False
        self.closes += 1
        print(f"[窗口] close (第{self.closes}次)")

    # ── 空闲关窗（仅 runner 主进程调用；approve 子进程不起 closer）─────

    def start_closer(self, idle_min: float | None = None) -> None:
        if idle_min is not None:
            self.idle_min = float(idle_min)
        if self._closer_started:
            return
        self._closer_started = True
        thread = threading.Thread(target=self._closer_loop,
                                  name="fullmode-window-closer", daemon=True)
        thread.start()

    def _closer_loop(self) -> None:
        while True:
            time.sleep(CLOSER_TICK_S)
            try:
                self._closer_step(time.time())
            except FullModeWindowError as exc:
                print(f"[窗口] 关窗失败（下周期重试）：{exc}")

    def _closer_step(self, now: float) -> bool:
        """窗口开着（state_file 存在）且空闲超 idle → 关窗。返回是否执行了关窗。

        跨进程正确性：窗口开/关以 state_file 为准（approve 子进程 touch 的新
        时间戳这里同样生效），不依赖本进程内存标志。
        """
        if not self.window.state_file.exists():
            return False
        idle_s = self.idle_min * 60
        if now - self.window.last_send_epoch() <= idle_s:
            return False
        with self._lock:
            if now - self.window.last_send_epoch() <= idle_s:  # 复查：send 可能刚 touch
                return False
            self.close()
        return True
