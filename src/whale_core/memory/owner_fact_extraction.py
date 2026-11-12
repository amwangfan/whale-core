"""owner_fact_v1 模型提炼与服务端校验。

v1 语义纪律（协调代理 2026-11-12 amendment）：
- 模型只负责选择值得记的证据原句 span、kind、可选 predicate/value、可选
  correction_target；**summary == quote 原句**，不做改写，因此否定/主体/
  数字天然保真，不需要词表硬证蕴含；
- 服务端 ``validate_candidate`` 是唯一裁决点，不信任模型 assertion 字段；
- 校验整句上下文：span 前缀截掉否定/第三方转述/引语一律拒绝；
  疑问句拒绝；假设/转述降级 pending（不召回）；拿不准不 accepted；
- 结构化输出失败 → 空候选（由 worker 有限重试），绝不自由文本兜底；
- 时间字段必须原文有明确数字依据；naive/非法时间拒绝；无用户时区时
  模型被要求不做相对日期折算。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Protocol, Sequence

from whale_core.memory.owner_facts import (
    Assertion,
    FactKind,
    FactRecord,
    FactStatus,
    UserEvidence,
)

MAX_CANDIDATES_PER_EVIDENCE = 4
MAX_INPUT_CHARS = 4000
MAX_SUMMARY_CHARS = 300
MAX_EXISTING_FACTS = 8

_SENT_SPLIT = re.compile(r"[。！？!?；;\n]")
_NEGATION_CHARS = ("不", "没", "无", "未", "别", "莫", "勿")
_HYPOTHETICAL_WORDS = ("如果", "要是", "假设", "万一", "假若", "倘若")
_UNCERTAIN_WORDS = ("似乎", "好像", "大概", "可能", "也许", "说不定", "估计")
_SUFFIX_NEGATORS = ("才怪", "个鬼", "骗你", "开玩笑", "逗你", "个屁", "不是真的", "是假的")
_CORRECTION_WORDS = ("说错", "记错", "不对", "更正", "纠正", "应该是", "不是", "改成", "改为", "其实")
# 全消息撤销/否认语境（保守 pending，不是无限补词：只覆盖明确撤销语义）
_REVOCATION_WORDS = (
    "不是真的", "是假的", "当我没说", "别记", "不要记", "不用记",
    "收回", "撤回", "忘掉", "不算数", "前面那句",
)
# quote 内部反讽/撤销标记（"我可太喜欢加班了，才怪"整句被选中时 suffix 检查漏网）
_SARCASM_IN_QUOTE = _SUFFIX_NEGATORS + _REVOCATION_WORDS
# 未来相对时间词：无明确 valid_* 的相对计划不得当永久现状
_FUTURE_RELATIVE_WORDS = (
    "明天", "后天", "下周", "周末", "今晚", "马上", "一会", "待会",
    "改天", "回头", "届时",
)
# 第三方主体名词/代词：quote 内出现则不得携带 owner 谓词（防误归属/误覆盖）
_THIRD_PARTY_MARKERS = ("朋友", "同事", "同学", "家人", "闺蜜", "哥们", "他", "她", "它")
# 整条消息的第三方言说/更正归属（如"我朋友更正了：它叫团子"）：更正例外对其失效
_THIRD_PARTY_SPEECH_RE = re.compile(
    r"(朋友|同事|同学|家人|闺蜜|哥们|他|她|它|TA|ta)\S{0,6}(说|告诉|问|更正|纠正|写)"
)
# 跨行框架前缀窗口：句前 N 字符内的假设框架词同样生效（如「假设如下：\n我住在北京」）
_FRAME_LOOKBACK_CHARS = 24
_FIRST_PERSON = ("我", "咱", "俺")
_QUOTE_OPEN = ("“", "「", "‘", "\"", "'")
_PREDICATE_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")


class ExtractionFormatError(ValueError):
    """模型输出无法解析为合法结构（worker 据此有限重试）。"""

# 明确日期表达：YYYY年M月D日 / YYYY-M-D / YYYY/M/D（带年）与 M月D日 / M-D / M/D（无年）
_DATE_WITH_YEAR_RES = (
    re.compile(r"(?P<y>\d{4})\s*年\s*(?P<m>\d{1,2})\s*月\s*(?P<d>\d{1,2})\s*日?"),
    re.compile(r"(?P<y>\d{4})[-/\.](?P<m>\d{1,2})[-/\.](?P<d>\d{1,2})(?!\d)"),
)
_DATE_NO_YEAR_RES = (
    re.compile(r"(?<!\d)(?P<m>\d{1,2})\s*月\s*(?P<d>\d{1,2})\s*日?"),
    re.compile(r"(?<!\d)(?P<m>\d{1,2})[-/](?P<d>\d{1,2})(?!\d)"),
)


class ModelReply(Protocol):
    """宿主已配置文本模型的最小适配面（Qwen 侧实现）。

    实现必须具备真实网络/推理超时；worker 经 BoundedCall 单线程门调用，
    超时后不再发新请求。
    """

    def complete(self, prompt: str, *, timeout: float) -> str:
        ...


@dataclass(frozen=True)
class FactCandidate:
    kind: FactKind
    summary: str  # v1: == quote
    source_evidence_id: str
    quote: str
    span_start: int
    span_end: int
    predicate: str | None = None
    value: str | None = None
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    assertion: Assertion = Assertion.SELF_REPORT  # 模型建议值，不作信任依据
    correction_target: str | None = None


# ---------------------------------------------------------------------------
# 提炼
# ---------------------------------------------------------------------------

_PROMPT_TEMPLATE = """你是证据选择器。从用户消息中选出值得长期记住的用户自述原句。

