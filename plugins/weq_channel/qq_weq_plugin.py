"""QQ/WeQ channel 插件：把既有 life/qq_* 组件包装成插件层标准四件套。

对应 docs/plugin-architecture.md：
- §1.1 六分类中的 **channel**（输入输出多端共用，QQ 为点名先例）；
- §5 部署两档中的 **外置进程**（WeQ 是独立 systemd 进程：入站 HTTP SSE 推送、
  出站本机 API + token），本模块只是宿主侧的进程内适配与注册件；
- §7 P1「plugin_action 与 ProactiveOutbox 的组合模板（communicate 默认走草稿
  审批流）」的第一个真实实例。

安全边界全部委托既有核心件，本模块**不新增任何放宽**：
- 身份门：OwnerChannelRegistry 配对仍在监听层（WeQSsePushListener）完成，
  未配对事件根本不进插件层；PluginEvent trust 固定 external；
- 审批与风险：send_message 声明 communicate 级，PluginGateway 经宿主可信
  verifier 查台账（意图 payload 不能自declare owner/审批）；
- 预算与账本：outbox 模式下发送仍走 ProactiveOutbox.dispatch——每日上限、
  静默时段、unknown 不重试、accepted/failed 记账全部沿用既有语义；
- 凭据：WeQ token 只在宿主 secrets/env（WeQWebSendPort 构造注入），
  不进 manifest、不进回执。
"""
from __future__ import annotations

from collections import deque
from collections.abc import Callable, Iterator, Mapping
from datetime import datetime, timezone
from typing import Any

from whale_core.core.types import ActionIntent
from whale_core.life.channels import OwnerEvent
from whale_core.life.initiative import ProactiveDraft, ProactivePrefs
from whale_core.plugins import (
    PluginAction,
    PluginActionPolicy,
    PluginEvent,
    PluginGateway,
    PluginManifest,
    PluginRegistry,
    RiskClass,
)

QQ_WEQ_PLUGIN_ID = "qq_weq"
QQ_MESSAGE_KIND = "qq.message"
SEND_ACTION = "send_message"


def qq_weq_manifest(version: str = "0.1.0") -> PluginManifest:
    """QQ/WeQ channel 插件的能力声明（无任何凭据字段）。"""
    return PluginManifest(
        plugin_id=QQ_WEQ_PLUGIN_ID,
        name="QQ/WeQ 通道插件",
        version=version,
        author="whale-io",
        description=(
            "WeQ 外置进程通道：入站 SSE 推送产 qq.message（已过 OwnerChannelRegistry "
            "身份门），出站 send_message（communicate 风险级，PluginGateway 强制 "
            "owner 校验 + 审批，账本沿用 ProactiveOutbox）。"
        ),
        kind="channel",
        inputs=(QQ_MESSAGE_KIND,),
        actions=(SEND_ACTION,),
        action_risk={SEND_ACTION: RiskClass.COMMUNICATE.value},
        process_model="external",
    )


class WeQEventAdapter:
    """PerceptionAdapter：已过身份门的 owner 事件 → PluginEvent(qq.message)。

    宿主从 WeQSsePushListener 的 on_owner_event 回调喂入（feed）；未配对 / 群聊 /
    自发消息在监听层就被拦下，到不了这里。有界队列：满时丢弃新事件并计数
    （dropped），不静默——与监听层同款语义。WhaleAgent 型宿主经
    ``registry.input_adapters()`` 取到本 adapter 后用 poll() 消费。
    """

    def __init__(self, max_pending: int = 256) -> None:
        self._pending: deque[PluginEvent] = deque()
        self._max = max(1, max_pending)
        self.dropped = 0

    def feed(self, event: OwnerEvent) -> bool:
        """入队一条 owner 事件；队列满返回 False 并计入 dropped。"""
        if len(self._pending) >= self._max:
            self.dropped += 1
            return False
        self._pending.append(
            PluginEvent(
                kind=QQ_MESSAGE_KIND,
                plugin_id=QQ_WEQ_PLUGIN_ID,
                payload={
                    "owner_id": event.owner_id,
                    "channel": event.channel,
                    "conversation": event.conversation,
                    "message_id": event.message_id,
                    "text": event.text,
                    "attachments": list(getattr(event, "attachments", ())),
                    # S1/S7：真实发送者必须随事件穿透——丢失它会让下游
                    # 身份解析退化为匿名（主人私聊记忆失效的根因）。
                    "sender_id": getattr(event, "sender_id", ""),
                    # G1：@/引用/时间元数据同样穿透
                    "sender_uid": getattr(event, "sender_uid", ""),
                    "mentions": list(getattr(event, "mentions", ()) or ()),
                    "reply_to": list(getattr(event, "reply_to", ()) or ()),
                },
            )
        )
        return True

    def poll(self) -> Iterator[PluginEvent]:
        while self._pending:
            yield self._pending.popleft()


