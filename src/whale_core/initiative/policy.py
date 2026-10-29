"""打扰控制 / 提醒策略接口 + QuietHours / Disturbance / ProactiveCall stubs。"""

from __future__ import annotations

from datetime import datetime, time, timezone
from typing import Any, Protocol, Sequence, runtime_checkable
from zoneinfo import ZoneInfo

from whale_core.core.types import Decision, Observation
from whale_core.perception.events import PerceptionEvent, PhoneForeground

# 用户默认时区：Asia/Shanghai（UTC+8）
DEFAULT_TZ = ZoneInfo("Asia/Shanghai")


@runtime_checkable
class InitiativePolicy(Protocol):
    def should_suppress(self, observation: Observation, decision: Decision) -> bool:
        ...


class SafeDefaultPolicy:
    """Fail closed for sensitive intents until configured by the owner."""
    SENSITIVE = frozenset({"place_call", "phone_action", "ambient_listen"})
    def should_suppress(self, observation: Observation, decision: Decision) -> bool:
        return any(intent.kind in self.SENSITIVE for intent in decision.intents)


class AlwaysAllowPolicy:
    """测试用：从不抑制。"""

    def should_suppress(self, observation: Observation, decision: Decision) -> bool:
        return False


def _coerce_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


class QuietHoursPolicy:
    """简单静默时段策略：在 [start, end) 内抑制主动意图。

    跨午夜时（如 22:00–07:00）按「或」区间处理。
    判定时刻优先：observation.context["now"] → observed_at → 墙钟。
    """

    def __init__(
        self,
        *,
        start: time = time(22, 0),
        end: time = time(7, 0),
        tz: ZoneInfo = DEFAULT_TZ,
    ) -> None:
        self.start = start
        self.end = end
        self.tz = tz

    def _now_local(self) -> datetime:
        return datetime.now(timezone.utc).astimezone(self.tz)

    def resolve_when(self, observation: Observation | None = None) -> datetime:
        """从 Observation 解析用于静默判定的本地时刻。"""
        if observation is not None:
            ctx_now = _coerce_datetime((observation.context or {}).get("now"))
            if ctx_now is not None:
                return ctx_now.astimezone(self.tz) if ctx_now.tzinfo else ctx_now.replace(
                    tzinfo=self.tz
                )
            if observation.observed_at is not None:
                oa = observation.observed_at
                return oa.astimezone(self.tz) if oa.tzinfo else oa.replace(tzinfo=self.tz)
        return self._now_local()

    def in_quiet_hours(self, when: datetime | None = None) -> bool:
        local = (when or self._now_local()).astimezone(self.tz)
        t = local.time()
        if self.start == self.end:
            # 空区间：永不静默（便于测试「全天允许」）
            return False
        if self.start < self.end:
            return self.start <= t < self.end
        # 跨午夜：start..24 或 0..end
        return t >= self.start or t < self.end

    def should_suppress(self, observation: Observation, decision: Decision) -> bool:
        if not decision.intents:
            return False
        return self.in_quiet_hours(self.resolve_when(observation))


class DisturbancePolicy:
    """提醒 vs 不打扰：综合静默时段 + 前台忙碌。

    - QuietHours 内默认抑制（保留 QuietHoursPolicy 行为）。
    - 前台包名落在 busy_packages 时抑制非紧急意图（含主动 show_card）。
    - Observation.context["urgent"] 为真时可放行（仍不得绕过基础设施鉴权）。
    """

    def __init__(
        self,
        *,
        quiet: QuietHoursPolicy | None = None,
        busy_packages: Sequence[str] = ("com.tencent.mm", "com.ss.android.ugc.aweme"),
        busy_labels: Sequence[str] = (),
    ) -> None:
        self.quiet = quiet or QuietHoursPolicy()
        self.busy_packages = frozenset(busy_packages)
        self.busy_labels = frozenset(busy_labels)

    def is_foreground_busy(self, observation: Observation) -> bool:
        ctx = dict(observation.context or {})
        fg_pkg = str(ctx.get("foreground_package") or "")
        fg_label = str(ctx.get("foreground_label") or "")
        if fg_pkg and fg_pkg in self.busy_packages:
            return True
        if fg_label and fg_label in self.busy_labels:
            return True
        return bool(ctx.get("user_busy"))

    def should_suppress(self, observation: Observation, decision: Decision) -> bool:
        if not decision.intents:
            return False
        if observation.context.get("urgent"):
            return False
        if self.quiet.should_suppress(observation, decision):
            return True
        return self.is_foreground_busy(observation)

    @staticmethod
    def enrich_busy_from_events(
        observation: Observation, events: Sequence[PerceptionEvent]
    ) -> Observation:
        """从 PhoneForeground 事件补全 busy 上下文字段（纯数据，无设备调用）。"""
        ctx = dict(observation.context)
        for e in events:
            if isinstance(e, PhoneForeground) or e.kind == "phone.foreground":
                pkg = getattr(e, "package", "") or (e.payload or {}).get("package", "")
                label = getattr(e, "label", "") or (e.payload or {}).get("label", "")
                if pkg:
                    ctx["foreground_package"] = pkg
                if label:
                    ctx["foreground_label"] = label
        return Observation(
            event_ids=observation.event_ids,
            summary=observation.summary,
            context=ctx,
            observed_at=observation.observed_at,
        )


class ProactiveCallPolicy:
    """何时允许 AI 主动打电话（PlaceCall 意图）。

    Stub 规则（可后续接模型）：
    - 默认不允许主动呼叫；
    - Observation.context["allow_proactive_call"] 为真，且不在静默时段，才允许；
    - 或情境 urgency_hint==elevated 且显式 suggest_call / call_reason（仍受静默约束）；
    - 紧急标记 urgent 可覆盖静默（仍由基础设施执行 VoIP 来电，本层只放行意图）。
    提议侧见 ProactiveCallPlanner；本类只做放行 / 过滤。
    """

    def __init__(self, *, quiet: QuietHoursPolicy | None = None) -> None:
        self.quiet = quiet or QuietHoursPolicy()

    def allows_proactive_call(self, observation: Observation) -> bool:
        ctx = observation.context or {}
        if ctx.get("urgent"):
            return True
        allowed = bool(ctx.get("allow_proactive_call"))
        if not allowed:
            # 情境 elevated + 显式 suggest_call / call_reason → 等同允许旗标
            sit = ctx.get("situation") or {}
            if isinstance(sit, dict) and sit.get("urgency_hint") == "elevated":
                if ctx.get("suggest_call") or ctx.get("call_reason"):
                    allowed = True
        if not allowed:
            return False
        if self.quiet.in_quiet_hours(self.quiet.resolve_when(observation)):
            return False
        return True

    def filter_decision(self, observation: Observation, decision: Decision) -> Decision:
        """从决策中去掉不被允许的 place_call 意图。"""
        if self.allows_proactive_call(observation):
            return decision
        kept = tuple(i for i in decision.intents if i.kind != "place_call")
        if len(kept) == len(decision.intents):
            return decision
        return Decision(
            intents=kept,
            rationale=f"{decision.rationale}; proactive_call filtered",
            suppress=decision.suppress and not kept,
            decided_at=decision.decided_at,
        )
