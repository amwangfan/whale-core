"""core/budget.py（每日请求数预算）离线测试：全 fake/内存 store，不触网。

覆盖：原子预留并发安全、超限拒绝、按天滚动（注入 now）、设置往返
（SettingsController，走 kv 不落配置文件）、0=禁用该能力、非法值拒绝、
失败仍占额度（媒体工具端到端）、模型调用接线点（_ManagedModel/_ManagedRoleModel）。
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from whale_core.core.budget import (
    DEFAULTS,
    SETTINGS_KEY,
    current_day,
    reserve,
    settings,
    used_today,
    validate,
    view,
)
from whale_core.core.deepseek import ModelError
from whale_core.memory.store import InMemoryStore

SH = ZoneInfo("Asia/Shanghai")


# ---------- 基础语义 ----------

def test_defaults_apply_without_settings():
    store = InMemoryStore()
    assert settings(store) == {"model_calls": 200, "media_jobs": 50}
    assert settings(store) == DEFAULTS and settings(store) is not DEFAULTS
    assert used_today(store) == {"model_calls": 0, "media_jobs": 0}
    assert view(store)["day"] == current_day()


def test_reserve_counts_and_over_limit_rejected():
    store = InMemoryStore()
    store.put(SETTINGS_KEY, {"model_calls": 2, "media_jobs": 50})
    reserve(store, "model_calls")
    reserve(store, "model_calls")
    with pytest.raises(ModelError) as exc_info:
        reserve(store, "model_calls")
    assert exc_info.value.kind == "budget"
    # 媒体额度独立，不受 model_calls 超限影响
    reserve(store, "media_jobs")
    assert used_today(store) == {"model_calls": 2, "media_jobs": 1}


def test_reserve_rejects_unknown_capability():
    store = InMemoryStore()
    with pytest.raises(ValueError):
        reserve(store, "gpu_hours")


def test_zero_limit_disables_capability():
    store = InMemoryStore()
    store.put(SETTINGS_KEY, {"model_calls": 0, "media_jobs": 0})
    with pytest.raises(ModelError) as exc_info:
        reserve(store, "model_calls")
    assert exc_info.value.kind == "budget"
    with pytest.raises(ModelError):
        reserve(store, "media_jobs")
    assert used_today(store) == {"model_calls": 0, "media_jobs": 0}


def test_corrupted_settings_and_counters_fall_back():
    store = InMemoryStore()
    store.put(SETTINGS_KEY, "not-a-dict")
    assert settings(store) == DEFAULTS
    store.put(SETTINGS_KEY, {"model_calls": True, "media_jobs": 5})
    assert settings(store) == {"model_calls": 200, "media_jobs": 5}
    day = current_day()
    store.put(f"budget:{day}:model_calls", "junk")
    assert used_today(store)["model_calls"] == 0
    reserve(store, "model_calls")  # 损坏计数器重置后继续服务
    assert used_today(store)["model_calls"] == 1


# ---------- 并发原子性 ----------

def test_reserve_atomic_under_thread_contention():
    store = InMemoryStore()
    store.put(SETTINGS_KEY, {"model_calls": 100, "media_jobs": 100})
    outcomes: list[bool] = []

    def worker() -> None:
        for _ in range(50):  # 8 线程 × 50 = 400 次尝试，限额 100
            try:
                reserve(store, "model_calls")
                outcomes.append(True)
            except ModelError:
                outcomes.append(False)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # 恰好 100 次成功，绝无超发；计数器与成功数一致
    assert outcomes.count(True) == 100
    assert outcomes.count(False) == 300
    assert used_today(store)["model_calls"] == 100


# ---------- 按天滚动（注入 now） ----------

def test_day_rollover_with_injected_now():
    store = InMemoryStore()
    store.put(SETTINGS_KEY, {"model_calls": 1, "media_jobs": 1})
    before_midnight = datetime(2026, 3, 8, 23, 59, tzinfo=SH)
    after_midnight = before_midnight + timedelta(minutes=2)
    assert current_day(before_midnight) == "2026-03-08"
    assert current_day(after_midnight) == "2026-03-09"

    reserve(store, "model_calls", now=before_midnight)
    with pytest.raises(ModelError):
        reserve(store, "model_calls", now=before_midnight)  # 当天额度用尽
    reserve(store, "model_calls", now=after_midnight)  # 跨天自动恢复
    assert used_today(store, now=before_midnight)["model_calls"] == 1
    assert used_today(store, now=after_midnight)["model_calls"] == 1
    assert view(store, now=after_midnight) == {
        "limits": {"model_calls": 1, "media_jobs": 1},
        "used": {"model_calls": 1, "media_jobs": 0},
        "day": "2026-03-09",
    }


def test_budget_day_uses_shanghai_calendar_not_utc():
    # UTC 还是 3-08 16:30，上海已是 3-09 00:30——预算日必须落在 3-09
    moment = datetime(2026, 3, 8, 16, 30, tzinfo=timezone.utc)
    assert current_day(moment) == "2026-03-09"
    store = InMemoryStore()
    reserve(store, "media_jobs", now=moment)
    assert store.get("budget:2026-03-09:media_jobs") == 1


def test_injected_now_must_be_timezone_aware():
    with pytest.raises(ValueError):
        current_day(datetime(2026, 3, 8, 12, 0))


# ---------- 校验 ----------

@pytest.mark.parametrize("bad", [
    None,
    "x",
    [],
    {},
    {"model_calls": 5},
    {"model_calls": 5, "media_jobs": 5, "extra": 1},
    {"model_calls": True, "media_jobs": 5},
    {"model_calls": 5, "media_jobs": 1.5},
    {"model_calls": -1, "media_jobs": 5},
    {"model_calls": 100001, "media_jobs": 5},
    {"model_calls": "5", "media_jobs": 5},
])
def test_validate_rejects_illegal_values(bad):
    with pytest.raises(ValueError):
        validate(bad)


@pytest.mark.parametrize("good", [
    {"model_calls": 0, "media_jobs": 0},
    {"model_calls": 100000, "media_jobs": 100000},
    {"model_calls": 200, "media_jobs": 50},
])
def test_validate_accepts_boundary_values(good):
    assert validate(good) == good


# ---------- 设置往返（SettingsController，kv 不落配置文件） ----------

def test_settings_controller_budget_roundtrip(tmp_path):
    import json

    from whale_core.configuration import SettingsController
    from whale_core.profiles import assemble_runtime

    store = InMemoryStore()
    bundle = assemble_runtime("local", env={}, store=store, use_saved_config=False)
    controller = SettingsController(tmp_path, bundle, env={})

    initial = controller.view()["budget"]
    assert initial["limits"] == DEFAULTS
    assert initial["used"] == {"model_calls": 0, "media_jobs": 0}

    candidate = controller.update({"budget": {"model_calls": 7, "media_jobs": 3}})
    assert store.get(SETTINGS_KEY) == {"model_calls": 7, "media_jobs": 3}
    # 预算走 kv，不落配置文件
    assert "budget" not in json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    # 重装配后的新 bundle 共享同一 store，视图立即可见
    assert candidate.store is store
    assert controller.view()["budget"]["limits"] == {"model_calls": 7, "media_jobs": 3}

    # 非法值整体拒绝，且旧值不被破坏
    with pytest.raises(ValueError):
        controller.update({"budget": {"model_calls": -1, "media_jobs": 3}})
    assert store.get(SETTINGS_KEY) == {"model_calls": 7, "media_jobs": 3}
    with pytest.raises(ValueError):
        controller.update({"budget": {"model_calls": 1.5, "media_jobs": 3}})


# ---------- 接线点：media.* 工具（runtime._run_tool） ----------

def _media_orchestrator(store, brain, handler):
    from whale_core.core.companion import WhaleCompanion
    from whale_core.core.runtime import ConversationOrchestrator, ToolRegistry, ToolSpec
    from whale_core.memory.curation import MemoryCurator

    tools = ToolRegistry()
    tools.register(ToolSpec(name="generate_image", capability="media.image.generate",
                            handler=handler))
    companion = WhaleCompanion(store, brain, curator=MemoryCurator(store))
    return ConversationOrchestrator(store, companion, tools=tools)


class _Brain:
    def __init__(self):
        self.calls: list[str] = []

    def reply(self, text, *, persona="", memories=(), history=()):
        self.calls.append(text)
        return "好的呀"


def test_media_tool_spends_budget_and_blocks_over_limit():
    store, brain = InMemoryStore(), _Brain()
    store.put(SETTINGS_KEY, {"model_calls": 200, "media_jobs": 1})
    orch = _media_orchestrator(store, brain, lambda args: "image-url")

    first = orch.execute_tool("generate_image", {"prompt": "猫"}, source="api")
    assert first.status == "answered"
    assert used_today(store)["media_jobs"] == 1

    second = orch.execute_tool("generate_image", {"prompt": "狗"}, source="api")
    assert second.status == "failed"
    assert any("budget-exhausted" in r for r in second.receipts)
    # 失败回执经陪伴模型表达的提示里明确「今日预算已用完」
    assert "今日预算已用完" in brain.calls[-1]
    assert used_today(store)["media_jobs"] == 1  # 超限请求不占名额


def test_media_tool_failure_still_spends_slot():
    store, brain = InMemoryStore(), _Brain()

    def boom(args):
        raise RuntimeError("provider exploded")

    store.put(SETTINGS_KEY, {"model_calls": 200, "media_jobs": 1})
    orch = _media_orchestrator(store, brain, boom)
    failed = orch.execute_tool("generate_image", {"prompt": "猫"}, source="api")
    assert failed.status == "failed"
    assert any("tool-fail:generate_image" in r for r in failed.receipts)
    assert used_today(store)["media_jobs"] == 1  # 失败仍花额度，防重试风暴

    blocked = orch.execute_tool("generate_image", {"prompt": "狗"}, source="api")
    assert any("budget-exhausted" in r for r in blocked.receipts)


def test_non_media_tool_does_not_spend_media_budget():
    store, brain = InMemoryStore(), _Brain()
    store.put(SETTINGS_KEY, {"model_calls": 200, "media_jobs": 0})
    from whale_core.core.companion import WhaleCompanion
    from whale_core.core.runtime import ConversationOrchestrator, ToolRegistry, ToolSpec
    from whale_core.memory.curation import MemoryCurator

    tools = ToolRegistry()
    tools.register(ToolSpec(name="fetch", capability="web.fetch", handler=lambda args: "ok"))
    orch = ConversationOrchestrator(
        store, WhaleCompanion(store, brain, curator=MemoryCurator(store)), tools=tools)
    result = orch.execute_tool("fetch", {"url": "https://example.com"}, source="api")
    assert result.status == "answered"  # 非 media.* 能力不受媒体预算限制


# ---------- 接线点：模型调用（profiles._ManagedModel / _ManagedRoleModel） ----------

def test_managed_model_reserve_blocks_chat_when_model_budget_exhausted():
    from whale_core.profiles import assemble_runtime

    store = InMemoryStore()
    bundle = assemble_runtime("local", env={}, store=store, use_saved_config=False)
    store.put(SETTINGS_KEY, {"model_calls": 0, "media_jobs": 50})
    with pytest.raises(ModelError) as exc_info:
        bundle.companion.chat("你好", conversation_id="t-budget")
    assert exc_info.value.kind == "budget"
    assert used_today(store)["model_calls"] == 0


def test_managed_role_model_reserve_blocks_when_model_budget_exhausted():
    from whale_core.profiles import _ManagedRoleModel, assemble_runtime

    store = InMemoryStore()
    bundle = assemble_runtime("local", env={}, store=store, use_saved_config=False)
    store.put(SETTINGS_KEY, {"model_calls": 0, "media_jobs": 50})
    role_model = _ManagedRoleModel(bundle.registry, store, "intent")
    with pytest.raises(ModelError) as exc_info:
        role_model.reply("规划一下")
    assert exc_info.value.kind == "budget"


def test_managed_model_counts_successful_call():
    """预算预留发生在真实调用前：端点无服务时调用失败，但名额已占。"""
    from whale_core.profiles import assemble_runtime

    store = InMemoryStore()
    # 127.0.0.1:9（discard 端口）回环即时拒绝，保证离线确定性
    bundle = assemble_runtime("local", env={"LOCAL_MODEL_BASE_URL": "http://127.0.0.1:9/v1"},
                              store=store, use_saved_config=False)
    store.put(SETTINGS_KEY, {"model_calls": 2, "media_jobs": 50})
    with pytest.raises(ModelError):  # 本地端点无服务 → network 错误
        bundle.companion.chat("你好", conversation_id="t-count")
    assert used_today(store)["model_calls"] == 1  # 失败仍占额度


# ---------- API 端到端：超限 UX（明确「今日预算已用完」） ----------

def test_chat_api_reports_budget_exhausted_clearly():
    import json as _json
    from http.client import HTTPConnection

    from whale_core.api.server import ApiContext, WhaleApiServer
    from whale_core.profiles import assemble_runtime

    store = InMemoryStore()
    bundle = assemble_runtime("local", env={}, store=store, use_saved_config=False)
    store.put(SETTINGS_KEY, {"model_calls": 0, "media_jobs": 50})
    ctx = ApiContext(store=store, manager=bundle.manager,
                     orchestrator=bundle.orchestrator, profile="local")
    server = WhaleApiServer(ctx, port=0, token="t")
    server.start()
    try:
        conn = HTTPConnection("127.0.0.1", server.port, timeout=5)
        conn.request("POST", "/api/v1/chat",
                     body=_json.dumps({"text": "你好"}),
                     headers={"Authorization": "Bearer t",
                              "Content-Type": "application/json"})
        resp = conn.getresponse()
        payload = _json.loads(resp.read())
        conn.close()
    finally:
        server.stop()
    assert resp.status == 429
    assert payload["error"]["code"] == "budget"
    assert "今日预算已用完" in payload["error"]["message"]
