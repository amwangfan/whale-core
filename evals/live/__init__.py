"""真实模型对话自然度评测基线（issue #40）。

与 ``evals`` 确定性回放基准（issue #19）互补：回放基线用假 Brain 验证真实
代码路径的结构正确性；本包把**真实模型**接入同一条代码路径，产出可供人工
盲评的多轮对话样本，用于版本间的自然度对比。

- ``evals.live.scenarios``：10 类合成多轮质量场景（全部合成，无私聊原文）；
- ``evals.live.live_runner``：真实模型驱动器——群消息先落时间线、旁观不
  发模型（复用 issue #19 的真实代码路径纪律），人格经
  ``build_effective_persona``（issue #36 唯一渲染器）取快照并记录
  ``content_sha``；真实模型调用只允许发生在手动入口；
- ``evals.live.pairing``：A/B 盲评配对（匿名化 + 固定种子可复现翻转）；
- ``evals.live.judge``：模型裁判辅助（**仅供参考，不能单独作通过依据**）；
- ``evals.live.report``：版本对比报告（人工分优先，回归阈值检查）。

数据纪律：场景全部合成；不提交真实私聊；密钥只从环境 / env 文件读取，
绝不写进任何输出或仓库。
"""

from evals.live.scenarios import LIVE_SCENARIOS, LIVE_SCENARIOS_BY_ID, LiveScenario

__all__ = [
    "LIVE_SCENARIOS",
    "LIVE_SCENARIOS_BY_ID",
    "LiveScenario",
]
