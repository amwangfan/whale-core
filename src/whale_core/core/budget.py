"""用户可控的每日请求数预算（按请求计数，不虚构价格/token 计价）。

计数的是「已放行的供应商调用」：拿到名额后失败的请求照样占名额
（失败仍花额度——防止失败→重试风暴绕过限额）。单把进程级锁覆盖
CLI/API 线程，预留是原子的；真实供应商计费在供应商侧。

- 限额存 store kv ``settings:budget``（用户设置，走 kv 不走配置文件），
  由设置页经 SettingsController 校验后写入；
- 用量按天存 ``budget:<day>:<capability>``（预算日按时区 Asia/Shanghai
  划分，跨天自动滚动到新键）；
- 限额 0 = 禁用该能力（任何预留都被拒）；
- 超限抛 ``ModelError("budget", ...)``——与模型错误同族，宿主按 kind
  分类呈现「今日预算已用完」。
"""

from __future__ import annotations

from datetime import datetime
from threading import RLock
from zoneinfo import ZoneInfo

from whale_core.core.deepseek import ModelError

_LOCK = RLock()
DEFAULTS = {"model_calls": 200, "media_jobs": 50}
MAX_LIMIT = 100000
SETTINGS_KEY = "settings:budget"
_DAY_ZONE = ZoneInfo("Asia/Shanghai")


def current_day(now: datetime | None = None) -> str:
    """预算日（Asia/Shanghai 日历日）。``now`` 可注入（须带时区）便于测试。"""
    if now is None:
        return datetime.now(_DAY_ZONE).date().isoformat()
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    return now.astimezone(_DAY_ZONE).date().isoformat()


def settings(store) -> dict:
    """当前限额：默认值与 store kv 合并；kv 损坏（非 dict/字段非法）容忍回默认。"""
    raw = store.get(SETTINGS_KEY)
    if not isinstance(raw, dict):
        return dict(DEFAULTS)
    merged = dict(DEFAULTS)
    for key in DEFAULTS:
        value = raw.get(key)
        if isinstance(value, bool) or not isinstance(value, int):
            continue
        merged[key] = min(max(value, 0), MAX_LIMIT)
    return merged


def validate(value) -> dict:
    """校验设置页提交的预算形状；合法原样返回，非法抛 ValueError。"""
    if not isinstance(value, dict) or set(value) != set(DEFAULTS):
        raise ValueError("expected model_calls and media_jobs")
    if any(
        isinstance(n, bool) or not isinstance(n, int) or not 0 <= n <= MAX_LIMIT
        for n in value.values()
    ):
        raise ValueError("budget values must be 0..100000 integers")
    return value


def used_today(store, *, now: datetime | None = None) -> dict:
    """今日已用（按预算日）；计数器损坏按 0 计，不抛。"""
    day = current_day(now)
    used = {}
    for capability in DEFAULTS:
        raw = store.get(f"budget:{day}:{capability}")
        used[capability] = (
            raw if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0 else 0
        )
    return used


def view(store, *, now: datetime | None = None) -> dict:
    """设置页视图：当前限额 + 今日已用 + 预算日。"""
    return {
        "limits": settings(store),
        "used": used_today(store, now=now),
        "day": current_day(now),
    }


def reserve(store, capability: str, *, now: datetime | None = None) -> None:
    """原子预留一个名额；超限抛 ModelError("budget", ...)。0 = 禁用该能力。

    预留成功即占名额——随后的请求失败不退还（失败仍花额度）。
    """
    if capability not in DEFAULTS:
        raise ValueError(f"unknown budget capability {capability!r}")
    day = current_day(now)
    key = f"budget:{day}:{capability}"
    with _LOCK:
        raw = store.get(key)
        used = raw if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0 else 0
        if used >= settings(store)[capability]:
            raise ModelError("budget", f"daily request budget reached: {capability}")
        store.put(key, used + 1)