规则：
- 只选用户明确自述的事实/偏好/经历原句；不要选问候、闲聊、问句、假设句、
  转述他人的话、包含手机号/证件号等敏感内容的句子。
- quote 必须是原文的**连续精确子串**（保留否定词如"不/没"，保留具体名字和
  数字，不得改写、补全或翻译）。
- 每条给出 kind: fact | preference | episode。
- 可选给出 predicate（简短谓词，如 pet_name / likes / lives_in）和 value。
  value 必须是 quote 原文的**连续子串**：不得归一化（"1994-06-18"≠原文
  "六月十八"）、翻译或补全；做不到就两者都留 null。
- 可选给出 valid_from / valid_to（ISO8601 带时区）：仅当原文明确写出具体
  日期/时间；相对时间（明天/下周）一律留 null。
- 若某条是对下方既有事实的明确更正（同一谓词、新值不同），给出
  correction_target = 该既有事实 id；拿不准留 null，绝不猜。
- 最多 {max_candidates} 条；没有可记的返回空列表。

观测时间（UTC）：{observed_at}
用户时区：{tz}
既有事实（仅供 correction_target 比对）：
{existing}

只输出 JSON：{{"candidates": [{{"kind": "...", "quote": "...", "predicate": null,
"value": null, "valid_from": null, "valid_to": null, "correction_target": null}}]}}

