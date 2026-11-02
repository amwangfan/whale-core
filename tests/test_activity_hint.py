"""issue #56：近期活动背景供给的测试（离线，InMemoryStore）。"""
from __future__ import annotations

from whale_core.life.activity import recent_activity_lines, render_activity_hint
from whale_core.memory.store import InMemoryStore


def _put_task(store, task_id, *, conv, phase, goal):
    store.put(f"task:{task_id}", {"task_id": task_id, "phase": phase,
                                  "goal": goal, "at": 1.0})
    store.put(f"runtime:task:{task_id}", {"conversation_id": conv})
    recent = store.get("tasks:recent") or []
    store.put("tasks:recent", [*recent, task_id])


def test_lines_only_terminal_and_scoped():
    store = InMemoryStore()
    _put_task(store, "t1", conv="c1", phase="succeeded", goal="画一只鲸鱼")
    _put_task(store, "t2", conv="c1", phase="running", goal="进行中的事")
    _put_task(store, "t3", conv="c2", phase="succeeded", goal="别的会话的事")
    _put_task(store, "t4", conv="c1", phase="failed", goal="抢票")
    _put_task(store, "t5", conv="c1", phase="unknown", goal="昨晚的下载")
    lines = recent_activity_lines(store, "c1")
    # 新→旧；running 与其它会话不出现
    assert lines == ["「昨晚的下载」结果不确定", "「抢票」没做成", "「画一只鲸鱼」完成了"]
    assert recent_activity_lines(store, "c2") == ["「别的会话的事」完成了"]
    assert recent_activity_lines(store, "") == []


def test_status_words_are_honest():
    store = InMemoryStore()
    _put_task(store, "t1", conv="c", phase="failed", goal="抢演唱会门票")
    hint = render_activity_hint(store, "c")
    assert "没做成" in hint and "完成了" not in hint.split("没做成")[0].split("票")[-1]
    assert "别装成完成了" in hint  # 给模型的诚实约束


def test_empty_store_renders_nothing():
    assert render_activity_hint(InMemoryStore(), "c1") == ""


def test_corrupt_records_fail_soft():
    store = InMemoryStore()
    store.put("tasks:recent", ["broken"])
    store.put("task:broken", "not-a-dict")
    _put_task(store, "t9", conv="c", phase="delivered", goal="发语音")
    assert recent_activity_lines(store, "c") == ["「发语音」完成了"]


def test_limit_caps_lines():
    store = InMemoryStore()
    for i in range(6):
        _put_task(store, f"t{i}", conv="c", phase="succeeded", goal=f"事{i}")
    assert len(recent_activity_lines(store, "c", limit=3)) == 3
