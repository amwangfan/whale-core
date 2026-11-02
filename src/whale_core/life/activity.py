"""近期活动背景供给（issue #56）：让鲸鱼能自然分享「最近在做什么」。

数据源是**既有**任务台账（``task:*`` + ``runtime:task:*`` journal）与
InnerState——不另建第二套记忆库或任务调度器。

纪律：

- 只列**本会话**的活动（journal.conversation_id 匹配）：私人会话的事不
  串进群聊，群的事不串进私聊；
- 只列终态任务，状态照实标注（完成/没成/结果不确定）——角色可以自然
  叙事，但外部操作结果必须与真实回执一致，计划/失败/未知不能说成已完成；
- 这是背景参考不是汇报清单：渲染文案明确「问到再提」。
"""
from __future__ import annotations

_STATUS_WORDS = {
    "succeeded": "完成了",
    "delivered": "完成了",
    "failed": "没做成",
    "unknown": "结果不确定",
    "denied": "没做成（没获准）",
    "cancelled": "取消了",
}
_TERMINAL = frozenset(_STATUS_WORDS)


def recent_activity_lines(store, conversation_id: str, *, limit: int = 3) -> list[str]:
    """本会话最近的终态任务，渲染为一行一条的近况事实（新→旧）。

    全部 fail-soft：台账缺失/损坏时返回空列表，不阻塞对话。
    """
    conv = str(conversation_id or "")
    if not conv:
        return []
    try:
        recent = store.get("tasks:recent") or []
        lines: list[str] = []
        for task_id in reversed(list(recent)[-50:]):
            record = store.get(f"task:{task_id}")
            journal = store.get(f"runtime:task:{task_id}")
            if not isinstance(record, dict) or not isinstance(journal, dict):
                continue
            if str(journal.get("conversation_id") or "") != conv:
                continue
            phase = str(record.get("phase") or record.get("status") or "")
            if phase not in _TERMINAL:
                continue
            goal = str(record.get("goal") or "").replace("\n", " ").strip()[:24]
            if not goal or goal.startswith("工具："):
                goal = goal.removeprefix("工具：") or "那件事"
            lines.append(f"「{goal}」{_STATUS_WORDS[phase]}")
            if len(lines) >= limit:
                break
        return lines
    except Exception:
        return []


def render_activity_hint(store, conversation_id: str, *, limit: int = 3) -> str:
    """渲染注入人格层的近况提示块；无活动记录时返回空串（零注入纪律）。"""
    lines = recent_activity_lines(store, conversation_id, limit=limit)
    if not lines:
        return ""
    return (
        "你最近在这里做过的事（真实记录，按真实状态说）：\n"
        + "\n".join(f"- {line}" for line in lines)
        + "\n被问到「最近在干嘛」之类的话可以自然提起；没问到就别主动汇报，"
          "没做成/不确定的别装成完成了。")
