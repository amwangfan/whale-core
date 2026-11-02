"""ConversationOrchestrator：对话流编排器主类（issue #30 拆分）。

按职责组装的 mixin 结构——各协作模块只依赖 ``self`` 状态，互不 import，
不产生循环依赖：

- ``context_resolution.ContextResolutionMixin``：服务端上下文重解析；
- ``turn_orchestration.TurnOrchestrationMixin``：统一入口与唯一规划点；
- ``tool_execution.ToolExecutionMixin``：有界工具执行与派发门控；
- ``persistence.PersistenceMixin``：任务台账/相位机/送达回执；
- ``response_assembly.ResponseAssemblyMixin``：结局回复人格化组装。
"""
from __future__ import annotations

from whale_core.core.companion import WhaleCompanion
from whale_core.core.runtime.context_resolution import ContextResolutionMixin
from whale_core.core.runtime.persistence import PersistenceMixin, _store_runtime_state
from whale_core.core.runtime.response_assembly import ResponseAssemblyMixin
from whale_core.core.runtime.tool_execution import ToolExecutionMixin
from whale_core.core.runtime.tools import (
    MAX_TOOL_ROUNDS,
    TOOL_OUTPUT_MAX_CHARS,
    ActionGatewayLike,
    IntentModel,
    ToolRegistry,
)
from whale_core.core.runtime.turn_orchestration import TurnOrchestrationMixin
from whale_core.memory.store import MemoryStore


class ConversationOrchestrator(
    ContextResolutionMixin,
    TurnOrchestrationMixin,
    ToolExecutionMixin,
    PersistenceMixin,
    ResponseAssemblyMixin,
):
    """对话流编排器：人格/记忆/历史经 WhaleCompanion 复用，工具经 ToolRegistry。"""

    def __init__(
        self,
        store: MemoryStore,
        companion: WhaleCompanion,
        *,
        intent_model: IntentModel | None = None,
        tools: ToolRegistry | None = None,
        gateway: ActionGatewayLike | None = None,
        tool_output_chars: int = TOOL_OUTPUT_MAX_CHARS,
        max_tool_rounds: int = MAX_TOOL_ROUNDS,
        executor_model=None,  # G1-C：可选多轮工具执行模型（llm.tool_executor 角色）
        context_resolver=None,  # S1：服务端会话上下文解析器（life.context_resolver）
        grant_gate=None,  # S4：callable(grant_ids, fingerprint) -> bool；租约重验
    ) -> None:
        self.store = store
        self.companion = companion
        self.intent_model = intent_model
        self.context_resolver = context_resolver
        self.grant_gate = grant_gate
        self.tools = tools or ToolRegistry()
        self.gateway = gateway
        self.executor_model = executor_model
        self.tool_output_chars = tool_output_chars
        self.max_tool_rounds = max(1, min(32, int(max_tool_rounds)))
        state, first_runtime = _store_runtime_state(store)
        self._task_lock = state["task_lock"]
        self._workers = state["workers"]
        self._active_tasks: set[str] = state["active_tasks"]
        self._transient_args: dict[str, dict] = state["transient_args"]
        # A process restart cannot prove whether an in-flight action happened.
        # Preserve it as unknown and never automatically replay it.
        self._delivery_handlers: dict[str, Callable] = {}
        for task_id in ([key.removeprefix("task:") for key in store.keys() if key.startswith("task:")] if first_runtime else []):
            record = store.get(f"task:{task_id}")
            if isinstance(record, dict) and record.get("status") == "running":
                record.update(status="unknown", phase="unknown",
                              error="runtime-restarted-during-execution")
                store.put(f"task:{task_id}", record)

    # 慢能力前缀：这些任务执行耗时（生图/视频/搜索/抓取/语音合成），适合两段式回复。
    # 2026-11-01 生产实证：TTS 经平台代理耗时 10–60s，同步路径 60s 上限会超时，
    # 且同步分支根本不投递音频产物——并入慢前缀走两段式（ack → 完成补发语音）。
    SLOW_CAPABILITY_PREFIXES = ("media.", "web.", "tts.")
