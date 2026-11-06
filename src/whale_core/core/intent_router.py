"""默认只读工具意图路由：常见显式搜索/本地生活/沙箱指令本地识别。

路由器只建议 capabilities，不能自行授权。

策略（2026-11-05 owner 确认；2026-11-06 修订：意图改为与回复并行后台跑，
寒暄短路默认关闭，仅 ``skip_clear_chitchat=True`` 时作为可选省调用优化）：
- 规则命中（search / local-life / weather→weather.now 或 web.search / shell.safe）
  → 直接 task，零意图 LLM；
- 规则未命中且**明显寒暄/短闲聊**：默认仍交意图 LLM（后台并行，不拖慢第一条）；
  ``skip_clear_chitchat=True`` 时才跳过（可选省调用，WHALE_INTENT_SKIP_CHITCHAT=1）；
- 其余模糊工具/媒体请求仍交意图 LLM（kimi）判 chat/task/clarify——不用
  「仅 fuzzy 词表才打 LLM」旧闸门（曾漏「找一下/同人/给我一个…图」）；
- 意图 LLM 未启用或返回非法计划 → 回落 kind=chat；
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
    r"^\s*(?:请|麻烦|能不能|可以|帮我|帮忙|给我|你能|能否|请你|请帮我|帮我查一下|麻烦你|你给我|你帮我)?\s*"
    r"(?:搜索|搜一下|搜搜|搜下|查一下|查查|查下|查找|检索|上网搜|上网查|"
    r"找一下|找一找|找找|找下|翻一下|翻翻|翻一翻|"
    r"百度一下|谷歌一下|google\s*(?:一下|下)?|bing\s*(?:一下|下)?|"
    r"search|look\s+up)\s*"
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

# 天气/气温/预报：无「搜索」动词也走 web.search。
# 用户口述城市优先，不得被默认位置（如缓存的「上海杨浦」）盖掉。
_WEATHER = re.compile(
    r"(天气|气温|温度|天气预报|空气质量|aqi|紫外线|穿什么)"
    r"|(会不会|会|要不要).{0,6}(下雨|下雪|刮风|打雷|降温|高温)"
    r"|(下雨|下雪|台风|暴雨|高温|降温).{0,6}(吗|嘛|么|不)"
    r"|\b(weather|forecast|temperature)\b",
    re.IGNORECASE,
)

# 口述异地城市/地名 + 天气：有则不要再拼默认位置。
_WEATHER_NAMED_PLACE = re.compile(
    r"(北京|上海|天津|重庆|广州|深圳|杭州|南京|武汉|成都|西安|苏州|青岛|"
    r"厦门|长沙|郑州|沈阳|大连|哈尔滨|福州|济南|合肥|昆明|南宁|南昌|"
    r"贵阳|太原|石家庄|乌鲁木齐|拉萨|银川|西宁|海口|三亚|香港|澳门|台北|"
    r"东京|大阪|京都|首尔|纽约|伦敦|巴黎|新加坡|洛杉矶|旧金山|"
    r"[\u4e00-\u9fff]{2,12}(?:省|市|县|区|州|盟|旗|镇))"
    r".{0,8}(天气|气温|温度|预报|空气质量|aqi|"
    r"下雨|下雪|台风|穿什么)",
    re.IGNORECASE,
)

# 「这里 / 我所在城市」→ 应用当前位置，不算口述异地。
_WEATHER_HERE = re.compile(
    r"(这里|这儿|本地|所在地|我所在|我这|当前位置).{0,8}"
    r"(天气|气温|温度|预报|空气质量)",
    re.IGNORECASE,
)


# 意图模型自己的目标/搜索词里写明是天气 → 改走和风（规则未命中的天气问法）。
_LLM_WEATHER_GOAL = re.compile(r"(天气|气温|预报|降雨|下雨|降水|weather|forecast)", re.IGNORECASE)


def _location_hint_from_context(context: str) -> str:
    """从意图上下文抽出「当前位置：」行；未知则空串。"""
    for line in (context or "").splitlines():
        if line.startswith("当前位置：") or line.startswith("位置："):
            hint = line.split("：", 1)[-1].strip()
            if hint and hint not in ("未知",) and "未知" not in hint:
                return hint[:80]
            return ""
    return ""


def _weather_search_query(text: str, *, context: str = "") -> str:
    """天气搜索词：口述城市原样；否则拼当前位置；无位置就用原话。"""
    query = re.sub(r"@\S+", " ", text or "")
    query = re.sub(r"\s+", " ", query).strip()[:500]
    if _WEATHER_NAMED_PLACE.search(query) and not _WEATHER_HERE.search(query):
        return query
    loc = _location_hint_from_context(context)
    if loc and loc not in query:
        return f"{loc} {query}"[:500]
    return query


# 地名正则是贪婪的 2–12 汉字 + 区/县/市：「现在郫都区下雨了吗」会抓成
# 「现在郫都区」，和风 Geo 必败后误回落搜索（2026-11-06 眼镜实证）。剥掉前置时间/语气词。
_PLACE_FILLER_PREFIX = re.compile(
    r"^(?:现在|目前|此刻|这会儿|这会|今天|明天|后天|今晚|明晚|今早|明早|今日|明日|"
    r"晚上|早上|上午|下午|中午|夜里|周末|请问|那|那么|你知道|知道|帮我|帮忙|给我|"
    r"看看|查查|查一下|看一下|问一下|想问|我想知道|我想问)+")


def clean_place_name(place: str) -> str:
    """去掉地名前的时间/语气前缀；剩余不足 2 字则视为无地名。"""
    cleaned = _PLACE_FILLER_PREFIX.sub("", (place or "").strip())
    return cleaned if len(cleaned) >= 2 else ""


def _weather_named_city(text: str) -> str:
    """口述异地城市名（排除「这里/我所在」）；无则空串。"""
    query = (text or "").strip()
    if not query or _WEATHER_HERE.search(query):
        return ""
    m = _WEATHER_NAMED_PLACE.search(query)
    if not m:
        return ""
    return clean_place_name(m.group(1) or "")[:40]


def _weather_tool_slots(text: str, *, context: str = "") -> dict:
    """weather.now 参数：口述城市优先；否则把当前位置 hint 交给工具解析坐标/地名。"""
    query = _weather_search_query(text or "", context=context)
    place = _weather_named_city(text or "")
    loc = _location_hint_from_context(context)
    slots = {"query": query[:500], "limit": 5}
    if place:
        slots["place"] = place
    if loc:
        # 口述地名优先；Geo 失败时工具再用当前位置兜底（不直接回落搜索）。
        slots["location_hint"] = loc[:120]
    return slots

# 显式要跑终端/沙箱命令时走 shell.safe（仍受 ACL+沙箱；非主人无 grant 会被门控拒绝）。
_SHELL_EXPLICIT = re.compile(
    r"(跑一下|执行一下|执行|运行).{0,12}(命令|终端|shell|bash|沙箱)"
    r"|(用|在).{0,8}(终端|shell|沙箱|受控终端).{0,12}(执行|运行|跑)"
    r"|run_shell|shell_sandbox"
    r"|(执行|运行)\s*(`[^`]+`|uname\b|date\b|ls\b|pwd\b|whoami\b|cat\s+/)",
    re.IGNORECASE,
)

# 显式指令（搜索/天气/本地生活/shell）规则直达，零 LLM。
# 明显寒暄短句可跳过意图 LLM；其余模糊工具/媒体仍交 kimi（不用「仅 fuzzy
# 才 LLM」旧闸门——曾漏「找一下/同人/给我一个…图」）。

# 保守寒暄白名单：整句匹配才短路；不确定 → False → 打意图 LLM。
_CLEAR_CHITCHAT = re.compile(
    r"^(?:"
    r"早|早安|早上好|早上好呀|早呀|早啊|早哦|"
    r"晚好|晚上好|晚上好呀|晚安|晚安啦|晚安哦|"
    r"午安|中午好|下午好|"
    r"你好|您好|你好啊|你好呀|你好哇|您好啊|"
    r"嗨|哈喽|嘿|哟|喂|"
    r"在吗|在不在|在么|在嘛|有人吗|"
    r"哈哈+|嘿嘿+|呵呵+|嗯+|哦+|噢+|喔+|啊+|唉+|唔+"
    r"|好的|好呀|好啊|行|行吧|嗯嗯|"
    r"拜拜|再见|回见|拜|"
    r"谢谢|多谢|谢啦|辛苦了|"
    r"ok|okay|hi|hello|hey|yo|sup|bye|thanks|thank\s*you"
    r")$",
    re.IGNORECASE,
)

# 短句里若已带工具/媒体迹象，禁止当闲聊短路（天气/搜索规则通常已先命中）。
_CHITCHAT_VETO = re.compile(
    r"(搜索|搜一下|搜搜|查一下|查找|找一下|翻一下|翻翻|"
    r"天气|气温|温度|下雨|下雪|预报|空气质量|"
    r"外卖|附近|画|生成|图片|插画|海报|视频|短片|动画|同人|壁纸|"
    r"唱|首歌|朗读|语音|命令|终端|shell|沙箱|下载|下单|支付|转账|"
    r"删除|安装|卸载|最新话|更新到|发个|来一张|给我一个|帮我改|执行|运行)",
    re.IGNORECASE,
)


def _looks_like_clear_chitchat(text: str) -> bool:
    """明显寒暄/短应答才 True；不确定一律 False（交给意图 LLM）。"""
    s = re.sub(r"@\S+", " ", text or "")
    # 去掉空白与常见语气标点；保留汉字/字母数字。
    s = re.sub(r"[\s\u3000]+", "", s)
    s = re.sub(r"[。！？!?…~～，,、\.!！？：:;；'\"“”‘’（）()【】\[\]~]+", "", s)
    # 粗滤常见 emoji / 符号（不影响「哈哈」等）
    s = re.sub(r"[\U0001F300-\U0001FAFF\u2600-\u27BF]+", "", s)
    if not s or len(s) > 16:
        return False
    if _CHITCHAT_VETO.search(s):
        return False
    return bool(_CLEAR_CHITCHAT.match(s))


class ReplyModel(Protocol):
    def reply(self, text: str, *, persona: str = "", memories=(), history=()) -> str: ...


def _chat_plan(text: str) -> str:
    return json.dumps({"goal": (text or "")[:500] or "对话", "kind": "chat"},
                      ensure_ascii=False)


class SearchIntentRouter:
    """规则优先；可选模型只处理模糊工具意图，不能批准动作。"""

    def __init__(self, *, model: ReplyModel | None = None, enable_llm: bool = False,
                 tools: ToolRegistry | None = None, skip_clear_chitchat: bool = False):
        self.tools = tools
        # 2026-11-06：意图全量后台跑（不再由寒暄白名单决定跑不跑意图）。
        # skip_clear_chitchat=True 仅作可选的省调用优化（不影响第一条速度）。
        self.skip_clear_chitchat = skip_clear_chitchat
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
        if _WEATHER.search(text or ""):
            caps = ({s.capability for s in self.tools.specs()}
                    if self.tools is not None else set())
            slots = _weather_tool_slots(text or "", context=context)
            query = str(slots.get("query") or "")
            if "weather.now" in caps:
                return json.dumps({
                    "goal": "查询天气：" + query[:300],
                    "kind": "task",
                    "slots": slots,
                    "capabilities": ["weather.now"],
                    "reason": "天气类优先走和风 weather.now；缺配置/失败由工具回落 web.search",
                }, ensure_ascii=False)
            return json.dumps({
                "goal": "搜索并回答天气：" + query[:300],
                "kind": "task",
                "slots": {"query": query[:500], "limit": 5},
                "capabilities": ["web.search"],
                "reason": "天气/气温类问题走只读网页搜索；口述城市优先于默认位置",
            }, ensure_ascii=False)
        if _LOCAL_LIFE.search(text or ""):
            query = (text or "").strip()[:500]
            # 把意图上下文里的位置线索拼进 query，便于「附近外卖」命中本地结果。
            loc_hint = _location_hint_from_context(context)
            if loc_hint and loc_hint not in query:
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
        # 明显寒暄零意图 LLM；其余模糊工具/媒体仍交 kimi。
        # LLM 未启用 → 诚实回落 chat。
        if self.skip_clear_chitchat and _looks_like_clear_chitchat(text or ""):
            return _chat_plan(text)
        if not self.enable_llm:
            return _chat_plan(text)
        specs = self.tools.specs() if self.tools is not None else []
        capabilities = ({s.capability for s in specs} if self.tools is not None
                        else {"web.search"})
        hints = {
            "web.search": "query: 搜索词, limit: 结果数量。追更（最新话/第几集/什么时候更新）、"
                          "最新动态、事实疑问、自己不知道的知识都走它——不要凭记忆回答",
            "weather.now": "place: 口述城市；location_hint/coords: 无口述时用当前位置；query: 回落搜索词",
            "web.fetch": "url: 要读取的公开HTTP(S)链接",
            "media.image.generate": "prompt: 图片描述, size/model: 可选（model 默认 qwen-image-3.0）, reference_query: 仅当主体是已知角色/人物/IP 形象时填参考图搜索词（系统会搜最多3张参考图带图生成）；若 web 搜索已拿到图片链接可用 reference_urls 直接传入；泛化主体不要填",
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
                detail += "；" + matching[0].description[:120]
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
            "用户说『画个猫』『念这段』这类已够用的媒体请求不要 clarify；"
            "确实无法推断才 clarify，且 clarification_question 只用一句口语短问。"
        )
        if context:
            prompt += f"\n上下文（记忆与近期对话，供推断缺省值）：\n{context[:900]}"
        prompt += f"\n用户原话：{text[:1000]}"
        raw = self.model.reply(prompt, persona="", memories=(), history=())
        if not (raw or "").strip():
            # AgentScope kimi-k3 长提示偶发空 content（finish=length）：视为不需要工具。
            print(f"[intent] empty model output → chat (prompt_chars={len(prompt)})",
                  flush=True)
            return _chat_plan(text)
        try:
            plan = plan_from_model_text(raw)
        except IntentPlanError:
            print(f"[intent] unparsable model output → chat (chars={len(raw or '')})",
                  flush=True)
            return _chat_plan(text)
        if plan.kind == "task" and any(cap not in capabilities for cap in plan.capabilities):
            return _chat_plan(text)
        if (plan.kind == "task" and "weather.now" in capabilities
                and "weather.now" not in plan.capabilities
                and set(plan.capabilities) <= {"web.search"}
                and _LLM_WEATHER_GOAL.search(
                    f"{plan.goal} {plan.slots.get('query', '')}")):
            # 天气类一律优先和风（工具内失败才回落 Tavily）：意图模型挑了
            # web.search 也改走 weather.now。
            slots = _weather_tool_slots(text or "", context=context)
            return json.dumps({
                "goal": "查询天气：" + str(slots.get("query") or "")[:300],
                "kind": "task", "slots": slots, "capabilities": ["weather.now"],
                "reason": "天气类优先和风 weather.now（意图模型选了搜索，已改写）",
            }, ensure_ascii=False)
        return raw
