"""v2 GUI Agent：observe → decide → act → verify 自主循环。

设计借鉴 /tmp/dsh-mobile-gui-agent（kunjinkao-os/dsh-mobile-gui-agent v0.2.1）：
- 决策一次只出一个动作；act 返回的观察直接作为下一回合观察（省一次往返）；
- 元素绑定 observation_id，过期检测在 transport 内（contracts/v2 §8）；
- 32x32 截图签名做屏稳/变化检测；before/after 双快照确定性验证；
- 卡住检测只做提示不强制终止；侧效动作超时绝不自动重放。

公开口：
- GuiAgentLoop / GuiRunResult / GuiStepRecord（loop）
- DecisionPort / ScriptedDecision / LlmJsonDecision / GuiAction（decision）
- ApprovalPort / classify_risk（approval）
- RunTraceWriter（trace）
- FakeTransport / FakeScreen（testing）
"""

from whale_core.devices.gui_agent.decision import (
    DecisionPort,
    GuiAction,
    LlmJsonDecision,
    ScriptedDecision,
)
from whale_core.devices.gui_agent.loop import (
    GuiAgentLoop,
    GuiRunResult,
    GuiStepRecord,
)
from whale_core.devices.gui_agent.approval import ApprovalPort, classify_risk
from whale_core.devices.gui_agent.trace import RunTraceWriter

__all__ = [
    "ApprovalPort",
    "DecisionPort",
    "GuiAction",
    "GuiAgentLoop",
    "GuiRunResult",
    "GuiStepRecord",
    "LlmJsonDecision",
    "RunTraceWriter",
    "ScriptedDecision",
    "classify_risk",
]
