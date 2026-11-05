"""Owner「做 xxx」轻量意图：规则识别 → 本地执行 → ResultDelivery 投递。

设计（2026-11-05）：
- 只认明确做事请求（规则/轻意图），不用强软提示堆「像真人」；
- 仅 owner 私聊与眼镜路径启用；群聊不走本短路（防刷群）；
- 首批落地：做一张简单结果图 → 本地真彩 PNG + 文字摘要 → ResultDelivery；
- 复杂任务仍回落既有 companion / 工具编排；本模块不替代全量编排器。
"""
from __future__ import annotations

import re
import struct
import time
import zlib
from dataclasses import dataclass
from typing import Callable, Sequence

from whale_core.life.result_delivery import (
    DeliveryItem, ResultDelivery, summarize_receipts)

# 「做一张…图发我 / 给我一张简单结果图」——冒烟与日常最简结果图闭环。
_RESULT_IMAGE = re.compile(
    r"(?:"
    r"(?:做|生成|画|出)\s*一?\s*[张幅个]\s*.{0,24}?(?:结果\s*图|简单\s*图|示意\s*图|测试\s*图|\b图\b)"
    r"|(?:发我|给我|发给我|寄我)\s*.{0,12}?(?:一?\s*[张幅个]\s*)?(?:结果\s*图|简单\s*图|示意\s*图|测试\s*图)"
    r"|(?:结果\s*图|简单\s*图).{0,12}?(?:发我|给我|发给我)"
    r")",
    re.IGNORECASE,
)

# 更宽的「做 xxx」显式请求（非结果图时仅标记，交回既有编排，不在此硬执行）。
_DO_EXPLICIT = re.compile(
    r"^\s*(?:请|麻烦|能不能|可以|帮我|帮忙|给我|你能|能否|请你|请帮我)?"
    r"\s*(?:做|去完成|执行|办理|搞定)\s*"
    r"(?:一?[下点个件]|一下|一下子)?\s*"
    r".{1,200}$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class DoRequest:
    kind: str  # result_image | explicit_task
    text: str
    goal: str = ""


def match_do_request(text: str) -> DoRequest | None:
    """规则匹配 owner 做事请求；未命中返回 None。"""
    raw = (text or "").strip()
    if not raw or len(raw) > 500:
        return None
    if _RESULT_IMAGE.search(raw):
        return DoRequest(kind="result_image", text=raw, goal="simple-result-image")
    if _DO_EXPLICIT.match(raw) and not _looks_like_chitchat(raw):
        # 显式「做 xxx」但非结果图：标记给宿主决定是否走编排；本模块不硬执行。
        goal = raw
        for prefix in ("请帮我", "请你", "麻烦你", "麻烦", "帮我", "帮忙",
                       "请", "能不能", "可以", "你能", "能否", "给我"):
            if goal.startswith(prefix):
                goal = goal[len(prefix):].lstrip()
                break
        if goal.startswith("做"):
            goal = goal[1:].lstrip("一下点个件 ").lstrip()
        return DoRequest(kind="explicit_task", text=raw, goal=goal[:200] or raw[:200])
    return None


def _looks_like_chitchat(text: str) -> bool:
    """排除「做什么呀」「你在做什么」等闲聊，避免误触发。"""
    t = text.strip()
    if re.search(r"(做什么|干嘛|干啥|在做[什啥]|你做[什啥]|怎么做的)", t):
        return True
    if re.search(r"(做梦|做个梦|做人|做事风格|做事方式)", t):
        return True
    return False


def is_owner_private_conversation(conversation: str) -> bool:
    """仅 private: 会话；群 group: 一律不走本短路。"""
    return str(conversation or "").startswith("private:")


def make_simple_result_png(w: int = 320, h: int = 180, *,
                           rgb=(32, 128, 220), label_rows: int = 8) -> bytes:
    """无第三方依赖的最小真彩 PNG（纯色 + 顶部深色条）。"""
    rows = []
    for y in range(h):
        row = bytearray([0])  # filter none
        for _x in range(w):
            if y < label_rows:
                row.extend((20, 20, 20))
            else:
                row.extend(rgb)
        rows.append(bytes(row))
    raw = b"".join(rows)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xffffffff))

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def build_result_image_items(text: str = "") -> tuple[str, list[DeliveryItem]]:
    """产出文字摘要 + 简单结果图。"""
    stamp = time.strftime("%Y-%m-%d %H:%M:%S Asia/Shanghai")
    summary = f"好了，简单结果图已生成（{stamp}）"
    if text:
        summary = f"{summary}\n请求：{(text or '')[:80]}"
    png = make_simple_result_png()
    items = [
        DeliveryItem(kind="text", text=summary),
        DeliveryItem(kind="image", image_bytes=png,
                     filename=f"result-{int(time.time())}.png"),
    ]
    return summary, items


ResultDeliverer = Callable[[Sequence[DeliveryItem], str], dict]


def execute_do_request(
    req: DoRequest,
    *,
    deliverer: ResultDeliverer | None = None,
    send_port=None,
    owner_uid: str = "",
    conversation: str = "",
    online_probe=None,
) -> dict:
    """执行可本地闭环的 do 请求并经 ResultDelivery 投递。

    返回：
      handled: bool — 是否已由本模块处理（True 则宿主勿再走 companion）；
      reply: str — 给会话的简短确认；
      delivery: dict | None — summarize_receipts 结果；
      kind: str
    """
    if req.kind != "result_image":
        # 显式任务：本轮只识别，不硬执行（复杂编排仍走既有 orchestrator）。
        return {
            "handled": False,
            "reply": "",
            "delivery": None,
            "kind": req.kind,
            "goal": req.goal,
        }

    summary, items = build_result_image_items(req.text)
    delivery_summary = summarize_receipts([])
    if deliverer is not None:
        delivery_summary = deliverer(items, summary)
    elif send_port is not None and owner_uid:
        delivery = ResultDelivery(
            send_port, owner_uid=owner_uid, online_probe=online_probe)
        receipts = delivery.deliver(
            items, conversation=conversation, summary=summary)
        delivery_summary = summarize_receipts(receipts)
    else:
        delivery_summary = {"ok": False, "error": "no-deliverer", "count": 0,
                            "channels": [], "receipts": []}

    ok = bool(delivery_summary.get("ok"))
    if ok:
        reply = "好了，结果图已经按投递路由发给你了。"
    else:
        detail = ""
        for r in delivery_summary.get("receipts") or ():
            if isinstance(r, dict) and r.get("detail"):
                detail = str(r["detail"])[:80]
                break
        reply = ("结果图做好了，但投递没完全成功"
                 + (f"（{detail}）" if detail else "")
                 + "。你可以再说一次「做一张简单结果图发我」。")
    return {
        "handled": True,
        "reply": reply,
        "delivery": delivery_summary,
        "kind": req.kind,
        "summary": summary,
    }


def try_owner_do(
    text: str,
    *,
    conversation: str,
    deliverer: ResultDeliverer | None = None,
    send_port=None,
    owner_uid: str = "",
    allow_non_private: bool = False,
    online_probe=None,
) -> dict | None:
    """Owner 私聊短路入口。未命中或群聊（默认）返回 None。"""
    if not allow_non_private and not is_owner_private_conversation(conversation):
        return None
    req = match_do_request(text)
    if req is None:
        return None
    if req.kind != "result_image":
        return None  # 其它「做 xxx」回落编排
    return execute_do_request(
        req, deliverer=deliverer, send_port=send_port,
        owner_uid=owner_uid, conversation=conversation,
        online_probe=online_probe)
