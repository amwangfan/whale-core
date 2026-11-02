"""可选的进程隔离工具执行（issue #50）。

线程内同步插件无法被安全杀死：一旦永久阻塞，工作线程永不退出。对确实
无法协作取消的工具，``ToolSpec(isolation="process")`` 把 handler 放进
**独立子进程**执行——超时/取消即终止整个进程组（POSIX 下子进程先
``setsid``，父进程 killpg 连同其子进程一起结束）。

约束（诚实边界）：

- 只支持 ``handler`` 为**可 pickle 的可调用**（模块级函数 / functools.partial
  等）的 read_only 工具；经插件网关的 executor 对象活在进程内，不能进
  子进程——那类插件必须自带内部超时（官方 provider 均有），槽位核算与
  orphan 上限在 ``tool_execution`` 兜底。
- 结果经有界队列回传并截断；子进程异常/崩溃如实报 fail，不粉饰。
- 外部副作用已发生但结果未回传时返回 ``None``（outcome unknown）——
  调用方按既有语义记 unknown，**不自动重放**。
"""
from __future__ import annotations

import os
import pickle
import queue as queue_mod
import signal
from typing import Any, Callable, Mapping


class IsolationError(RuntimeError):
    """隔离执行不可用（如 handler 不可序列化）——配置错误，诚实抛出。"""


def _child_entry(handler_bytes: bytes, args: Mapping[str, Any], q) -> None:
    """子进程入口：先自立进程组（POSIX），再执行并回传 (ok, text)。"""
    if os.name == "posix":
        try:
            os.setsid()
        except OSError:
            pass
    try:
        handler = pickle.loads(handler_bytes)
        out = handler(args)
        # 子进程侧先截断，避免巨型输出撑爆进程间队列
        q.put((True, str(out)[:65536]))
    except Exception as exc:  # 子进程内任何失败都如实回传为 fail
        try:
            q.put((False, f"isolated-error:{type(exc).__name__}"))
        except Exception:
            pass


def _kill_tree(proc) -> None:
    """终止子进程及其进程组（尽力而为；已退出则忽略）。"""
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except (ProcessLookupError, PermissionError, OSError):
        pass
    try:
        proc.join(timeout=2)
    except Exception:
        pass


def run_callable_isolated(
    handler: Callable[[Mapping[str, Any]], Any],
    args: Mapping[str, Any],
    *,
    timeout_s: float,
    output_chars: int,
    proc_registry: dict | None = None,
    registry_key: str = "",
    cancel_check: Callable[[], bool] | None = None,
    poll_s: float = 0.05,
) -> tuple[bool, str] | None:
    """在独立子进程执行 handler(args)，超时/取消即杀进程组。

    返回 ``(ok, text)``；超时或取消导致结果不可知时返回 ``None``。
    ``proc_registry`` 可选：登记活进程供服务关闭时统一回收（issue #50）。
    """
    try:
        handler_bytes = pickle.dumps(handler)
    except Exception as exc:
        raise IsolationError(
            f"isolated handler must be picklable (module-level function): {exc}") from exc
    import multiprocessing as mp

    ctx = mp.get_context("spawn")
    q = ctx.Queue()
    proc = ctx.Process(target=_child_entry, args=(handler_bytes, dict(args), q),
                       daemon=True)
    proc.start()
    if proc_registry is not None and registry_key:
        proc_registry[registry_key] = proc
    import time
    deadline = time.monotonic() + timeout_s
    try:
        while proc.is_alive():
            if time.monotonic() >= deadline or (
                    cancel_check is not None and cancel_check()):
                _kill_tree(proc)
                return None  # 结果不可知：外部副作用可能已发生，不重放
            proc.join(timeout=poll_s)
        proc.join(timeout=1)
        if proc.exitcode is not None and proc.exitcode < 0:
            # 被信号杀死（关闭回收/外部 OOM 等）：结果不可知，按 unknown 处理
            return None
        try:
            ok, text = q.get(timeout=1)
        except (queue_mod.Empty, EOFError, OSError):
            # 进程正常退出但无结果回传（崩溃于 put 之前）
            return False, "isolated-error:no-result"
        return ok, str(text)[:output_chars]
    finally:
        if proc_registry is not None and registry_key:
            proc_registry.pop(registry_key, None)
        try:
            q.close()
        except Exception:
            pass
