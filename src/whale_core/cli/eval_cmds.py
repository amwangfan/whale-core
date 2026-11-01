"""``whale eval`` —— 真实模型评测入口（issue #40；生产可用）。

转发器：把子命令映射到仓内 ``evals.live.*`` 模块（评测代码不进包体，
保持在仓根 evals/）。真实模型调用只发生在这些手动入口；密钥只从
``--env`` 文件或进程环境读取，绝不落盘。

子命令：
- ``run``     → evals.live.live_runner（跑一个版本的 run JSON）
- ``pair``    → evals.live.pairing（两 run 匿名配对 + 打分表模板）
- ``judge``   → evals.live.judge（单裁判，DeepSeek）
- ``judges``  → evals.live.judges（多裁判：kimi/qwen/glm/deepseek 交叉打分）
- ``report``  → evals.live.report（版本对比报告）
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Sequence

_MODULES = {
    "run": "evals.live.live_runner",
    "pair": "evals.live.pairing",
    "judge": "evals.live.judge",
    "judges": "evals.live.judges",
    "report": "evals.live.report",
}


def _repo_root() -> Path:
    """src/whale_core/cli/eval_cmds.py 上四级 = 仓库根。"""
    return Path(__file__).resolve().parents[3]


def main(argv: Sequence[str]) -> int:
    if not argv or argv[0] in ("-h", "--help") or argv[0] not in _MODULES:
        known = "、".join(sorted(_MODULES))
        print(f"用法: whale eval <{known}> [子命令参数…]")
        return 2 if argv and argv[0] not in ("-h", "--help") else 0
    repo = str(_repo_root())
    if repo not in sys.path:
        sys.path.insert(0, repo)
    import importlib

    module = importlib.import_module(_MODULES[argv[0]])
    return int(module.main(list(argv[1:])))
