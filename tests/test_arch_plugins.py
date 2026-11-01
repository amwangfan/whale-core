"""issue #31 架构守卫：plugins 三层职责（framework / builtin providers / bundled）。

- ``src/whale_core/plugins/`` 下的**框架契约模块**（manifest / registry /
  loader / manager / gateway / policy / events，外加 intents / processing
  两个契约模块）不得 import 任何**内置 provider 实现 / 支撑模块**，
  也不得 import 根 ``plugins/`` 下的 bundled 插件包（``import`` 与
  ``from ... import`` 都算，函数体内的延迟导入也算——ast.walk 覆盖全部
  嵌套作用域；当前零违规，逐对锁定）。
- ``whale_core.plugins.loader`` 不得 import ``whale_core.life``
  （当前无此依赖，锁定）。
- 根 ``plugins/`` 下 ``search_web`` / ``weq_channel`` 的 ``__init__``
  必须可导入（懒加载设计，import 不起依赖）。
- 覆盖守卫：``src/whale_core/plugins/`` 下每个模块必须在本文件的
  分层清单中归类（与 ``src/whale_core/plugins/README.md`` 一致），
  新增模块不归类则失败。

已知例外（记录在 README，不在本测试断言方向内）：
``provider_http.py``（builtin 支撑设施）import ``search_web.web_fetch``
的 ``_NoRedirectHandler``——方向是「支撑设施 → bundled 插件」，
framework 契约模块无此依赖。
"""

import ast
import importlib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGINS_DIR = REPO_ROOT / "src" / "whale_core" / "plugins"
BUNDLED_PLUGINS_DIR = REPO_ROOT / "plugins"

# 三层分类（与 src/whale_core/plugins/README.md 的表格一致，同步维护）。
FRAMEWORK_MODULES = {
    "manifest",
    "registry",
    "loader",
    "manager",
    "gateway",
    "policy",
    "events",
    "intents",
    "processing",
}
BUILTIN_PROVIDER_MODULES = {
    "media_image",
    "media_video",
    "dashscope_media",
    "minimax_media",
    "speech",
    "pcm_voice",
    "realtime_voice",
    "realtime_bridge",
    "embedding",
    "model_provider",
    "shell_tool",
    "whale_io",
    "bridge",
    "capability_setup",
}
# 内置 provider 共享支撑（传输 / 存储），同样不是框架契约。
BUILTIN_SUPPORT_MODULES = {
    "provider_http",
    "artifacts",
}

# framework 模块禁止 import 的目标：内置实现 + 支撑 + bundled 插件包根。
FRAMEWORK_FORBIDDEN_PLUGINS_SUBMODULES = BUILTIN_PROVIDER_MODULES | BUILTIN_SUPPORT_MODULES
BUNDLED_PLUGIN_PACKAGES = {"search_web", "weq_channel"}


def _imported_names(path: Path) -> "list[tuple[int, str]]":
    """返回文件内全部 import 的 ``(行号, 模块名)``（含函数体内嵌套导入）。

    模块名规整为点分绝对形式；``from whale_core.plugins import X`` 展开为
    ``whale_core.plugins.X``，相对导入 ``from .X import y`` / ``from . import X``
    规整为 ``whale_core.plugins.X``。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: "list[tuple[int, str]]" = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.append((node.lineno, alias.name))
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                # 包内相对导入：本包即 whale_core.plugins。
                base = "whale_core.plugins"
                if node.module:
                    names.append((node.lineno, f"{base}.{node.module}"))
                else:
                    for alias in node.names:
                        names.append((node.lineno, f"{base}.{alias.name}"))
                continue
            mod = node.module or ""
            names.append((node.lineno, mod))
            if mod == "whale_core.plugins":
                # from whale_core.plugins import media_image 形式：展开到子模块。
                for alias in node.names:
                    names.append((node.lineno, f"{mod}.{alias.name}"))
    return names


def _framework_violations(path: Path) -> "list[str]":
    hits: "list[str]" = []
    for lineno, mod in _imported_names(path):
        parts = mod.split(".")
        if parts[:2] == ["whale_core", "plugins"] and len(parts) >= 3:
            submodule = parts[2]
            if submodule in FRAMEWORK_FORBIDDEN_PLUGINS_SUBMODULES:
                hits.append(f"{path.name}:{lineno} import {mod}（内置 provider/支撑模块）")
        if parts[0] in BUNDLED_PLUGIN_PACKAGES:
            hits.append(f"{path.name}:{lineno} import {mod}（bundled 插件包）")
    return hits


def test_every_module_is_classified():
    """目录内新增模块必须在 README/本文件中归类，否则分层表失真。"""
    on_disk = {
        p.stem
        for p in PLUGINS_DIR.glob("*.py")
        if p.name != "__init__.py"
    }
    classified = (
        FRAMEWORK_MODULES | BUILTIN_PROVIDER_MODULES | BUILTIN_SUPPORT_MODULES
    )
    unclassified = on_disk - classified
    phantom = classified - on_disk
    assert not unclassified and not phantom, (
        "plugins 包模块分类与磁盘不一致："
        f"未归类={sorted(unclassified)} 已删除={sorted(phantom)}"
        "——请同步 src/whale_core/plugins/README.md 与本文件清单"
    )


def test_framework_modules_never_import_builtin_providers():
    offenders: "list[str]" = []
    for name in sorted(FRAMEWORK_MODULES):
        offenders.extend(_framework_violations(PLUGINS_DIR / f"{name}.py"))
    assert not offenders, (
        "插件框架契约模块不得 import 内置 provider 实现/支撑/bundled 插件"
        "（issue #31 分层）：\n" + "\n".join(offenders)
    )


def test_loader_never_imports_life():
    """loader 服务外部目录插件，不得反向依赖 life 域（当前无此依赖，锁定）。"""
    hits = [
        f"loader.py:{lineno} import {mod}"
        for lineno, mod in _imported_names(PLUGINS_DIR / "loader.py")
        if mod == "whale_core.life" or mod.startswith("whale_core.life.")
    ]
    assert not hits, (
        "whale_core.plugins.loader 不得 import whale_core.life：\n"
        + "\n".join(hits)
    )


def test_framework_modules_present():
    """issue 点名的七个框架模块必须存在（防误删/误移动后守卫失效）。"""
    for name in ("manifest", "registry", "loader", "manager", "gateway",
                 "policy", "events"):
        assert (PLUGINS_DIR / f"{name}.py").exists(), f"缺少框架模块 {name}.py"


@pytest.mark.parametrize("package", sorted(BUNDLED_PLUGIN_PACKAGES))
def test_bundled_plugin_package_importable(package: str):
    """根 plugins/ 官方插件包的 __init__ 必须可导入（懒加载、无副作用）。"""
    pkg_dir = BUNDLED_PLUGINS_DIR / package
    assert (pkg_dir / "__init__.py").exists(), f"{package}/__init__.py 缺失"
    assert (pkg_dir / "plugin.json").exists(), f"{package}/plugin.json 缺失"
    module = importlib.import_module(package)
    assert callable(getattr(module, "register", None)), (
        f"{package}.__init__ 必须暴露 register()（loader 的显式加载入口）"
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
