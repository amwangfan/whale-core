"""云端行动闭环 stub。

入站事件 → Companion/Reasoner（或规则选择器）选行动
→ 经 Actions 网关发出「推送卡片」「shell 白名单 / 分档命令」意图。

默认不发真实 HTTP；MockHttpActionGateway 只记录拟发送载荷。
密钥 / Base URL 只读环境变量，不入库、不编造。

shell 分档（裁决在 ``whale_core.cloud.shell_policy``，本模块只产意图）：

- ``tool:name`` / ``shell:name`` / 关键词 → 白名单工具意图（``tool_verdict`` 按档放行）；
- safe 档关键词（网页搜索类）→ ``web_search`` 等只读工具，managed 档不放行；
- ``cmd:命令`` → 自由命令：仅 personal 档 + ``meta.owner_authenticated`` 才产生
  ``shell_command`` 意图，被拒时回一张 ``shell_denied`` 卡保证闭环可观测；
- 裁决记录在 ``CloudActionLoop.last_verdicts``，审计入口 ``ShellPolicy.audit``。
"""

from __future__ import annotations

from typing import Protocol, Sequence

from whale_core.actions.gateway import ActionGateway, CompositeActionGateway
from whale_core.actions.intents import ShellCommand, ShellWhitelist, ShowCard
from whale_core.cloud.events import InboundEvent
from whale_core.cloud.shell_policy import MANAGED_TOOLS, ShellPolicy, ShellVerdict
from whale_core.core.types import ActionIntent

# canonical 定义在 cloud.shell_policy；保留旧名以兼容既有引用与文档
DEFAULT_SHELL_ALLOWLIST: frozenset[str] = MANAGED_TOOLS

# 自由命令前缀（与 tool:/shell: 白名单前缀区分）
CMD_PREFIXES: tuple[str, ...] = ("cmd:",)

# safe 档检索类工具关键词（「网页搜索之类」）；managed 档由 tool_verdict 拦截
_SEARCH_KEYS: tuple[str, ...] = (
    "网页搜索",
    "网上搜",
    "搜一下",
    "查一下网页",
    "查资料",
    "web search",
    "search online",
    "search the web",
)


class ActionSelector(Protocol):
    def select(self, event: InboundEvent) -> Sequence[ActionIntent]:
        """从入站事件选出零或多条行动意图。"""
        ...


class RuleActionSelector:
    """最小可跑规则选择器（无 LLM、无网络）。

    - 消息含「卡片」「推送」→ show_card
    - 消息匹配白名单工具关键词 / 显式 tool:name → shell_whitelist（按档裁决）
    - 显式 cmd:命令 → ShellPolicy 裁决；放行发 shell_command，拒绝发 shell_denied 卡
    - tool_result → 推一张结果摘要卡；tool_name 通过档位裁决则附带回执意图
    """

    def __init__(
        self,
        *,
        allowlist: frozenset[str] | None = None,
        shell_policy: ShellPolicy | None = None,
    ) -> None:
        if shell_policy is not None:
            self.policy = shell_policy
        elif allowlist is not None:
            self.policy = ShellPolicy(managed_tools=allowlist)
        else:
            self.policy = ShellPolicy()
        self.allowlist = self.policy.managed_tools
        self.last_verdicts: tuple[ShellVerdict, ...] = ()

    def select(self, event: InboundEvent) -> Sequence[ActionIntent]:
        text = (event.text or "").strip()
        if event.kind == "tool_result":
            intents: list[ActionIntent] = [
                ShowCard(
                    title="工具结果",
                    body=text[:500] or f"{event.tool_name or 'tool'} done",
                    layout_hint="tool_result",
                ).to_intent()
            ]
            name = (event.tool_name or "").strip()
            if name and self.policy.tool_verdict(name).allowed:
                intents.append(
                    ShellWhitelist(tool_name=name, args={"echo_result": True}).to_intent()
                )
            return intents

        # message
        lower = text.lower()
        intents_out: list[ActionIntent] = []
        verdicts: list[ShellVerdict] = []

        explicit = _explicit_tool(text, self.policy)
        if explicit:
            intents_out.append(ShellWhitelist(tool_name=explicit).to_intent())

        keyword_tool = _keyword_tool(text, self.policy)
        if keyword_tool and keyword_tool not in {i.payload.get("tool_name") for i in intents_out}:
            intents_out.append(ShellWhitelist(tool_name=keyword_tool).to_intent())

        command = _explicit_command(text)
        if command is not None:
            verdict = self.policy.command_verdict(
                command,
                is_owner=bool((event.meta or {}).get("owner_authenticated")),
            )
            verdicts.append(verdict)
            if verdict.allowed:
                intents_out.append(
                    ShellCommand(command=command, tier=self.policy.tier.value).to_intent()
                )
            else:
                intents_out.append(
                    ShowCard(
                        title="命令未放行",
                        body=f"{verdict.reason}（档位 {self.policy.tier.value}）",
                        layout_hint="shell_denied",
                    ).to_intent()
                )

        if any(k in text for k in ("卡片", "推送", "通知")) or "card" in lower or "push" in lower:
            intents_out.append(
                ShowCard(title="云端推送", body=text[:500] or "（空消息）").to_intent()
            )

        # 无匹配时：默认回一张轻量确认卡，保证闭环可观测
        if not intents_out:
            intents_out.append(
                ShowCard(
                    title="已收到",
                    body=text[:500] or f"from:{event.source or 'unknown'}",
                    layout_hint="ack",
                ).to_intent()
            )

        self.last_verdicts = tuple(verdicts)
        return intents_out


