"""模型供给插件（model kind）的官方装配与能力解析。

分层：内置 provider 实现，非插件框架契约（builtin-provider，issue #31）。

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
    name: str = "回复模型（互动对话）",
    api_key: str | None = None,
    base_url: str | None = None,
    model: str | None = None,
    timeout: float = 25.0,
    capability: str | tuple[str, ...] = LLM_CHAT,
) -> DeepSeekCompanion:
    """注册一个 OpenAI 兼容 chat 端点为 model 插件。

    凭据缺省走 DEEPSEEK_API_KEY/DEEPSEEK_BASE_URL/DEEPSEEK_MODEL 环境变量
    （DeepSeekCompanion 既有约定）；返回接线好的 provider 对象。
    ``capability`` 默认 llm.chat；可传 tuple 同时声明多个能力
    （如意图模型同时提供 llm.intent + llm.safety）。
    """
    companion = DeepSeekCompanion(api_key=api_key, base_url=base_url,
                                  model=model, timeout=timeout)
    registry.register(
        PluginManifest(
            plugin_id=plugin_id, name=name, version="1.0.0", kind="model",
            provides=(capability,) if isinstance(capability, str) else tuple(capability),
            description=f"当前接线 OpenAI 兼容端点（{capability}）；供应商/端点/模型/密钥可在设置页或插件配置中更换",
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


def resolve_role_model(registry: PluginRegistry, role: str):
    """按角色解析模型 provider（llm.intent / llm.tool_executor / llm.safety 等）；
    未注册该角色时回落 llm.chat；都没有返回 None。"""
    for _pid, provider in registry.providers_for(f"llm.{role}"):
        if provider is not None:
            return provider
    if role != "chat":
        return resolve_chat_model(registry)
    return None