class WeQSendExecutor:
    """PluginExecutor：send_message → 真实出站。两种模式（宿主注入决定）：

    - **outbox 模式（生产推荐）**：复用 ``ProactiveOutbox.dispatch``——风控预算
      （每日上限/静默时段）、账本记账（accepted/unknown/failed）、unknown 不自动
      重试语义全部留在既有核心链路；回执 = 账本裁决摘要。此模式下
      ``explicitly_approved=True`` 不是绕过审批：审批已由 PluginGateway 的
      approval_verifier（宿主台账）在执行前强制，Outbox 在此只承担预算与记账，
      避免同一动作被两层重复 hold。
    - **直连模式**：直接调 ``OutboundPort.send``（测试与无账本宿主用）。

    回执契约：短文本（网关再截 120 字）、无 token/密钥、无消息正文。
    ``target`` 只进回执便于溯源，不改变发送目的地——目的地绑定在 port 侧
    （WeQWebSendPort 单一对端），本 executor 不接受任何「换目标」语义。
    """

    def __init__(self, port: Any, *, outbox: Any = None, prefs: ProactivePrefs | None = None,
                 now_fn: Callable[[], datetime] | None = None) -> None:
        if outbox is not None and prefs is None:
            raise ValueError("outbox mode requires explicit prefs (budget must never default silently)")
        self._port = port
        self._outbox = outbox
        self._prefs = prefs
        self._now = now_fn or (lambda: datetime.now(timezone.utc))
        self.last_receipt = ""

    def execute(self, action: str, target: str, args: Mapping[str, Any]) -> tuple[bool, str]:
        if action != SEND_ACTION:
            return False, f"unsupported action {action!r}"
        draft_id = str(args.get("draft_id") or "")
        if self._outbox is not None:
            if not draft_id:
                return False, "outbox mode requires draft_id"
            verdict = self._outbox.dispatch(draft_id, self._prefs, now=self._now(),
                                            port=self._port, explicitly_approved=True,
                                            passive_reply=True)
            ok = verdict.status == "accepted"
            receipt = f"{verdict.status}:{verdict.reason}"
        else:
            text = str(args.get("text") or "")
            if not text.strip():
                return False, "empty text"
            draft = ProactiveDraft(draft_id or f"plugin-qq:{target[:40]}", text,
                                   "plugin send_message", (f"target:{target[:80]}",),
                                   channel="direct_message")
            ok = bool(self._port.send(draft))
            receipt = "sent" if ok else "port refused or unreachable"
        self.last_receipt = receipt[:120]
        return ok, self.last_receipt


def register_qq_weq_plugin(
    registry: PluginRegistry,
    *,
    port: Any,
    outbox: Any = None,
    prefs: ProactivePrefs | None = None,
    adapter: WeQEventAdapter | None = None,
    max_pending: int = 256,
) -> tuple[WeQEventAdapter, WeQSendExecutor]:
    """宿主一行注册（§7 验收口径）。返回 (adapter, executor) 供宿主接线。

    outbox 建议传工作流共享的同一 ProactiveOutbox 实例（同一账本，避免双份记账）。
    """
    adapter = adapter or WeQEventAdapter(max_pending=max_pending)
    executor = WeQSendExecutor(port, outbox=outbox, prefs=prefs)
    registry.register(qq_weq_manifest(), executor=executor, input_adapter=adapter)
    return adapter, executor


def send_message_intent(conversation: str, text: str, *, draft_id: str = "") -> ActionIntent:
    """构造 send_message 的 plugin_action 意图（不携带任何身份/审批声明）。"""
    args: dict[str, Any] = {"text": text}
    if draft_id:
        args["draft_id"] = draft_id
    return PluginAction(QQ_WEQ_PLUGIN_ID, SEND_ACTION, conversation, args).to_intent()


def _draft_id_of(intent: ActionIntent) -> str:
    payload = intent.payload if isinstance(intent.payload, Mapping) else {}
    args = payload.get("args") if isinstance(payload, Mapping) else None
    if not isinstance(args, Mapping):
        return ""
    return str(args.get("draft_id") or "")


def build_qq_weq_gateway(
    registry: PluginRegistry,
    *,
    owner_draft_ids: set[str],
    approved_draft_ids: set[str],
) -> PluginGateway:
    """装配接好宿主可信状态的 PluginGateway。

    两个 verifier 只查宿主自己的台账集合，绝不看意图 payload 的自declare字段：
    - owner_draft_ids：工作流处理 **owner 事件**产出草稿时加入（身份门的下游事实）；
    - approved_draft_ids：操作者批准（approve 子命令 / --auto-approve 档）时加入。
    集合为空 = 全拒绝（fail-closed，与 PluginGateway 默认一致）。
    """
    return PluginGateway(
        registry,
        PluginActionPolicy(registry),
        owner_verifier=lambda intent: _draft_id_of(intent) in owner_draft_ids,
        approval_verifier=lambda intent: _draft_id_of(intent) in approved_draft_ids,
    )
