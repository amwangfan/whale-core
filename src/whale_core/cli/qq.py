"""``whale qq`` —— QQ 渠道实况运行器的正式 CLI 入口（issue #28）。

设计：本模块只做**转发**，不复制业务逻辑。组合根（composition root）仍是
仓内 ``examples/qq_live_workflow.py``（生产 systemd 当前也指向它，后续统一
切换部署）；这里按路径加载该运行器并把 ``whale qq`` 之后的参数原样交给它的
``main(argv)``，子命令（run / approve / show / jobs）与参数语义完全一致。

纪律保持不变：

- 双开关（issue #12）：无人值守真实发送需 ``--live-auto-send`` **且**
  环境变量 ``QQ_LIVE_AUTOSEND=1``，缺一即 fail closed；默认人工审批，
  ``--auto-approve`` 只是 dry-run 演示。判定逻辑全在运行器内，本模块不动。
- 单实例锁：``run`` 子命令转发前对 ``/tmp/whale-qq-runner.lock`` 取
  ``fcntl.flock`` 非阻塞排他锁；锁冲突打印明确错误并返回退出码 **3**。
  锁 fd 持有至进程退出（进程终止即释放）。``approve``/``show``/``jobs``
  是短命查询进程，不加锁；``run --help`` 等纯求助路径也不取锁。
  运行器本体（examples 直跑）暂无锁——锁是 CLI 入口的增量保护。
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from typing import Sequence

# 单实例锁路径（测试可 monkeypatch 到 tmp_path）。
LOCK_PATH = Path("/tmp/whale-qq-runner.lock")

LOCK_CONFLICT_EXIT = 3

# 锁 fd 存活引用：进程退出前不关闭（flock 随 fd 关闭/进程终止释放）。
_LOCK_FD: int | None = None


def _runner_path() -> Path:
    """运行器文件路径：仓库根 = src/whale_core/cli/qq.py 上四级。"""
    return Path(__file__).resolve().parents[3] / "examples" / "qq_live_workflow.py"


def _load_runner():
    """按路径加载 examples 运行器模块（import 不触发其 main）。"""
    path = _runner_path()
    if not path.is_file():
        return None
    spec = importlib.util.spec_from_file_location("whale_qq_live_runner", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _try_acquire_lock(lock_path: Path) -> int | None:
    """非阻塞排他 flock；成功返回打开的 fd（调用方须保持引用），冲突返回 None。"""
    import fcntl

    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


def _help_requested(argv: Sequence[str]) -> bool:
    return any(arg in ("-h", "--help") for arg in argv)


def main(argv: Sequence[str] | None = None) -> int:
    """``whale qq ...`` 入口：转发给运行器 main；返回退出码（不自行 sys.exit）。"""
    global _LOCK_FD
    args = list(sys.argv[1:] if argv is None else argv)

    # 单实例锁只护 run（常驻监听进程）；求助路径不取锁，保证 --help 永远可用。
    if args[:1] == ["run"] and not _help_requested(args[1:]):
        try:
            import fcntl  # noqa: F401
        except ImportError:
            print("[qq] 提示：本平台无 fcntl，单实例锁不可用（继续启动）",
                  file=sys.stderr)
        else:
            fd = _try_acquire_lock(LOCK_PATH)
            if fd is None:
                print(
                    f"错误：已有 whale qq 运行器实例持有锁 {LOCK_PATH}；"
                    "请先停止旧实例（或确认无残留进程）后再启动。",
                    file=sys.stderr,
                )
                return LOCK_CONFLICT_EXIT
            _LOCK_FD = fd

    runner = _load_runner()
    if runner is None:
        print(
            f"错误：未找到 QQ 运行器 {_runner_path()}（whale qq 目前随源码仓"
            "examples/ 分发；请在仓库内使用或安装包含运行器的版本）。",
            file=sys.stderr,
        )
        return 2
    return int(runner.main(args, prog="whale qq"))


if __name__ == "__main__":
    raise SystemExit(main())
