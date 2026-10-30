"""``media.video.generate`` 异步任务契约 + 任务生命周期管理器（PR-16，roadmap §8 P2-1）。

视频生成是异步任务形态：``submit`` 拿 task_id，``poll`` 查进度，终态拿产物 URL。
真实异步视频服务（各家 API 形状差异大）经 :class:`VideoProvider` 协议注入；
**本层只定义契约与任务生命周期，未与任何真实视频服务联调**。缺省 provider 是
:class:`MockVideoProvider`（确定性替身，无网络、无真实生成，仅供开发/测试）。

任务状态持久化到 MemoryStore 的 kv 层（``video:task:{task_id}``），进程重启后
新建 manager（同 store 同 provider）即可恢复查询。终态（succeeded/failed/
cancelled）不再轮询 provider；poll 遇网络异常保持上次状态返回，不盲目置 failed。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, replace
from typing import Any, Mapping, Protocol

from whale_core.plugins.manifest import PluginManifest
from whale_core.plugins.registry import PluginRegistry

MEDIA_VIDEO_GENERATE = "media.video.generate"

TASK_KEY_PREFIX = "video:task:"

MAX_PROMPT_CHARS = 4000
MIN_DURATION_S = 1
MAX_DURATION_S = 60
DEFAULT_DURATION_S = 5
DEFAULT_SIZE = "1280x720"
VIDEO_SIZES: tuple[str, ...] = ("1280x720", "720x1280", "1024x1024")

STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"
TERMINAL_STATUSES: tuple[str, ...] = (STATUS_SUCCEEDED, STATUS_FAILED, STATUS_CANCELLED)


class VideoError(RuntimeError):
    """视频任务失败（分类化）。kind: config/network/bad_response/quota/not_found。"""

    def __init__(self, kind: str, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.kind = kind
        self.retryable = retryable


@dataclass(frozen=True)
class VideoTask:
    """一条视频生成任务记录（不可变；状态迁移用 ``dataclasses.replace``）。"""

    task_id: str
    status: str  # "pending" | "running" | "succeeded" | "failed" | "cancelled"
    prompt: str
    url: str = ""  # 产物 URL（succeeded 时）
    error: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        """JSON 可序列化视图（供 MemoryStore kv 持久化）。"""
        return {
            "task_id": self.task_id,
            "status": self.status,
            "prompt": self.prompt,
            "url": self.url,
            "error": self.error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "VideoTask":
        """从 kv 读回的 dict 还原；缺字段给默认值（向后兼容旧记录）。"""
        return cls(
            task_id=str(data.get("task_id") or ""),
            status=str(data.get("status") or STATUS_PENDING),
            prompt=str(data.get("prompt") or ""),
            url=str(data.get("url") or ""),
            error=str(data.get("error") or ""),
            created_at=float(data.get("created_at") or 0.0),
            updated_at=float(data.get("updated_at") or 0.0),
        )


class VideoProvider(Protocol):
    """异步视频生成服务契约（真实服务由宿主注入；本层不联调）。

    - ``submit`` 返回 task_id；
    - ``poll`` 返回 ``(status, url, error)``，status 取 pending/running/
      succeeded/failed/cancelled；
    - ``cancel`` 返回是否取消成功。
    实现方抛出的异常由 manager 分类兜底（见 :class:`VideoTaskManager`）。
    """

    def submit(
        self,
        prompt: str,
        *,
        duration_s: int = DEFAULT_DURATION_S,
        size: str = DEFAULT_SIZE,
    ) -> str: ...

    def poll(self, task_id: str) -> tuple[str, str, str]: ...

    def cancel(self, task_id: str) -> bool: ...


def _validate_prompt(prompt: str) -> str:
    """prompt 必填非空；超长截断到 MAX_PROMPT_CHARS。"""
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("prompt must be a non-empty string")
    return prompt[:MAX_PROMPT_CHARS]


def _clamp_duration(duration_s: int) -> int:
    """duration_s 钳制到 [MIN_DURATION_S, MAX_DURATION_S]。"""
    d = int(duration_s)
    return max(MIN_DURATION_S, min(MAX_DURATION_S, d))


def _validate_size(size: str) -> str:
    if size not in VIDEO_SIZES:
        raise ValueError(f"unsupported size {size!r}: expected one of {VIDEO_SIZES}")
    return size


class VideoTaskManager:
    """视频任务生命周期管理：提交 / 轮询 / 取消 / 列表。

    - 状态持久化到 store kv（``video:task:{task_id}``），进程重启后新建
      manager（同 store 同 provider）可恢复查询；
    - 终态（succeeded/failed/cancelled）不再轮询 provider，直接读缓存；
    - poll 遇 provider 异常保持上次状态返回（不盲目置 failed），error 字段
      记 transient 错误类型名；
    - 未知 task_id 的 poll/cancel 抛 ``VideoError(kind="not_found")``。
    """

    def __init__(self, store: Any, provider: VideoProvider):
        self._store = store
        self._provider = provider

    @property
    def provider(self) -> VideoProvider:
        return self._provider

    # ---- 生命周期 ----

    def submit(
        self,
        prompt: str,
        *,
        duration_s: int = DEFAULT_DURATION_S,
        size: str = DEFAULT_SIZE,
    ) -> VideoTask:
        """校验参数 → provider.submit → 落库为 pending 任务。

        provider 抛出的非 VideoError 异常一律上抛为 ``VideoError(kind="network")``
        （可重试）；provider 自己抛的 VideoError 保留其更具体的 kind 原样上抛。
        """
        prompt = _validate_prompt(prompt)
        duration_s = _clamp_duration(duration_s)
        size = _validate_size(size)
        try:
            task_id = self._provider.submit(prompt, duration_s=duration_s, size=size)
        except VideoError:
            raise
        except Exception as exc:
            raise VideoError(  # 不回显异常原文（脱敏纪律）
                "network",
                f"video submit failed: {type(exc).__name__}",
                retryable=True,
            ) from exc
        task_id = str(task_id or "").strip()
        if not task_id:
            raise VideoError("bad_response", "provider returned an empty task_id")
        now = time.time()
        task = VideoTask(
            task_id=task_id,
            status=STATUS_PENDING,
            prompt=prompt,
            created_at=now,
            updated_at=now,
        )
        self._save(task)
        return task

    def poll(self, task_id: str) -> VideoTask:
        """查询任务：终态直接读缓存；非终态问 provider 并持久化最新状态。

        provider 抛异常（网络抖动等）时保持上次状态返回，error 字段记
        ``transient:<异常类型名>``，状态不盲目置 failed。
        """
        task = self._load(task_id)
        if task.status in TERMINAL_STATUSES:
            return task
        try:
            status, url, error = self._provider.poll(task_id)
        except Exception as exc:
            task = replace(
                task, error=f"transient:{type(exc).__name__}", updated_at=time.time()
            )
            self._save(task)
            return task
        status = str(status or "").strip() or task.status
        task = replace(
            task,
            status=status,
            url=str(url or task.url),
            error=str(error or ""),
            updated_at=time.time(),
        )
        self._save(task)
        return task

    def cancel(self, task_id: str) -> bool:
        """取消任务：成功则落库为 cancelled 并返回 True；provider 异常返回 False。

        已 cancelled 幂等返回 True；其余终态（succeeded/failed）不可取消返回 False。
        """
        task = self._load(task_id)
        if task.status == STATUS_CANCELLED:
            return True
        if task.status in TERMINAL_STATUSES:
            return False
        try:
            ok = bool(self._provider.cancel(task_id))
        except Exception:
            return False
        if ok:
            self._save(replace(task, status=STATUS_CANCELLED, updated_at=time.time()))
        return ok

    def list_tasks(self, *, limit: int = 20) -> list[VideoTask]:
        """列出已知任务，按 created_at 倒序（新的在前），最多 ``limit`` 条。"""
        out: list[VideoTask] = []
        for key in self._store.keys():
            if not str(key).startswith(TASK_KEY_PREFIX):
                continue
            data = self._store.get(key)
            if not isinstance(data, Mapping):
                continue
            try:
                out.append(VideoTask.from_dict(data))
            except (TypeError, ValueError):
                continue
        out.sort(key=lambda t: t.created_at, reverse=True)
        if limit is not None and limit >= 0:
            out = out[:limit]
        return out

    # ---- 持久化 ----

    def _save(self, task: VideoTask) -> None:
        self._store.put(TASK_KEY_PREFIX + task.task_id, task.as_dict())

    def _load(self, task_id: str) -> VideoTask:
        data = self._store.get(TASK_KEY_PREFIX + str(task_id))
        if not isinstance(data, Mapping):
            raise VideoError("not_found", f"unknown video task: {task_id!r}")
        return VideoTask.from_dict(data)


def register_video_gen(
    registry: PluginRegistry, *, provider: VideoProvider | None = None
) -> VideoProvider:
    """注册 ``media.video.generate`` 能力为 model 插件（plugin_id="media_video"）。

    ``provider`` 必须由宿主显式注入——**没有 mock 默认**（mock 模式已按
    owner 决策移除：缺 provider 诚实失败）。未与任何真实视频服务联调。
    返回接线好的 provider 对象。
    """
    if provider is None:
        raise ValueError(
            "register_video_gen requires a real provider; mock defaults were "
            "removed — inject your VideoProvider implementation"
        )
    registry.register(
        PluginManifest(
            plugin_id="media_video",
            name="视频生成（异步任务）",
            version="1.0.0",
            kind="model",
            provides=(MEDIA_VIDEO_GENERATE,),
            description="异步视频生成能力；真实服务经 VideoProvider 契约注入，缺省 mock（未联调真实服务）",
        ),
        provider=provider,
    )
    return provider


class HttpVideoProvider:
    """HTTP bridge adapter (not a claim of compatibility with every vendor).

    POST /jobs {prompt,model,duration_s,size} -> {task_id};
    GET /jobs/{id} -> {status,url,error}; POST /jobs/{id}/cancel -> {cancelled}.
    No retry of a submission with unknown outcome. See service-bridges.md.
    """

    def __init__(self, **kwargs):
        from whale_core.plugins.http_provider import JsonProvider

        self.client = JsonProvider(**kwargs)

    def healthcheck(self):
        return self.client.healthcheck()

    @staticmethod
    def _id(task_id):
        from urllib.parse import quote

        if not isinstance(task_id, str) or not task_id or len(task_id) > 200:
            raise VideoError("bad_response", "invalid video task id")
        return quote(task_id, safe="")

    def submit(self, prompt, *, duration_s=DEFAULT_DURATION_S, size=DEFAULT_SIZE):
        data = self.client.request(
            "/jobs",
            {
                "prompt": _validate_prompt(prompt),
                "duration_s": _clamp_duration(duration_s),
                "size": _validate_size(size),
                "model": self.client.model,
            },
        )
        self._id(data.get("task_id"))
        return data["task_id"]

    def poll(self, task_id):
        data = self.client.request("/jobs/" + self._id(task_id), method="GET")
        if data.get("status") not in (
            STATUS_PENDING,
            STATUS_RUNNING,
            *TERMINAL_STATUSES,
        ):
            raise VideoError("bad_response", "invalid video status")
        return (
            data["status"],
            str(data.get("url") or ""),
            str(data.get("error") or "")[:120],
        )

    def cancel(self, task_id):
        data = self.client.request("/jobs/" + self._id(task_id) + "/cancel", {})
        return data.get("cancelled") is True


class DashScopeVideoProvider:
    """Wan text-to-video asynchronous HTTP API (legacy 2.1--2.6 protocol).

    Primary reference: help.aliyun.com/zh/model-studio/legacy-wan-text-to-video-api-reference
    Region/model/key must match. Cancellation is only supported while pending.
    """

    poll_interval_s = 15

    def __init__(self, **kwargs):
        from whale_core.plugins.http_provider import JsonProvider

        self.client = JsonProvider(**kwargs)

    def healthcheck(self):
        return self.client.healthcheck()

    def submit(self, prompt, *, duration_s=5, size=DEFAULT_SIZE):
        if size not in ("1280x720", "720x1280") or duration_s not in (5, 10, 15):
            raise ValueError("Wan adapter accepts 720p and 5/10/15 second clips")
        data = self.client.request(
            "/services/aigc/video-generation/video-synthesis",
            {
                "model": self.client.model,
                "input": {"prompt": _validate_prompt(prompt)[:1500]},
                "parameters": {
                    "size": size.replace("x", "*"),
                    "duration": duration_s,
                    "prompt_extend": True,
                },
            },
            extra_headers={"X-DashScope-Async": "enable"},
        )
        task_id = (data.get("output") or {}).get("task_id")
        HttpVideoProvider._id(task_id)
        return task_id

    def poll(self, task_id):
        data = self.client.request(
            "/tasks/" + HttpVideoProvider._id(task_id), method="GET"
        )
        output = data.get("output") or {}
        status = {
            "PENDING": "pending",
            "QUEUED": "pending",
            "RUNNING": "running",
            "SUCCEEDED": "succeeded",
            "FAILED": "failed",
            "CANCELED": "cancelled",
        }.get(output.get("task_status"))
        if not status:
            raise VideoError(
                "bad_response", "unknown vendor task state; check external task id"
            )
        url = output.get("video_url", "")
        if not isinstance(url, str) or status == "succeeded" and not url:
            raise VideoError("bad_response", "video output is missing")
        return status, url, "vendor_failed" if status == "failed" else ""

    def cancel(self, task_id):
        from whale_core.plugins.http_provider import ProviderError

        try:
            data = self.client.request(
                "/tasks/" + HttpVideoProvider._id(task_id) + "/cancel", {}
            )
            return bool(data.get("request_id")) and not data.get("code")
        except ProviderError:
            return False  # running tasks cannot be cancelled; preserve unknown
