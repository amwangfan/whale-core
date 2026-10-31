"""PR-15 诊断与成本统计：InstrumentedProvider 调用统计 / 汇总报告 / 错误分类表。

全部离线；store 覆盖 InMemoryStore 与 SqliteMemoryStore（tmp_path）两种后端。
"""

from __future__ import annotations

import json

import pytest

from whale_core.core.deepseek import ModelError
from whale_core.core.diagnostics import (
    InstrumentedProvider,
    error_taxonomy,
    provider_health_report,
)
from whale_core.memory.sqlite_store import SqliteMemoryStore
from whale_core.memory.store import InMemoryStore


class StubProvider:
    """脚本化 chat provider：脚本元素为 str 原样返回、为异常实例抛出。"""

    def __init__(self, *script):
        self._script = list(script)
        self.calls = []

    def reply(self, text, *, persona="", memories=(), history=()):
        self.calls.append(
            {
                "text": text,
                "persona": persona,
                "memories": tuple(memories),
                "history": tuple(history),
            }
        )
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _provider(store, script, name="stub"):
    return InstrumentedProvider(name, StubProvider(*script), store)


# ---------- 成功调用统计 ----------

def test_success_call_stats():
    store = InMemoryStore()
    prov = _provider(store, ["你好呀"])
    out = prov.reply(
        "你好",
        persona="人格",
        memories=["记忆1"],
        history=[{"role": "user", "content": "hi"}],
    )
    assert out == "你好呀"
    health = prov.health()
    assert health["name"] == "stub"
    assert health["calls"] == 1
    assert health["errors"] == 0
    assert health["error_rate"] == 0
    assert health["chars_in"] == len("你好")
    assert health["chars_out"] == len("你好呀")
    assert health["avg_latency_ms"] >= 0
    assert isinstance(health["avg_latency_ms"], int)
    assert health["updated_at"]
    assert health["last_error_kind"] is None
    assert health["last_error_at"] is None


def test_kv_record_shape():
    """kv `provider:health:{name}` 的契约字段（G4 增加 error_events/latency_samples）。"""
    store = InMemoryStore()
    _provider(store, ["ok"]).reply("hi")
    raw = store.get("provider:health:stub")
    assert set(raw) == {
        "calls", "errors", "last_error_kind", "last_error_at",
        "last_latency_ms", "total_latency_ms", "chars_in", "chars_out",
        "updated_at", "error_events", "latency_samples",
    }
    assert raw["last_latency_ms"] >= 0
    assert raw["total_latency_ms"] >= raw["last_latency_ms"]


def test_return_value_and_kwargs_passthrough():
    """被包装 provider 的返回值原样透传，关键字参数原样到达。"""
    stub = StubProvider("原样返回值")
    prov = InstrumentedProvider("stub", stub, InMemoryStore())
    out = prov.reply("问题", persona="人格", memories=["m1"], history=[{"role": "user", "content": "之前"}])
    assert out == "原样返回值"
    seen = stub.calls[0]
    assert seen["text"] == "问题"
    assert seen["persona"] == "人格"
    assert seen["memories"] == ("m1",)
    assert seen["history"] == ({"role": "user", "content": "之前"},)


# ---------- 失败统计 ----------

def test_model_error_recorded_and_reraised():
    store = InMemoryStore()
    err = ModelError("auth", "bad credentials", status=401)
    prov = _provider(store, [err])
    with pytest.raises(ModelError) as ei:
        prov.reply("hi")
    assert ei.value is err  # 异常原样上抛（同一个对象）
    health = prov.health()
    assert health["calls"] == 1
    assert health["errors"] == 1
    assert health["error_rate"] == 1
    assert health["last_error_kind"] == "auth"
    assert health["last_error_at"]
    assert health["chars_in"] == 2
    assert health["chars_out"] == 0  # 失败不计出账字符


def test_non_model_error_kind_is_type_name():
    prov = _provider(InMemoryStore(), [ValueError("boom")])
    with pytest.raises(ValueError):
        prov.reply("hi")
    health = prov.health()
    assert health["errors"] == 1
    assert health["last_error_kind"] == "ValueError"


def test_error_keeps_prior_success_counters():
    store = InMemoryStore()
    prov = _provider(store, ["a", ModelError("server", "x"), "bb"])
    prov.reply("一")
    with pytest.raises(ModelError):
        prov.reply("二")
    prov.reply("三")
    health = prov.health()
    assert health["calls"] == 3
    assert health["errors"] == 1
    assert health["error_rate"] == round(1 / 3, 3)
    assert health["chars_in"] == 3
    assert health["chars_out"] == len("a") + len("bb")
    assert health["last_error_kind"] == "server"


# ---------- error_rate / avg_latency 计算 ----------

