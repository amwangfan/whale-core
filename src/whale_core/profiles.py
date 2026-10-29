"""开箱即用配置档（roadmap §9）：demo / cloud / local 三档装配组合根。

- ``demo``：零密钥零网络——mock 模型（回复带 [mock] 前缀，诚实标注）+
  演示搜索/抓取工具（罐装结果），展示对话/记忆/意图/插件调用闭环；
- ``cloud``：DeepSeek（OpenAI 兼容）经 env 注入（DEEPSEEK_API_KEY 等）；
  配置了 TAVILY_API_KEY / BRAVE_API_KEY 时挂真实搜索与抓取工具；
- ``local``：本地 OpenAI 兼容端点（默认 http://127.0.0.1:11434/v1，可用
  LOCAL_MODEL_BASE_URL / LOCAL_MODEL_NAME 覆盖）；回环 http 允许无 key。

装配只组合已有组件（store/registry/manager/tools/companion/orchestrator），
不发明新逻辑；每档返回 notes 如实标注哪些是 mock、哪些能力缺凭据。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Mapping

from whale_core.core.companion import WhaleCompanion
from whale_core.core.runtime import ConversationOrchestrator, ToolRegistry, ToolSpec
from whale_core.core.intent_router import SearchIntentRouter
from whale_core.memory.curation import MemoryCurator
from whale_core.memory.sqlite_store import SqliteMemoryStore
from whale_core.memory.store import InMemoryStore, MemoryStore
from whale_core.plugins.manager import PluginManager
from whale_core.plugins.model_provider import register_openai_compatible
from whale_core.plugins.registry import PluginRegistry

PROFILE_NAMES = ("demo", "cloud", "local")

PROFILE_DESCRIPTIONS = {
    "demo": "离线演示：mock 模型与罐装工具，不需要任何密钥（输出标注 [mock]）",
    "cloud": "云端模型：DEEPSEEK_API_KEY 注入后真实对话；搜索/抓取按 key 可用性挂载",
    "local": "本地模型：回环 OpenAI 兼容端点（默认 127.0.0.1:11434/v1），无需密钥",
}


class MockChatModel:
    """demo 档替身模型：确定性回复，永远带 [mock] 前缀（不冒充真实模型）。"""

    def reply(self, text: str, *, persona: str = "", memories=(), history=()) -> str:
        note = f"（召回 {len(memories)} 条记忆，历史 {len(history)} 条）"
        return f"[mock] 收到「{text[:40]}」{note}"


@dataclass
class RuntimeBundle:
    """一档装配出的完整运行时（宿主按需取用部件）。"""

    profile: str
    store: MemoryStore
    registry: PluginRegistry
    manager: PluginManager
    tools: ToolRegistry
    companion: WhaleCompanion
    orchestrator: ConversationOrchestrator
    notes: list[str] = field(default_factory=list)


def _default_store(config_dir: str | None) -> MemoryStore:
    if config_dir:
        from pathlib import Path
        path = Path(config_dir)
        path.mkdir(parents=True, exist_ok=True)
        return SqliteMemoryStore(path / "memory.sqlite3")
    return InMemoryStore()


def assemble_runtime(
    profile: str = "demo",
    *,
    store: MemoryStore | None = None,
    env: Mapping[str, str] | None = None,
    config_dir: str | None = None,
) -> RuntimeBundle:
    """按配置档装配运行时。profile 非法 → ValueError。"""
    if profile not in PROFILE_NAMES:
        raise ValueError(f"unknown profile {profile!r}（可选：{', '.join(PROFILE_NAMES)}）")
    env = os.environ if env is None else env
    store = store or _default_store(config_dir)
    registry = PluginRegistry()
    manager = PluginManager(registry, store)
    tools = ToolRegistry()
    notes: list[str] = []

    if profile == "demo":
        model = MockChatModel()
        notes.append("[mock] 离线演示档：模型与工具均为罐装替身")
        tools.register(ToolSpec(
            name="search_web", capability="web.search",
            description="[mock] 罐装搜索",
            handler=lambda args: "[mock] 罐装结果：示例标题 — 摘要（https://example.com）",
        ))
    elif profile == "cloud":
        model = register_openai_compatible(
            registry, api_key=env.get("DEEPSEEK_API_KEY"),
            base_url=env.get("DEEPSEEK_BASE_URL"),
            model=env.get("DEEPSEEK_MODEL"),
        )
        notes.append("cloud 档：DeepSeek（OpenAI 兼容）模型已注册（llm.chat）")
        if not (env.get("DEEPSEEK_API_KEY") or "").strip():
            notes.append("[warn] DEEPSEEK_API_KEY 未配置——模型调用会以 config 错误失败")
        if (env.get("TAVILY_API_KEY") or env.get("BRAVE_API_KEY")):
            from whale_core.plugins.web_fetch import register_web_fetch
            from whale_core.plugins.web_search import register_web_search
            from whale_core.plugins.web_search import select_provider
            register_web_search(registry, tools, provider=select_provider(env))
            register_web_fetch(registry, tools)
            notes.append("搜索/抓取工具已挂载（web.search / web.fetch）")
        else:
            notes.append("[warn] 无 TAVILY_API_KEY/BRAVE_API_KEY——搜索工具未挂载")
    else:  # local
        base_url = env.get("LOCAL_MODEL_BASE_URL", "http://127.0.0.1:11434/v1")
        model_name = env.get("LOCAL_MODEL_NAME", "")
        model = register_openai_compatible(
            registry, plugin_id="local_model", name="本地模型端点",
            api_key="", base_url=base_url,   # 回环端点允许空 key（deepseek.py R3）
            model=model_name or None,
        )
        notes.append(f"local 档：本地端点 {model.base_url}（模型 {model.model}）")

    companion = WhaleCompanion(store, model, curator=MemoryCurator(store))
    # Explicit searches route to web.search; arbitrary LLM tool planning is opt-in.
    # Safe if no search provider is installed: runtime reports capability unavailable.
    llm_intent = env.get("WHALE_INTENT_MODEL_ENABLED", "") == "1"
    intent_model = SearchIntentRouter(model=model, enable_llm=llm_intent and profile != "demo")
    if llm_intent and profile != "demo":
        notes.append("LLM 意图识别已启用（WHALE_INTENT_MODEL_ENABLED=1）")
    orchestrator = ConversationOrchestrator(store, companion, tools=tools,
                                            intent_model=intent_model)
    return RuntimeBundle(
        profile=profile, store=store, registry=registry, manager=manager,
        tools=tools, companion=companion, orchestrator=orchestrator, notes=notes,
    )
