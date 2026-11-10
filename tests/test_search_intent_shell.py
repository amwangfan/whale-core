"""Explicit fast paths skip intent LLM; everything else (incl. chitchat) uses it.

2026-11-06 修订：意图与回复并行后台跑，寒暄默认也交 kimi；寒暄短路仅作
可选省调用（skip_clear_chitchat=True）。
"""
from __future__ import annotations
import json
from whale_core.core.intent_router import SearchIntentRouter
from whale_core.core.runtime import ToolSpec


class _Tools:
    def specs(self):
        return [ToolSpec(name="run_shell", capability="shell.safe",
                         plugin_id="shell_sandbox", action="run_command",
                         risk_class="communicate", description="x")]


class _Boom:
    def reply(self, text: str, *, persona: str = "", memories=(), history=()) -> str:
        raise AssertionError("intent LLM must not be called for this utterance")


class _EchoChat:
    """记录调用并返回 chat 计划（模拟意图 LLM 判闲聊）。"""

    def __init__(self):
        self.calls = 0
        self.prompts = []

    def reply(self, text: str, *, persona: str = "", memories=(), history=()) -> str:
        self.calls += 1
        self.prompts.append(text)
        return json.dumps({"goal": "对话", "kind": "chat", "slots": {},
                           "capabilities": []}, ensure_ascii=False)


class _Echo:
    def __init__(self):
        self.calls = 0

    def reply(self, text: str, *, persona: str = "", memories=(), history=()) -> str:
        self.calls += 1
        return json.dumps({
            "goal": "clarify write",
            "kind": "clarify",
            "slots": {},
            "capabilities": [],
            "clarification_question": "要改哪一处？",
        }, ensure_ascii=False)


def test_explicit_shell_routes_when_capability_present():
    router = SearchIntentRouter(tools=_Tools(), enable_llm=False)
    plan = json.loads(router.plan("在沙箱里执行 `date +%F`"))
    assert plan["kind"] == "task"
    assert "shell.safe" in plan["capabilities"]
    assert "date" in plan["slots"]["command"]


def test_no_shell_capability_stays_chat():
    router = SearchIntentRouter(tools=None, enable_llm=False)
    plan = json.loads(router.plan("在沙箱里执行 `date +%F`"))
    assert plan["kind"] == "chat"


def test_clear_chitchat_reaches_intent_llm_by_default():
    """2026-11-06：意图全量后台跑——寒暄默认也交 kimi 判定（不再硬闸）。"""
    echo = _EchoChat()
    router = SearchIntentRouter(model=echo, enable_llm=True, tools=_Tools())
    for text in ("晚上好", "你好"):
        assert json.loads(router.plan(text))["kind"] == "chat"
    assert echo.calls == 2


def test_empty_intent_output_is_safe_chat():
    """kimi-k3 长提示空 content（finish=length）→ 视为不需要工具。"""
    class _Empty:
        def reply(self, text, *, persona="", memories=(), history=()):
            return ""
    router = SearchIntentRouter(model=_Empty(), enable_llm=True, tools=_Tools())
    assert json.loads(router.plan("帮我看看那个东西"))["kind"] == "chat"


def test_clear_chitchat_skip_is_optional_optimization():
    """skip_clear_chitchat=True（WHALE_INTENT_SKIP_CHITCHAT=1）：明显寒暄零意图 LLM。"""
    router = SearchIntentRouter(model=_Boom(), enable_llm=True, tools=_Tools(),
                                skip_clear_chitchat=True)
    for text in ("晚上好", "早", "你好", "你好啊", "在吗", "哈哈", "嗯", "晚安",
                 "hi", "Hello!", "早上好呀"):
        plan = json.loads(router.plan(text))
        assert plan["kind"] == "chat", text

    chat_echo = _EchoChat()
    router = SearchIntentRouter(model=chat_echo, enable_llm=True, tools=_Tools(),
                                skip_clear_chitchat=True)
    for text in ("现在几点", "今天心情怎么样", "聊聊天"):
        plan = json.loads(router.plan(text))
        assert plan["kind"] == "chat", text
    assert chat_echo.calls == 3


def test_llm_disabled_or_bad_plan_falls_back_chat():
    """LLM 未启用 → 不打模型直接 chat；LLM 返回非法计划 → 回落 chat。"""
    router = SearchIntentRouter(model=_Boom(), enable_llm=False, tools=_Tools())
    assert json.loads(router.plan("你好啊"))["kind"] == "chat"

    class _Garbage:
        def reply(self, text, *, persona="", memories=(), history=()):
            return "not a json plan"

    router = SearchIntentRouter(model=_Garbage(), enable_llm=True, tools=_Tools())
    assert json.loads(router.plan("今天心情怎么样"))["kind"] == "chat"


