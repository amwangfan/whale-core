"""能力枚举（contracts/v2 §5.1 单一事实源的 Core 侧读取口）。

运行时从镜像 contracts/v2/messages/device.hello.schema.json 读取枚举，
保证与协议逐字一致、不产生第二份手写清单。路径可用 WHALE_CONTRACTS_DIR
覆盖（与 schema.py 约定一致）。
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from whale_core.devices.contract_paths import messages_dir


def _contracts_root() -> str:
    return str(messages_dir().parent.parent)


@lru_cache(maxsize=16)
def _enum(directory: str, message: str, field: str, array: bool) -> tuple[str, ...]:
    schema = json.loads((Path(directory) / f'{message}.schema.json').read_text(encoding='utf-8'))
    node = schema['properties']['payload']['properties'][field]
    return tuple((node['items'] if array else node)['enum'])


def all_capabilities() -> tuple[str, ...]:
    """hello/hello_ack 全集（§5.1）。"""
    return _enum(str(messages_dir()), 'device.hello', 'capabilities', True)


def action_capabilities() -> tuple[str, ...]:
    """action.request 可用子集（§5.3）。"""
    return _enum(str(messages_dir()), 'action.request', 'capability', False)


def query_operations() -> tuple[str, ...]:
    """sensor.query 可用子集（§5.3）。"""
    return _enum(str(messages_dir()), 'sensor.query', 'operation', False)


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
