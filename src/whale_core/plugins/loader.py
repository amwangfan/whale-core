"""插件目录加载器：把仓内 plugins/<name>/ 拆分包显式接进注册表。

分层：插件框架契约（framework，issue #31）。

**官方插件发行策略（单一明确）**：仓内 ``plugins/`` 下的官方拆分包
（search_web / weq_channel 等）随 wheel/frozen/Docker 一起打包发行，
宿主直接按包名导入；本加载器服务的是**外部第三方目录插件**（显式路径
加载，plugin.json 校验 + api_version 兼容检查）——不做自动发现。

加载约定（延续「不做自动发现」纪律）：

- 宿主**显式**给出插件目录路径——不扫描、不猜；加载 ≠ 启用；
- 每个拆分包一个 ``plugin.json``（清单，对齐 PluginManifest 字段）+
  一个入口模块（默认 ``main.py``，可用 entrypoint 覆盖）；
- 入口模块实现 ``register(registry, tools=None, env=None, store=None)``——
  只注册能力，不自带凭据；
- 模块经 importlib 按文件路径加载（不走包安装）；加载失败抛 PluginLoadError
  （含路径与原因），不静默。

对应路线图 §4.5：官方插件同仓但独立目录；拆分包只依赖核心契约
（manifest/registry/runtime），核心不反向依赖拆分包。
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any, Mapping

from whale_core.plugins.manifest import PluginManifest
from whale_core.plugins.registry import PluginRegistry

PLUGIN_API_VERSION = "1"


class PluginLoadError(RuntimeError):
    """插件目录加载失败（消息含路径与原因，不含敏感值）。"""


def read_plugin_manifest(path: str | Path) -> PluginManifest:
    """读插件目录的 plugin.json 并构造 PluginManifest（校验沿用 manifest 层）。"""
    root = Path(path)
    manifest_file = root / "plugin.json"
    if not manifest_file.is_file():
        raise PluginLoadError(f"missing plugin.json in {root}")
    try:
        data = json.loads(manifest_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PluginLoadError(f"invalid plugin.json in {root}: {type(exc).__name__}") from exc
    if not isinstance(data, dict):
        raise PluginLoadError(f"plugin.json must be an object in {root}")
    api_version = str(data.get("api_version") or "1")
    if api_version.split(".", 1)[0] != PLUGIN_API_VERSION:
        raise PluginLoadError(
            f"plugin api_version {api_version!r} incompatible with core {PLUGIN_API_VERSION!r}")
    try:
        return PluginManifest(
            plugin_id=str(data["plugin_id"]),
            name=str(data.get("name") or data["plugin_id"]),
            version=str(data.get("version") or "0.0.0"),
            author=str(data.get("author") or ""),
            description=str(data.get("description") or ""),
            homepage=str(data.get("homepage") or ""),
            kind=str(data.get("kind") or ""),
            inputs=tuple(data.get("inputs") or ()),
            actions=tuple(data.get("actions") or ()),
            action_risk=dict(data.get("action_risk") or {}),
            provides=tuple(data.get("provides") or ()),
            api_version=api_version,
            trust=str(data.get("trust") or "bundled"),
        )
    except (ValueError, KeyError) as exc:
        raise PluginLoadError(f"invalid manifest in {root}: {exc}") from exc


def load_plugin_module(path: str | Path, *, entrypoint: str = "main.py") -> Any:
    """加载插件入口模块。目录含 ``__init__.py`` 时按包加载（支持包内相对导入，
    不污染 sys.path）；否则按单文件入口加载。"""
    import sys
    root = Path(path)
    pkg_init = root / "__init__.py"
    if pkg_init.is_file():
        module_name = f"whale_plugin_{root.name.replace('-', '_')}"
        spec = importlib.util.spec_from_file_location(
            module_name, pkg_init, submodule_search_locations=[str(root)])
        if spec is None or spec.loader is None:
            raise PluginLoadError(f"cannot load package {root}")
        module = importlib.util.module_from_spec(spec)
        # 包内相对导入要求模块先挂进 sys.modules
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
        except Exception as exc:
            sys.modules.pop(module_name, None)
            raise PluginLoadError(f"package {root} failed to load: {type(exc).__name__}") from exc
        return module
    entry = root / entrypoint
    if not entry.is_file():
        raise PluginLoadError(f"missing entrypoint {entrypoint} in {root}")
    module_name = f"whale_plugin_{root.name.replace('-', '_')}"
    spec = importlib.util.spec_from_file_location(module_name, entry)
    if spec is None or spec.loader is None:
        raise PluginLoadError(f"cannot load {entry}")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        raise PluginLoadError(f"entrypoint {entry} failed to load: {type(exc).__name__}") from exc
    return module


def load_plugin_dir(
    registry: PluginRegistry,
    path: str | Path,
    *,
    tools=None,
    env: Mapping[str, str] | None = None,
    store=None,
    **register_kwargs,
) -> PluginManifest:
    """显式加载一个拆分包：校验 plugin.json → 加载入口模块 → 调 register()。

    额外 ``**register_kwargs`` 原样透传给插件的 register()（渠道类插件需要
    宿主注入依赖，如 WeQ 的 port/outbox/prefs）。返回注册成功的 manifest。
    模块没有 register 函数 → PluginLoadError。
    """
    root = Path(path)
    manifest = read_plugin_manifest(root)
    data = json.loads((root / "plugin.json").read_text(encoding="utf-8"))
    entrypoint = str(data.get("entrypoint") or "main.py")
    module = load_plugin_module(root, entrypoint=entrypoint)
    register = getattr(module, "register", None)
    if not callable(register):
        raise PluginLoadError(f"{entrypoint} has no callable register()")
    kwargs = {"tools": tools, "env": env, "store": store, **register_kwargs}
    try:
        register(registry, **kwargs)
    except TypeError:
        # 兼容更窄的注册签名（如渠道插件只收宿主依赖）
        register(registry, **register_kwargs)
    return manifest
