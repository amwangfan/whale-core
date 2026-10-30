"""插件注册表：显式注册、能力查询、执行器解析。

与仓内既有约定一致（qq_onebot / gateway 同款）：**不做自动发现**——
不扫描 entry_points、不读环境变量自动加载。宿主在自己的组装代码里
显式 ``register()``；外置进程插件只注册 manifest + HTTP/队列执行器。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from whale_core.perception.adapters import PerceptionAdapter
from whale_core.plugins.manifest import PROVIDER_KINDS, PluginKind, PluginManifest, RiskClass


class PluginExecutor(Protocol):
    """插件动作执行器（输出侧）。

    返回 ``(ok, receipt)``；receipt 是**短文本回执**，不得包含密钥 / token /
    完整消息正文（与 bridge_tools 日志脱敏同款约定）。真实网络 / 设备调用
    在插件进程内，核心永远不 import 第三方插件业务代码。
    """

    def execute(self, action: str, target: str, args: Mapping[str, Any]) -> tuple[bool, str]:
        ...


class NullPluginExecutor:
    """未接线 stub：注册了 manifest 但执行器尚未部署（契约先行）。"""

    def execute(self, action: str, target: str, args: Mapping[str, Any]) -> tuple[bool, str]:
        return False, "plugin not connected"


@dataclass(frozen=True)
class PluginRegistration:
    manifest: PluginManifest
    executor: PluginExecutor
    input_adapter: PerceptionAdapter | None = None
    # provider 类插件（processor / model / memory）的内部对象：
    # IntentProcessor / 模型客户端 / MemoryStore 后端等，宿主按能力名取用。
    # 类型在 processing.py 等模块定义，这里保持结构化 typing 避免循环 import。
    provider: Any = None


class PluginRegistry:
    """plugin_id → 注册项；事件 kind 全局唯一归属，防止跨插件冒充。"""

    def __init__(self) -> None:
        self._items: dict[str, PluginRegistration] = {}
        self._kind_owner: dict[str, str] = {}

    def register(
        self,
        manifest: PluginManifest,
        *,
        executor: PluginExecutor | None = None,
        input_adapter: PerceptionAdapter | None = None,
        provider: Any = None,
    ) -> None:
        pid = manifest.plugin_id
        if pid in self._items:
            raise ValueError(f"plugin already registered: {pid!r}")
        resolved = manifest.resolved_kind
        if provider is not None and resolved not in PROVIDER_KINDS:
            raise ValueError(
                f"provider object is for provider-kind plugins (processor/model/memory), "
                f"got {resolved.value} plugin {pid!r}"
            )
        if resolved in PROVIDER_KINDS and (executor is not None or input_adapter is not None):
            raise ValueError("provider-kind plugins cannot register I/O executors or input adapters")
        if input_adapter is not None and not manifest.inputs:
            raise ValueError("input_adapter requires declared manifest inputs")
        if executor is not None and not manifest.actions:
            raise ValueError("executor requires declared manifest actions")
        for kind in manifest.inputs:
            owner = self._kind_owner.get(kind)
            if owner is not None and owner != pid:
                raise ValueError(f"event kind {kind!r} already owned by plugin {owner!r}")
        for kind in manifest.inputs:
            self._kind_owner[kind] = pid
        self._items[pid] = PluginRegistration(
            manifest=manifest,
            executor=executor or NullPluginExecutor(),
            input_adapter=input_adapter,
            provider=provider,
        )

    def unregister(self, plugin_id: str) -> None:
        item = self._items.pop(plugin_id, None)
        if item is None:
            return
        for kind in item.manifest.inputs:
            if self._kind_owner.get(kind) == plugin_id:
                del self._kind_owner[kind]

    def manifest(self, plugin_id: str) -> PluginManifest | None:
        item = self._items.get(plugin_id)
        return item.manifest if item else None

    def manifests(self) -> list[PluginManifest]:
        return [item.manifest for item in self._items.values()]

    def executor(self, plugin_id: str) -> PluginExecutor | None:
        item = self._items.get(plugin_id)
        return item.executor if item else None

    def provider_of(self, plugin_id: str):
        """provider 类插件的内部对象（无则 None）。"""
        item = self._items.get(plugin_id)
        return item.provider if item else None

    def risk_of(self, plugin_id: str, action: str) -> RiskClass | None:
        manifest = self.manifest(plugin_id)
        return manifest.risk_of(action) if manifest else None

    def kind_of(self, plugin_id: str) -> PluginKind | None:
        manifest = self.manifest(plugin_id)
        return manifest.resolved_kind if manifest else None

    def input_adapters(self) -> list[tuple[str, PerceptionAdapter]]:
        return [
            (pid, item.input_adapter)
            for pid, item in self._items.items()
            if item.input_adapter is not None
        ]

    def providers_for(
        self, capability: str, *, kind: PluginKind | None = None
    ) -> list[tuple[str, Any]]:
        """按能力名解析 provider 类插件（注册顺序即优先级）。

        返回 ``(plugin_id, provider)``；provider 为 None（未接线）也会返回，
        由调用方（如 ProcessorRouter）落到 Null 行为——契约先行、部署后置。
        """
        out: list[tuple[str, Any]] = []
        for pid, item in self._items.items():
            m = item.manifest
            resolved = m.resolved_kind
            if resolved not in PROVIDER_KINDS:
                continue
            if kind is not None and resolved is not kind:
                continue
            if capability in m.provides:
                out.append((pid, item.provider))
        return out

    def processors_for(self, capability: str) -> list[tuple[str, Any]]:
        """processors_for = providers_for(capability, kind=PROCESSOR) 的快捷方式。"""
        return self.providers_for(capability, kind=PluginKind.PROCESSOR)

    def owner_of_kind(self, kind: str) -> str | None:
        return self._kind_owner.get(kind)
