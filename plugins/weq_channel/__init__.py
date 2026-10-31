"""weq_channel 插件包入口：QQ/WeQ 渠道注册。

拆分自 src/whale_core/life/（2026-10-30 owner 决策：WeQ 功能独立为插件）。
宿主（生产 runner）经 plugins/loader.py 显式加载；渠道依赖（发送端口/
账本/策略）由宿主注入，本包不自带凭据。
"""
from __future__ import annotations



def _load(name: str):
    # 包内模块相对导入（本包由 loader 以包形式加载）
    from importlib import import_module
    return import_module(f".{name}", package=__name__)


def register(registry, **kwargs):
    """注册 QQ/WeQ channel 插件。kwargs 透传 register_qq_weq_plugin
    （port/outbox/prefs 等宿主依赖）。"""
    qq_weq_plugin = _load("qq_weq_plugin")
    return qq_weq_plugin.register_qq_weq_plugin(registry, **kwargs)
