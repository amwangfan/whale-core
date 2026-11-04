"""默认只读工具意图路由：常见显式搜索/本地生活/沙箱指令本地识别。

路由器只建议 capabilities，不能自行授权。

策略（2026-11-05 owner 确认）：
- 规则命中（search / local-life / shell.safe）→ 直接 task，零意图 LLM；
- 规则未命中且像普通闲聊（含「现在几点」等）→ kind=chat，**跳过**意图 LLM
  （闲聊回复仍走宿主 companion / DeepSeek 官模，不在此短路本地时钟）；
- 仅当宿主 enable_llm=True **且**文本像模糊写操作/媒体生成等可能需工具时，
  才调用意图 LLM（通常为 AgentScope 上的 kimi）做 task/clarify；
- 授权仍走 context_resolver → authz → PluginGateway → ShellPolicy，与意图模型无关。
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

# 模糊写操作 / 媒体生成等：规则未覆盖、但可能真要动工具时才打意图 LLM。
# 故意不含「现在几点」「聊聊天」等纯陪伴问句。
_FUZZY_TOOLISH = re.compile(
    r"(画一?[张幅个]|画个|生成一?[张幅个]|做一?[张幅个图]|做个图)"
    r"|(画|生成).{0,16}(图|图片|插画|海报|视频|短片|动画)"
    r"|(文生图|文生视频|image\s*gen|text-to-image|text-to-video)"
    r"|(朗读|念一下|转成语音|语音合成|tts\b|听写|转写|asr\b)"
    r"|(删(除|掉)?|移除|覆盖|写入|保存到|下载到|改写|重命名|移动到).{0,24}"
    r"|(发(一?[封条])?邮件|发短信|推送到|下单|支付|转账|汇款)"
    r"|(安装|卸载|sudo\b|chmod\b|chown\b|rm\s+-|kubectl\b|docker\s+(rm|kill|exec))"
    r"|(帮我|请|麻烦).{0,16}(改(一下|掉|成)|操作一下|处理一下|写进|写到)",
    re.IGNORECASE,
)


class ReplyModel(Protocol):
    def reply(self, text: str, *, persona: str = "", memories=(), history=()) -> str: ...


def _chat_plan(text: str) -> str:
    return json.dumps({"goal": (text or "")[:500] or "对话", "kind": "chat"},
                      ensure_ascii=False)


class SearchIntentRouter:
    """规则优先；可选模型只处理模糊工具意图，不能批准动作。"""

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
        # 闲聊 / 时间问答等：默认 chat，不打意图 LLM（省 kimi 一轮）。
        if not (self.enable_llm and _FUZZY_TOOLISH.search(text or "")):
            return _chat_plan(text)
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
            return _chat_plan(text)
        if plan.kind == "task" and any(cap not in capabilities for cap in plan.capabilities):
            return _chat_plan(text)
        return raw
