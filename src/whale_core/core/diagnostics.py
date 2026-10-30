"""诊断与成本统计（roadmap §8 P1-8）：provider 健康状态、错误分类、成本/额度统计。

``InstrumentedProvider`` 包装任意 chat provider（``reply(text, *, persona,
memories, history)`` 签名），把每次调用统计写入 MemoryStore kv 键
``provider:health:{name}``；``provider_health_report`` 扫描该前缀汇总全部
provider；``error_taxonomy`` 静态文档化 ``ModelError.kind`` 的六个取值。

安全约定（与 plugins/manager.py 同款）：**统计与报告只含计数、延迟毫秒数、
错误分类名（kind / 异常类型名）与时间戳——绝不写入异常消息文本或输入正文，
因此任何输出（health / report 及其 JSON 序列化）都不含 api_key / token 等
敏感值。**chars_in / chars_out 是字符数，仅作粗略成本代理；真实 token 计数
属供应商侧。
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

_HEALTH_PREFIX = "provider:health:"

# ModelError.kind 六类（见 core/deepseek.py）→ 中文说明；静态表，随 deepseek 同步维护。
_ERROR_TAXONOMY: dict[str, str] = {
    "config": "配置缺失或非法（如未配置 API key、端点协议不允许）；需修正配置，不可重试",
    "auth": "认证 / 授权失败（HTTP 401/403），凭证无效或权限不足；不可重试",
    "rate_limited": "触发供应商限流（HTTP 429）；瞬时状态，可稍后重试",
    "server": "供应商服务端错误（HTTP 5xx）；瞬时故障，可重试",
    "network": "网络层错误（连接失败 / 超时等）；瞬时故障，可重试",
    "bad_response": "响应无法解析或形状异常（非 JSON / 缺字段 / 非文本）；不可重试",
}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def error_taxonomy() -> dict:
    """ModelError 六类 kind → 中文说明的静态说明表（返回副本，可自由改动）。"""
    return dict(_ERROR_TAXONOMY)


def _zero_stats() -> dict:
    return {
        "calls": 0,
        "errors": 0,
        "last_error_kind": None,
        "last_error_at": None,
        "last_latency_ms": 0.0,
        "total_latency_ms": 0.0,
        "chars_in": 0,
        "chars_out": 0,
        "updated_at": None,
        # G4 真实运营指标：错误事件 [(epoch, kind)] 与延迟样本（毫秒），
        # 各有界——24h 窗口与 P50/P95 由读取侧按真实数据计算，无数据标
        # unavailable，绝不猜。
        "error_events": [],
        "latency_samples": [],
    }


_MAX_ERROR_EVENTS = 500
_MAX_LATENCY_SAMPLES = 500


def _prune_24h(events: list) -> list:
    cutoff = time.time() - 86400
    return [e for e in events if isinstance(e, (list, tuple)) and len(e) == 2
            and isinstance(e[0], (int, float)) and e[0] >= cutoff]


def _percentile(samples: list[float], pct: float) -> float:
    ordered = sorted(samples)
    if not ordered:
        return 0.0
    rank = min(len(ordered) - 1, max(0, int(pct / 100 * len(ordered) + 0.5) - 1))
    return ordered[rank]


def _load_stats(store, key: str) -> dict:
    """读 kv 统计；数据损坏（非 dict / 字段类型或取值非法）→ 重置为零值计数器，不抛。"""
    try:
        raw = store.get(key)
    except Exception:
        return _zero_stats()
    if not isinstance(raw, Mapping):
        return _zero_stats()
    stats = _zero_stats()
    for field in ("calls", "errors", "chars_in", "chars_out"):
        value = raw.get(field, 0)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return _zero_stats()
        stats[field] = value
    for field in ("last_latency_ms", "total_latency_ms"):
        value = raw.get(field, 0.0)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            return _zero_stats()
        stats[field] = float(value)
    for field in ("last_error_kind", "last_error_at", "updated_at"):
        value = raw.get(field)
        if value is not None and not isinstance(value, str):
            return _zero_stats()
        stats[field] = value
    events = raw.get("error_events", [])
    if isinstance(events, list):
        stats["error_events"] = [list(e) for e in events
                                 if isinstance(e, (list, tuple)) and len(e) == 2
                                 and isinstance(e[0], (int, float))
                                 and isinstance(e[1], str)][-_MAX_ERROR_EVENTS:]
    samples = raw.get("latency_samples", [])
    if isinstance(samples, list):
        stats["latency_samples"] = [float(v) for v in samples
                                    if isinstance(v, (int, float))
                                    and not isinstance(v, bool) and v >= 0][-_MAX_LATENCY_SAMPLES:]
    return stats


def _health_view(name: str, stats: Mapping[str, Any]) -> dict:
    """对外健康视图；零调用时 error_rate=0、avg_latency_ms=0。

    errors_24h 与延迟分位只在有真实样本时给出，否则 ``"unavailable"``
    （运营指标不猜数，G4）。
    """
    calls = stats["calls"]
    errors = stats["errors"]
    events_24h = _prune_24h(list(stats.get("error_events") or []))
    errors_by_kind: dict[str, int] = {}
    for _ts, kind in events_24h:
        errors_by_kind[kind] = errors_by_kind.get(kind, 0) + 1
    samples = list(stats.get("latency_samples") or [])
    latency = (
        {"p50_ms": int(_percentile(samples, 50)),
         "p95_ms": int(_percentile(samples, 95)),
         "samples": len(samples)}
        if samples else "unavailable"
    )
    return {
        "name": name,
        "calls": calls,
        "errors": errors,
        "error_rate": round(errors / calls, 3) if calls else 0,
        "avg_latency_ms": int(stats["total_latency_ms"] / calls) if calls else 0,
        "errors_24h": errors_by_kind if events_24h else "unavailable",
        "latency": latency,
        "last_error_kind": stats["last_error_kind"],
        "last_error_at": stats["last_error_at"],
        "chars_in": stats["chars_in"],
        "chars_out": stats["chars_out"],
        "updated_at": stats["updated_at"],
    }


class InstrumentedProvider:
    """包任意 chat provider（reply(text, *, persona, memories, history)），
    把调用统计写进 store kv ``provider:health:{name}``：
    {calls, errors, last_error_kind, last_error_at, last_latency_ms,
     total_latency_ms, chars_in, chars_out, updated_at}
    chars_in/out 是字符数（粗略成本代理；真实 token 计数属供应商侧）。
    异常原样上抛（记录后不吞）。

    last_error_kind：ModelError 取其 ``kind``，其它异常取类型名；**绝不记录
    异常消息文本**，故统计与 health() 输出不含 api_key / token。
    """

    def __init__(self, name: str, inner, store):
        self.name = name
        self._inner = inner
        self._store = store
        self._key = _HEALTH_PREFIX + name

    def reply(self, text, *, persona="", memories=(), history=()) -> str:
        stats = _load_stats(self._store, self._key)
        t0 = time.monotonic()
        try:
            result = self._inner.reply(
                text, persona=persona, memories=memories, history=history
            )
        except Exception as exc:
            kind = getattr(exc, "kind", None)
            if not isinstance(kind, str) or not kind:
                kind = type(exc).__name__
            self._record(stats, t0, len(text), 0, error_kind=kind)
            raise
        self._record(stats, t0, len(text), len(result), error_kind=None)
        return result

    def _record(self, stats: dict, t0: float, in_len: int, out_len: int, *,
                error_kind: str | None) -> None:
        elapsed_ms = (time.monotonic() - t0) * 1000.0
        stats["calls"] += 1
        stats["chars_in"] += in_len
        if error_kind is None:
            stats["chars_out"] += out_len
        else:
            stats["errors"] += 1
            stats["last_error_kind"] = error_kind
            stats["last_error_at"] = _utc_now_iso()
            events = list(stats.get("error_events") or [])
            events.append([time.time(), error_kind])
            stats["error_events"] = events[-_MAX_ERROR_EVENTS:]
        stats["last_latency_ms"] = elapsed_ms
        stats["total_latency_ms"] += elapsed_ms
        samples = list(stats.get("latency_samples") or [])
        samples.append(elapsed_ms)
        stats["latency_samples"] = samples[-_MAX_LATENCY_SAMPLES:]
        stats["updated_at"] = _utc_now_iso()
        self._store.put(self._key, stats)

    def health(self) -> dict:
        """读回统计 + {"name": ...}；kv 数据损坏时返回零值视图，不抛。"""
        return _health_view(self.name, _load_stats(self._store, self._key))


def provider_health_report(store) -> dict:
    """扫 store.keys() 中 ``provider:health:*`` 键汇总。

    返回 {"providers": [health 视图, ...], "totals": {"calls", "errors"}}；
    损坏条目按零值计入，不抛。
    """
    providers: list[dict] = []
    total_calls = 0
    total_errors = 0
    try:
        keys = store.keys()
    except Exception:
        keys = []
    for key in sorted(k for k in keys if isinstance(k, str)):
        if not key.startswith(_HEALTH_PREFIX):
            continue
        view = _health_view(key[len(_HEALTH_PREFIX):], _load_stats(store, key))
        providers.append(view)
        total_calls += view["calls"]
        total_errors += view["errors"]
    return {"providers": providers, "totals": {"calls": total_calls, "errors": total_errors}}


def capability_health(manager, tools, store=None) -> dict:
    """按 Capability 的五层健康视图（G4）：

    declared → 有工具声明该能力；
    registered → 工具映射的插件已注册；
    enabled → 插件当前启用；
    reachable → provider.healthcheck() 通过（纯本地 handler 工具记 True）；
    verified → 有真实成功证据（任务台账回执 / provider 成功调用计数），
               没有证据标 ``"unavailable"``——**「声明了」绝不冒充「能用」**。

    返回 {"capabilities": [...], "summary": {...}}；不含任何凭据与用户正文。
    """
    registry = getattr(manager, "registry", manager)
    enabled_by_id: dict[str, bool] = {}
    if hasattr(manager, "status"):
        try:
            for row in manager.status():
                enabled_by_id[row["plugin_id"]] = bool(row.get("enabled"))
        except Exception:
            enabled_by_id = {}
    verified_tools: set[str] = set()
    if store is not None:
        try:
            for task_id in list(store.get("tasks:recent") or [])[-50:]:
                record = store.get(f"task:{task_id}")
                if not isinstance(record, Mapping):
                    continue
                for receipt in record.get("receipts") or ():
                    text = str(receipt)
                    if ":ok:" in text:
                        # 形如 tool:<name>:ok:… / plugin:<pid>:<action>:ok:…
                        parts = text.split(":")
                        if parts and parts[0] in ("tool", "plugin"):
                            verified_tools.add(parts[1])
        except Exception:
            verified_tools = set()
    provider_calls: dict[str, int] = {}
    if store is not None:
        for view in provider_health_report(store)["providers"]:
            provider_calls[view["name"]] = view["calls"]

    caps: dict[str, dict] = {}
    for spec in tools.specs():
        entry = caps.setdefault(spec.capability, {
            "capability": spec.capability, "tools": [], "plugin_ids": [],
            "declared": True, "registered": False, "enabled": False,
            "reachable": "unavailable", "verified": "unavailable"})
        entry["tools"].append(spec.name)
        if spec.plugin_id and spec.plugin_id not in entry["plugin_ids"]:
            entry["plugin_ids"].append(spec.plugin_id)
        if spec.name in verified_tools or spec.plugin_id in verified_tools:
            entry["verified"] = True
    for entry in caps.values():
        pids = entry["plugin_ids"]
        if pids:
            entry["registered"] = any(
                registry.manifest(pid) is not None for pid in pids)
            entry["enabled"] = any(enabled_by_id.get(pid, False) for pid in pids)
            for pid in pids:
                provider = registry.provider_of(pid)
                healthcheck = getattr(provider, "healthcheck", None)
                if callable(healthcheck):
                    try:
                        if healthcheck():
                            entry["reachable"] = True
                        elif entry["reachable"] != True:
                            entry["reachable"] = False
                    except Exception:
                        entry["reachable"] = False
                if provider_calls.get(pid):
                    entry["verified"] = True
        else:
            # 纯本地 handler 工具：配置即可达
            entry["registered"] = True
            entry["enabled"] = True
            entry["reachable"] = True
    summary = {
        "total": len(caps),
        "verified": sum(1 for e in caps.values() if e["verified"] is True),
        "unverified": sum(1 for e in caps.values() if e["verified"] != True),
    }
    return {"capabilities": sorted(caps.values(), key=lambda e: e["capability"]),
            "summary": summary}
