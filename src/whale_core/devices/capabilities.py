"""能力枚举（contracts/v2 §5.1 单一事实源的 Core 侧读取口）。

运行时从镜像 contracts/v2/messages/device.hello.schema.json 读取枚举，
保证与协议逐字一致、不产生第二份手写清单。路径可用 WHALE_CONTRACTS_DIR
覆盖（与 schema.py 约定一致）。
"""
from __future__ import annotations

import json
import os
from functools import lru_cache


def _contracts_root() -> str:
    override = os.environ.get("WHALE_CONTRACTS_DIR")
    if override:
        return override
    here = os.path.dirname(os.path.abspath(__file__))
    # src/whale_core/devices/ -> 仓根
    return os.path.normpath(os.path.join(here, "..", "..", "..", "contracts"))


@lru_cache(maxsize=1)
def all_capabilities() -> tuple[str, ...]:
    """hello/hello_ack 全集（§5.1）。"""
    path = os.path.join(_contracts_root(), "v2", "messages",
                        "device.hello.schema.json")
    with open(path, encoding="utf-8") as f:
        schema = json.load(f)
    return tuple(schema["properties"]["payload"]["properties"]
                 ["capabilities"]["items"]["enum"])


@lru_cache(maxsize=1)
def action_capabilities() -> tuple[str, ...]:
    """action.request 可用子集（§5.3）。"""
    path = os.path.join(_contracts_root(), "v2", "messages",
                        "action.request.schema.json")
    with open(path, encoding="utf-8") as f:
        schema = json.load(f)
    return tuple(schema["properties"]["payload"]["properties"]
                 ["capability"]["enum"])


@lru_cache(maxsize=1)
def query_operations() -> tuple[str, ...]:
    """sensor.query 可用子集（§5.3）。"""
    path = os.path.join(_contracts_root(), "v2", "messages",
                        "sensor.query.schema.json")
    with open(path, encoding="utf-8") as f:
        schema = json.load(f)
    return tuple(schema["properties"]["payload"]["properties"]
                 ["operation"]["enum"])


# GUI 类能力：observe 与屏幕交互（选路偏好 embedded_wss > adb > ssh）
GUI_CAPS = frozenset({
    "device.observe", "device.tap", "device.long_press", "device.swipe",
    "device.input_text", "device.key", "device.wait",
    "device.app.launch", "device.app.list", "device.screen.record",
})

# Shell/文件类能力（选路偏好 ssh > embedded_wss > adb）
SHELL_CAPS = frozenset({
    "device.shell", "device.file.read", "device.file.write",
    "device.file.list", "device.logcat",
})
