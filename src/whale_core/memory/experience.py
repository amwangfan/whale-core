"""issue #21：经验提炼——从任务回执与成员反馈沉淀低风险、可溯源的建议权重。

纪律：
- 来源限定：**可信执行回执**（supervisor/网关 receipt）与**明确用户反馈**
  （本人私聊的明确表达）才产生经验；未验证的外部通知/OCR/群聊传言只能
  作为 ``candidate=True`` 候选，不更新稳定权重。
- 每条经验带 experience_id/principal/source/evidence/time/confidence/TTL/
  action/outcome/是否本人确认；与原始对话记录区分存放（kv ``exp:`` 域）。
- 经验只调**建议权重**（如提醒时机、解释深度、打扰频率倾向）；
  永不修改安全策略/授权/身份绑定/Vault ACL。
- 幂等：同 experience_id 覆盖；可查看/纠正/删除；成员退出/删除记忆时
  一并清除（与 wipe 管道共用 purge）。
- 作用域硬隔离：按 principal 存取，跨成员永不串线。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Tuple

from whale_core.memory.store import MemoryStore

#: 经验键前缀（按主体隔离）。
EXP_PREFIX = "exp:"
#: 默认 TTL：90 天（经验会消退；`ttl_s=0` = 永久，仅本人确认的稳定偏好）。
DEFAULT_EXP_TTL_S = 90 * 86400
#: 单主体经验条数上限（有界）。
MAX_PER_PRINCIPAL = 200


@dataclass(frozen=True)
class Experience:
    """一条可溯源经验。"""

    experience_id: str
    principal_id: str
    source: str            # receipt | user_feedback | candidate:<origin>
    evidence: str          # 证据引用（task_id/消息 id/回执 id；非正文）
    action: str            # 当时行为（如 "remind.lead_time:30m"）
    outcome: str           # "success" | "failed" | "rejected" | "corrected"
    confidence: float
    ttl_s: float
    confirmed_by_user: bool
    recorded_at: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "experience_id": self.experience_id,
            "principal_id": self.principal_id,
            "source": self.source,
            "evidence": self.evidence[:200],
            "action": self.action[:120],
            "outcome": self.outcome,
            "confidence": round(float(self.confidence), 3),
            "ttl_s": self.ttl_s,
            "confirmed_by_user": self.confirmed_by_user,
            "recorded_at": self.recorded_at,
        }


class ExperienceStore:
    """经验的存取与权重视图。全部确定性，不调模型。"""

    def __init__(self, store: MemoryStore):
        self.store = store

    def _key(self, principal_id: str, experience_id: str) -> str:
        return f"{EXP_PREFIX}{principal_id}:{experience_id}"

    # -- 写入（幂等） --------------------------------------------------------

    def record(self, exp: Experience, *, now: Optional[datetime] = None) -> bool:
        """记录/覆盖一条经验。候选来源强制低置信 + 未确认标记。"""
        if not exp.principal_id or not exp.experience_id:
            return False
        data = exp.as_dict()
        if exp.source.startswith("candidate"):
            data["confidence"] = min(data["confidence"], 0.3)
            data["confirmed_by_user"] = False
        self.store.put(self._key(exp.principal_id, exp.experience_id), data)
        self._trim(exp.principal_id)
        return True

    def record_receipt_outcome(self, principal_id: str, *, task_id: str,
                               action: str, success: bool,
                               receipt: str, ttl_s: float = DEFAULT_EXP_TTL_S
                               ) -> None:
        """从可信执行回执沉淀经验（幂等键 = 任务回执）。"""
        self.record(Experience(
            experience_id=f"rcpt-{task_id}",
            principal_id=principal_id,
            source="receipt",
            evidence=f"task:{task_id};receipt:{receipt[:80]}",
            action=action,
            outcome="success" if success else "failed",
            confidence=0.8, ttl_s=ttl_s, confirmed_by_user=False,
            recorded_at=datetime.now(timezone.utc).isoformat()))

    def record_user_feedback(self, principal_id: str, *, feedback_id: str,
                             action: str, outcome: str,
                             evidence: str, ttl_s: float = 0) -> None:
        """本人明确反馈（最高置信、永久；可纠正/删除）。"""
        self.record(Experience(
            experience_id=f"fb-{feedback_id}",
            principal_id=principal_id,
            source="user_feedback",
            evidence=evidence,
            action=action,
            outcome=outcome,
            confidence=1.0, ttl_s=ttl_s, confirmed_by_user=True,
            recorded_at=datetime.now(timezone.utc).isoformat()))

    # -- 读取/纠正/删除 -------------------------------------------------------

    def list_for(self, principal_id: str) -> Tuple[dict, ...]:
        out = []
        for key in self.store.keys():
            if not key.startswith(f"{EXP_PREFIX}{principal_id}:"):
                continue
            data = self.store.get(key)
            if isinstance(data, dict):
                out.append(dict(data))
        return tuple(sorted(out, key=lambda d: str(d.get("recorded_at"))))

    def correct(self, principal_id: str, experience_id: str, *,
                action: str) -> bool:
        """纠正行为内容（保留证据链；outcome 标记 corrected）。"""
        key = self._key(principal_id, experience_id)
        data = self.store.get(key)
        if not isinstance(data, dict):
            return False
        data["action"] = action[:120]
        data["outcome"] = "corrected"
        data["corrected_at"] = datetime.now(timezone.utc).isoformat()
        self.store.put(key, data)
        return True

    def delete(self, principal_id: str, experience_id: str) -> bool:
        key = self._key(principal_id, experience_id)
        if self.store.get(key) is None:
            return False
        self.store.delete(key)
        return True

    def purge_principal(self, principal_id: str) -> int:
        """成员退出/删除时清空其全部经验（真删）。"""
        n = 0
        for key in list(self.store.keys()):
            if key.startswith(f"{EXP_PREFIX}{principal_id}:"):
                self.store.delete(key)
                n += 1
        return n

    # -- 权重视图（只读建议；不碰任何安全策略） ------------------------------

    def suggested_weights(self, principal_id: str, *,
                          now: Optional[datetime] = None) -> dict[str, float]:
        """聚合经验为建议权重：``{action: weight}``。

        - 只计入未过期、非候选的经验；成功 +0.1、失败 −0.15、
          本人确认的纠正/反馈 ±0.3（按 outcome 方向）；
        - 权重 clamp 到 [-1, 1]；只表达「倾向」，不触发任何动作。
        """
        current = now or datetime.now(timezone.utc)
        weights: dict[str, float] = {}
        for data in self.list_for(principal_id):
            if str(data.get("source", "")).startswith("candidate"):
                continue
            ttl = float(data.get("ttl_s") or 0)
            if ttl > 0:
                try:
                    at = datetime.fromisoformat(str(data.get("recorded_at")))
                except ValueError:
                    continue
                if at + timedelta(seconds=ttl) < current:
                    continue  # 已消退
            action = str(data.get("action") or "")
            if not action:
                continue
            outcome = str(data.get("outcome") or "")
            delta = {"success": 0.1, "failed": -0.15,
                     "rejected": -0.3, "corrected": 0.3}.get(outcome, 0.0)
            if data.get("confirmed_by_user"):
                delta *= 2
            weights[action] = max(-1.0, min(1.0,
                                            weights.get(action, 0.0) + delta))
        return weights

    # -- 内部 ---------------------------------------------------------------

    def _trim(self, principal_id: str) -> None:
        items = list(self.list_for(principal_id))
        if len(items) <= MAX_PER_PRINCIPAL:
            return
        for old in items[:len(items) - MAX_PER_PRINCIPAL]:
            self.store.delete(self._key(principal_id,
                                        str(old["experience_id"])))
