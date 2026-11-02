"""``media.video.generate`` 异步任务契约 + 任务生命周期管理器（PR-16，roadmap §8 P2-1）。

分层：内置 provider 实现，非插件框架契约（builtin-provider，issue #31）。

视频生成是异步任务形态：``submit`` 拿 task_id，``poll`` 查进度，终态拿产物 URL。
真实异步视频服务（各家 API 形状差异大）经 :class:`VideoProvider` 协议注入；
内置 DashScope Wan HTTP provider；是否已完成真实账号联调由部署验收记录说明。
生产环境没有 mock provider 或静默回退。

任务状态持久化到 MemoryStore 的 kv 层（``video:task:{task_id}``），进程重启后
新建 manager（同 store 同 provider）即可恢复查询。终态（succeeded/failed/
cancelled）不再轮询 provider；poll 遇网络异常保持上次状态返回，不盲目置 failed。
"""

from __future__ import annotations

import time
import json
import re
import threading
import urllib.request
import urllib.error
import urllib.parse
from dataclasses import dataclass, replace
from typing import Any, Mapping, Protocol

from whale_core.plugins.manifest import PluginManifest
from whale_core.plugins.registry import PluginRegistry
from whale_core.plugins import provider_http

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
    url: str = ""          # 产物 URL（succeeded 时）
    error: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0
    model: str = ""        # 生成所用模型（多模型路由；空 = 默认 provider）

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
            "model": self.model,
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
            model=str(data.get("model") or ""),
        )


class VideoProvider(Protocol):
    """异步视频生成服务契约（真实服务由宿主注入；本层不联调）。

    - ``submit`` 返回 task_id；
    - ``poll`` 返回 ``(status, url, error)``，status 取 pending/running/
      succeeded/failed/cancelled；
    - ``cancel`` 返回是否取消成功。
    实现方抛出的异常由 manager 分类兜底（见 :class:`VideoTaskManager`）。
    """

    def submit(self, prompt: str, *, duration_s: int = DEFAULT_DURATION_S,
               size: str = DEFAULT_SIZE) -> str:
        ...

    def poll(self, task_id: str) -> tuple[str, str, str]:
        ...

    def cancel(self, task_id: str) -> bool:
        ...


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

    def __init__(self, store: Any, provider: VideoProvider, *, artifact_store=None, min_poll_interval: float = 0,
                 providers: Mapping[str, VideoProvider] | None = None,
                 default_model: str = ""):
        self._store = store
        self._provider = provider
        self._lock = threading.RLock()
        self.timeout_seconds = 1800.0
        self.artifact_store = artifact_store
        self.min_poll_interval = max(0, min_poll_interval)
        self._last_poll: dict[str, float] = {}
        # 多模型路由（2026-11-01：允许调用方指定模型）：model 名 → provider。
        # 默认 provider 挂在 default_model 名下；未知模型名提交期即拒（诚实报错）。
        self._providers: dict[str, VideoProvider] = dict(providers or {})
        self._default_model = str(default_model or "")
        if self._default_model and provider is not None:
            self._providers.setdefault(self._default_model, provider)

    @property
    def provider(self) -> VideoProvider:
        return self._provider

    @property
    def available_models(self) -> tuple[str, ...]:
        return tuple(sorted(self._providers)) if self._providers else ()

    def _provider_for(self, model: str) -> VideoProvider:
        if not model or not self._providers:
            return self._provider
        found = self._providers.get(model)
        if found is None:
            raise VideoError("config", f"unknown video model {model!r} "
                                       f"(available: {', '.join(self.available_models)})")
        return found

    # ---- 生命周期 ----

    def submit(self, prompt: str, *, duration_s: int = DEFAULT_DURATION_S,
               size: str = DEFAULT_SIZE, model: str = "") -> VideoTask:
        """校验参数 → provider.submit → 落库为 pending 任务。

        provider 抛出的非 VideoError 异常一律上抛为 ``VideoError(kind="network")``
        （可重试）；provider 自己抛的 VideoError 保留其更具体的 kind 原样上抛。
        """
        prompt = _validate_prompt(prompt)
        duration_s = _clamp_duration(duration_s)
        size = _validate_size(size)
        model = str(model or "").strip()
        provider = self._provider_for(model)
        try:
            task_id = provider.submit(prompt, duration_s=duration_s, size=size)
        except VideoError:
            raise
        except Exception as exc:
            raise VideoError(  # 不回显异常原文（脱敏纪律）
                "network", f"video submit failed: {type(exc).__name__}",
                retryable=False,  # response loss may still have created a paid job
            ) from exc
        task_id = str(task_id or "").strip()
        if not task_id:
            raise VideoError("bad_response", "provider returned an empty task_id")
        now = time.time()
        task = VideoTask(task_id=task_id, status=STATUS_PENDING, prompt=prompt,
                         created_at=now, updated_at=now, model=model)
        self._save(task)
        return task

    def poll(self, task_id: str) -> VideoTask:
        with self._lock:
            return self._poll_unlocked(task_id)

    def _poll_unlocked(self, task_id: str) -> VideoTask:
        """查询任务：终态直接读缓存；非终态问 provider 并持久化最新状态。

        provider 抛异常（网络抖动等）时保持上次状态返回，error 字段记
        ``transient:<异常类型名>``，状态不盲目置 failed。
        """
        task = self._load(task_id)
        if task.status in TERMINAL_STATUSES:
            return task
        if task.created_at and time.time() - task.created_at > self.timeout_seconds:
            task = replace(task, status=STATUS_FAILED, error="timeout:remote_execution_may_continue", updated_at=time.time())
            self._save(task)
            return task
        last = self._last_poll.get(task_id, 0)
        if time.monotonic() - last < self.min_poll_interval:
            return task
        self._last_poll[task_id] = time.monotonic()
        try:
            status, url, error = self._provider_for(task.model).poll(task_id)
        except Exception as exc:
            task = replace(task, error=f"transient:{type(exc).__name__}",
                           updated_at=time.time())
            self._save(task)
            return task
        status = str(status or "").strip() or task.status
        if status not in (STATUS_PENDING, STATUS_RUNNING, *TERMINAL_STATUSES):
            task = replace(task, error="bad_response:invalid_status", updated_at=time.time())
            self._save(task)
            return task
        if status == STATUS_SUCCEEDED and not str(url or "").strip():
            task = replace(task, error="bad_response:missing_artifact", updated_at=time.time())
            self._save(task)
            return task
        if status == STATUS_SUCCEEDED and self.artifact_store is not None:
            try:
                url = self.artifact_store.save_reference(str(url), content_type="video/mp4").url
            except Exception as exc:
                task = replace(task, error="artifact_save_failed:"+type(exc).__name__, updated_at=time.time())
                self._save(task)
                return task
        task = replace(task, status=status, url=str(url or task.url),
                       error=str(error or ""), updated_at=time.time())
        self._save(task)
        return task

    def cancel(self, task_id: str) -> bool:
        with self._lock:
            return self._cancel_unlocked(task_id)

    def _cancel_unlocked(self, task_id: str) -> bool:
        """取消任务：成功则落库为 cancelled 并返回 True；provider 异常返回 False。

        已 cancelled 幂等返回 True；其余终态（succeeded/failed）不可取消返回 False。
        """
        task = self._load(task_id)
        if task.status == STATUS_CANCELLED:
            return True
        if task.status in TERMINAL_STATUSES:
            return False
        try:
            ok = bool(self._provider_for(task.model).cancel(task_id))
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


