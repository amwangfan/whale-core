"""反应图片/表情素材库（issue #57）。

纪律：

- 素材只来自**主人显式导入**（宿主命令/管理面），不自动抓取网络图片，
  也不把私人聊天图片自动变成共享素材；
- 模型只提出**语义意图**（``[[表情:标签]]``，response_planner 解析为
  ``ReplyPlan.reaction_tag``），实际选图由本模块完成——模型不拼任何
  文件路径或 URL；
- 素材有稳定 ID、标签与角色归属（persona_id 空 = 全局可用）；
- 近期去重 + 频率控制：同会话不连续重复同一张，发送间隔有下限；
  无匹配素材或触达频率上限 → 返回 None，调用方退回纯文字（不阻塞回复）。
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field

KEY_PREFIX = "reaction:media:"
_RECENT_PREFIX = "reaction:recent:"
_LAST_SEND_PREFIX = "reaction:last:"

#: 默认每会话反应图最小间隔（10 分钟）与近期去重窗口（5 张）。
DEFAULT_MIN_INTERVAL_S = 600
DEFAULT_RECENT_WINDOW = 5


@dataclass(frozen=True)
class ReactionMedia:
    media_id: str
    artifact_id: str           # ArtifactStore 引用（私有产物，鉴权读取）
    tags: tuple[str, ...] = ()
    persona_id: str = ""       # 角色归属；空 = 全局
    added_by: str = "owner"
    created_at: float = field(default_factory=time.time)

    def as_dict(self) -> dict:
        return {"media_id": self.media_id, "artifact_id": self.artifact_id,
                "tags": list(self.tags), "persona_id": self.persona_id,
                "added_by": self.added_by, "created_at": self.created_at}

    @classmethod
    def from_dict(cls, data: dict) -> "ReactionMedia":
        return cls(media_id=str(data["media_id"]),
                   artifact_id=str(data["artifact_id"]),
                   tags=tuple(str(t)[:20] for t in data.get("tags") or ()),
                   persona_id=str(data.get("persona_id") or ""),
                   added_by=str(data.get("added_by") or "owner"),
                   created_at=float(data.get("created_at") or 0))


class ReactionMediaStore:
    """素材的登记/检索/去重/频控（kv 持久化；图片字节在 ArtifactStore）。"""

    def __init__(self, store, artifacts=None):
        self.store = store
        self.artifacts = artifacts  # ArtifactStore | None（读取图片字节用）

    # ---- 登记 ------------------------------------------------------------

    def add(self, artifact_id: str, tags, *, persona_id: str = "",
            added_by: str = "owner") -> ReactionMedia:
        artifact_id = str(artifact_id or "").strip()
        if not artifact_id:
            raise ValueError("artifact_id required")
        if self.artifacts is not None:
            self.artifacts.get(artifact_id)  # 不存在 → KeyError（如实失败）
        media = ReactionMedia(
            media_id=uuid.uuid4().hex[:12], artifact_id=artifact_id,
            tags=tuple(dict.fromkeys(str(t).strip()[:20] for t in tags if str(t).strip())),
            persona_id=str(persona_id or "")[:80], added_by=str(added_by or "owner")[:40])
        if not media.tags:
            raise ValueError("至少一个标签")
        self.store.put(f"{KEY_PREFIX}{media.media_id}", media.as_dict())
        return media

    def remove(self, media_id: str) -> bool:
        key = f"{KEY_PREFIX}{media_id}"
        if self.store.get(key) is None:
            return False
        self.store.delete(key)
        return True

    def list(self, *, persona_id: str | None = None) -> tuple[ReactionMedia, ...]:
        """persona_id=None 列全部；否则只列全局 + 该角色归属的素材。"""
        out: list[ReactionMedia] = []
        for key in self.store.keys():
            if not key.startswith(KEY_PREFIX):
                continue
            data = self.store.get(key)
            if not isinstance(data, dict):
                continue
            try:
                media = ReactionMedia.from_dict(data)
            except (KeyError, TypeError, ValueError):
                continue
            if persona_id is not None and media.persona_id not in ("", persona_id):
                continue
            out.append(media)
        out.sort(key=lambda m: m.created_at)
        return tuple(out)

    # ---- 选取（语义标签 + 去重 + 频控） -----------------------------------

    def pick(self, want_tags, *, persona_id: str = "", conversation_id: str,
             min_interval_s: float = DEFAULT_MIN_INTERVAL_S,
             recent_window: int = DEFAULT_RECENT_WINDOW,
             now: float | None = None) -> ReactionMedia | None:
        """按语义标签选一张素材；无匹配/频率受限/近期用过 → None（退回文字）。"""
        now = time.time() if now is None else now
        conv = str(conversation_id or "")
        if not conv:
            return None
        # 频率控制：距上次反应图发送不足间隔 → 不发
        last = self.store.get(f"{_LAST_SEND_PREFIX}{conv}")
        if isinstance(last, (int, float)) and now - float(last) < min_interval_s:
            return None
        want = {str(t).strip() for t in want_tags if str(t).strip()}
        if not want:
            return None
        recent = self._recent(conv)
        best: ReactionMedia | None = None
        best_score = 0
        for media in self.list(persona_id=persona_id):
            score = len(want & set(media.tags))
            if score <= 0 or media.media_id in recent:
                continue
            if score > best_score or (
                    best is not None and score == best_score
                    and media.created_at > best.created_at):
                best, best_score = media, score
        if best is None:
            return None
        # 记账：近期去重窗口 + 频控时间戳
        recent.append(best.media_id)
        self.store.put(f"{_RECENT_PREFIX}{conv}", recent[-recent_window:])
        self.store.put(f"{_LAST_SEND_PREFIX}{conv}", now)
        return best

    def _recent(self, conv: str) -> list[str]:
        raw = self.store.get(f"{_RECENT_PREFIX}{conv}")
        return [str(x) for x in raw] if isinstance(raw, list) else []

    def read_bytes(self, media: ReactionMedia) -> bytes | None:
        """读图片字节；产物缺失/读取失败 → None（调用方退回文字）。"""
        if self.artifacts is None:
            return None
        try:
            blob = self.artifacts.read(media.artifact_id)
            return blob[1] if isinstance(blob, tuple) else blob
        except Exception:
            return None
