"""人格自然度 / 关系连续性回放基准（issue #19）。

可复现回放集 + 确定性指标：

- ``evals.scenarios``：固定样例集（消息序列带 sender_id/conversation_id/时间，
  每条 focus 消息带确定性断言）；
- ``evals.runner``：回放执行器——InMemoryStore + 假 Brain 驱动真实的
  RelationshipResolver / derive_policy / GroupTimelineStore / WhaleCompanion
  代码路径，不调任何真实模型 API；
- ``evals.metrics``：确定性指标（归属正确率 / 自述事实召回 / 客服腔命中 /
  误亲密检查），全部是字符串与结构比对，不用模型自评；
- ``evals.report``：markdown 评测报告（场景 × 指标表格 + 通过/失败）。

手动跑报告（whale_core 未安装时从仓库根执行）：

    PYTHONPATH=src python3 -m evals.report
"""

from evals.scenarios import SCENARIOS, SCENARIOS_BY_ID, Scenario
from evals.runner import ScenarioResult, TurnRecord, run_scenario
from evals.metrics import (
    BenchmarkMetrics,
    CheckResult,
    aggregate_metrics,
    evaluate_scenario,
    scenario_metrics,
)

# build_report 惰性导出：避免 ``python3 -m evals.report`` 时 report 模块被
# 包初始化提前加载而产生 RuntimeWarning。


def __getattr__(name: str):
    if name == "build_report":
        from evals.report import build_report

        return build_report
    raise AttributeError(name)


__all__ = [
    "SCENARIOS",
    "SCENARIOS_BY_ID",
    "Scenario",
    "ScenarioResult",
    "TurnRecord",
    "run_scenario",
    "BenchmarkMetrics",
    "CheckResult",
    "aggregate_metrics",
    "evaluate_scenario",
    "scenario_metrics",
    "build_report",
]
