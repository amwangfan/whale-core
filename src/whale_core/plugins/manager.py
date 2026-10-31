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
import os
import tempfile
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
    "trust",
)
_WIRING_KEYS = ("executor", "input_adapter", "provider")  # 对齐 PluginRegistry.register
_SECRET_HINTS = ("token", "key", "secret", "password", "passwd",
                 "credential", "auth", "session", "cookie", "private")

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
        secret_dir: str | Path | None = None,
    ) -> None:
        self._registry = registry
        self._store = store
        self._factories = dict(factories or {})
        self._states: dict[str, PluginState] = {}
        self._schemas: dict[str, Mapping[str, Any]] = {}
        # Sensitive values are not placed in the regular kv/SQLite config.
        # Without secret_dir they live only in this process.
        self._secret_dir = Path(secret_dir) if secret_dir is not None else None
        self._secret_cache: dict[str, dict[str, Any]] = {}
        self.errors: list[dict[str, str]] = []

    @property
    def registry(self) -> PluginRegistry:
        """只读访问底层注册表（供宿主/API 层查询 provider）。"""
        return self._registry

    def adopt_registered(self, *, default_enabled: bool = True) -> None:
        """Bring explicitly wired official plugins under lifecycle management.

        Wiring is retained for re-enable; persisted disabled state is applied before
        any request can observe the bundle. This does not import or discover code.
        """
        for manifest in list(self._registry.manifests()):
            pid = manifest.plugin_id
            if pid in self._states:
                continue
            registration = self._registry._items[pid]
            wiring = {}
            if registration.provider is not None:
                wiring['provider'] = registration.provider
            if manifest.actions:
                wiring['executor'] = registration.executor
            if registration.input_adapter is not None:
                wiring['input_adapter'] = registration.input_adapter
            self._factories[pid] = lambda wiring=wiring: dict(wiring)
            self._registry.unregister(pid)
            self.load_manifest(manifest)
            if self._store.get(self._state_key(pid)) is None and default_enabled:
                self.enable(pid)

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
        # Upgrade legacy configs which stored plain secret values in SQLite.
        # A missing/unwritable private directory is reported as an error rather
        # than silently leaving tokens in the ordinary kv.
        previous = self._store.get(self._config_key(pid))
        if isinstance(previous, Mapping):
            properties = (schema or {}).get("properties") or {}
            names = {name for name, spec in properties.items()
                     if isinstance(spec, Mapping) and spec.get("format") == "secret"}
            sanitized, _ = self._partition_secrets(previous, names)
            # Do NOT write markers back to the private file on reload.
            if sanitized != dict(previous):
                try:
                    self.set_config(pid, previous)
                except (OSError, ValueError) as exc:
                    self.errors.append({"path": source,
                                        "error": "secret migration failed: " + type(exc).__name__})
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
                    "trust": m.trust,
                    "provides": list(m.provides),
                    "actions": list(m.actions),
                    "inputs": list(m.inputs),
                    "error": state.error,
                    "description": m.description,
                    "config_schema": dict(self._schemas.get(pid) or {}),
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
        executor = self._registry.provider_of(plugin_id) or self._registry.executor(plugin_id)
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

    def _secret_path(self, plugin_id: str) -> Path | None:
        if self._secret_dir is None:
            return None
        return self._secret_dir / (plugin_id + ".json")

    def _load_secrets(self, plugin_id: str) -> dict[str, Any]:
        path = self._secret_path(plugin_id)
        if path is None:
            return dict(self._secret_cache.get(plugin_id, {}))
        try:
            result = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        return result if isinstance(result, dict) else {}

    def _save_secrets(self, plugin_id: str, data: dict[str, Any]) -> None:
        path = self._secret_path(plugin_id)
        if path is None:
            self._secret_cache[plugin_id] = dict(data)
            return
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(path.parent, 0o700)
        fd, name = tempfile.mkstemp(prefix=".whale-secrets-", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                os.chmod(name, 0o600)
                json.dump(data, stream, ensure_ascii=False)
            os.replace(name, path)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    @classmethod
    def _partition_secrets(cls, data: Any, secret_fields: set[str],
                           *, private: bool = False) -> tuple[Any, Any]:
        """Return (nonsecret config, secrets) with the original shape preserved.

        A secret leaf becomes a marker in normal config. Store encrypted secrets
        through a dedicated backend in production; our stdlib fallback is a
        private 0600 file outside SQLite, *not encryption at rest*.
        """
        if isinstance(data, Mapping):
            plain, secrets = {}, {}
            for key, value in data.items():
                key = str(key)
                child_private = private or key in secret_fields or any(
                    hint in key.lower() for hint in _SECRET_HINTS)
                sanitized, sensitive = cls._partition_secrets(
                    value, secret_fields, private=child_private)
                plain[key] = sanitized
                if sensitive is not None:
                    secrets[key] = sensitive
            return plain, secrets or None
        if isinstance(data, list):
            plain, secrets = [], []
            for value in data:
                sanitized, sensitive = cls._partition_secrets(
                    value, secret_fields, private=private)
                plain.append(sanitized)
                secrets.append(sensitive)
            return plain, secrets if any(x is not None for x in secrets) else None
        if private and data not in ("", None):
            return "***", data
        return data, None

    @classmethod
    def _restore_secrets(cls, plain: Any, sensitive: Any) -> Any:
        if sensitive is None:
            return plain
        if isinstance(plain, Mapping) and isinstance(sensitive, Mapping):
            return {k: cls._restore_secrets(v, sensitive.get(k)) for k, v in plain.items()}
        if isinstance(plain, list) and isinstance(sensitive, list):
            return [cls._restore_secrets(v, sensitive[i] if i < len(sensitive) else None)
                    for i, v in enumerate(plain)]
        return sensitive

    def runtime_config(self, plugin_id: str) -> dict[str, Any]:
        """Trusted-host-only view; NEVER return this through the management API."""
        if plugin_id not in self._states:
            raise KeyError(f"unknown plugin: {plugin_id!r}")
        plain = self._store.get(self._config_key(plugin_id)) or {}
        return self._restore_secrets(plain, self._load_secrets(plugin_id))

    def set_config(self, plugin_id: str, config: Mapping[str, Any]) -> None:
        """Validate and split secrets out of ordinary SQLite/kv configuration."""
        if plugin_id not in self._states:
            raise KeyError(f"unknown plugin: {plugin_id!r}")
        schema = self._schemas.get(plugin_id)
        if schema is not None:
            self._validate_config(plugin_id, schema, config)
        properties = (schema or {}).get("properties") or {}
        names = {name for name, spec in properties.items()
                 if isinstance(spec, Mapping) and spec.get("format") == "secret"}
        plain, secrets = self._partition_secrets(dict(config), names)
        self._save_secrets(plugin_id, secrets or {})
        self._store.put(self._config_key(plugin_id), plain)

    @classmethod
    def _apply_patch(cls, current: dict, changes: Mapping[str, Any]) -> None:
        for key, value in changes.items():
            if value is None:
                current.pop(key, None)
            elif value == "***":
                continue  # untouched masked secret
            elif isinstance(value, Mapping) and isinstance(current.get(key), Mapping):
                nested = dict(current[key])
                cls._apply_patch(nested, value)
                current[key] = nested
            elif isinstance(value, list) and isinstance(current.get(key), list):
                # UI may round-trip masked nested list entries unchanged.
                old = current[key]
                current[key] = [
                    old[i] if item == "***" and i < len(old) else item
                    for i, item in enumerate(value)
                ]
            else:
                current[key] = value

    def patch_config(self, plugin_id: str, changes: Mapping[str, Any]) -> None:
        """PATCH semantics: absent keys stay; null deletes; *** preserves secrets."""
        if not isinstance(changes, Mapping):
            raise ValueError("config changes must be an object")
        current = self.runtime_config(plugin_id)
        self._apply_patch(current, changes)
        self.set_config(plugin_id, current)

    def config_view(self, plugin_id: str) -> dict[str, Any]:
        """配置脱敏视图：secret 字段值替换为 "***"（存在）或 ""（未配置）。

        脱敏范围（**尽力而为**，非证明）：schema properties 中 ``format: "secret"``
        的字段，以及键名含 token/key/secret/password/credential/passwd/auth/
        session/cookie/private（大小写不敏感）的字段；**递归**下钻嵌套
        dict/list（安全审查修正：嵌套值此前原样外泄）。
        """
        if plugin_id not in self._states:
            raise KeyError(f"unknown plugin: {plugin_id!r}")
        stored = self._store.get(self._config_key(plugin_id)) or {}
        schema = self._schemas.get(plugin_id) or {}
        properties = schema.get("properties") or {}
        schema_secrets = {
            name
            for name, spec in properties.items()
            if isinstance(spec, Mapping) and spec.get("format") == "secret"
        }
        # schema secret 字段即使未配置也以 "" 出现在视图中
        base: dict[str, Any] = dict(stored)
        for name in schema_secrets:
            base.setdefault(name, "")
        return {k: self._mask_value(k, v, schema_secrets) for k, v in base.items()}

    @classmethod
    def _mask_value(cls, key: str, value: Any, schema_secrets: frozenset | set) -> Any:
        lowered = key.lower()
        if key in schema_secrets or any(hint in lowered for hint in _SECRET_HINTS):
            return "***" if value not in (None, "") else ""
        if isinstance(value, Mapping):
            return {k: cls._mask_value(str(k), v, schema_secrets) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [cls._mask_value(key, item, schema_secrets) if isinstance(item, (Mapping, list, tuple)) else item
                    for item in value]
        return value
