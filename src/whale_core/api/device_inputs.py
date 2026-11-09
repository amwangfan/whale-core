"""REST input normalization; wire schemas remain the protocol's source of truth."""
from __future__ import annotations

import math
from decimal import Decimal
import threading
import time


def positive_number(body: dict, key: str, default: float) -> float:
    raw = body.get(key, default)
    if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
        raise ValueError(f"{key} 必须为正有限数值")
    try:
        value = float(raw)
    except (ValueError, OverflowError) as exc:
        raise ValueError(f"{key} 必须为正有限数值") from exc
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{key} 必须为正有限数值")
    return value


def positive_integer(body: dict, key: str, default: int) -> int:
    positive_number(body, key, default)
    value = Decimal(str(body.get(key, default)))
    if value != value.to_integral_value():
        raise ValueError(f"{key} 必须为整数")
    return int(value)


def request_timeout(body: dict) -> float:
    return max(1.0, min(60.0, positive_number(body, "timeout", 15)))


def object_field(body: dict, key: str) -> dict:
    value = body.get(key)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{key} 必须为对象")
    return value


def optional_text(body: dict, key: str) -> str | None:
    value = body.get(key)
    if value is not None and not isinstance(value, str):
        raise ValueError(f"{key} 必须为字符串")
    return value


def query_options(body: dict) -> dict:
    count = positive_integer(body, "max_items", 10)
    if count > 100:
        raise ValueError("max_items 须在 1..100")
    return {"filters": object_field(body, "filters"), "max_items": count,
            "timeout_s": request_timeout(body)}


def action_options(body: dict) -> dict:
    canonical = body.get("arguments")
    arguments = object_field({"arguments": canonical if canonical is not None else body.get("args")}, "arguments")
    scope = body.get("authorized_scope")
    if scope is not None and not (isinstance(scope, list) and all(isinstance(s, str) for s in scope)):
        raise ValueError("authorized_scope 必须为字符串数组")
    return {"arguments": arguments, "authorized_scope": scope, "timeout_s": request_timeout(body),
            **{key: optional_text(body, key) for key in (
                "idempotency_key", "correlation_id", "approval_ref", "task_id", "run_id", "observation_id")}}


def confirmation_options(body: dict) -> dict:
    result = dict(body)
    for key, default in (("ttl_s", 120), ("grant_ttl_s", 600)):
        result[key] = positive_number(body, key, default)
    maximum = min(threading.TIMEOUT_MAX - 10, 4102444800 - time.time())
    if result["ttl_s"] > maximum:
        raise ValueError("ttl_s 超出协议日期或平台等待范围")
    result["grant_max_uses"] = positive_integer(body, "grant_max_uses", 1)
    for key, default, limit in (("principal_id", "owner", 128), ("channel", "api", 64),
                                ("conversation_id", "", 128), ("action_fingerprint", "", 128)):
        value = optional_text(body, key)
        result[key] = value or default
        if len(result[key]) > limit:
            raise ValueError(f"{key} 长度超出范围")
    return result
