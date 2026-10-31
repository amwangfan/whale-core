"""search_web 插件包入口：注册 web.search + web.fetch 两个能力。

拆分自 src/whale_core/plugins/（2026-10-30 owner 决策：网页搜索独立为插件）。
经 plugins/loader.py 按目录显式加载；同目录模块用 importlib 按路径装载。
"""
from __future__ import annotations



def _load(name: str):
    # 包内模块相对导入（本包由 loader 以包形式加载）
    from importlib import import_module
    return import_module(f".{name}", package=__name__)


def register(registry, tools=None, env=None, store=None):
    """注册搜索与抓取。env 提供 TAVILY_API_KEY/BRAVE_API_KEY/WHALE_SEARCH_PROVIDER。"""
    web_search = _load("web_search")
    web_fetch = _load("web_fetch")
    provider = web_search.select_provider(env)
    web_search.register_web_search(registry, tools, provider=provider)
    web_fetch.register_web_fetch(registry, tools)
    return provider
