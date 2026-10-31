"""issue #26 架构守卫：memory 域对 life 域的单向依赖边界。

- ``src/whale_core/memory/*.py`` 的 AST 中不得出现任何指向
  ``whale_core.life`` 的 import（``import`` 与 ``from ... import`` 都算，
  函数体内的延迟导入也算——ast.walk 覆盖全部嵌套作用域）。
- ``whale_core/memory/types.py`` 必须存在且定义 ``MemorySpaceType`` /
  ``MemorySpaceRef``（枚举值逐字冻结，见 issue 验收）。
- 兼容窗口：``life.conversation_context`` 仍 re-export 两个类型，
  旧 import 路径不破坏。
"""

import ast
from pathlib import Path

import pytest

SRC_DIR = Path(__file__).resolve().parent.parent / "src" / "whale_core"
MEMORY_DIR = SRC_DIR / "memory"


def _life_import_hits(path: Path) -> "list[str]":
    """返回该文件内所有指向 whale_core.life 的 import 描述（含函数内）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    hits: "list[str]" = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "whale_core.life" or \
                        alias.name.startswith("whale_core.life."):
                    hits.append(f"{path.name}:{node.lineno} import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            # 相对导入（level>0）解析在包内，不可能指向 whale_core.life。
            if node.level:
                continue
            mod = node.module or ""
            if mod == "whale_core.life" or mod.startswith("whale_core.life."):
                names = ", ".join(a.name for a in node.names)
                hits.append(f"{path.name}:{node.lineno} from {mod} import {names}")
    return hits


def test_memory_package_never_imports_life():
    offenders: "list[str]" = []
    for path in sorted(MEMORY_DIR.glob("*.py")):
        offenders.extend(_life_import_hits(path))
    assert not offenders, (
        "whale_core.memory 不得依赖 whale_core.life（issue #26 单向边界）：\n"
        + "\n".join(offenders)
    )


def test_memory_types_module_defines_space_types():
    path = MEMORY_DIR / "types.py"
    assert path.exists(), (
        "memory/types.py 必须存在——MemorySpaceType/MemorySpaceRef 的新家"
    )
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    defined = {
        node.name
        for node in tree.body
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert {"MemorySpaceType", "MemorySpaceRef"} <= defined, (
        f"memory/types.py 必须定义 MemorySpaceType 与 MemorySpaceRef，实际：{defined}"
    )


def test_conversation_context_still_reexports_space_types():
    """旧 import 路径（life.conversation_context）在兼容窗口内不破坏。"""
    from whale_core.life.conversation_context import (
        MemorySpaceRef,
        MemorySpaceType,
    )
    from whale_core.memory.types import (
        MemorySpaceRef as CanonicalRef,
        MemorySpaceType as CanonicalType,
    )

    assert MemorySpaceType is CanonicalType
    assert MemorySpaceRef is CanonicalRef
    # re-export 的名字照常可用：构造一个引用并读取字段。
    ref = MemorySpaceRef("sp-x", MemorySpaceType.GROUP_SPACE, 1)
    assert ref.type is MemorySpaceType.GROUP_SPACE and ref.can_write is False


def test_space_type_enum_values_frozen():
    """枚举值/序列化值逐字冻结（issue 验收：不改枚举值与持久化口径）。"""
    from whale_core.memory.types import MemorySpaceType

    assert [m.value for m in MemorySpaceType] == [
        "protected_original",
        "owner_working_copy",
        "group_space",
        "shareable_fork",
        "member_private",
    ]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
