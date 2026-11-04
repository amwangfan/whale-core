"""默认只读工具意图路由：常见显式搜索指令本地识别，可选 LLM 规划。

路由器只建议 capabilities，不能自行授权。对不匹配的消息返回 chat；
LLM 规划只在宿主显式开启时使用，避免所有闲聊多一次模型请求。
"""
from __future__ import annotations

import json
import re
from typing import Protocol, TYPE_CHECKING

if TYPE_CHECKING:
    from whale_core.core.runtime import ToolRegistry

from whale_core.core.intent_plan import plan_from_model_text, IntentPlanError

_SEARCH = re.compile(
    r"^\s*(?:请|麻烦|能不能|可以|帮我|帮忙|给我|你能|能否|请你|请帮我|帮我查一下|麻烦你)?\s*"
    r"(?:搜索|搜一下|搜搜|查一下|查查|查找|检索|上网搜|上网查|search|look\s+up)\s*"
    r"(?:一下|一下子|关于|有关|下|：|:|，|,|\s)*",
    re.IGNORECASE,
)

# 附近/外卖等本地生活问询：无显式「搜索」动词时也走 web.search（AskAi 式），
# 仍只读搜索，不触发下单/支付能力。
_LOCAL_LIFE = re.compile(
    r"(附近|周边|旁边|周围).{0,16}(外卖|餐馆|饭店|美食|吃的|好吃的|咖啡|奶茶|小吃|"
    r"超市|便利店|药店|医院|景点|停车场)"
    r"|(外卖|美团|饿了么|到家|闪购)"
    r"|((有什么|哪有|哪里有|推荐).{0,12}(外卖|吃的|餐馆|饭店))",
    re.IGNORECASE,
)

# 显式要跑终端/沙箱命令时走 shell.safe（仍受 ACL+沙箱；非主人无 grant 会被门控拒绝）。
_SHELL_EXPLICIT = re.compile(
    r"(跑一下|执行一下|执行|运行).{0,12}(命令|终端|shell|bash|沙箱)"
    r"|(用|在).{0,8}(终端|shell|沙箱|受控终端).{0,12}(执行|运行|跑)"
    r"|run_shell|shell_sandbox"
    r"|(执行|运行)\s*(`[^`]+`|uname\b|date\b|ls\b|pwd\b|whoami\b|cat\s+/)",
    re.IGNORECASE,
)


class ReplyModel(Protocol):
    def reply(self, text: str, *, persona: str = "", memories=(), history=()) -> str: ...


class SearchIntentRouter:
    """规则优先；可选模型识别其它任务，但模型不能批准动作。"""

    def __init__(self, *, model: ReplyModel | None = None, enable_llm: bool = False,
                 tools: ToolRegistry | None = None):
        self.tools = tools
        self.model = model
        self.enable_llm = enable_llm and model is not None

    def plan(self, text: str, *, context: str = "") -> str:
        match = _SEARCH.match(text)
        if match:
            query = text[match.end():].strip(" ：:，,。\n\t")
            if query:
                return json.dumps({
                    "goal": "搜索并回答：" + query[:300],
                    "kind": "task",
                    "slots": {"query": query[:500], "limit": 5},
                    "capabilities": ["web.search"],
                    "reason": "用户明确要求搜索公开信息",
                }, ensure_ascii=False)
        if _LOCAL_LIFE.search(text or ""):
            query = (text or "").strip()[:500]
            # 把意图上下文里的位置线索拼进 query，便于「附近外卖」命中本地结果。
            loc_hint = ""
            for line in (context or "").splitlines():
                if line.startswith("当前位置：") or line.startswith("位置："):
                    loc_hint = line.split("：", 1)[-1].strip()
                    break
            if loc_hint and loc_hint not in ("未知",) and "未知" not in loc_hint:
                query = f"{loc_hint} {query}"
            return json.dumps({
                "goal": "搜索并回答本地生活／附近信息：" + query[:300],
                "kind": "task",
                "slots": {"query": query[:500], "limit": 5},
                "capabilities": ["web.search"],
                "reason": "附近/外卖类问题走只读网页搜索（不下单、不支付）",
            }, ensure_ascii=False)
        caps = ({s.capability for s in self.tools.specs()} if self.tools is not None else set())
        if "shell.safe" in caps and _SHELL_EXPLICIT.search(text or ""):
            # 尽量抽出命令：反引号优先，否则整句交给执行器（策略层会截断/拒绝）。
            m = re.search(r"`([^`]+)`", text or "")
            if m:
                command = m.group(1).strip()[:500]
            else:
                command = (text or "").strip()[:500]
            return json.dumps({
                "goal": "在受控沙箱执行命令并回报输出：" + command[:200],
                "kind": "task",
                "slots": {"command": command},
                "capabilities": ["shell.safe"],
                "reason": "用户明确要求终端/沙箱执行",
            }, ensure_ascii=False)
        if self.enable_llm:
            specs = self.tools.specs() if self.tools is not None else []
            capabilities = ({s.capability for s in specs} if self.tools is not None
                            else {"web.search"})
            hints = {
                "web.search": "query: 搜索词, limit: 结果数量",
                "web.fetch": "url: 要读取的公开HTTP(S)链接",
                "media.image.generate": "prompt: 图片描述, size: 可选图片尺寸, model: 可选（用户指定才填）",
                "media.video.generate": "prompt: 视频描述, model: 可选（用户指定才填）",
                "tts.synthesize": "text: 要朗读的文字, model/voice: 可选（用户指定才填）",
                "asr.transcribe": "audio: 已提供的音频数据或资源引用；缺音频时澄清",
                "shell.safe": "command: 命令字符串；仅主人或有效 sudo shell 租约；执行仍受沙箱限制",
            }
            catalog = []
            for capability in sorted(capabilities):
                matching = [s for s in specs if s.capability == capability]
                detail = hints.get(capability, "参数须符合工具契约")
                if matching:
                    detail += "；" + matching[0].description[:300]
                catalog.append(f"{capability}（{detail}）")
            prompt = (
                "仅返回一个 JSON 对象，不要代码围栏。你负责建议意图，不能授予权限。"
                '格式：{"goal":"目标","kind":"chat|task|clarify",'
                '"slots":{},"capabilities":[],"clarification_question":""}。'
                "只有确实需要工具且能力已明确时才用 task；普通谈话用 chat。"
                "不得自行声明审批通过；能力的风险与审批由宿主策略裁决。"
                "仅可选择以下当前已挂载能力：" + "；".join(catalog) + "。"
                "澄清要克制：能从上下文/记忆合理推断的缺省值就直接默认（如地点类请求"
                "没明说地点时，默认用户所在地；用户有出行信息时，用行程目的地或出发地）；"
                "确实无法推断才 clarify。"
            )
            if context:
                prompt += f"\n上下文（记忆与近期对话，供推断缺省值）：\n{context[:1500]}"
            prompt += f"\n用户原话：{text[:1500]}"
            raw = self.model.reply(prompt, persona="", memories=(), history=())
            try:
                plan = plan_from_model_text(raw)
            except IntentPlanError:
                return json.dumps({"goal": text[:500] or "对话", "kind": "chat"},
                                  ensure_ascii=False)
            if plan.kind == "task" and any(cap not in capabilities for cap in plan.capabilities):
                return json.dumps({"goal": text[:500] or "对话", "kind": "chat"},
                                  ensure_ascii=False)
            return raw
        return json.dumps({"goal": text[:500] or "对话", "kind": "chat"}, ensure_ascii=False)
