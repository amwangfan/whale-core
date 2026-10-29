"""PR-16 media.video.generate：异步任务契约 + VideoTaskManager 生命周期。

全部离线：MockVideoProvider 为确定性替身，无任何网络访问。
"""

from __future__ import annotations

import time

import pytest

from whale_core.memory.sqlite_store import SqliteMemoryStore
from whale_core.memory.store import InMemoryStore
from whale_core.plugins.media_video import (
    MAX_PROMPT_CHARS,
    MEDIA_VIDEO_GENERATE,
    MockVideoProvider,
    VideoError,
    VideoTask,
    VideoTaskManager,
    register_video_gen,
)
from whale_core.plugins.registry import PluginRegistry


def _manager(provider=None, store=None):
    store = store if store is not None else InMemoryStore()
    provider = provider if provider is not None else MockVideoProvider(running_polls=2)
    return VideoTaskManager(store, provider), provider, store


class _SubmitBoomProvider(MockVideoProvider):
    """submit 即抛网络异常的替身。"""

    def submit(self, prompt, *, duration_s=5, size="1280x720"):
        raise ConnectionError("submit boom")


class _FlakyPollProvider(MockVideoProvider):
    """poll 可脚本化抛网络异常的替身。"""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.raise_on_poll = False

    def poll(self, task_id):
        if self.raise_on_poll:
            raise ConnectionError("poll boom")
        return super().poll(task_id)


class _CancelBoomProvider(MockVideoProvider):
    """cancel 抛网络异常的替身。"""

    def cancel(self, task_id):
        raise TimeoutError("cancel boom")


# ---------- 参数校验 ----------

def test_empty_prompt_rejected():
    mgr, _, _ = _manager()
    for bad in ("", "   ", None):
        with pytest.raises(ValueError):
            mgr.submit(bad)


def test_long_prompt_truncated_to_4000():
    mgr, provider, _ = _manager()
    task = mgr.submit("x" * (MAX_PROMPT_CHARS + 500))
    assert len(task.prompt) == MAX_PROMPT_CHARS
    assert provider.submitted[0][0] == "x" * MAX_PROMPT_CHARS


def test_duration_clamped_to_range():
    mgr, provider, _ = _manager()
    mgr.submit("a", duration_s=0)
    mgr.submit("b", duration_s=999)
    mgr.submit("c", duration_s=30)
    assert [d for _p, d, _s in provider.submitted] == [1, 60, 30]


def test_size_whitelist():
    mgr, provider, _ = _manager()
    with pytest.raises(ValueError):
        mgr.submit("a", size="4096x4096")
    for size in ("1280x720", "720x1280", "1024x1024"):
        mgr.submit("a", size=size)
    assert [s for _p, _d, s in provider.submitted] == ["1280x720", "720x1280", "1024x1024"]


# ---------- Mock 生命周期 ----------

def test_mock_lifecycle_running_then_succeeded():
    mgr, provider, _ = _manager(MockVideoProvider(running_polls=2))
    task = mgr.submit("一只鲸鱼跃出海面")
    assert task.status == "pending" and task.url == ""

    assert mgr.poll(task.task_id).status == "running"
    assert mgr.poll(task.task_id).status == "running"
    done = mgr.poll(task.task_id)
    assert done.status == "succeeded"
    assert done.url and task.task_id in done.url
    assert provider.poll_calls == 3


def test_mock_scriptable_failure():
    mgr, _, _ = _manager(MockVideoProvider(running_polls=0, final_status="failed",
                                           fail_error="quota burned"))
    task = mgr.submit("x")
    done = mgr.poll(task.task_id)
    assert done.status == "failed" and done.error == "quota burned"


def test_terminal_status_cached_without_provider_poll():
    mgr, provider, _ = _manager(MockVideoProvider(running_polls=0))
    task = mgr.submit("x")
    assert mgr.poll(task.task_id).status == "succeeded"
    calls_after_done = provider.poll_calls
    for _ in range(3):
        again = mgr.poll(task.task_id)
        assert again.status == "succeeded" and again.url
    assert provider.poll_calls == calls_after_done  # 终态不再轮询


def test_poll_network_error_keeps_last_state():
    provider = _FlakyPollProvider(running_polls=5)
    mgr, _, _ = _manager(provider)
    task = mgr.submit("x")
    assert mgr.poll(task.task_id).status == "running"

    provider.raise_on_poll = True
    degraded = mgr.poll(task.task_id)
    assert degraded.status == "running"          # 状态不动
    assert "ConnectionError" in degraded.error   # error 记 transient 类型名

    provider.raise_on_poll = False
    recovered = mgr.poll(task.task_id)
    assert recovered.status == "running" and recovered.error == ""


# ---------- 取消 ----------

