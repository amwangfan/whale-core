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
from dataclasses import replace
from whale_core.configuration import configured_env
from whale_core.plugins.model_provider import require_chat_model
from whale_core.core.diagnostics import InstrumentedProvider

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
    optional: object = None


class _ManagedTools(ToolRegistry):
    """Resolve enabled tools dynamically instead of retaining disabled handlers."""
    def __init__(self, registry):
        super().__init__()
        self.registry = registry
        self.owners = {}

    def get(self, name):
        spec = super().get(name)
        owner = self.owners.get(name, getattr(spec, 'plugin_id', ''))
        return spec if spec and (not owner or self.registry.manifest(owner)) else None

    def specs(self):
        return [s for s in super().specs() if self.get(s.name) is not None]

    def for_capability(self, capability):
        return [s for s in self.specs() if s.capability == capability]

    def guard(self):
        for name, spec in list(self._tools.items()):
            if spec.handler is None:
                continue
            owner = self.owners.get(name, spec.plugin_id)
            if not owner:
                continue
            def call(args, handler=spec.handler, owner=owner):
                if self.registry.manifest(owner) is None:
                    raise LookupError('plugin disabled')
                return handler(args)
            self._tools[name] = replace(spec, handler=call)


class _ManagedModel:
    def __init__(self, registry, store):
        self.registry, self.store = registry, store

    def __getattr__(self, name):
        return getattr(require_chat_model(self.registry), name)

    def reply(self, text, **kwargs):
        model = require_chat_model(self.registry)
        # 预算预留先于真实调用：拿到名额后失败照样占额度（防重试风暴）。
        from whale_core.core.budget import reserve
        reserve(self.store, "model_calls")
        return InstrumentedProvider('llm.chat', model, self.store).reply(text, **kwargs)


# 分角色模型能力映射（设置页/环境变量可配；未配置的角色回落 llm.chat）
# 分角色模型能力映射（设置页/环境变量可配；未配置的角色回落 llm.chat）。
# owner 决策（2026-10-30）：意图与安全审核合并为一个角色「意图」
# （同一模型既做规划也做发送前复核），executor 仍独立。
ROLE_CAPABILITIES = {
    "intent": ("llm.intent", "llm.safety"),  # 意图规划 + 发送前安全审核（同一插件）
    "executor": ("llm.tool_executor",),      # 任务执行（委托工具循环）
}


class _ManagedRoleModel:
    """角色模型的动态解析代理：每次调用按当前注册表解析 llm.<role>（热切换跟随），
    未配置该角色时回落 llm.chat。与 _ManagedModel 同款插桩。"""

    def __init__(self, registry, store, role: str):
        self.registry, self.store, self.role = registry, store, role

    def __getattr__(self, name):
        from whale_core.plugins.model_provider import resolve_role_model
        return getattr(resolve_role_model(self.registry, self.role), name)

    def reply(self, text, **kwargs):
        from whale_core.plugins.model_provider import resolve_role_model
        model = resolve_role_model(self.registry, self.role)
        if model is None:
            raise LookupError(f"no model for role {self.role!r} and no llm.chat fallback")
        from whale_core.core.budget import reserve
        reserve(self.store, "model_calls")
        return InstrumentedProvider(f'llm.{self.role}', model, self.store).reply(text, **kwargs)


def _default_store(config_dir: str | None) -> MemoryStore:
    if config_dir:
        path = Path(config_dir)
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(path, 0o700)
        return SqliteMemoryStore(path / "memory.sqlite3")
    return InMemoryStore()