def test_zero_call_health_is_zeroed():
    prov = _provider(InMemoryStore(), [])
    assert prov.health() == {
        "name": "stub",
        "calls": 0,
        "errors": 0,
        "error_rate": 0,
        "avg_latency_ms": 0,
        "last_error_kind": None,
        "last_error_at": None,
        "chars_in": 0,
        "chars_out": 0,
        "updated_at": None,
        # G4：无真实样本的运营指标标 unavailable，不猜
        "errors_24h": "unavailable",
        "latency": "unavailable",
    }


def test_avg_latency_ms_with_fake_clock(monkeypatch):
    ticks = iter([0.0, 0.1, 1.0, 1.3])  # 两次调用分别耗时 100ms / 300ms
    monkeypatch.setattr(
        "whale_core.core.diagnostics.time.monotonic", lambda: next(ticks)
    )
    prov = _provider(InMemoryStore(), ["a", "b"])
    prov.reply("x")
    prov.reply("y")
    health = prov.health()
    assert health["calls"] == 2
    assert health["avg_latency_ms"] == 200  # int((100 + 300) / 2)


# ---------- store 损坏数据容错 ----------

def test_corrupted_store_data_resets_without_raising():
    store = InMemoryStore()
    store.put("provider:health:stub", "这不是统计字典")
    prov = _provider(store, ["ok"])
    health = prov.health()  # 不抛
    assert health["calls"] == 0
    assert health["errors"] == 0
    assert health["error_rate"] == 0
    # 部分字段类型损坏同样整体归零
    store.put("provider:health:stub", {"calls": "很多", "errors": 1})
    assert prov.health()["calls"] == 0
    # 归零后可继续正常累计
    prov.reply("hi")
    assert prov.health()["calls"] == 1


# ---------- 汇总报告 ----------

def test_report_shape_and_totals():
    store = InMemoryStore()
    alpha = _provider(store, ["x", ModelError("network", "n")], name="alpha")
    beta = _provider(store, ["yy"], name="beta")
    alpha.reply("1")
    with pytest.raises(ModelError):
        alpha.reply("2")
    beta.reply("3")
    store.put("plugin:state:xyz", {"enabled": True})  # 无关键不影响扫描
    store.put("provider:health:broken", 12345)  # 损坏条目按零值计入
    report = provider_health_report(store)
    assert set(report) == {"providers", "totals"}
    assert report["totals"] == {"calls": 3, "errors": 1}
    by_name = {p["name"]: p for p in report["providers"]}
    assert set(by_name) == {"alpha", "beta", "broken"}
    assert by_name["alpha"]["errors"] == 1
    assert by_name["alpha"]["error_rate"] == 0.5
    assert by_name["beta"]["chars_out"] == 2
    assert by_name["broken"]["calls"] == 0


def test_report_empty_store():
    assert provider_health_report(InMemoryStore()) == {
        "providers": [],
        "totals": {"calls": 0, "errors": 0},
    }


# ---------- sqlite 后端持久化 ----------

def test_sqlite_backend_persistence_across_reopen(tmp_path):
    db = tmp_path / "mem.sqlite3"
    prov = _provider(SqliteMemoryStore(db), ["你好"], name="ds")
    prov.reply("喂")
    reopened = SqliteMemoryStore(db)  # 重开同一路径
    report = provider_health_report(reopened)
    assert report["totals"] == {"calls": 1, "errors": 0}
    assert report["providers"][0]["name"] == "ds"
    assert report["providers"][0]["chars_out"] == 2
    # 新实例包装同名 provider：统计延续而非清零
    prov2 = InstrumentedProvider("ds", StubProvider("嗯"), reopened)
    prov2.reply("在吗")
    assert prov2.health()["calls"] == 2


# ---------- 输出不含 api_key / token ----------

def test_outputs_never_contain_secrets():
    secret = "sk-test-secret-0123456789"
    store = InMemoryStore()
    prov = _provider(
        store,
        [
            ModelError("auth", f"invalid api_key {secret}"),
            RuntimeError(f"token={secret}"),
        ],
        name="ds",
    )
    for _ in range(2):
        with pytest.raises(Exception):
            prov.reply(f"请用 {secret} 聊天")  # 连输入正文都含密钥
    blob = json.dumps(
        {"health": prov.health(), "report": provider_health_report(store)},
        ensure_ascii=False,
    )
    assert secret not in blob
    assert "api_key" not in blob
    assert "token" not in blob


# ---------- 错误分类表 ----------

def test_error_taxonomy_static_table():
    table = error_taxonomy()
    assert set(table) == {
        "config", "auth", "rate_limited", "server", "network", "bad_response",
    }
    for desc in table.values():
        assert isinstance(desc, str) and desc
        assert any("一" <= ch <= "鿿" for ch in desc)  # 中文说明
    # 与 ModelError 实际产生的 kind 对齐（抽样）
    assert ModelError("auth", "x").kind in table
    assert ModelError("rate_limited", "x", retryable=True).kind in table
