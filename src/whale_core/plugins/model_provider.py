"""模型供给插件（model kind）的官方装配与能力解析。

``llm.chat`` 能力由 model 插件提供；核心按能力解析 provider，业务逻辑不硬编码
供应商型号（roadmap §4.2）。端点凭据只在宿主 env / 私有配置，manifest 不含。
"""
from __future__ import annotations

from whale_core.core.deepseek import DeepSeekCompanion
from whale_core.plugins.manifest import PluginManifest
from whale_core.plugins.registry import PluginRegistry

LLM_CHAT = "llm.chat"


def register_openai_compatible(
    registry: PluginRegistry,
    *,
    plugin_id: str = "deepseek",
    name: str = "DeepSeek（OpenAI 兼容端点）",
    api_key: str | None = None,
    base_url: str | None = None,
    model: str | None = None,
    timeout: float = 25.0,
) -> DeepSeekCompanion:
    """注册一个 OpenAI 兼容 chat 端点为 model 插件（provides llm.chat）。

    凭据缺省走 DEEPSEEK_API_KEY/DEEPSEEK_BASE_URL/DEEPSEEK_MODEL 环境变量
    （DeepSeekCompanion 既有约定）；返回接线好的 provider 对象。
    """
    companion = DeepSeekCompanion(api_key=api_key, base_url=base_url,
                                  model=model, timeout=timeout)
    registry.register(
        PluginManifest(
            plugin_id=plugin_id, name=name, version="1.0.0", kind="model",
            provides=(LLM_CHAT,),
            description="OpenAI 兼容 chat completions 端点（默认 DeepSeek）",
        ),
        provider=companion,
    )
    return companion


def resolve_chat_model(registry: PluginRegistry):
    """解析 llm.chat 能力的 provider；未注册返回 None（契约先行，调用方决定回退）。"""
    for _pid, provider in registry.providers_for(LLM_CHAT):
        if provider is not None:
            return provider
    return None


def require_chat_model(registry: PluginRegistry):
    """同 resolve，但未配置时给可理解的显式错误（替代隐式 stub 行为）。"""
    provider = resolve_chat_model(registry)
    if provider is None:
        raise LookupError(
            "no llm.chat provider registered: register a model plugin "
            "(e.g. register_openai_compatible) or set up a local endpoint"
        )
    return provider
