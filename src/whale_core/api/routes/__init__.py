"""/api/v1/* 路由实现模块（issue #30 拆分）。

按 API surface 分面；函数风格与 ``whale_core.api.devices_api`` 一致——
首参为请求 handler（提供 ``_send``/``_body_json``/``ctx``/``server``）。
鉴权、Origin 检查、统一异常映射、静态服务与生命周期仍留在
``whale_core.api.server`` 主模块；本包只含路由本体。

studio/auth（``whale_core.api.studio``）与 devices
（``whale_core.api.devices_api``）沿用既有独立模块，不在本包内。
"""

from whale_core.api.routes import (
    artifacts,
    chat,
    media,
    operations,
    plugins,
    system,
    traces,
    workflows,
)

__all__ = [
    "artifacts",
    "chat",
    "media",
    "operations",
    "plugins",
    "system",
    "traces",
    "workflows",
]
