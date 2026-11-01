#!/usr/bin/env python3
"""跨仓契约漂移校验（issue #46）。

逐文件比较两个 checkout 的 ``contracts/v1/`` 约定范围：

- ``PROTOCOL.md``
- ``messages/*.schema.json``
- ``examples/*.json``

canonical source 是 **whale-mobile**（``contracts/v1/README.md`` 与
``docs/devices.md`` 已声明）；本脚本只做双向逐文件对比，不区分方向——
任一侧多了文件、缺了文件或内容不同都算漂移，输出逐文件差异并以退出码 1
失败。README.md 等仓内元文档不在校验范围内。

用法::

    python3 scripts/contract_sync_check.py --core /path/whale-core \
        --mobile /path/whale-mobile
    python3 scripts/contract_sync_check.py --a /path/a --b /path/b  # 通用

CI 里先按 ref checkout 两个仓到本地路径再调用本脚本；合并时序造成的
假失败由流程约束（先更新 canonical，再同步 mirror，最后以同一批次 refs
联合验收），脚本本身不做任何豁免。
"""

from __future__ import annotations

import argparse
import filecmp
import sys
from pathlib import Path

SCOPE_FILES = ("PROTOCOL.md",)
SCOPE_GLOBS = ("messages/*.schema.json", "examples/*.json")


def collect(root: Path) -> "dict[str, Path]":
    base = root / "contracts" / "v1"
    found: dict[str, Path] = {}
    for name in SCOPE_FILES:
        p = base / name
        if p.is_file():
            found[name] = p
    for pattern in SCOPE_GLOBS:
        for p in sorted(base.glob(pattern)):
            found[str(p.relative_to(base))] = p
    return found


def compare(a_root: Path, b_root: Path) -> "list[str]":
    a_files = collect(a_root)
    b_files = collect(b_root)
    problems: list[str] = []
    for rel in sorted(set(a_files) | set(b_files)):
        in_a, in_b = rel in a_files, rel in b_files
        if in_a and not in_b:
            problems.append(f"ONLY-IN-A  {rel}")
        elif in_b and not in_a:
            problems.append(f"ONLY-IN-B  {rel}")
        elif not filecmp.cmp(a_files[rel], b_files[rel], shallow=False):
            problems.append(f"DIFFERS    {rel}")
    return problems


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--core", help="whale-core checkout 路径")
    parser.add_argument("--mobile", help="whale-mobile checkout 路径")
    parser.add_argument("--a", help="通用：A 侧 checkout 路径")
    parser.add_argument("--b", help="通用：B 侧 checkout 路径")
    args = parser.parse_args(argv)

    a = args.a or args.core
    b = args.b or args.mobile
    if not a or not b:
        parser.error("需要 --core/--mobile 或 --a/--b")
    a_root, b_root = Path(a), Path(b)
    for label, root in (("A", a_root), ("B", b_root)):
        if not (root / "contracts" / "v1").is_dir():
            print(f"error: {label} 侧 {root} 缺少 contracts/v1/", file=sys.stderr)
            return 2

    problems = compare(a_root, b_root)
    if problems:
        print(f"contract drift detected ({len(problems)} 处)：")
        for line in problems:
            print(f"  {line}")
        print(
            "\n处置：先在 canonical（whale-mobile）落定变更，再整体同步到 "
            "whale-core 镜像；不要两侧分别改。"
        )
        return 1
    total = len(collect(a_root))
    print(f"ok: contracts/v1 范围内 {total} 个文件两侧一致")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