用户消息原文：
\"\"\"
{text}
\"\"\"
"""


class OwnerFactExtractor:
    """结构化提炼器。解析失败/结构不符 → 空候选（worker 计一次失败重试）。"""

    def __init__(
        self,
        model: ModelReply,
        *,
        max_candidates: int = MAX_CANDIDATES_PER_EVIDENCE,
        max_input_chars: int = MAX_INPUT_CHARS,
        max_summary_chars: int = MAX_SUMMARY_CHARS,
        max_existing: int = MAX_EXISTING_FACTS,
        timeout_s: float = 10.0,
    ) -> None:
        self._model = model
        self._max_candidates = max(1, int(max_candidates))
        self._max_input_chars = max(1, int(max_input_chars))
        self._max_summary = max(1, int(max_summary_chars))
        self._max_existing = max(0, int(max_existing))
        self._timeout = float(timeout_s)

    def extract(
        self,
        evidence: UserEvidence,
        bounded_existing_facts: Sequence[FactRecord],
    ) -> tuple[FactCandidate, ...]:
        text = evidence.text or ""
        if not text.strip() or len(text) > self._max_input_chars:
            return ()
        existing_view = [
            {
                "id": f.fact_id,
                "predicate": f.predicate,
                "value": f.value,
                "summary": f.summary,
            }
            for f in list(bounded_existing_facts)[: self._max_existing]
            if f.status == FactStatus.ACCEPTED
        ]
        prompt = _PROMPT_TEMPLATE.format(
            max_candidates=self._max_candidates,
            observed_at=evidence.received_at.astimezone(timezone.utc).isoformat(),
            tz=evidence.timezone or "未知",
            existing=json.dumps(existing_view, ensure_ascii=False),
            text=text,
        )
        raw = self._model.complete(prompt, timeout=self._timeout)
        items = _parse_candidates_json(raw)
        if items is None:
            # 解析失败：受控异常，worker 有限重试；绝不自由文本兜底
            raise ExtractionFormatError("model output is not valid candidates JSON")
        # 合法空候选：正常完成，不重试（不为空闲聊浪费模型调用）
        out: list[FactCandidate] = []
        for item in items[: self._max_candidates]:
            cand = _build_candidate(item, evidence)
            if cand is not None:
                out.append(cand)
        return tuple(out)


def _parse_candidates_json(raw: str) -> list[Mapping[str, Any]] | None:
    """从模型输出提取首个合法 JSON 对象；无法解析返回 None（区别于合法空列表）。"""
    if not raw:
        return None
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", raw):
        try:
            obj, _ = decoder.raw_decode(raw[match.start() :])
        except (ValueError, json.JSONDecodeError):
            continue
        if isinstance(obj, dict) and isinstance(obj.get("candidates"), list):
            return [c for c in obj["candidates"] if isinstance(c, dict)]
    return None


def _build_candidate(item: Mapping[str, Any], evidence: UserEvidence) -> FactCandidate | None:
    quote = str(item.get("quote") or "").strip()
    if not quote or len(quote) > MAX_SUMMARY_CHARS:
        return None
    # 服务端定位 span：quote 必须在原文中唯一出现，避免模型手算 offset
    if evidence.text.count(quote) != 1:
        return None
    start = evidence.text.index(quote)
    end = start + len(quote)
    try:
        kind = FactKind(str(item.get("kind") or "fact"))
    except ValueError:
        return None
    valid_from = _parse_aware(item.get("valid_from"))
    valid_to = _parse_aware(item.get("valid_to"))
    if item.get("valid_from") and valid_from is None:
        return None
    if item.get("valid_to") and valid_to is None:
        return None
    predicate = item.get("predicate")
    value = item.get("value")
    predicate_s = str(predicate).strip() or None if predicate is not None else None
    value_s = str(value).strip() or None if value is not None else None
    # 解析层宽容：模型归一化的 value（如"1994-06-18"对应原文"六月十八"）
    # 不是 quote 子串时，丢弃 predicate/value 但保留精确 quote 事实；
    # 服务端直接候选仍由 validate_candidate 严格拒绝未支撑 value。
    if value_s is not None and value_s not in quote:
        predicate_s = None
        value_s = None
    correction_target = item.get("correction_target")
    return FactCandidate(
        kind=kind,
        summary=quote,  # v1: summary == quote
        source_evidence_id=evidence.evidence_id,
        quote=quote,
        span_start=start,
        span_end=end,
        predicate=predicate_s,
        value=value_s,
        valid_from=valid_from,
        valid_to=valid_to,
        correction_target=str(correction_target) if correction_target else None,
    )


def _parse_aware(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        return None
    return dt.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# 服务端校验（唯一裁决点；返回 (decision, reason_code)，decision ∈ accepted|pending|reject）
# ---------------------------------------------------------------------------


def validate_candidate(
    cand: FactCandidate,
    evidence: UserEvidence,
    allowed_correction_targets: Mapping[str, int],
) -> tuple[str, str]:
    """服务端唯一裁决点。``allowed_correction_targets``: 本轮展示给模型的
    fact_id → revision 快照（≤8 条）；模型不得引用快照之外的任何 ID。
    """
    # span 精确性（Python Unicode codepoint 语义）
    if cand.source_evidence_id != evidence.evidence_id:
        return ("reject", "bad_source")
    if cand.span_start < 0 or cand.span_end > len(evidence.text) or cand.span_start >= cand.span_end:
        return ("reject", "bad_span")
    if evidence.text[cand.span_start : cand.span_end] != cand.quote:
        return ("reject", "quote_mismatch")
    if not cand.quote.strip():
        return ("reject", "empty_quote")
    if cand.summary != cand.quote:
        return ("reject", "summary_rewrite")
    if len(cand.summary) > MAX_SUMMARY_CHARS:
        return ("reject", "too_long")

    # 整句上下文：span 所在句、前缀、后缀
    sentence_start = _sentence_start(evidence.text, cand.span_start)
    prefix = evidence.text[sentence_start : cand.span_start]
    sentence_end = _sentence_end(evidence.text, cand.span_end)
    sentence = evidence.text[sentence_start:sentence_end]
    suffix = evidence.text[cand.span_end : sentence_end]

    # 定向更正例外：correction_target 在本轮快照内 + 明确更正语境 +
    # value 有 quote 支撑（predicate 绑定由服务层复核）。仅此类候选允许
    # quote 省略第一人称主体；普通第三人称/省略主体新事实不适用。
    # 例外不得豁免转述：整条消息若是第三方言说/更正（"我朋友更正了：…"），
    # 例外失效——更正权威只能属于 owner 本人。
    is_targeted_correction = (
        cand.correction_target is not None
        and cand.correction_target in allowed_correction_targets
    )
    correction_context = any(w in sentence for w in _CORRECTION_WORDS)
    third_party_message = _THIRD_PARTY_SPEECH_RE.search(evidence.text) is not None
    correction_exception = (
        is_targeted_correction and correction_context and not third_party_message
    )

    # 前缀截掉否定/第三方转述 → 拒绝。区分第一人称交流引导
    # （"跟你说个事，我养的猫叫年糕"的"跟你说"是用户对我说，放行）与
    # 第三方转述（"朋友说我养猫"拒）；定向更正语境豁免（"名字说错了，…"
    # 的"说错"是更正措辞，旧事实由服务层按快照 revision 复核后才失效）。
    if any(ch in prefix for ch in _NEGATION_CHARS) and not correction_exception:
        return ("reject", "context_truncation_negation")
    if _THIRD_PARTY_SPEECH_RE.search(prefix) and not correction_exception:
        return ("reject", "context_truncation_speech")
    if any(q in prefix for q in _QUOTE_OPEN) and not any(
        q in prefix for q in ("”", "」", "’")
    ):
        return ("reject", "quoted_context")

    # 后缀截掉否定/反讽/否认（"我喜欢咖啡才怪"、"我喜欢咖啡，这不是真的"）→ 拒绝
    if any(w in suffix for w in _SUFFIX_NEGATORS):
        return ("reject", "context_truncation_suffix")

    # 疑问句不入库
    if sentence.rstrip().endswith(("？", "?")):
        return ("reject", "question")

    # 主体：quote 本身必须是第一人称自述（句子含"我"≠ quote 是 owner 自述；
    # "我问过她喜欢咖啡"中的"她喜欢咖啡"不得升级为 owner 事实）。
    # 唯一例外：上述定向更正。
    if not any(p in cand.quote for p in _FIRST_PERSON) and not correction_exception:
        return ("reject", "no_first_person")

    # predicate/value 必须有证据支撑；格式非法/值不在 quote 中 → 拒
    if cand.predicate is not None and not _PREDICATE_RE.match(cand.predicate):
        return ("reject", "bad_predicate")
    if cand.value is not None and cand.value not in cand.quote:
        return ("reject", "value_not_grounded")

    # 时间字段必须原文有明确日期表达依据（不捏造、不折算相对日期）
    for dt in (cand.valid_from, cand.valid_to):
        if dt is not None and not _time_grounded(dt, evidence):
            return ("reject", "ungrounded_time")

    # correction_target 只能引用本轮展示快照内的 ID
    if cand.correction_target is not None and not is_targeted_correction:
        return ("reject", "bad_correction_target")

    # quote 内含反讽/撤销标记（模型选了整句"我可太喜欢加班了，才怪"时
    # suffix 检查必然漏网）→ 保守 pending；"我不喜欢咖啡"不含此类词不受影响
    if any(w in cand.quote for w in _SARCASM_IN_QUOTE):
        return ("pending", "sarcasm_in_quote")

    # 混合主体（"我朋友喜欢咖啡"）：不得携带 owner 谓词/值（防误归属与
    # 误覆盖）；不带谓词的原句可存 pending（记录但不召回）
    if (
        any(m in cand.quote for m in _THIRD_PARTY_MARKERS)
        and not correction_exception
    ):
        if cand.predicate is not None or cand.value is not None:
            return ("reject", "predicate_subject_mismatch")
        return ("pending", "mixed_subject")

    # 全消息撤销/否认语境：span 之外出现撤销语 → 保守 pending
    # （不能只看 span 所在句："我喜欢咖啡。其实前面那句不是真的。"）
    outside = evidence.text[: cand.span_start] + evidence.text[cand.span_end :]
    if any(w in outside for w in _REVOCATION_WORDS):
        return ("pending", "message_revocation")

    # 相对未来计划且未给明确 valid_*（"下周三看牙医"）→ pending；
    # v1 不扩相对日期解析器，无期限计划不当永久现状
    if (
        cand.valid_from is None
        and cand.valid_to is None
        and any(w in cand.quote for w in _FUTURE_RELATIVE_WORDS)
    ):
        return ("pending", "relative_time_unresolved")

    # 假设句：同句 + 跨行框架前缀窗口（"假设如下：\n我住在北京"）
    lookback = evidence.text[max(0, sentence_start - _FRAME_LOOKBACK_CHARS) : sentence_start]
    if any(w in sentence for w in _HYPOTHETICAL_WORDS) or any(
        w in lookback for w in _HYPOTHETICAL_WORDS
    ):
        return ("pending", "hypothetical")
    if any(w in sentence for w in _UNCERTAIN_WORDS):
        return ("pending", "uncertain")
    # 句级第三方转述 → pending；第一人称交流引导（跟你说/告诉你/我说）不降权
    if _THIRD_PARTY_SPEECH_RE.search(sentence) and not correction_exception:
        return ("pending", "speech_report")

    return ("accepted", "")


def has_correction_context(evidence: UserEvidence, cand: FactCandidate) -> bool:
    """候选 span 所在句是否含明确更正语境（说错/不对/不是/更正…）。

    服务层 supersede 门：同谓词不同值 ≠ 明确矛盾（"我喜欢咖啡"→"我也喜欢
    茶"是多值偏好）；无更正语境绝不失效旧事实。
    """
    start = _sentence_start(evidence.text, cand.span_start)
    end = _sentence_end(evidence.text, cand.span_end)
    sentence = evidence.text[start:end]
    return any(w in sentence for w in _CORRECTION_WORDS)


def _sentence_start(text: str, pos: int) -> int:
    idx = 0
    for m in _SENT_SPLIT.finditer(text, 0, pos):
        idx = m.end()
    return idx


def _sentence_end(text: str, pos: int) -> int:
    m = _SENT_SPLIT.search(text, pos)
    return m.end() if m else len(text)


def _time_grounded(dt: datetime, evidence: UserEvidence) -> bool:
    """候选时间必须对应原文一处明确日期表达。

    - 带年日期（2026年3月5日 / 2026-3-5 / 2026/3/5）：年月日全等；
    - 无年日期（3月5日 / 3-5 / 3/5）：月日相等，且候选年份必须等于
      证据接收年（不允许模型补出任意未来/过去年份）。
    """
    text = evidence.text or ""
    for rx in _DATE_WITH_YEAR_RES:
        for m in rx.finditer(text):
            if (
                int(m.group("y")) == dt.year
                and int(m.group("m")) == dt.month
                and int(m.group("d")) == dt.day
            ):
                return True
    received_year = evidence.received_at.astimezone(timezone.utc).year
    if dt.year != received_year:
        return False
    for rx in _DATE_NO_YEAR_RES:
        for m in rx.finditer(text):
            if int(m.group("m")) == dt.month and int(m.group("d")) == dt.day:
                return True
    return False