def test_explicit_search_skips_intent_llm():
    router = SearchIntentRouter(model=_Boom(), enable_llm=True, tools=None)
    plan = json.loads(router.plan("搜索量子计算"))
    assert plan["kind"] == "task"
    assert "web.search" in plan["capabilities"]


def test_fuzzy_write_may_call_intent_llm():
    echo = _Echo()
    router = SearchIntentRouter(model=echo, enable_llm=True, tools=None)
    raw = router.plan("请帮我改一下配置文件")
    assert echo.calls == 1
    plan = json.loads(raw)
    assert plan["kind"] == "clarify"


def test_baidu_google_shortcuts_route_search():
    router = SearchIntentRouter(model=_Boom(), enable_llm=True, tools=None)
    for text in ("百度一下量子计算", "谷歌一下 rust async", "google一下 fastapi"):
        plan = json.loads(router.plan(text))
        assert plan["kind"] == "task", text
        assert "web.search" in plan["capabilities"]


def test_weather_routes_web_search_without_search_verb():
    router = SearchIntentRouter(model=_Boom(), enable_llm=True, tools=None)
    for text in ("今天天气怎么样", "北京天气", "大阪明天气温", "会下雨吗"):
        plan = json.loads(router.plan(text, context="当前位置：上海杨浦"))
        assert plan["kind"] == "task", text
        assert "web.search" in plan["capabilities"]


def test_named_city_weather_not_overridden_by_default_location():
    router = SearchIntentRouter(tools=None, enable_llm=False)
    plan = json.loads(router.plan(
        "北京今天天气怎么样", context="当前位置：上海杨浦"))
    assert plan["kind"] == "task"
    q = plan["slots"]["query"]
    assert "北京" in q
    assert "上海" not in q  # 口述城市优先，不被默认位置盖掉


def test_bare_weather_uses_cached_location():
    router = SearchIntentRouter(tools=None, enable_llm=False)
    plan = json.loads(router.plan(
        "今天天气怎么样", context="当前位置：上海杨浦"))
    assert "上海杨浦" in plan["slots"]["query"]
    assert "天气" in plan["slots"]["query"]


def test_here_weather_uses_location_when_known():
    router = SearchIntentRouter(tools=None, enable_llm=False)
    plan = json.loads(router.plan(
        "我所在城市天气", context="当前位置：大阪·难波"))
    assert "大阪" in plan["slots"]["query"]




def test_here_weather_uses_chengdu_pidu_home_location():
    """「我所在城市天气」应拼入缓存/默认位置（成都郫都），不是口述异地。"""
    router = SearchIntentRouter(tools=None, enable_llm=False)
    plan = json.loads(router.plan(
        "我所在城市天气",
        context="当前位置：成都郫都区电子科技大学；约 30.7497,103.9315"))
    q = plan["slots"]["query"]
    assert "成都" in q or "郫都" in q
    assert "上海" not in q
    assert "杨浦" not in q

def test_weather_without_location_still_searches_user_text():
    """无位置时仍走搜索（允许口述城市）；不硬编码上海。"""
    router = SearchIntentRouter(tools=None, enable_llm=False)
    plan = json.loads(router.plan(
        "成都天气", context="当前位置：未知（未提供城市或坐标）"))
    assert plan["kind"] == "task"
    assert plan["slots"]["query"] == "成都天气"
    assert "上海" not in plan["slots"]["query"]


def test_weather_prefers_qweather_when_mounted():
    class _WTools:
        def specs(self):
            return [
                ToolSpec(name="weather_now", capability="weather.now",
                         plugin_id="qweather", action="lookup",
                         risk_class="read_only", description="w"),
                ToolSpec(name="search_web", capability="web.search",
                         description="s"),
            ]
    router = SearchIntentRouter(tools=_WTools(), enable_llm=False)
    plan = json.loads(router.plan(
        "我所在城市天气",
        context="当前位置：成都郫都区电子科技大学；约 30.7497,103.9315"))
    assert plan["capabilities"] == ["weather.now"]
    assert "location_hint" in plan["slots"]
    assert "成都" in plan["slots"]["location_hint"] or "郫都" in plan["slots"]["location_hint"]
    plan2 = json.loads(router.plan(
        "北京今天天气怎么样", context="当前位置：成都郫都区电子科技大学"))
    assert plan2["capabilities"] == ["weather.now"]
    assert plan2["slots"].get("place") == "北京"
    assert "上海" not in json.dumps(plan2["slots"], ensure_ascii=False)


