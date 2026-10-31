"""whale 命令行入口包（roadmap §9 demo 档）。

只暴露 ``main``（懒加载，避免 ``python -m whale_core.cli.main`` 的
runpy 双重导入警告）；实现见 ``whale_core.cli.main``。CLI 是宿主侧薄壳：
装配现有的 PluginManager / MemoryStore / ConversationOrchestrator，
不在核心新增行为。
"""

from __future__ import annotations

__all__ = ["main"]


def __getattr__(name: str):
    if name == "main":
        from whale_core.cli.main import main

        return main
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