def test_cancel_success_and_poll_reads_cached_cancelled():
    mgr, provider, _ = _manager()
    task = mgr.submit("x")
    assert mgr.cancel(task.task_id) is True
    cancelled = mgr.poll(task.task_id)
    assert cancelled.status == "cancelled"
    assert provider.poll_calls == 0  # 取消即终态，poll 直接读缓存


def test_cancel_provider_error_returns_false_state_kept():
    mgr, _, _ = _manager(_CancelBoomProvider())
    task = mgr.submit("x")
    assert mgr.cancel(task.task_id) is False
    assert mgr.poll(task.task_id).status in ("pending", "running")


def test_unknown_task_poll_cancel_not_found():
    mgr, _, _ = _manager()
    with pytest.raises(VideoError) as ei:
        mgr.poll("no-such-task")
    assert ei.value.kind == "not_found"
    with pytest.raises(VideoError) as ei:
        mgr.cancel("no-such-task")
    assert ei.value.kind == "not_found"


# ---------- 持久化恢复 ----------

def test_sqlite_persistence_recovery_with_new_manager(tmp_path):
    db = str(tmp_path / "video.db")
    provider = MockVideoProvider(running_polls=1)
    store1 = SqliteMemoryStore(db)
    mgr1 = VideoTaskManager(store1, provider)
    task = mgr1.submit("重启前的任务", duration_s=8, size="720x1280")
    assert mgr1.poll(task.task_id).status == "running"
    store1.close()

    # 模拟进程重启：同一 db 文件新开 store，同一 provider，新建 manager
    store2 = SqliteMemoryStore(db)
    mgr2 = VideoTaskManager(store2, provider)
    restored = mgr2.poll(task.task_id)  # running → succeeded（Mock running_polls=1）
    assert restored.task_id == task.task_id
    assert restored.prompt == "重启前的任务"
    assert restored.status == "succeeded" and restored.url
    assert any(t.task_id == task.task_id for t in mgr2.list_tasks())
    store2.close()


def test_as_dict_from_dict_roundtrip():
    task = VideoTask(task_id="t1", status="succeeded", prompt="p", url="u",
                     error="", created_at=1.5, updated_at=2.5)
    assert VideoTask.from_dict(task.as_dict()) == task


# ---------- 列表 ----------

def test_list_tasks_desc_order_and_limit(monkeypatch):
    clock = {"t": 1000.0}

    def fake_time():
        clock["t"] += 1.0
        return clock["t"]

    monkeypatch.setattr(time, "time", fake_time)
    mgr, _, _ = _manager()
    ids = [mgr.submit(f"prompt-{i}").task_id for i in range(4)]

    listed = mgr.list_tasks()
    assert [t.task_id for t in listed] == [ids[3], ids[2], ids[1], ids[0]]  # 新的在前
    assert [t.task_id for t in mgr.list_tasks(limit=2)] == [ids[3], ids[2]]


def test_list_tasks_ignores_unrelated_keys():
    mgr, _, store = _manager()
    store.put("persona:default", {"x": 1})
    store.put("video:task:broken", "not-a-dict")
    task = mgr.submit("x")
    assert [t.task_id for t in mgr.list_tasks()] == [task.task_id]


# ---------- 注册形状 ----------

def test_register_shape_and_providers_for_resolution():
    registry = PluginRegistry()
    provider = register_video_gen(registry)
    assert isinstance(provider, MockVideoProvider)  # 缺省挂 mock

    manifest = registry.manifest("media_video")
    assert manifest is not None
    assert manifest.resolved_kind.value == "model"
    assert manifest.provides == (MEDIA_VIDEO_GENERATE,)

    found = registry.providers_for(MEDIA_VIDEO_GENERATE)
    assert [pid for pid, _ in found] == ["media_video"]
    assert found[0][1] is provider


def test_register_with_custom_provider():
    registry = PluginRegistry()
    custom = MockVideoProvider(running_polls=0)
    assert register_video_gen(registry, provider=custom) is custom
    assert registry.providers_for(MEDIA_VIDEO_GENERATE)[0][1] is custom


# ---------- submit 异常分类 ----------

def test_submit_network_error_raises_video_error():
    mgr, _, _ = _manager(_SubmitBoomProvider())
    with pytest.raises(VideoError) as ei:
        mgr.submit("x")
    assert ei.value.kind == "network" and ei.value.retryable


def test_submit_bad_response_empty_task_id():
    class _EmptyIdProvider(MockVideoProvider):
        def submit(self, prompt, *, duration_s=5, size="1280x720"):
            return ""

    mgr, _, _ = _manager(_EmptyIdProvider())
    with pytest.raises(VideoError) as ei:
        mgr.submit("x")
    assert ei.value.kind == "bad_response"