def register_video_gen(registry: PluginRegistry, *,
                       provider: VideoProvider | None = None) -> VideoProvider:
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
            description="异步视频生成能力；真实服务经 VideoProvider 契约注入",
        ),
        provider=provider,
    )
    return provider


class DashScopeWanVideoProvider:
    """Wan 2.6 compatible text-to-video API, verified against Alibaba's API reference.

    No automatic retries on submit: a lost response may still have created a paid job.
    Cancellation is supported by the vendor only while the job is queued.
    """
    def __init__(self, *, api_key: str, base_url: str = "https://dashscope.aliyuncs.com/api/v1",
                 model: str = "wan2.6-t2v", timeout: float = 30):
        from whale_core.plugins.speech import _is_local_or_private_http
        parts = urllib.parse.urlsplit(base_url)
        if (parts.scheme != "https" and not _is_local_or_private_http(base_url)) or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
            raise ValueError("invalid video endpoint URL")
        self.api_key, self.base_url, self.model = api_key, provider_http.validate_base_url(base_url), model
        self.timeout = timeout

    def _request(self, path: str, body: dict | None = None, *, method: str = "GET") -> dict:
        if not self.api_key:
            raise VideoError("config", "video API key not configured")
        req = urllib.request.Request(self.base_url+path,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Content-Type": "application/json", "X-DashScope-Async": "enable"}, method=method)
        # urllib redirects copy ordinary headers; unredirected headers protect credentials.
        req.add_unredirected_header("Authorization", "Bearer "+self.api_key)
        try:
            with provider_http.open_request(req, timeout=self.timeout) as response:
                raw = response.read(1024*1024+1)
            if len(raw) > 1024*1024:
                raise VideoError("bad_response", "video response exceeds limit")
            data = json.loads(raw)
        except urllib.error.HTTPError as exc:
            kind = "auth" if exc.code in (401,403) else "quota" if exc.code == 429 else "server" if exc.code >= 500 else "bad_response"
            raise VideoError(kind, f"video HTTP {exc.code}", retryable=method=="GET" and exc.code>=500) from None
        except (urllib.error.URLError, OSError) as exc:
            raise VideoError("network", f"video network error:{type(exc).__name__}", retryable=method=="GET") from None
        except (ValueError, UnicodeError):
            raise VideoError("bad_response", "invalid video JSON") from None
        if not isinstance(data, dict) or data.get("code"):
            raise VideoError("bad_response", "video service rejected request")
        return data

    @staticmethod
    def _task_id(task_id: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", task_id):
            raise VideoError("bad_response", "invalid video task id")
        return task_id

    def submit(self, prompt: str, *, duration_s: int = DEFAULT_DURATION_S, size: str = DEFAULT_SIZE) -> str:
        prompt = _validate_prompt(prompt)
        size = _validate_size(size)
        if duration_s not in (5,10,15):
            raise ValueError("Wan video duration must be 5, 10 or 15 seconds")
        payload = {"model": self.model, "input": {"prompt": prompt[:1500]},
                   "parameters": {"size": size.replace("x", "*"), "duration": duration_s}}
        data = self._request("/services/aigc/video-generation/video-synthesis", payload, method="POST")
        output = data.get("output") or {}
        if not isinstance(output, dict):
            raise VideoError("bad_response", "invalid video output")
        return self._task_id(str(output.get("task_id") or ""))

    def poll(self, task_id: str) -> tuple[str,str,str]:
        output = self._request("/tasks/"+self._task_id(task_id)).get("output") or {}
        if not isinstance(output, dict):
            raise VideoError("bad_response", "invalid video output")
        status = {"PENDING": STATUS_PENDING, "RUNNING": STATUS_RUNNING, "SUCCEEDED": STATUS_SUCCEEDED,
                  "FAILED": STATUS_FAILED, "CANCELED": STATUS_CANCELLED}.get(output.get("task_status"))
        if status is None:
            # 手册外形状态（如 UNKNOWN=任务过期/不存在）：诚实标失败，不抛错
            if str(output.get("task_status") or "") == "UNKNOWN":
                return STATUS_FAILED, "", "task unknown or expired on platform"
            raise VideoError("bad_response", "unknown video job state")
        url = str(output.get("video_url") or "")
        if status == STATUS_SUCCEEDED and not url.startswith("https://"):
            raise VideoError("bad_response", "video result missing HTTPS URL")
        error = "provider_failed" if status == STATUS_FAILED else ""
        return status, url, error

    def cancel(self, task_id: str) -> bool:
        self._request("/tasks/"+self._task_id(task_id)+"/cancel", method="POST")
        return True


class DashScopeHappyHorseVideoProvider(DashScopeWanVideoProvider):
    """AgentScope 平台 happyhorse 视频模型（手册 §2）：参数为 resolution/ratio
    （与 Wan 的 size 不同）；duration 3-15 秒；其余 submit/poll/cancel 复用父类。"""

    _RESOLUTIONS = ("720P", "1080P")
    _RATIOS = ("16:9", "9:16", "1:1", "4:3", "3:4", "4:5", "5:4", "9:21", "21:9")

    def submit(self, prompt: str, *, duration_s: int = 5, size: str = "1280x720") -> str:
        prompt = _validate_prompt(prompt)
        if not (3 <= int(duration_s) <= 15):
            raise ValueError("happyhorse video duration must be 3-15 seconds")
        w, _, h = size.partition("x")
        try:
            ratio = f"{int(w)}/{int(h)}" and _ratio_of(int(w), int(h))
        except ValueError:
            ratio = "16:9"
        resolution = "1080P" if (h and int(h) >= 1000) else "720P"
        payload = {"model": self.model, "input": {"prompt": prompt[:1500]},
                   "parameters": {"resolution": resolution, "duration": duration_s,
                                  "ratio": ratio, "watermark": False}}
        data = self._request("/services/aigc/video-generation/video-synthesis", payload, method="POST")
        output = data.get("output") or {}
        if not isinstance(output, dict):
            raise VideoError("bad_response", "invalid video output")
        return self._task_id(str(output.get("task_id") or ""))


def _ratio_of(w: int, h: int) -> str:
    if w == h:
        return "1:1"
    from math import gcd
    g = gcd(w, h)
    return f"{w // g}:{h // g}"