def test_find_verbs_route_to_search():
    """2026-11-05 生产实证：「找一下/翻翻」曾落 chat，模型谎称搜过。"""
    from whale_core.core.intent_router import SearchIntentRouter
    from whale_core.core.runtime import ToolRegistry

    class _B:
        def plan(self, text, *, context=""):
            return '{"kind":"chat","goal":"chat","capabilities":[]}'

        def reply(self, text, **kw):
            return "x"

    r = SearchIntentRouter(model=_B(), enable_llm=False, tools=ToolRegistry())
    for t in ("帮我找一下来自深渊最新话", "你给我找一下来自深渊最新话",
              "翻一下来自深渊更新到第几话了", "查下明天上海天气"):
        import json
        plan = json.loads(r.plan(t))
        assert plan["kind"] == "task", t
        assert "web.search" in plan["capabilities"], t


def test_media_and_toolish_phrases_all_reach_intent_llm():
    """2026-11-05 owner 决策：词表闸门废除——追更/同人/请求动词+媒体名词
    这些曾三度漏网的口语句式，现在一律直达意图 LLM（kimi）判断。"""
    echo = _EchoChat()
    router = SearchIntentRouter(model=echo, enable_llm=True, tools=_Tools())
    phrases = (
        "来自深渊最新话什么时候更新",       # 生产实证1：追更
        "同人也行，我要看娜娜奇和米蒂的同人",  # 生产实证2：同人
        "给我一个千早爱音抱着奶龙的图",      # 生产实证3：请求动词+媒体名词
        "发个奶龙的视频看看",
        "给我唱首歌",
        "来一张鲸鱼的壁纸",
        "你发了吗\n你没发啊",               # 元讨论也过 LLM（由它判 chat）
        "这表情包还是刚从别的群偷来的",
    )
    for text in phrases:
        plan = json.loads(router.plan(text))
        assert plan["kind"] in ("chat", "task", "clarify"), text
    assert echo.calls == len(phrases)  # 全部咨询了意图 LLM
    # 用户原话确实进了 LLM prompt（判断依据可见）
    assert "给我一个千早爱音抱着奶龙的图" in echo.prompts[2]


def test_prompt_tracks_actual_tool_results_without_telling_character_how_to_act():
    from whale_core.core.prompt_composer import HOST_CONTRACT
    assert "实际完成" in HOST_CONTRACT
    assert "不编造完成状态" in HOST_CONTRACT
    assert "这就安排" not in HOST_CONTRACT


class _WeatherTools:
    def specs(self):
        return [ToolSpec(name="weather_now", capability="weather.now", description="和风"),
                ToolSpec(name="search_web", capability="web.search", description="搜索")]


def test_place_filler_prefix_stripped_for_qweather():
    """2026-11-06 眼镜实证：「现在郫都区下雨了吗」place 抓成「现在郫都区」→ 和风 Geo 失败回落搜索。"""
    router = SearchIntentRouter(tools=_WeatherTools(), enable_llm=False)
    plan = json.loads(router.plan("现在郫都区下雨了吗？", context="当前位置：成都郫都"))
    assert plan["capabilities"] == ["weather.now"]
    assert plan["slots"]["place"] == "郫都区"
    assert plan["slots"]["location_hint"] == "成都郫都"


def test_llm_weather_via_search_rewritten_to_qweather():
    """规则没认出、kimi 自己判成「搜天气」时也改走和风。"""
    class _SearchPlan:
        def reply(self, text, *, persona="", memories=(), history=()):
            return json.dumps({"goal": "查成都明早天气", "kind": "task",
                               "capabilities": ["web.search"],
                               "slots": {"query": "成都 明早 天气"}}, ensure_ascii=False)
    router = SearchIntentRouter(model=_SearchPlan(), enable_llm=True, tools=_WeatherTools())
    plan = json.loads(router.plan("明早出门用不用带伞", context="当前位置：成都郫都"))
    assert plan["capabilities"] == ["weather.now"]
    assert plan["slots"]["location_hint"] == "成都郫都"

    class _News:
        def reply(self, text, *, persona="", memories=(), history=()):
            return json.dumps({"goal": "搜成都活动", "kind": "task",
                               "capabilities": ["web.search"],
                               "slots": {"query": "成都 今天 活动"}}, ensure_ascii=False)
    router = SearchIntentRouter(model=_News(), enable_llm=True, tools=_WeatherTools())
    assert json.loads(router.plan("今天成都有什么活动"))["capabilities"] == ["web.search"]


