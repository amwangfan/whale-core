"""开箱即用配置档（roadmap §9）：cloud / local 两档装配组合根。

- ``cloud``：DeepSeek（OpenAI 兼容）经 env 注入（DEEPSEEK_API_KEY 等）；
  配置了 TAVILY_API_KEY / BRAVE_API_KEY 时挂真实搜索与抓取工具；
- ``local``：本地 OpenAI 兼容端点（默认 http://127.0.0.1:11434/v1，可用
  LOCAL_MODEL_BASE_URL / LOCAL_MODEL_NAME 覆盖）；回环 http 允许无 key。

**没有 mock/demo 档**（owner 决策 2026-10-30：mock 模式完全没必要）——
模型与工具永远来自真实配置；缺凭据时 notes 如实警告、调用期报
config/network 错误，绝不用罐装结果冒充真实能力。

装配只组合已有组件（store/registry/manager/tools/companion/orchestrator），
不发明新逻辑。
"""

from __future__ import annotations

import os
from pathlib import Path
from dataclasses import dataclass, field
from typing import Mapping

from whale_core.core.companion import WhaleCompanion
from whale_core.core.runtime import ConversationOrchestrator, ToolRegistry
from whale_core.core.intent_router import SearchIntentRouter
from whale_core.memory.curation import MemoryCurator
from whale_core.memory.sqlite_store import SqliteMemoryStore
from whale_core.memory.store import InMemoryStore, MemoryStore
from whale_core.plugins.manager import PluginManager
from whale_core.plugins.model_provider import register_openai_compatible
from whale_core.plugins.registry import PluginRegistry

PROFILE_NAMES = ("cloud", "local")

PROFILE_DESCRIPTIONS = {
    "cloud": "云端模型：DEEPSEEK_API_KEY 注入后真实对话；搜索/抓取按 key 可用性挂载",
    "local": "本地模型：回环 OpenAI 兼容端点（默认 127.0.0.1:11434/v1），无需密钥",
}


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
        path = Path(config_dir)
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        return SqliteMemoryStore(path / "memory.sqlite3")
    return InMemoryStore()


def assemble_runtime(
    profile: str = "local",
    *,
    store: MemoryStore | None = None,
    env: Mapping[str, str] | None = None,
    config_dir: str | None = None,
) -> RuntimeBundle:
    """按配置档装配运行时。profile 非法 → ValueError。"""
    if profile not in PROFILE_NAMES:
        raise ValueError(
            f"unknown profile {profile!r}（可选：{', '.join(PROFILE_NAMES)}）"
        )
    env = dict(os.environ if env is None else env)
    # Keep the existing QQ runner's opt-in provider variable names compatible.
    for legacy, current in (
        ("WHALE_IMAGE_BASE_URL", "IMAGE_BASE_URL"),
        ("WHALE_IMAGE_API_KEY", "IMAGE_API_KEY"),
        ("WHALE_IMAGE_MODEL", "IMAGE_MODEL"),
        ("WHALE_SPEECH_BASE_URL", "ASR_BASE_URL"),
        ("WHALE_SPEECH_BASE_URL", "TTS_BASE_URL"),
        ("WHALE_SPEECH_API_KEY", "ASR_API_KEY"),
        ("WHALE_SPEECH_API_KEY", "TTS_API_KEY"),
    ):
        if legacy in env and current not in env:
            env[current] = env[legacy]
    store = store or _default_store(config_dir)
    registry = PluginRegistry()
    manager = PluginManager(
        registry,
        store,
        secret_dir=(Path(config_dir) / "secrets") if config_dir else None,
    )
    import json

    config = {}
    if config_dir:
        path = Path(config_dir) / "config.json"
        if path.exists():
            config = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(config, dict):
                raise ValueError("config.json must be an object")
    from whale_core.plugins.builtin import wire_builtins

    tools, model, notes = wire_builtins(manager, store, env, profile, config)
    if config_dir:
        manager.load_dir(Path(config_dir) / "manifests")
    from whale_core.memory.hybrid import HybridCurator

    class DynamicEmbedding:
        def embed(self, texts):
            provider = registry.provider_of("embedding")
            if provider is None:
                raise LookupError("embedding disabled")
            return provider.embed(texts)

    # Enable optional semantic retrieval only when explicitly selected.
    class DynamicCurator(MemoryCurator):
        def recall(self, query, *, limit=5):
            if registry.provider_of("embedding") is not None:
                return HybridCurator(store, embedder=DynamicEmbedding()).recall(
                    query, limit=limit
                )
            return super().recall(query, limit=limit)

    manager.watch_current()
    companion = WhaleCompanion(store, model, curator=DynamicCurator(store))
    llm_intent = env.get("WHALE_INTENT_MODEL_ENABLED", "") == "1" or bool(
        store.get("settings:intent_model_enabled")
        if store.get("settings:intent_model_enabled") is not None
        else config.get("intent_model_enabled")
    )

    class IntentModelProxy:
        def reply(self, text, **kwargs):
            provider = registry.provider_of("intent_model")
            if provider is not None:
                from whale_core.core.budget import reserve

                reserve(store, "model_calls")
            return (provider or model).reply(text, **kwargs)

    intent_model = SearchIntentRouter(model=IntentModelProxy(), enable_llm=llm_intent)
    orchestrator = ConversationOrchestrator(
        store, companion, tools=tools, intent_model=intent_model
    )
    return RuntimeBundle(
        profile=profile,
        store=store,
        registry=registry,
        manager=manager,
        tools=tools,
        companion=companion,
        orchestrator=orchestrator,
        notes=notes,
    )