def _explicit_tool(text: str, policy: ShellPolicy) -> str | None:
    # 支持 "tool:what_time" / "shell:bridge_status"；放行与否由分档策略裁决
    for prefix in ("tool:", "shell:"):
        if prefix in text:
            rest = text.split(prefix, 1)[1].strip()
            if not rest:
                continue
            name = rest.split()[0].strip(",.;")
            if policy.tool_verdict(name).allowed:
                return name
    return None


def _keyword_tool(text: str, policy: ShellPolicy) -> str | None:
    rules: list[tuple[tuple[str, ...], str]] = [
        (("几点", "what time", "现在时间"), "what_time"),
        (("桥状态", "bridge status", "查桥"), "bridge_status"),
        (("voip", "来电", "响铃"), "ring_fake_call"),
        (("工作区", "list workspace", "列目录"), "list_workspace"),
        (("桥日志", "bridge log"), "tail_bridge_log"),
        (("重启桥", "restart bridge"), "restart_bridge"),
        (_SEARCH_KEYS, "web_search"),
    ]
    lower = text.lower()
    for keys, name in rules:
        if not policy.tool_verdict(name).allowed:
            continue
        if any(k in text or k in lower for k in keys):
            return name
    return None


def _explicit_command(text: str) -> str | None:
    """解析 "cmd:<命令>"：取前缀后全部内容（保留原始参数与空格）。"""
    for prefix in CMD_PREFIXES:
        if prefix in text:
            frag = text.split(prefix, 1)[1].strip()
            return frag or None
    return None


class CloudActionLoop:
    """最小可跑闭环：handle(event) → receipts；shell 裁决暴露在 last_verdicts。"""

    def __init__(
        self,
        *,
        gateway: ActionGateway | None = None,
        selector: ActionSelector | None = None,
    ) -> None:
        self.gateway = gateway or CompositeActionGateway()
        self.selector = selector or RuleActionSelector()
        self.last_intents: tuple[ActionIntent, ...] = ()
        self.last_receipts: list[str] = []
        self.last_verdicts: tuple[ShellVerdict, ...] = ()

    def handle(self, event: InboundEvent) -> list[str]:
        intents = tuple(self.selector.select(event))
        self.last_intents = intents
        self.last_verdicts = tuple(getattr(self.selector, "last_verdicts", ()))
        receipts = [self.gateway.dispatch(i) for i in intents]
        self.last_receipts = receipts
        return receipts