def test_url_in_message_routes_to_fetch_not_search():
    """2026-11-10 生产实证：群友发性格测试链接让她做，被路由成 web.search
    搜出无关新闻还谎称「没加载出来」。链接应读（web.fetch）不是搜。"""
    from whale_core.core.runtime import ToolRegistry, ToolSpec
    tools = ToolRegistry()
    tools.register(ToolSpec(name="fetch_url", capability="web.fetch",
                            plugin_id="web_fetch", handler=lambda a: ""))
    tools.register(ToolSpec(name="search", capability="web.search",
                            plugin_id="web_search", handler=lambda a: ""))
    router = SearchIntentRouter(model=_Boom(), enable_llm=True, tools=tools)
    for text in ("https://exoticflicker.github.io/cati/",
                 "你去做上面那个测试 https://exoticflicker.github.io/cati/",
                 "@Enxi 看看这个 https://example.com/a?b=1"):
        plan = json.loads(router.plan(text))
        assert plan["kind"] == "task", text
        assert plan["capabilities"] == ["web.fetch"], text
        assert plan["slots"]["url"].startswith("http"), text
    # 无 web.fetch 挂载时不误判（回落 chat，不捏造 fetch 任务）
    tools2 = ToolRegistry()
    tools2.register(ToolSpec(name="search", capability="web.search",
                             plugin_id="web_search", handler=lambda a: ""))
    router2 = SearchIntentRouter(model=None, enable_llm=False, tools=tools2)
    plan = json.loads(router2.plan("看看 https://example.com"))
    assert plan["kind"] == "chat"


def test_heavy_writing_routes_chat_heavy_not_search():
    """2026-11-10 owner 实证：「给我写2000字卢唐同人文」被 kimi 路由成
    web.search（搜出一堆推荐链接）。写作请求 → chat + heavy，重模型作答。"""
    router = SearchIntentRouter(model=_Boom(), enable_llm=True, tools=None)
    for text in ("给我写2000字卢唐同人文", "写一篇关于鲸鱼的短诗",
                 "帮我写一封信", "扩写这段话"):
        plan = json.loads(router.plan(text))
        assert plan["kind"] == "chat", text
        assert plan.get("heavy") is True, text
        assert not plan.get("capabilities"), text


def test_intent_plan_heavy_field_parsed():
    from whale_core.core.intent_plan import plan_from_model_text
    p = plan_from_model_text('{"goal":"写文","kind":"chat","heavy":true}')
    assert p.heavy is True
    p2 = plan_from_model_text('{"goal":"聊","kind":"chat"}')
    assert p2.heavy is False


def test_orchestrator_chat_route_uses_heavy_companion():
    """heavy 计划 → 重模型陪伴作答；普通 chat 仍走快模型。"""
    from whale_core.core.companion import WhaleCompanion
    from whale_core.core.runtime import ConversationOrchestrator
    from whale_core.memory.store import InMemoryStore

    class _B:
        def __init__(self, tag): self.tag = tag
        def reply(self, text, **kw): return self.tag

    orch = ConversationOrchestrator(
        InMemoryStore(), WhaleCompanion(InMemoryStore(), _B("fast")),
        heavy_companion=WhaleCompanion(InMemoryStore(), _B("KIMI")))

    class _HeavyPlan:
        kind = "chat"; heavy = True; task_id = ""; intent_id = "h1"
    d = orch._finish_decision(
        _env("给我写2000字卢唐同人文"),
        __import__("whale_core.core.runtime.turn_orchestration", fromlist=["_Decision"])._Decision(
            route="chat", plan=_HeavyPlan()))
    assert d.reply == "KIMI"

    class _LightPlan:
        kind = "chat"; heavy = False; task_id = ""; intent_id = "l1"
    d2 = orch._finish_decision(
        _env("你好"),
        __import__("whale_core.core.runtime.turn_orchestration", fromlist=["_Decision"])._Decision(
            route="chat", plan=_LightPlan()))
    assert d2.reply == "fast"


def _env(text):
    from whale_core.core.runtime import InputEnvelope
    return InputEnvelope(conversation_id="c1", text=text, source="test")
