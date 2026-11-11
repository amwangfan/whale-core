"""有界外部调用（BoundedCall）：单 worker + busy 短路。

2026-11-12 reviewer 决策：旧的 ThreadPoolExecutor-with 用法在超时后
``__exit__``（wait=True）仍阻塞等慢调用收尾，且每次调用新建线程池——
语义审批扩大调用面后不可接受。

纪律：
- 永远只有 **1 个** worker 线程；一次调用超时后它仍在后台跑完（线程无法
  安全强杀），期间后续调用**直接返回 default**（busy 短路，不并发堆积）；
- worker 完成后经 done 回调释放 busy（成败都释放，回调内不再持外层锁）；
- 超时/异常一律返回调用方给的 default（fail-soft）；
- 本模块不读任何环境变量/密钥：被调函数由宿主注入。
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Callable


class BoundedCall:
    """``fn`` 的有界包装：``bc(*args, default=..., timeout_s=...)``。"""

    def __init__(self, fn: Callable, *, default_timeout_s: float = 6.0):
        self._fn = fn
        self._timeout = max(0.5, float(default_timeout_s))
        self._pool = ThreadPoolExecutor(max_workers=1,
                                        thread_name_prefix="bounded-call")
        self._lock = threading.Lock()
        self._busy = False

    @property
    def busy(self) -> bool:
        with self._lock:
            return self._busy

    def __call__(self, *args, default="", timeout_s: float = 0.0, **kw):
        with self._lock:
            if self._busy:
                return default  # worker 忙：立即短路，不排队不堆积
            self._busy = True
        try:
            fut = self._pool.submit(self._fn, *args, **kw)
        except Exception:
            with self._lock:
                self._busy = False
            return default

        def _release(_f) -> None:
            with self._lock:
                self._busy = False

        fut.add_done_callback(_release)
        try:
            return fut.result(
                timeout=max(0.5, float(timeout_s or self._timeout)))
        except Exception:
            # 超时或 fn 异常：调用方立即拿 default；busy 由回调在 worker
            # 真正收尾后释放（超时的慢调用期间保持 busy，防并发堆积）。
            return default
