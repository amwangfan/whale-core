"""PluginManager：显式清单加载 + 生命周期 + 健康检查 + 配置管理。

与仓内约定一致（见 registry.py）：**不做自动发现**——不扫 entry_points、
不读环境变量自动接线。宿主把 manifest JSON 显式放进目录，``load_dir``
只加载不启用；启用必须显式 ``enable()``。加载 ≠ 启用。

- 启停状态持久化到 MemoryStore（kv 键 ``plugin:state:{plugin_id}``），
  重启后重新 ``load_dir`` 即回放：持久化为 enabled 的插件会重新注册
  （工厂接线，无工厂则 NullPluginExecutor——契约先行、部署后置）；
- 配置存 kv 键 ``plugin:config:{plugin_id}``；``set_config`` 按 manifest
  附带的 config_schema（JSON Schema 最小子集）校验，未知字段宽容放行；
- ``config_view`` 是唯一对外配置读取口：``format: "secret"`` 字段与键名含
  token/key/secret/password 的值一律脱敏；
- 任何错误 / 日志文本只含字段名与异常类型名，**绝不含配置值或工厂参数**。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from whale_core.memory.store import MemoryStore
from whale_core.plugins.manifest import PluginManifest
from whale_core.plugins.registry import PluginRegistry

PLUGIN_API_VERSION = "1"  # 核心当前支持的 manifest API major 版本

# JSON 清单中属于 PluginManifest 的键；单独的 "config_schema" 键由 manager 持有
_MANIFEST_KEYS = (
    "plugin_id",
    "name",
    "version",
    "author",
    "description",
    "homepage",
    "kind",
    "inputs",
    "actions",
    "action_risk",
    "provides",
    "process_model",
    "api_version",
)
_WIRING_KEYS = ("executor", "input_adapter", "provider")  # 对齐 PluginRegistry.register
_SECRET_HINTS = ("token", "key", "secret", "password")

# config_schema 最小子集的 type → Python 类型
_CONFIG_TYPES: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "number": (int, float),  # bool 另行排除
    "boolean": (bool,),
    "object": (dict,),
    "array": (list,),
}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class PluginState:
    """生命周期状态。"""

    plugin_id: str
    status: str  # "loaded" | "enabled" | "disabled" | "error"
    manifest: PluginManifest
    error: str = ""  # status=error 时的原因（异常类型名，不含敏感参数）


class PluginManager:
    """显式清单加载 + 启停生命周期 + 健康检查 + 配置管理。

    ``store`` 持久化启停状态与配置；``factories`` 是宿主显式提供的接线表
    plugin_id → factory()，factory 返回 dict（可含 executor / input_adapter /
    provider 键，对齐 PluginRegistry.register 参数）。没有工厂的插件 enable
    时注册 NullPluginExecutor。
    """

    def __init__(
        self,
        registry: PluginRegistry,
        store: MemoryStore,
        *,
        factories: Mapping[str, Callable[[], Mapping[str, Any] | None]] | None = None,
    ) -> None:
        self._registry = registry
        self._store = store
        self._factories = dict(factories or {})
        self._states: dict[str, PluginState] = {}
        self._schemas: dict[str, Mapping[str, Any]] = {}
        self.errors: list[dict[str, str]] = []

    # ------------------------------------------------------------------
    # kv 键
    # ------------------------------------------------------------------

    @staticmethod
    def _state_key(plugin_id: str) -> str:
        return f"plugin:state:{plugin_id}"

    @staticmethod
    def _config_key(plugin_id: str) -> str:
        return f"plugin:config:{plugin_id}"

    # ------------------------------------------------------------------
    # 清单加载
    # ------------------------------------------------------------------

    def load_dir(self, path: str | Path) -> list[str]:
        """读目录下全部 ``*.json``（按文件名排序），逐个解析为 PluginManifest。

        单个文件失败（非法 JSON / 清单校验失败 / api_version 不兼容）记入
        ``self.errors`` 不抛出；目录不存在返回空列表并记一条 error。
        重复加载同 id 覆盖 loaded 记录；该插件当前 enabled 则拒绝覆盖并记 error。
        返回成功加载的 plugin_id 列表。
        """
        directory = Path(path)
        if not directory.is_dir():
            self.errors.append(
                {"path": str(directory), "error": "manifest directory not found"}
            )
            return []
        loaded: list[str] = []
        for file in sorted(directory.glob("*.json")):
            try:
                raw = json.loads(file.read_text(encoding="utf-8"))
                if not isinstance(raw, dict):
                    raise ValueError("manifest JSON must be an object")
                schema = raw.get("config_schema")
                if schema is not None and not isinstance(schema, Mapping):
                    raise ValueError("config_schema must be an object")
                kwargs = {k: raw[k] for k in _MANIFEST_KEYS if k in raw}
                for seq in ("inputs", "actions", "provides"):
                    if seq in kwargs:
                        kwargs[seq] = tuple(kwargs[seq])
                manifest = PluginManifest(**kwargs)
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                self.errors.append(
                    {"path": str(file), "error": f"{type(exc).__name__}: {exc}"}
                )
                continue
            if self._load(manifest, schema, source=str(file)):
                loaded.append(manifest.plugin_id)
        return loaded

    def load_manifest(
        self, manifest: PluginManifest, *, config_schema: Mapping[str, Any] | None = None
    ) -> None:
        """同 load_dir 的单条加载（供代码侧直接用）；失败记 self.errors 不抛出。"""
        self._load(manifest, config_schema, source=f"manifest:{manifest.plugin_id}")

    def _load(
        self, manifest: PluginManifest, schema: Mapping[str, Any] | None, *, source: str
    ) -> bool:
        pid = manifest.plugin_id
        major = manifest.api_version.split(".", 1)[0]
        if major != PLUGIN_API_VERSION:
            self.errors.append(
                {
                    "path": source,
                    "error": (
                        f"plugin {pid!r} api_version {manifest.api_version!r} "
                        f"incompatible with core plugin API {PLUGIN_API_VERSION!r}"
                    ),
                }
            )
            return False
        existing = self._states.get(pid)
        if existing is not None and existing.status == "enabled":
            self.errors.append(
                {
                    "path": source,
                    "error": f"plugin {pid!r} is enabled: refusing to overwrite manifest",
                }
            )
            return False
        if schema is not None:
            self._schemas[pid] = schema
        else:
            self._schemas.pop(pid, None)
        state = PluginState(plugin_id=pid, status="loaded", manifest=manifest)
        self._states[pid] = state
        # 回放持久化启停状态：enabled 的插件重新注册，disabled 的保持禁用
        persisted = self._store.get(self._state_key(pid))
        if isinstance(persisted, Mapping):
            if persisted.get("enabled"):
                self._activate(pid)
            else:
                state.status = "disabled"
        return True

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def _build_wiring(self, plugin_id: str) -> dict[str, Any]:
        """调宿主工厂取接线参数；无工厂返回空（registry 落 NullPluginExecutor）。"""
        factory = self._factories.get(plugin_id)
        if factory is None:
            return {}
        wiring = factory()
        if wiring is None:
            return {}
        if not isinstance(wiring, Mapping):
            raise TypeError(f"factory for plugin {plugin_id!r} must return a mapping")
        unknown = set(wiring) - set(_WIRING_KEYS)
        if unknown:
            raise ValueError(
                f"factory for plugin {plugin_id!r} returned unknown wiring keys: "
                f"{sorted(unknown)}"
            )
        return dict(wiring)

    def _activate(self, plugin_id: str) -> bool:
        """接线 + 注册 + 持久化 enabled；失败置 error 状态（只记异常类型名）。"""
        state = self._states[plugin_id]
        try:
            wiring = self._build_wiring(plugin_id)
            self._registry.register(state.manifest, **wiring)
        except Exception as exc:
            state.status = "error"
            state.error = type(exc).__name__
            return False
        state.status = "enabled"
        state.error = ""
        self._store.put(
            self._state_key(plugin_id), {"enabled": True, "at": _utc_now_iso()}
        )
        return True

    def enable(self, plugin_id: str) -> None:
        """启用已加载插件（loaded/disabled/error → enabled）。

        工厂或注册抛异常 → 状态 error、记录异常类型名、不注册、不抛给调用方。
        已 enabled 幂等返回。
        """
        state = self._states.get(plugin_id)
        if state is None:
            raise KeyError(f"unknown plugin: {plugin_id!r}")
        if state.status == "enabled":
            return
        self._activate(plugin_id)

    def disable(self, plugin_id: str) -> None:
        """注销 + 状态 disabled + kv 持久化。幂等（未加载时静默返回）。"""
        state = self._states.get(plugin_id)
        if state is None:
            return
        self._registry.unregister(plugin_id)
        state.status = "disabled"
        state.error = ""
        self._store.put(
            self._state_key(plugin_id), {"enabled": False, "at": _utc_now_iso()}
        )

    # ------------------------------------------------------------------
    # 状态与健康检查
    # ------------------------------------------------------------------

    def status(self) -> list[dict[str, Any]]:
        """每个已加载插件一行的状态视图。"""
        out: list[dict[str, Any]] = []
        for pid, state in self._states.items():
            m = state.manifest
            out.append(
                {
                    "plugin_id": pid,
                    "name": m.name,
                    "version": m.version,
                    "kind": m.resolved_kind.value,
                    "status": state.status,
                    "enabled": state.status == "enabled",
                    "api_version": m.api_version,
                    "provides": list(m.provides),
                    "actions": list(m.actions),
                    "inputs": list(m.inputs),
                }
            )
        return out

    def healthcheck(self, plugin_id: str) -> dict[str, str]:
        """{"plugin_id","status","detail"}。

        enabled 插件：executor 有 ``healthcheck()`` 则调用——返回 True →
        "healthy"；返回 False 或 str（视为原因描述）→ "unhealthy"；抛异常 →
        "unhealthy" + 异常类型名。无 healthcheck 方法 → "unknown"。
        未启用 → "disabled"；未加载 → "missing"。
        """
        state = self._states.get(plugin_id)
        if state is None:
            return {
                "plugin_id": plugin_id,
                "status": "missing",
                "detail": "plugin not loaded",
            }
        if state.status != "enabled":
            return {
                "plugin_id": plugin_id,
                "status": "disabled",
                "detail": f"plugin status: {state.status}",
            }
        executor = self._registry.executor(plugin_id)
        check = getattr(executor, "healthcheck", None)
        if not callable(check):
            return {
                "plugin_id": plugin_id,
                "status": "unknown",
                "detail": "executor exposes no healthcheck()",
            }
        try:
            result = check()
        except Exception as exc:
            return {
                "plugin_id": plugin_id,
                "status": "unhealthy",
                "detail": type(exc).__name__,
            }
        if isinstance(result, str):
            return {"plugin_id": plugin_id, "status": "unhealthy", "detail": result}
        if result:
            return {"plugin_id": plugin_id, "status": "healthy", "detail": "ok"}
        return {
            "plugin_id": plugin_id,
            "status": "unhealthy",
            "detail": "healthcheck returned false",
        }

    # ------------------------------------------------------------------
    # 配置管理（schema 校验 + 脱敏视图）
    # ------------------------------------------------------------------

    def _validate_config(
        self, plugin_id: str, schema: Mapping[str, Any], config: Mapping[str, Any]
    ) -> None:
        """JSON Schema 最小子集校验：required 存在性 + properties type 匹配。

        未知字段宽容放行。错误消息只含字段名与期望类型，不含配置值。
        """
        for name in schema.get("required") or ():
            if name not in config:
                raise ValueError(
                    f"plugin {plugin_id!r} config missing required field {name!r}"
                )
        properties = schema.get("properties") or {}
        for name, spec in properties.items():
            if name not in config or not isinstance(spec, Mapping):
                continue
            types = _CONFIG_TYPES.get(spec.get("type"))
            if types is None:
                continue  # 未声明 type 或未知 type：宽容放行
            value = config[name]
            # bool 是 int 子类，number 不得接受布尔
            if not isinstance(value, types) or (
                spec.get("type") == "number" and isinstance(value, bool)
            ):
                raise ValueError(
                    f"plugin {plugin_id!r} config field {name!r} "
                    f"must be of type {spec.get('type')!r}"
                )

    def set_config(self, plugin_id: str, config: Mapping[str, Any]) -> None:
        """按 config_schema（若有）校验后存 kv；失败 ValueError（消息含字段名）。"""
        if plugin_id not in self._states:
            raise KeyError(f"unknown plugin: {plugin_id!r}")
        schema = self._schemas.get(plugin_id)
        if schema is not None:
            self._validate_config(plugin_id, schema, config)
        self._store.put(self._config_key(plugin_id), dict(config))

    def config_view(self, plugin_id: str) -> dict[str, Any]:
        """配置脱敏视图：secret 字段值替换为 "***"（存在）或 ""（未配置）。

        脱敏范围：schema properties 中 ``format: "secret"`` 的字段，以及键名
        含 token/key/secret/password（大小写不敏感）的字段；其余原样。
        """
        if plugin_id not in self._states:
            raise KeyError(f"unknown plugin: {plugin_id!r}")
        stored = self._store.get(self._config_key(plugin_id)) or {}
        view: dict[str, Any] = dict(stored)
        schema = self._schemas.get(plugin_id) or {}
        properties = schema.get("properties") or {}
        schema_secrets = {
            name
            for name, spec in properties.items()
            if isinstance(spec, Mapping) and spec.get("format") == "secret"
        }

        def _mask(name: str) -> None:
            value = view.get(name)
            view[name] = "***" if value not in (None, "") else ""

        for name in schema_secrets:
            _mask(name)  # 未配置的 secret 字段也以 "" 出现在视图中
        for name in list(view):
            if name not in schema_secrets and any(
                hint in name.lower() for hint in _SECRET_HINTS
            ):
                _mask(name)
        return view
