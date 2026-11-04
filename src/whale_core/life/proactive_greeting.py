"""主动问候骨架（默认关闭；完整 timer 下回合可接）。

Owner 2026-11-05 确认的分工：
- **判断要不要回**：每约 3 分钟一次，模型走 AgentScope
  ``https://platform.agentscope.io/v1``（该端点下模型目前对其免费，含图/视频）。
- **真正闲聊/问候文案**：仍走 DeepSeek 官方 API（更快），不要用 AgentScope 做回复。
- 本模块只提供配置与空转 tick；不启 timer、不发消息，除非宿主显式
  ``WHALE_PROACTIVE_GREETING=1`` 且注入 outbound。

权限与工具越权无关：问候是陪伴通道，不得借此挂 shell / 写操作。
"""
from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Callable, Protocol


AGENTSCOPE_BASE_URL = "https://platform.agentscope.io/v1"
DEFAULT_INTERVAL_S = 180  # 3 min


class JudgeModel(Protocol):
    """AgentScope 侧：只输出是否该主动说一句（是/否或 JSON）。"""

    def reply(self, text: str, *, persona: str = "", memories=(), history=()) -> str: ...


class ReplyModel(Protocol):
    """DeepSeek 官模：生成问候正文。"""

    def reply(self, text: str, *, persona: str = "", memories=(), history=()) -> str: ...


@dataclass(frozen=True)
class ProactiveGreetingConfig:
    enabled: bool = False
    interval_s: int = DEFAULT_INTERVAL_S
    judge_base_url: str = AGENTSCOPE_BASE_URL
    # 回复端点故意不在这里默认 AgentScope：宿主应注入 DeepSeek 官模。
    reply_via: str = "deepseek_official"

    @classmethod
    def from_env(cls, env: dict | None = None) -> "ProactiveGreetingConfig":
        e = env if env is not None else os.environ
        raw = (e.get("WHALE_PROACTIVE_GREETING") or "").strip().lower()
        enabled = raw in ("1", "true", "yes", "on")
        try:
            interval = int((e.get("WHALE_PROACTIVE_GREETING_INTERVAL_S") or "").strip()
                           or DEFAULT_INTERVAL_S)
        except ValueError:
            interval = DEFAULT_INTERVAL_S
        return cls(enabled=enabled, interval_s=max(30, interval))


@dataclass
class ProactiveGreetingTick:
    """一次 3min 判断的结果；should_speak=False 时不调用回复模型。"""

    should_speak: bool
    reason: str = ""
    draft: str = ""


class ProactiveGreetingLoop:
    """轻量 stub：可单测；默认 enabled=False 时 tick 直接空返回。"""

    def __init__(
        self,
        *,
        config: ProactiveGreetingConfig | None = None,
        judge: JudgeModel | None = None,
        reply_model: ReplyModel | None = None,
        send: Callable[[str], None] | None = None,
    ):
        self.config = config or ProactiveGreetingConfig()
        self.judge = judge
        self.reply_model = reply_model
        self.send = send

    def tick(self, *, situation: str = "") -> ProactiveGreetingTick:
        if not self.config.enabled:
            return ProactiveGreetingTick(False, reason="disabled")
        if self.judge is None:
            return ProactiveGreetingTick(False, reason="no_judge")
        # 判断提示刻意短；真实 system/persona 由宿主拼。
        verdict = (self.judge.reply(
            "判断是否现在主动问候主人一句。只回答 yes 或 no。\n"
            f"情境：{(situation or '')[:800]}",
            persona="", memories=(), history=()) or "").strip().lower()
        if not verdict.startswith("y"):
            return ProactiveGreetingTick(False, reason=f"judge:{verdict[:40]}")
        if self.reply_model is None:
            return ProactiveGreetingTick(True, reason="yes_no_reply_model")
        draft = (self.reply_model.reply(
            "写一句简短自然的主动问候（中文，一两句）。",
            persona="", memories=(), history=()) or "").strip()
        if draft and self.send is not None:
            self.send(draft)
        return ProactiveGreetingTick(True, reason="sent" if draft else "empty",
                                     draft=draft)
