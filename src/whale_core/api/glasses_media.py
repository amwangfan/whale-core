"""眼镜 QQ 语音接线（独立模块；不改 api/glasses.py，由宿主集成）。

install_media_capabilities：configure_optional_capabilities 的薄封装，把
TTS 等可选能力并入现有 registry/tools 并保留 artifacts。
deliver_audio_from_receipts：复用 qq_workflow._extract_audio_artifacts 从
":ok:" 成功回执提取 audio artifact、读 bytes，交宿主现有 deliver_result
回调投递——不传 conversation，恒按回调默认 prefer_owner_dm 发 owner 默认
QQ（目标绝不来自用户话语）；delivered 认领集合每 turn 一个（_settled 闭包
捕获），同一 artifact 至多投一次；失败（缺失/拒绝/抛异常）如实 ok=False +
errors，返回 dict 原样进 timing（audioDelivery），不得谎报已发送。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Iterable

__all__ = [
    "MediaCapabilities",
    "install_media_capabilities",
    "extract_audio_artifact_ids",
    "deliver_audio_from_receipts",
]

# 语音产物扩展名（与 plugins.artifacts._TYPES 的 audio 子集对齐）；
# 未知 audio 类型回落 .mp3——默认 TTS（MiniMax）产物即 audio/mpeg。
_AUDIO_EXT = {
    "audio/mpeg": ".mp3",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/ogg": ".ogg",
    "audio/webm": ".webm",
}


@dataclass
class MediaCapabilities:
    """安装结果：并入后的 registry/tools + 工厂原始产物（artifacts 等）。

    - ``registry`` / ``tools``：调用方传入的同一对象（未传则新建）；
    - ``artifacts``：``OptionalCapabilities.artifacts``（ArtifactStore | None），
      语音产物读 bytes 的唯一合法路径；
    - ``video_manager``：``OptionalCapabilities.video_manager``（透传，宿主
      跟踪线程用；未配置视频时为 None）；
    - ``notes``：工厂的能力装配说明（日志/诊断用）；
    - ``optional``：工厂返回的 ``OptionalCapabilities`` 原样（不拆丢字段）。
    """

    registry: object
    tools: object
    artifacts: object | None
    video_manager: object | None
    notes: list[str] = field(default_factory=list)
    optional: object | None = None


def install_media_capabilities(store, *, env, artifact_dir,
                               registry=None, tools=None) -> MediaCapabilities:
    """把现有可选能力（TTS/图像/视频…按 env 配置）装进 registry/tools。

    ``plugins.capability_setup.configure_optional_capabilities`` 的薄封装：
    不改工厂行为、不新增 provider。``registry``/``tools`` 传 None 时新建，
    传入现有对象时并入（glasses 编排器同一套 registry/tools 即可见媒体工具）。

    ``artifact_dir`` 必传：ArtifactStore 的落盘目录。工厂在 ``artifact_dir``
    为 None 时不建 ArtifactStore，TTS 工具会诚实抛
    ``artifact storage required for speech output``——本封装不做静默降级。
    """
    if registry is None:
        from whale_core.plugins.registry import PluginRegistry
        registry = PluginRegistry()
    if tools is None:
        from whale_core.core.runtime import ToolRegistry
        tools = ToolRegistry()
    from whale_core.plugins.capability_setup import configure_optional_capabilities
    optional = configure_optional_capabilities(
        registry, tools, store, env=env, artifact_dir=artifact_dir)
    return MediaCapabilities(
        registry=registry, tools=tools,
        artifacts=optional.artifacts,
        video_manager=optional.video_manager,
        notes=list(optional.notes),
        optional=optional)


def extract_audio_artifact_ids(receipts: Iterable) -> tuple[str, ...]:
    """从**成功**工具回执提取 audio artifact id（顺序去重）。

    提取本体复用 ``life.qq_workflow._extract_audio_artifacts``（QQ 侧同一
    事实源：JSON 回执含 ``artifact_id`` 且 ``content_type=audio/*``）；
    成功判定只用既有回执结构——须含 ``":ok:"``（与 ``_extract_image_artifacts``
    同款判据）；无法确认成功的回执不投递，不另造防伪规则。
    """
    from whale_core.life.qq_workflow import _extract_audio_artifacts
    ok_receipts = [str(r) for r in (receipts or ()) if ":ok:" in str(r)]
    seen: dict[str, None] = {}
    for aid in _extract_audio_artifacts(ok_receipts):
        seen.setdefault(str(aid), None)
    return tuple(seen)


def deliver_audio_from_receipts(receipts, *, artifacts,
                                deliver_result: Callable[..., dict],
                                delivered: set | None = None,
                                summary: str = "") -> dict:
    """从成功回执提取 audio artifact → 读 bytes → 现有 deliver_result 投递。

    参数：

    - ``receipts``：编排器一轮的 receipts（同步 result.receipts 或异步
      on_settled 的 out.receipts）；
    - ``artifacts``：``MediaCapabilities.artifacts``（ArtifactStore）；
    - ``deliver_result``：宿主现有回调（glasses.build_delegates 的
      ``deliver_result(items, *, conversation="", summary="",
      prefer_owner_dm=True)``）。本函数**只传 summary**——conversation 留空
      走回调默认 owner 私聊，目标恒为 owner 默认 QQ；
    - ``delivered``：**每轮（turn）一个**的认领集合（并行分支由 ``_settled``
      闭包捕获同一集合；不要传全服务共享的无界 set）；None = 仅本次调用
      内去重；
    - ``summary``：透传给回调的摘要。默认空——ResultDelivery 对非空
      summary 会前置一条文本消息，纯语音投递避免噪音；需要文字交代时由
      调用方显式给。

    返回 ``{"ok", "delivered", "duplicates", "audio_ids", "errors",
    "receipts"}``：``delivered`` 为本次交给回调的语音条数；``duplicates``
    为被认领集合跳过的条数；``receipts`` 为回调返回的原始回执（真实成败
    以它为准）；``errors`` 如实记录每一处失败。调用方须把本 dict 原样并入
    用户可见的响应/时序（``ok=False`` 时回复文本不得声称已发送）。无音频
    回执时是 no-op（``ok=True, delivered=0``），不调用回调。
    """
    audio_ids = list(extract_audio_artifact_ids(receipts))
    out: dict = {"ok": True, "delivered": 0, "duplicates": 0,
                 "audio_ids": audio_ids, "errors": [], "receipts": []}
    if not audio_ids:
        return out

    if delivered is None:
        pending = audio_ids  # 一次性调用：无跨调用去重（集合内已去重）
    else:
        # 先认领后投递：同一 turn 内（含 _settled 重入）不重复外发；失败
        # 不重投——离线补传归 ResultDelivery 自管。集合每 turn 一个，规模
        # 以单轮 artifact 数为界，无跨 turn 状态。
        pending = [aid for aid in audio_ids if aid not in delivered]
        delivered.update(pending)
        out["duplicates"] = len(audio_ids) - len(pending)
    if not pending:
        return out

    if artifacts is None:
        out["ok"] = False
        out["errors"].append("artifact-store-missing")
        return out

    items: list[dict] = []
    for aid in pending:
        try:
            artifact, data = artifacts.read(aid)
        except Exception as exc:
            out["ok"] = False
            out["errors"].append(
                f"artifact-read-failed:{aid}:{type(exc).__name__}")
            continue
        content_type = str(getattr(artifact, "content_type", "") or "")
        if not content_type.startswith("audio/"):
            out["ok"] = False
            out["errors"].append(f"artifact-not-audio:{aid}:{content_type}")
            continue
        items.append({
            "kind": "audio",
            "audio_bytes": bytes(data),
            "filename": "voice" + _AUDIO_EXT.get(content_type, ".mp3"),
        })
    if not items:
        return out

    try:
        # 目标纪律：不传 conversation / prefer_owner_dm——用回调默认值
        # （owner 私聊优先）。目标永远不来自用户话语。
        result = deliver_result(items, summary=summary)
    except Exception as exc:
        out["ok"] = False
        out["errors"].append(f"deliver-raised:{type(exc).__name__}")
        return out
    if not isinstance(result, dict):
        out["ok"] = False
        out["errors"].append("deliver-result-not-dict")
        return out
    out["receipts"] = list(result.get("receipts") or [])
    out["delivered"] = len(items)
    if not result.get("ok"):
        out["ok"] = False
        out["errors"].append("deliver-rejected")
    return out
