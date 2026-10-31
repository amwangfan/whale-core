"""统一对话编排（ConversationOrchestrator）：roadmap §3.1/§6 的落地。

一条入站消息的标准流程（roadmap §6.2）：

1. 记忆指令捷径（记住/忘掉——确定性、最便宜，不调模型）；
2. 无意图模型 → 直接陪伴对话（便宜路径；多数消息是纯聊天）；
3. 意图模型产出 IntentPlan 文本 → ``plan_from_model_text`` 校验（**核心裁决，
   模型输出只是提议**；解析失败/异常一律回落纯对话并在回执留痕）；
4. kind=chat → 陪伴对话；kind=clarify / needs_clarification → 反问澄清；
5. kind=task → 有界工具执行：按 plan.capabilities 解析 ToolRegistry 中的
   工具，read_only 直接执行；非 read_only 必须经 ActionGateway（插件网关）
   派发——无网关或未映射插件动作一律拒绝，**执行器不能自批**；
6. 工具结果摘要（截断）随人格/记忆/历史一起回流陪伴模型生成最终回复；
7. 回执如实记录；「已计划」绝不伪装成「已执行」（status 字段如实标注）。

与 WhaleAgent（事件流）并存：本类是**对话流**的家；两者共用记忆/人格/
策略/审计组件。长任务预算、超时与优先级独立，不阻塞实时链路。

结构（issue #30）：本包按职责拆分——envelope（信封/相位表）/ tools（工具
契约）/ context_resolution（上下文解析）/ policy（审批绑定与风险裁决）/
turn_orchestration（规划编排）/ tool_execution（有界执行）/ persistence
（台账与送达）/ response_assembly（回复组装）/ orchestrator（主类组装）。
本 __init__ 是兼容 facade：原 ``whale_core.core.runtime`` 模块的公开名
全部原位 re-export（同一对象），外部 import 路径不变。
"""
from __future__ import annotations

# 规划裁决与计划类型（保持在本命名空间：测试经 monkeypatch
# ``whale_core.core.runtime.plan_from_model_text`` 替换裁决函数，
# turn_orchestration._decide 调用期经本 facade 模块属性解析）。
from whale_core.core import budget
from whale_core.core.companion import WhaleCompanion
from whale_core.core.deepseek import ModelError
from whale_core.core.intent_plan import IntentPlan, IntentPlanError, plan_from_model_text
from whale_core.core.memory_commands import handle_memory_command
from whale_core.core.types import ActionIntent
from whale_core.memory.history import append_turn, conversation_lock
from whale_core.memory.store import MemoryStore
from whale_core.plugins.intents import PluginAction
from whale_core.plugins.manifest import RiskClass

from whale_core.core.runtime.context_resolution import (
    ContextResolutionMixin,
    _authz_snapshot,
    _ctx_spaces,
    _ctx_write_space,
)
from whale_core.core.runtime.envelope import (
    _PHASE_STATUS,
    TASK_PHASES,
    InputEnvelope,
    TurnResult,
    _Decision,
)
from whale_core.core.runtime.orchestrator import ConversationOrchestrator
from whale_core.core.runtime.persistence import (
    _STORE_STATES,
    PersistenceMixin,
    _store_runtime_state,
)
from whale_core.core.runtime.policy import (
    _action_fingerprint,
    runtime_approval_verifier,
    runtime_owner_verifier,
    validate_tool_manifest,
)
from whale_core.core.runtime.response_assembly import (
    ResponseAssemblyMixin,
    _scrub_untrusted,
)
from whale_core.core.runtime.tool_execution import ToolExecutionMixin
from whale_core.core.runtime.tools import (
    MAX_TOOL_ROUNDS,
    TOOL_OUTPUT_MAX_CHARS,
    ActionGatewayLike,
    IntentModel,
    ToolRegistry,
    ToolSpec,
)
from whale_core.core.runtime.turn_orchestration import TurnOrchestrationMixin
