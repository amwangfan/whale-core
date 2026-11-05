"""主动问候循环（默认关闭；开启默认 dry-run，不外发）。

Owner 2026-11-05 确认的分工：
- **判断要不要回**：每约 3 分钟一次，模型走 AgentScope
  ``https://platform.agentscope.io/v1``（该端点下模型目前对其免费，含图/视频）。
- **真正闲聊/问候文案**：仍走 DeepSeek 官方 API（更快），不要用 AgentScope 做回复。
- 默认 ``WHALE_PROACTIVE_GREETING`` 未设 → 完全关闭；
  设为 1 且未显式 ``WHALE_PROACTIVE_GREETING_DRY_RUN=0`` → 跑循环但只记日志不发送。
- 真发时仍需过冷却与日额度；``WHALE_PROACTIVE_GREETING_DAILY_QUOTA``：**0=不限**，正整数=上海日历日上限（默认 2）。

权限与工具越权无关：问候是陪伴通道，不得借此挂 shell / 写操作。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import logging
import os
import threading
import time
from typing import Callable, Protocol
from zoneinfo import ZoneInfo

AGENTSCOPE_BASE_URL = "https://platform.agentscope.io/v1"
DEFAULT_INTERVAL_S = 180  # 3 min
DEFAULT_COOLDOWN_S = 3600  # 真发后至少 1h
DEFAULT_DAILY_QUOTA = 2  # 默认保守；0=不限
QUOTA_KEY_PREFIX = "life:quota:proactive_greeting:"
LAST_SENT_KEY = "life:proactive_greeting:last_sent_at"
TZ = ZoneInfo("Asia/Shanghai")

_log = logging.getLogger("whale.proactive_greeting")


class JudgeModel(Protocol):
    """AgentScope 侧：只输出是否该主动说一句（是/否或 JSON）。"""

    def reply(self, text: str, *, persona: str = "", memories=(), history=()) -> str: ...


class ReplyModel(Protocol):
    """DeepSeek 官模：生成问候正文。"""

    def reply(self, text: str, *, persona: str = "", memories=(), history=()) -> str: ...


class QuotaStore(Protocol):
    def get(self, key: str): ...
    def put(self, key: str, value) -> None: ...


def _truthy(raw: str) -> bool:
    return (raw or "").strip().lower() in ("1", "true", "yes", "on")


def _shanghai_day(now: datetime | None = None) -> str:
    when = now or datetime.now(TZ)
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc).astimezone(TZ)
    else:
        when = when.astimezone(TZ)
    return when.strftime("%Y-%m-%d")


@dataclass(frozen=True)
class ProactiveGreetingConfig:
    enabled: bool = False
    dry_run: bool = True
    interval_s: int = DEFAULT_INTERVAL_S
    cooldown_s: int = DEFAULT_COOLDOWN_S
    daily_quota: int = DEFAULT_DAILY_QUOTA
    judge_base_url: str = AGENTSCOPE_BASE_URL
    # 回复端点故意不在这里默认 AgentScope：宿主应注入 DeepSeek 官模。
    reply_via: str = "deepseek_official"

    @classmethod
    def from_env(cls, env: dict | None = None) -> "ProactiveGreetingConfig":
        e = env if env is not None else os.environ
        enabled = _truthy(e.get("WHALE_PROACTIVE_GREETING") or "")
        # 开启时默认 dry-run；只有显式 DRY_RUN=0/false/off 才真发。
        dry_raw = e.get("WHALE_PROACTIVE_GREETING_DRY_RUN")
        if dry_raw is None or str(dry_raw).strip() == "":
            dry_run = True
        else:
            dry_run = _truthy(str(dry_raw))
        try:
            interval = int((e.get("WHALE_PROACTIVE_GREETING_INTERVAL_S") or "").strip()
                           or DEFAULT_INTERVAL_S)
        except ValueError:
            interval = DEFAULT_INTERVAL_S
        try:
            cooldown = int((e.get("WHALE_PROACTIVE_GREETING_COOLDOWN_S") or "").strip()
                           or DEFAULT_COOLDOWN_S)
        except ValueError:
            cooldown = DEFAULT_COOLDOWN_S
        try:
            quota = int((e.get("WHALE_PROACTIVE_GREETING_DAILY_QUOTA") or "").strip()
                        or DEFAULT_DAILY_QUOTA)
        except ValueError:
            quota = DEFAULT_DAILY_QUOTA
        return cls(
            enabled=enabled,
            dry_run=dry_run,
            interval_s=max(30, interval),
            cooldown_s=max(60, cooldown),
            daily_quota=max(0, quota),
        )


@dataclass
class ProactiveGreetingTick:
    """一次 3min 判断的结果；should_speak=False 时不调用回复模型。"""

    should_speak: bool
    reason: str = ""
    draft: str = ""
    sent: bool = False


def check_send_budget(
    store: QuotaStore | None,
    cfg: ProactiveGreetingConfig,
    *,
    now: float | None = None,
) -> tuple[bool, str]:
    """真发前的额度/冷却门；dry-run 不调用。

    日额度语义：``daily_quota == 0`` → **不限**（仍受冷却约束）；
    正整数 → 上海日历日上限。负数在 from_env 已钳为 0（不限）。
    """
    if store is None:
        return True, "ok_no_store"
    now_ts = time.time() if now is None else float(now)
    last = store.get(LAST_SENT_KEY)
    try:
        last_ts = float(last) if last is not None else 0.0
    except (TypeError, ValueError):
        last_ts = 0.0
    if last_ts and (now_ts - last_ts) < cfg.cooldown_s:
        return False, "cooldown"
    if cfg.daily_quota <= 0:
        return True, "ok_unlimited"
    day = _shanghai_day(datetime.fromtimestamp(now_ts, tz=timezone.utc))
    key = QUOTA_KEY_PREFIX + day
    try:
        used = int(store.get(key) or 0)
    except (TypeError, ValueError):
        used = 0
    if used >= cfg.daily_quota:
        return False, "daily_quota"
    return True, "ok"


def record_send(store: QuotaStore | None, *, now: float | None = None) -> None:
    if store is None:
        return
    now_ts = time.time() if now is None else float(now)
    store.put(LAST_SENT_KEY, now_ts)
    day = _shanghai_day(datetime.fromtimestamp(now_ts, tz=timezone.utc))
    key = QUOTA_KEY_PREFIX + day
    try:
        used = int(store.get(key) or 0)
    except (TypeError, ValueError):
        used = 0
    store.put(key, used + 1)


class ProactiveGreetingLoop:
    """可单测的 tick + 可选后台循环；默认 enabled=False。"""

    def __init__(
        self,
        *,
        config: ProactiveGreetingConfig | None = None,
        judge: JudgeModel | None = None,
        reply_model: ReplyModel | None = None,
        send: Callable[[str], bool | None] | None = None,
        situation_provider: Callable[[], str] | None = None,
        store: QuotaStore | None = None,
        now_fn: Callable[[], float] | None = None,
    ):
        self.config = config or ProactiveGreetingConfig()
        self.judge = judge
        self.reply_model = reply_model
        self.send = send
        self.situation_provider = situation_provider
        self.store = store
        self._now = now_fn or time.time
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def tick(self, *, situation: str = "") -> ProactiveGreetingTick:
        if not self.config.enabled:
            return ProactiveGreetingTick(False, reason="disabled")
        if self.judge is None:
            return ProactiveGreetingTick(False, reason="no_judge")
        # 判断提示刻意短；真实 system/persona 由宿主拼。
        verdict = (self.judge.reply(
            "判断是否现在主动问候主人一句。只回答 yes 或 no。"
            "默认谨慎：没有明显契机就回答 no。\n"
            f"情境：{(situation or '')[:800]}",
            persona="", memories=(), history=()) or "").strip().lower()
        if not (verdict.startswith("y") or verdict.startswith('{"yes')
                or '"should_speak": true' in verdict
                or '"should_speak":true' in verdict):
            return ProactiveGreetingTick(False, reason=f"judge:{verdict[:40]}")
        if self.reply_model is None:
            return ProactiveGreetingTick(True, reason="yes_no_reply_model")
        # 真发路径：额度/冷却耗尽时提前返回，避免再调回复模型浪费 token。
        if not self.config.dry_run:
            ok, why = check_send_budget(self.store, self.config, now=self._now())
            if not ok:
                _log.info("proactive send blocked early reason=%s", why)
                return ProactiveGreetingTick(True, reason=why, draft="", sent=False)
        draft = (self.reply_model.reply(
            "写一句简短自然的主动问候（中文，一两句）。不要提问连环，不要提工具。",
            persona="", memories=(), history=()) or "").strip()
        if not draft:
            return ProactiveGreetingTick(True, reason="empty", draft="")
        if self.config.dry_run:
            _log.info("proactive dry-run draft=%s", draft[:120])
            return ProactiveGreetingTick(True, reason="dry_run", draft=draft, sent=False)
        if self.send is not None:
            send_ok = self.send(draft)
            # False = 明确失败；None/True 视为成功（兼容旧回调只副作用不返回值）。
            if send_ok is False:
                _log.info("proactive send failed draft=%s", draft[:80])
                return ProactiveGreetingTick(
                    True, reason="send_failed", draft=draft, sent=False)
            record_send(self.store, now=self._now())
            return ProactiveGreetingTick(True, reason="sent", draft=draft, sent=True)
        return ProactiveGreetingTick(True, reason="no_send", draft=draft, sent=False)

    def start_background(self) -> bool:
        """启后台循环；未 enabled 或已在跑则返回 False。"""
        if not self.config.enabled:
            return False
        if self._thread is not None and self._thread.is_alive():
            return False
        self._stop.clear()

        def _run() -> None:
            _log.info(
                "proactive greeting loop start interval=%ss dry_run=%s "
                "cooldown=%ss daily_quota=%s",
                self.config.interval_s, self.config.dry_run,
                self.config.cooldown_s, self.config.daily_quota)
            # 启动后先睡一轮，避免服务刚起就打扰。
            if self._stop.wait(self.config.interval_s):
                return
            while not self._stop.is_set():
                try:
                    situation = ""
                    if self.situation_provider is not None:
                        try:
                            situation = self.situation_provider() or ""
                        except Exception as exc:  # noqa: BLE001 — 后台不可因情境失败退出
                            _log.warning("situation_provider failed: %s", type(exc).__name__)
                    tick = self.tick(situation=situation)
                    _log.info(
                        "proactive tick should_speak=%s reason=%s sent=%s",
                        tick.should_speak, tick.reason, tick.sent)
                except Exception as exc:  # noqa: BLE001
                    _log.warning("proactive tick failed: %s", type(exc).__name__)
                if self._stop.wait(self.config.interval_s):
                    break
            _log.info("proactive greeting loop stopped")

        self._thread = threading.Thread(
            target=_run, name="whale-proactive-greeting", daemon=True)
        self._thread.start()
        return True

    def stop_background(self, *, timeout: float = 2.0) -> None:
        self._stop.set()
        th = self._thread
        if th is not None and th.is_alive():
            th.join(timeout=timeout)
