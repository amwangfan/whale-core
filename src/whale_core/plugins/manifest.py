"""插件清单：能力声明 + 风险分级。注册时校验，运行期只读。

安全约定（与 shell_policy 同款思路——护栏在核心，隔离在宿主）：

- 事件 kind 必须带命名空间（``a.b`` 形式），且**不得冒充核心事件**
  （核心 kind 与核心命名空间前缀列入拒绝清单）；
- 动作名**禁止包装任意 shell**（shell/exec/run 等保留名一律拒绝）——
  系统 shell 只有 ``cloud.shell_policy`` 分档一个入口；
- 每个动作必须在 ``action_risk`` 声明风险级，未声明的动作在策略层直接拒绝；
- 清单不含任何凭据；插件配置（token 等）只存在宿主机私有配置 / env。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping

PLUGIN_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,63}$")
EVENT_KIND_RE = re.compile(r"^[a-z0-9_]+(?:\.[a-z0-9_]+)+$")
ACTION_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

# 核心感知事件与其命名空间：插件不得声明，防止伪造核心语义
CORE_EVENT_KINDS = frozenset(
    {
        "phone.foreground",
        "schedule.tick",
        "location.hint",
        "ambient.speech_segment",
        "context.course",
        "context.weather",
        "context.location",
        "nav.route_progress",
    }
)
CORE_EVENT_PREFIXES = ("phone.", "schedule.", "location.", "ambient.", "context.", "nav.")

# 保留动作名：任意 shell 只能走 cloud.shell_policy 分档，插件不得提供等价物
FORBIDDEN_ACTION_NAMES = frozenset(
    {"shell", "exec", "execute", "system", "command", "run", "spawn", "eval"}
)

# 信任级别（G4）：bundled = 随仓发行/宿主显式装配的官方插件；
# external = 第三方清单。trust 由**放置清单的宿主/运维者**声明（清单目录是
# 宿主控制的显式加载面），插件自身无法借此提权——默认 bundled 保持既有行为。
TRUST_LEVELS = frozenset({"bundled", "external"})
DEFAULT_TRUST = "bundled"


class RiskClass(str, Enum):
    """动作风险级（决定门控强度，见 plugins.policy）。"""

    READ_ONLY = "read_only"        # 只读查询，无外部副作用（如搜邮箱、看日程）
    COMMUNICATE = "communicate"    # 对外发送 / 通知（发信息、写邮件、桌面提醒）
    TRANSACTION = "transaction"    # 交易 / 不可逆外部副作用（买东西、下单、支付）


class PluginKind(str, Enum):
    """插件六分类（前四类为维护者定义，后两类为架构补齐）：

    - INPUT：只产事件（前台监测「手机在看什么应用」、邮件接收、日程、刷视频、
      日常语音、雷鸟全天纪要）；
    - OUTPUT：只执行动作（发邮件、桌面弹提示、购物下单）；
    - CHANNEL：输入输出多端共用（QQ、微信、通话桥——收发消息/事件 + 发送动作）；
    - PROCESSOR：内部处理型（意图执行委托、摘要、翻译、记忆整理）——
      无对外 I/O，声明 ``provides`` 能力；典型用法：DeepSeek 产操作意图，
      委托 qwen 等执行器模型跑工具循环，只回摘要（上下文隔离，见 processing.py）；
    - MODEL：模型供给型——注册模型端点能力（chat / realtime / asr / tts /
      embedding），对应编排方案四档路由与 RealtimeProvider 抽象；端点凭据只在
      宿主 env / 私有配置，manifest 不含；
    - MEMORY：记忆/存储型——替代 MemoryStore 后端、混合召回（嵌入+重排）、
      同步 / 备份目标（编排方案 §3 阶段 B 与召回升级的挂载点）。

    PROCESSOR / MODEL / MEMORY 统称 provider 类：无 inputs/actions，只声明
    provides 能力，宿主按能力名解析接线。

    **不可插件化（安全不变量）**：Initiative 频控、ShellPolicy 分档、
    OwnerChannelRegistry 身份门、PluginActionPolicy 风险矩阵属于核心，
    任何插件类型都不能替换或削弱它们。
    """

    INPUT = "input"
    OUTPUT = "output"
    CHANNEL = "channel"
    PROCESSOR = "processor"
    MODEL = "model"
    MEMORY = "memory"


PROVIDER_KINDS: tuple[PluginKind, ...] = (
    PluginKind.PROCESSOR,
    PluginKind.MODEL,
    PluginKind.MEMORY,
)


@dataclass(frozen=True)
class PluginManifest:
    """一个插件的能力声明。inputs/动作全部显式枚举，注册时校验。

    ``kind`` 可省略（按声明的能力推断：有 inputs+actions → channel；
    只 inputs → input；只 actions → output）；processor 必须显式声明
    kind="processor" 且给出 ``provides`` 能力名。显式 kind 与声明能力
    不一致时拒绝（防止「自称只读却带动作」的错标）。

    ``api_version`` 声明清单 API 版本（如 ``"1"`` / ``"1.2"``）；加载方
    （PluginManager）按 major 部分（``"."`` 前）判定兼容性，major 不一致
    拒绝加载。默认 ``"1"``。
    """

    plugin_id: str
    name: str
    version: str
    author: str = ""
    description: str = ""
    homepage: str = ""
    kind: str = ""  # "" = 推断；或 PluginKind 值
    inputs: tuple[str, ...] = ()  # 命名空间事件 kind，如 "email.received"
    actions: tuple[str, ...] = ()
    action_risk: Mapping[str, str] = field(default_factory=dict)
    provides: tuple[str, ...] = ()  # 仅 processor：能力名，如 "operation_execution"
    process_model: str = "external"  # external（外置进程，推荐）| in_process（owner 自写）
    api_version: str = "1"  # 清单 API 版本；加载方按 major（"." 前）判定兼容性
    trust: str = DEFAULT_TRUST  # bundled | external（G4；见 policy 信任钩子）

    def __post_init__(self) -> None:
        if not PLUGIN_ID_RE.fullmatch(self.plugin_id or ""):
            raise ValueError(f"invalid plugin_id: {self.plugin_id!r}")
        if not (self.name or "").strip() or not (self.version or "").strip():
            raise ValueError("manifest requires name and version")
        if self.process_model not in ("external", "in_process"):
            raise ValueError(f"invalid process_model: {self.process_model!r}")
        if not isinstance(self.trust, str) or self.trust not in TRUST_LEVELS:
            raise ValueError(
                f"invalid trust {self.trust!r}: must be one of {sorted(TRUST_LEVELS)}"
            )
        major = self.api_version.split(".", 1)[0] if isinstance(self.api_version, str) else ""
        if not major or not (major.isascii() and major.isdigit()):
            raise ValueError(f"invalid api_version: {self.api_version!r}")

        seen_inputs: set[str] = set()
        for kind in self.inputs:
            if not EVENT_KIND_RE.fullmatch(kind or ""):
                raise ValueError(f"invalid event kind {kind!r}: must be namespaced a.b")
            if kind in CORE_EVENT_KINDS or kind.startswith(CORE_EVENT_PREFIXES):
                raise ValueError(f"event kind {kind!r} impersonates a core event")
            if kind in seen_inputs:
                raise ValueError(f"duplicate input kind {kind!r}")
            seen_inputs.add(kind)

        seen_actions: set[str] = set()
        for action in self.actions:
            if not ACTION_NAME_RE.fullmatch(action or ""):
                raise ValueError(f"invalid action name {action!r}")
            if action in FORBIDDEN_ACTION_NAMES:
                raise ValueError(
                    f"action {action!r} is reserved: arbitrary shell must go through "
                    "cloud.shell_policy tiers, never through plugins"
                )
            if action in seen_actions:
                raise ValueError(f"duplicate action {action!r}")
            seen_actions.add(action)

        for action, risk in dict(self.action_risk).items():
            if action not in seen_actions:
                raise ValueError(f"action_risk declares unknown action {action!r}")
            try:
                RiskClass(risk)
            except ValueError:
                raise ValueError(f"invalid risk class {risk!r} for action {action!r}") from None

        # 分类一致性（resolved_kind 内含全部校验，构造期即失败）
        self.resolved_kind

    @property
    def resolved_kind(self) -> PluginKind:
        """显式 kind 或按能力推断；不一致 / 无能力声明一律 ValueError。"""
        declared = (self.kind or "").strip().lower()
        if declared and declared not in {k.value for k in PluginKind}:
            raise ValueError(f"invalid plugin kind: {self.kind!r}")
        has_in, has_out, has_prov = bool(self.inputs), bool(self.actions), bool(self.provides)

        provider_values = {k.value for k in PROVIDER_KINDS}
        if declared in provider_values:
            if has_in or has_out:
                raise ValueError(
                    f"{declared} plugins declare no inputs/actions (internal provider only)"
                )
            if not has_prov:
                raise ValueError(f"{declared} plugins must declare provides capabilities")
            return PluginKind(declared)

        if declared:
            expected = PluginKind(declared)
        elif has_in and has_out:
            expected = PluginKind.CHANNEL
        elif has_in:
            expected = PluginKind.INPUT
        elif has_out:
            expected = PluginKind.OUTPUT
        else:
            raise ValueError(
                "manifest declares no capabilities: inputs/actions empty "
                "(provider kinds require explicit kind + provides)"
            )

        if expected is PluginKind.INPUT and (has_out or has_prov):
            raise ValueError("input plugins declare inputs only (no actions/provides)")
        if expected is PluginKind.OUTPUT and (has_in or has_prov):
            raise ValueError("output plugins declare actions only (no inputs/provides)")
        if expected is PluginKind.CHANNEL and not (has_in and has_out):
            raise ValueError("channel plugins require both inputs and actions")
        if has_prov:
            raise ValueError("provides is for provider-kind plugins only (processor/model/memory)")
        return expected

    def risk_of(self, action: str) -> RiskClass | None:
        """动作风险级；未声明返回 None（策略层将拒绝执行）。"""
        raw = dict(self.action_risk).get(action)
        return RiskClass(raw) if raw is not None else None