def assemble_runtime(
    profile: str = "local",
    *,
    store: MemoryStore | None = None,
    env: Mapping[str, str] | None = None,
    config_dir: str | None = None,
    use_saved_config: bool = True,
) -> RuntimeBundle:
    """按配置档装配运行时。profile 非法 → ValueError。"""
    if profile not in PROFILE_NAMES:
        raise ValueError(f"unknown profile {profile!r}（可选：{', '.join(PROFILE_NAMES)}）")
    env = os.environ if env is None else env
    if use_saved_config:
        env = configured_env(profile, config_dir, env)
    store = store or _default_store(config_dir)
    registry = PluginRegistry()
    manager = PluginManager(registry, store,
                            secret_dir=(Path(config_dir) / 'secrets') if config_dir else None)
    tools = _ManagedTools(registry)
    notes: list[str] = []

    if profile == "cloud":
        model = register_openai_compatible(
            registry, api_key=env.get("DEEPSEEK_API_KEY", ""),
            base_url=env.get("DEEPSEEK_BASE_URL") or "https://api.deepseek.com",
            model=env.get("DEEPSEEK_MODEL") or "deepseek-chat",
        )
        notes.append("cloud 档：DeepSeek（OpenAI 兼容）模型已注册（llm.chat）")
        if not (env.get("DEEPSEEK_API_KEY") or "").strip():
            notes.append("[warn] DEEPSEEK_API_KEY 未配置——模型调用会以 config 错误失败")
    else:  # local
        base_url = env.get("LOCAL_MODEL_BASE_URL", "http://127.0.0.1:11434/v1")
        model_name = env.get("LOCAL_MODEL_NAME", "")
        model = register_openai_compatible(
            registry, plugin_id="local_model", name="本地模型端点",
            api_key=env.get("LOCAL_MODEL_API_KEY", ""), base_url=base_url,   # 回环端点允许空 key（deepseek.py R3）
            model=model_name or "local-model",
        )
        notes.append(f"local 档：本地端点 {model.base_url}（模型 {model.model}）")

    # 搜索/抓取与模型档位正交：两档都按 key 可用性挂载
    if (env.get("TAVILY_API_KEY") or env.get("BRAVE_API_KEY") or env.get("WHALE_SEARCH_PROVIDER")):
        from search_web.web_fetch import register_web_fetch
        from search_web.web_search import register_web_search, select_provider
        register_web_search(registry, tools, provider=select_provider(env))
        register_web_fetch(registry, tools)
        tools.owners.update(search_web="web_search", fetch_url="web_fetch")
        notes.append("搜索/抓取工具已挂载（web.search / web.fetch）")
    else:
        notes.append("[warn] 无 TAVILY_API_KEY/BRAVE_API_KEY——搜索工具未挂载")

    # Fetching is useful independently of paid search configuration.
    if registry.manifest('web_fetch') is None:
        from search_web.web_fetch import register_web_fetch
        register_web_fetch(registry, tools)
        tools.owners['fetch_url'] = 'web_fetch'
    from whale_core.plugins.capability_setup import configure_optional_capabilities
    optional = configure_optional_capabilities(registry, tools, store, env=env,
                artifact_dir=Path(config_dir) / 'artifacts' if config_dir else None)
    notes.extend(optional.notes)
    if env.get('WHALE_SHELL_ENABLED') == '1':
        from whale_core.plugins.shell_tool import register_shell
        if not config_dir:
            notes.append('[warn] Shell requires a persistent workspace (config_dir)')
        else:
            register_shell(registry, tools, Path(config_dir) / 'workspace', env=env)
            notes.append('受控终端已挂载；自由命令需WHALE_SHELL_TIER=personal、逐次审批和可用Linux系统沙箱')
    # 分角色模型注册（可选）：WHALE_INTENT_* / WHALE_EXECUTOR_*
    # （旧 WHALE_SAFETY_* 迁移为 intent——安全审核与规划同一角色）
    # 只配 MODEL 名时复用主端点与凭据（同平台换模型）；配 BASE_URL 则独立供应商。
    if not env.get('WHALE_INTENT_MODEL') and not env.get('WHALE_INTENT_BASE_URL'):
        legacy_safety = {k: env[k] for k in ('WHALE_SAFETY_BASE_URL', 'WHALE_SAFETY_MODEL', 'WHALE_SAFETY_API_KEY') if env.get(k)}
        if legacy_safety:
            env = dict(env)
            for k, v in legacy_safety.items():
                env[k.replace('WHALE_SAFETY_', 'WHALE_INTENT_')] = v
            notes.append('旧 WHALE_SAFETY_* 配置已迁移为意图角色（审核=规划同模型）')
    for role, capability in ROLE_CAPABILITIES.items():
        r_base = env.get(f"WHALE_{role.upper()}_BASE_URL", "").strip()
        r_model = env.get(f"WHALE_{role.upper()}_MODEL", "").strip()
        r_key = env.get(f"WHALE_{role.upper()}_API_KEY", "")
        if not (r_base or r_model):
            continue
        if not r_base:
            r_base = model.base_url          # 复用主端点
            r_key = r_key or model.api_key   # 复用主凭据
            r_model = r_model or model.model
        register_openai_compatible(
            registry, plugin_id=f"llm_{role}",
            name={"intent": "意图模型（规划+审核）", "executor": "任务执行模型",
                  "safety": "安全审核模型"}.get(role, f"{role} 模型"),
            api_key=r_key, base_url=r_base, model=r_model, capability=capability)
        notes.append(f"角色模型已注册：{role} → {r_model}（{r_base}）")
    manager.adopt_registered()
    if config_dir:
        manifest_dir = Path(config_dir) / 'manifests'
        manifest_dir.mkdir(parents=True, exist_ok=True)
        manager.load_dir(manifest_dir)

    tools.guard()
    managed_model = _ManagedModel(registry, store)
    from whale_core.memory.semantic import DynamicHybridCurator
    companion = WhaleCompanion(store, managed_model, curator=DynamicHybridCurator(store, registry, base=MemoryCurator(store)))
    # Explicit searches route to web.search; arbitrary LLM tool planning is opt-in.
    # Safe if no search provider is installed: runtime reports capability unavailable.
    llm_intent = env.get("WHALE_INTENT_MODEL_ENABLED", "") == "1"
    # 意图模型按角色解析：llm.intent 未配置时回落 llm.chat（热切换跟随注册表）
    intent_router_model = _ManagedRoleModel(registry, store, "intent") if llm_intent else managed_model
    intent_model = SearchIntentRouter(model=intent_router_model, enable_llm=llm_intent, tools=tools)
    if llm_intent:
        notes.append("LLM 意图识别已启用（WHALE_INTENT_MODEL_ENABLED=1）")
    from whale_core.plugins.gateway import PluginGateway
    from whale_core.plugins.policy import PluginActionPolicy
    from whale_core.core.runtime import runtime_approval_verifier, runtime_owner_verifier
    gateway = PluginGateway(registry, PluginActionPolicy(registry),
                            owner_verifier=runtime_owner_verifier(store),
                            approval_verifier=runtime_approval_verifier(store))
    # G1-C：多轮工具执行模型（可选，默认关）——plan 未声明能力链时由
    # llm.tool_executor 角色逐轮提议工具；固定链仍是稳定保底模式。
    executor_model = None
    if env.get('WHALE_EXECUTOR_ENABLED', '') == '1':
        executor_model = _ManagedRoleModel(registry, store, 'executor')
        notes.append('多轮工具执行模型已启用（WHALE_EXECUTOR_ENABLED=1）')
    orchestrator = ConversationOrchestrator(store, companion, tools=tools,
                                            intent_model=intent_model, gateway=gateway,
                                            executor_model=executor_model)
    return RuntimeBundle(
        profile=profile, store=store, registry=registry, manager=manager,
        tools=tools, companion=companion, orchestrator=orchestrator, notes=notes, optional=optional,
    )
