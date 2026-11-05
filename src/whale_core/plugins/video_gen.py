"""视频生成网关：提示词 + 模型/时长/尺寸选择的单文件收口。

分层：内置 provider 编排（宿主装配点），非插件框架契约（builtin-provider，issue #31）。

2026-11-05 owner 定版的视频流程（与生图网关 image_gen.py 同形态）：

1. 群消息/私聊 → 意图模型（kimi-k3）判定要视频并产出提示词；
2. kimi 可选指定 ``model``（用户点名或按内容选——选型指南在工具描述里；
   默认 happyhorse-1.1-t2v）；
3. 进入本模块 ``VideoGenGateway.generate``：
   - 模型分派（未知模型 ValueError 并列可用清单，诚实报错）；
   - 视频是异步任务：提交即返回 pending 回执（task_id），轮询/取消/
     状态持久化/产物落存由内嵌 :class:`VideoTaskManager` 负责。

参考图（i2v 首帧）当前 provider 均为纯文生视频，未接入；需要时在
本模块加 reference 采集（与 image_gen 同模式），provider 声明能力即可。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from whale_core.plugins.media_video import (
    DEFAULT_DURATION_S, DEFAULT_SIZE, VideoTask, VideoTaskManager)


@dataclass(frozen=True)
class VideoGenResult:
    """提交回执（异步任务）：task_id + 初始状态 + 实际模型。"""

    task_id: str
    status: str
    model: str
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        """JSON 可序列化视图（qq_workflow 凭 task_id/status 跟踪异步任务）。"""
        return {"task_id": self.task_id, "status": self.status,
                "model": self.model, "note": self.note}


class VideoGenGateway:
    """视频生成统一入口：模型分派 + 任务管理器托管。

    入参语义（对应工具参数）：
    - ``prompt``：视频描述（必填）；
    - ``model``：视频模型名，空 = 默认模型（未知模型 ValueError 并列出可用）；
    - ``duration_s`` / ``size``：时长与画幅（provider 侧各自校验/归一）。
    """

    def __init__(self, providers: Mapping[str, object], *, default_model: str,
                 store: Any, artifact_store: Any = None,
                 min_poll_interval: float = 15.0) -> None:
        if default_model and default_model not in providers:
            raise ValueError(f"default model {default_model!r} not in providers")
        self.providers = dict(providers)
        self.default_model = default_model
        self._manager = VideoTaskManager(
            store, self.providers[default_model], artifact_store=artifact_store,
            min_poll_interval=min_poll_interval,
            providers=self.providers, default_model=default_model)

    @property
    def manager(self) -> VideoTaskManager:
        """任务管理器（轮询/取消/列表/持久化；qq_workflow 的视频跟踪线程用它）。"""
        return self._manager

    @property
    def available_models(self) -> list[str]:
        return sorted(self.providers)

    def generate(self, prompt: str, *, model: str = "",
                 duration_s: int = DEFAULT_DURATION_S,
                 size: str = DEFAULT_SIZE) -> VideoGenResult:
        model = (model or "").strip() or self.default_model
        if model not in self.providers:
            raise ValueError(
                f"未知视频模型 {model!r}（可用：{', '.join(self.available_models)}）")
        task: VideoTask = self._manager.submit(
            prompt, duration_s=int(duration_s or DEFAULT_DURATION_S),
            size=size or DEFAULT_SIZE, model=model)
        return VideoGenResult(task_id=task.task_id, status=task.status, model=model)
