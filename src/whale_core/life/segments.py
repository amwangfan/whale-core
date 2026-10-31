"""G3：ResponsePlan / MessageSegment / 语义分段。

纪律（PRD §5）：
- 默认一次完整消息；偶发 2–3 条；群聊默认最多 1–2 条；
- 不拆破代码块、公式、Markdown 表格、长 URL、@引用、安全提示；
- 同一 reply_group_id 在历史里只算一轮逻辑回复。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Tuple

# 私有上限 3、群聊上限 2（首版默认）
MAX_SEGMENTS_PRIVATE = 3
MAX_SEGMENTS_GROUP = 2

_FENCE_RE = re.compile(r"```.*?```", re.S)
_URL_RE = re.compile(r"https?://\S+")


@dataclass(frozen=True)
class MessageSegment:
    segment_id: str
    reply_group_id: str
    index: int
    kind: str                       # text / image / voice / mixed
    content: str
    reply_to_message_id: str | None = None


def split_reply(text: str, *, is_group: bool = False,
                max_segments: int | None = None) -> Tuple[str, ...]:
    """把一条回复按语义边界拆成 ≤N 段。

    拆分点优先级：空行 > 句末标点换行候选。**宁可少拆**：内容短、
    含代码块/表格/长链接时不拆。
    """
    limit = max_segments or (MAX_SEGMENTS_GROUP if is_group else MAX_SEGMENTS_PRIVATE)
    text = (text or "").strip()
    if not text or limit <= 1:
        return (text,) if text else ()
    # 保护区域：代码块（含未闭合 fence——拆一半更糟）/ 长 URL / 表格行不拆
    if _FENCE_RE.search(text) or text.count("```") % 2 == 1 \
            or _URL_RE.search(text) or "\n|" in text:
        return (text,)
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    if len(paragraphs) <= 1:
        # 单段落：短就不拆；长则按句号/问号/感叹号边界尝试拆两段
        if len(text) <= 240:
            return (text,)
        parts = re.split(r"(?<=[。！？!?…])\s*", text)
        parts = [p.strip() for p in parts if p.strip()]
        if len(parts) <= 1:
            return (text,)
        paragraphs = parts
    # 贪心合并到 limit 段
    segments: list[str] = []
    current = ""
    for para in paragraphs:
        if segments and len(segments) >= limit - 1:
            current = (current + "\n\n" + para) if current else para
            continue
        candidate = (current + "\n\n" + para) if current else para
        if current and len(candidate) > 160:
            segments.append(current)
            current = para
        else:
            current = candidate
    if current:
        segments.append(current)
    if len(segments) > limit:
        segments = segments[: limit - 1] + ["\n\n".join(segments[limit - 1:])]
    return tuple(s for s in segments if s.strip())
