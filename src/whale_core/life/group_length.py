"""群聊长度软约束：贴合近期群友发言中位，避免长文也不刻意碎句。

定位（相对 ExpressionPolicy / ResponsePlanner / GroupTurnPolicy）：

- **不是**硬字数上限（≤40 会「为了短而短」）；
- 从 GroupTimeline 统计非己方近期消息字符中位数，给表达层一句软提示，
  并在定稿时对**远超**常态的长文做句界软裁；
- 私聊不启用；样本不足时用温和默认中位，仍允许正常一两句；
- 无权限语义（PER-01）；提示极简，不堆「像真人」类强模板（PER-02）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional, Sequence, Tuple

#: 统计窗口：近期群友消息条数上限
WINDOW = 40
#: 至少多少条有效 peer 消息才采信中位；否则用 DEFAULT_MEDIAN
MIN_SAMPLES = 5
#: 样本不足时的默认中位（典型 QQ 群短句）
DEFAULT_MEDIAN = 28
#: 参与统计的单条长度上下界（过短噪声 / 长文广告不拉高中位）
_LEN_MIN = 2
_LEN_MAX = 120
#: 软裁天花板相对中位的倍率与夹紧区间（字符）
_CEIL_MULT = 3
_CEIL_MIN = 72
_CEIL_MAX = 160

_SENT_END_RE = re.compile(r"[。！？!?…]+")
_LIST_LINE_RE = re.compile(r"^\s*(?:[-*•]|\d+[.)、])\s+")
_URL_RE = re.compile(r"https?://\S+")


@dataclass(frozen=True)
class GroupLengthNorm:
    """一群当前的长度常态（表达层参考，非硬配额）。"""

    median: int
    sample_n: int
    soft_ceiling: int
    from_default: bool = False

    def hint(self) -> str:
        """注入人格层的一行软提示；私聊路径不应调用。"""
        return (
            f"这群近期群友发言中位大约 {self.median} 字："
            "接话贴近这个长度与语气，别写长段落或列表，也别故意碎成一两字。"
        )


def _visible_len(text: str) -> int:
    """统计可见长度：压空白、去掉裸 URL（链接不代表聊天口吻）。"""
    t = " ".join(str(text or "").split())
    t = _URL_RE.sub("", t).strip()
    return len(t)


def peer_char_lengths(entries: Iterable[Mapping[str, Any]], *,
                      exclude_self: bool = True) -> list[int]:
    """从时间线条目提取 peer 发言可见长度（供中位统计）。"""
    out: list[int] = []
    for e in entries:
        if not isinstance(e, Mapping):
            continue
        if exclude_self and e.get("is_self"):
            continue
        n = _visible_len(e.get("text") or "")
        if _LEN_MIN <= n <= _LEN_MAX:
            out.append(n)
    return out


def median_int(values: Sequence[int]) -> Optional[int]:
    if not values:
        return None
    s = sorted(int(v) for v in values)
    mid = len(s) // 2
    if len(s) % 2:
        return s[mid]
    return (s[mid - 1] + s[mid]) // 2


def soft_ceiling_for(median: int) -> int:
    """由中位推导软裁天花板：远超常态才裁，不把正常一两句砍碎。"""
    m = max(1, int(median))
    return max(_CEIL_MIN, min(_CEIL_MAX, max(m * _CEIL_MULT, m + 40)))


def derive_group_length_norm(
    entries: Sequence[Mapping[str, Any]] | None,
    *,
    window: int = WINDOW,
) -> GroupLengthNorm:
    """从群时间线推导长度常态；样本不足 → 默认中位（仍软约束）。"""
    recent = tuple(entries or ())[-max(1, int(window)):]
    lengths = peer_char_lengths(recent)
    if len(lengths) < MIN_SAMPLES:
        med = DEFAULT_MEDIAN
        return GroupLengthNorm(
            median=med, sample_n=len(lengths),
            soft_ceiling=soft_ceiling_for(med), from_default=True)
    med = median_int(lengths) or DEFAULT_MEDIAN
    return GroupLengthNorm(
        median=med, sample_n=len(lengths),
        soft_ceiling=soft_ceiling_for(med), from_default=False)


def _deflate_listy(text: str) -> str:
    """群聊里多行列表读着像通知：压成一两句（仅软裁路径调用）。"""
    lines = [ln.rstrip() for ln in str(text or "").splitlines() if ln.strip()]
    if len(lines) < 3:
        return text
    listish = sum(1 for ln in lines if _LIST_LINE_RE.match(ln))
    if listish < 2 and text.count("\n") < 3:
        return text
    # 取非列表引导句 + 前两条要点拼一句
    lead = next((ln for ln in lines if not _LIST_LINE_RE.match(ln)), "")
    bullets = [_LIST_LINE_RE.sub("", ln).strip() for ln in lines
               if _LIST_LINE_RE.match(ln)][:2]
    parts = [p for p in (lead, "；".join(x for x in bullets if x)) if p]
    return " ".join(parts) if parts else lines[0]


def _is_listy(text: str) -> bool:
    lines = [ln for ln in str(text or "").splitlines() if ln.strip()]
    if len(lines) < 3:
        return False
    listish = sum(1 for ln in lines if _LIST_LINE_RE.match(ln))
    return listish >= 2 or text.count("\n") >= 3


def soft_trim_text(text: str, ceiling: int) -> str:
    """句界软裁：不超过 ceiling 且非列表时原样；超出/列表则压平后再按句界裁。"""
    raw = str(text or "").strip()
    if not raw:
        return raw
    # 多行列表先压成一两句（群聊读着像通知）
    if _is_listy(raw) or (ceiling > 0 and len(raw) > ceiling):
        raw = _deflate_listy(raw)
    if ceiling <= 0 or len(raw) <= ceiling:
        return raw
    chunk = raw[:ceiling]
    # 优先句末；允许短句（中位本就短，不能要求 cut>=8）
    cut = None
    for m in _SENT_END_RE.finditer(chunk):
        cut = m.end()
    min_keep = max(2, min(6, ceiling // 4))
    if cut and cut >= min_keep:
        return raw[:cut].strip()
    for sep in ("；", ";", "，", ",", " "):
        i = chunk.rfind(sep)
        if i >= min_keep:
            return raw[: i + (0 if sep == " " else 1)].strip()
    return chunk.rstrip()


def soft_trim_bubbles(
    bubbles: Sequence[str],
    *,
    soft_ceiling: int,
    max_segments: int = 2,
) -> Tuple[str, ...]:
    """群聊气泡定稿软约束：段数封顶 + 单段软裁；已短则不动。"""
    if soft_ceiling <= 0:
        return tuple(b for b in bubbles if str(b or "").strip())
    cap = max(1, int(max_segments))
    cleaned = [str(b).strip() for b in bubbles if str(b or "").strip()]
    if not cleaned:
        return ()
    # 多段合计仍过长时优先合并再裁（避免「碎句刷屏」）
    if len(cleaned) > cap:
        cleaned = cleaned[: cap - 1] + ["\n".join(cleaned[cap - 1:])]
    # 群聊默认更想一条说完：两段都短才保留两段
    if len(cleaned) == 2 and sum(len(x) for x in cleaned) <= soft_ceiling:
        return (soft_trim_text(cleaned[0], soft_ceiling),
                soft_trim_text(cleaned[1], soft_ceiling))
    if len(cleaned) >= 2 and sum(len(x) for x in cleaned) > soft_ceiling:
        cleaned = ["\n".join(cleaned)]
    return tuple(soft_trim_text(b, soft_ceiling) for b in cleaned if b.strip())
