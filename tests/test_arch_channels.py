"""issue #27 架构守卫：channels.qq 正式领域入口 + devices.bridge 新址。

- ``whale_core.channels.qq`` 必须可导入，且 re-export 的名字与 life 原模块
  是**同一对象**（``is`` 恒等——入口收拢不是复制实现）。
- ``whale_core.devices.bridge`` 是 device_bridge 的新家；
  ``whale_core.life.device_bridge`` 薄 shim 继续可用且同名对象恒等。
- devices/bridge.py 的 life 依赖白名单冻结：只允许 life.autonomy /
  life.situation（例外原因见该模块 docstring），防止边界模块长出
  新的临时依赖。
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parent.parent / "src" / "whale_core"

ONEBOT_NAMES = (
    "CHANNEL_QQ", "DEFAULT_ONEBOT_HOST", "DEFAULT_ONEBOT_PORT",
    "MAX_BODY_BYTES", "MAX_TEXT_CHARS",
    "HttpOneBotTransport", "NullOneBotTransport", "OneBotHttpListener",
    "OneBotInboundBridge", "OneBotOutboundPort", "OneBotTransport",
    "OneBotTransportError", "bearer_authenticated", "parse_onebot_message",
)
WORKFLOW_NAMES = ("QqInboundWorkflow", "REPLY_CHANNEL")
BRIDGE_NAMES = (
    "BridgeError", "GatewayActionPort", "SimulatedActionPort",
    "SimulatedDeviceBridge", "SituationState", "default_projector",
)


def test_channels_qq_entry_reexports_same_objects():
    import whale_core.channels.qq as qq
    import whale_core.life.qq_onebot as onebot
    import whale_core.life.qq_workflow as workflow
    for name in ONEBOT_NAMES:
        assert getattr(qq, name) is getattr(onebot, name), name
    for name in WORKFLOW_NAMES:
        assert getattr(qq, name) is getattr(workflow, name), name


def test_device_bridge_new_home_and_shim_identity():
    import whale_core.devices.bridge as bridge
    import whale_core.life.device_bridge as shim
    for name in BRIDGE_NAMES:
        assert getattr(shim, name) is getattr(bridge, name), name
    # 旧 from-import 路径继续可用且为同一对象
    from whale_core.life.device_bridge import SimulatedDeviceBridge
    assert SimulatedDeviceBridge is bridge.SimulatedDeviceBridge


def test_bridge_life_dependencies_allowlisted():
    """devices/bridge.py 只允许 import 白名单内的 life 稳定接口模块。"""
    allowed = {"whale_core.life.autonomy", "whale_core.life.situation"}
    tree = ast.parse(
        (SRC_DIR / "devices" / "bridge.py").read_text(encoding="utf-8"))
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            mods = [] if node.level else [node.module or ""]
        else:
            continue
        for mod in mods:
            if (mod == "whale_core.life" or mod.startswith("whale_core.life.")) \
                    and mod not in allowed:
                offenders.append(f"line {node.lineno}: {mod}")
    assert not offenders, (
        "devices/bridge.py 出现白名单外的 life 依赖（例外需在模块 docstring "
        "注明理由并同步本测试）：\n" + "\n".join(offenders)
    )
