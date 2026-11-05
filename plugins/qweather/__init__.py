"""qweather 插件包入口：注册 weather.now（和风实时+短期预报）。

凭据仅由宿主经 env 注入：``QWEATHER_API_KEY`` + ``QWEATHER_API_HOST``。
缺任一则 ``register()`` 跳过挂载（意图路由回落 web.search）。
"""
from __future__ import annotations


def _load(name: str):
    from importlib import import_module
    return import_module(f".{name}", package=__name__)


def register(registry, tools=None, env=None, store=None, fallback_search=None):
    """注册 weather.now。env 提供 QWEATHER_API_KEY / QWEATHER_API_HOST。

    ``fallback_search`` 可选：``callable(query) -> str``，和风失败时回落网页搜索。
    """
    weather = _load("weather")
    return weather.register_qweather(
        registry, tools, env=env, fallback_search=fallback_search)
