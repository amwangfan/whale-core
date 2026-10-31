"""issue #12：眼镜 HUD 能力策略与显示回执分层（雷鸟 iO 桥，Core 侧）。

纪律：
- 「发送到手机」≠「眼镜显示」——回执分层：requested（请求已受理）→
  phone_handled（手机桥已处理）→ bridge_sent（桥已发出）→ shown（设备
  显示可证）。无桥接/桥未回证时只能报 degraded/unknown，绝不报 shown。
- 内容策略在 Core 裁决：字段白名单 + 大小上限 + 私人屏幕内容（OCR/通知
  正文）默认不上镜；scope/频控/静默由既有 Gateway/Initiative 承担。
- 无真实 RayNeo 设备时，测试只能标 simulated；真机验证后才可标
  device-tested（本仓测试均为 simulated）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional

#: 眼镜能力集合（版本化 minor 协议的一部分）。
GLASSES_CAPABILITIES = frozenset({
    "glasses.card.show", "glasses.card.update", "glasses.card.dismiss",
    "glasses.feedback",
})

#: 卡片字段上限（硬限制）。
MAX_TITLE_CHARS = 40
MAX_BODY_CHARS = 200
MAX_CARD_ID_CHARS = 64
ALLOWED_PRIORITIES = frozenset({"low", "normal", "high"})
MAX_TTL_S = 3600

_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class GlassesPolicyError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def validate_card(*, card_id: str, title: str, body: str,
                  priority: str = "normal", ttl_s: float = 300) -> dict:
    """校验并规范化卡片载荷（字段白名单 + 截断 + 控制字符剥离）。

    私人屏幕 OCR / 通知正文默认不上镜由调用方在内容来源侧保证
    （event_consumer 根本不把 private 级事件送进任何通知路径）；本层
    只做形状与大小裁决。
    """
    cid = str(card_id or "").strip()
    if not cid or len(cid) > MAX_CARD_ID_CHARS:
        raise GlassesPolicyError("bad_card_id", "card_id 必填且 ≤64 字符")
    if priority not in ALLOWED_PRIORITIES:
        raise GlassesPolicyError("bad_priority",
                                 f"priority 必须是 {sorted(ALLOWED_PRIORITIES)}")
    ttl = float(ttl_s)
    if not 0 < ttl <= MAX_TTL_S:
        raise GlassesPolicyError("bad_ttl", f"ttl_s 须在 (0, {MAX_TTL_S}]")
    clean_title = _CONTROL_RE.sub(" ", str(title or "")).strip()[:MAX_TITLE_CHARS]
    clean_body = _CONTROL_RE.sub(" ", str(body or "")).strip()[:MAX_BODY_CHARS]
    if not clean_title and not clean_body:
        raise GlassesPolicyError("empty_card", "title/body 不能同时为空")
    return {"card_id": cid, "title": clean_title, "body": clean_body,
            "priority": priority, "ttl_s": ttl}


# ---------------------------------------------------------------------------
# 回执分层
# ---------------------------------------------------------------------------

#: 显示状态层级（严格递增的可证性）。
DISPLAY_STATES = ("requested", "phone_handled", "bridge_sent", "shown",
                  "degraded", "unknown")

#: 桥接回执 status → 显示状态。保守映射：任何缺失字段都降级。
_RECEIPT_MAP = {
    "executed": "bridge_sent",      # 手机桥确认已发出（≠ 眼镜已显示）
    "displayed": "shown",           # 眼镜显示可证（桥回显证明）
    "failed": "degraded",
    "unknown": "unknown",
}


@dataclass(frozen=True)
class DisplayVerdict:
    """一次上镜尝试的诚实结论。"""

    state: str               # DISPLAY_STATES 之一
    evidence: str            # 回执来源标识（receipt_id / 错误码）
    simulated: bool = False  # 无真机回证的模拟路径标记


def classify_receipt(receipt: Optional[dict]) -> DisplayVerdict:
    """把手机桥回执翻译成显示状态。无回执 → unknown（绝不猜 shown）。

    只有桥回执显式 ``display_proof=True`` 且 status=executed 才报 shown。
    """
    if not isinstance(receipt, dict):
        return DisplayVerdict("unknown", "no-receipt")
    status = str(receipt.get("status") or "unknown")
    rid = str(receipt.get("receipt_id") or "")[:64]
    if status == "executed" and receipt.get("display_proof") is True:
        return DisplayVerdict("shown", rid or "bridge-proof")
    return DisplayVerdict(_RECEIPT_MAP.get(status, "unknown"),
                          rid or status or "unknown")
