"""Explicit shell routing + chat skips intent LLM; fuzzy toolish may call it."""
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


def test_chat_and_time_skip_intent_llm_even_when_enabled():
    router = SearchIntentRouter(model=_Boom(), enable_llm=True, tools=_Tools())
    for text in ("你好啊", "现在几点", "今天心情怎么样", "聊聊天"):
        plan = json.loads(router.plan(text))
        assert plan["kind"] == "chat", text


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


def test_content_tracking_phrases_reach_llm_router():
    """「最新话什么时候更新」类无动词的追更句式 → 进模糊工具意图（LLM 兜底）。"""
    from whale_core.core.intent_router import _FUZZY_TOOLISH
    assert _FUZZY_TOOLISH.search("来自深渊最新话什么时候更新")
    assert _FUZZY_TOOLISH.search("这个番更新到第几话了")
    # 普通闲聊不触发（不进 LLM 省一轮）
    assert not _FUZZY_TOOLISH.search("今天天气真不错")
    assert not _FUZZY_TOOLISH.search("我吃完饭了")


def test_fanart_request_reaches_llm_router():
    """2026-11-05 生产实证2：「我要看娜娜奇和米蒂的同人」无 画/生成 动词，
    曾走闲聊→模型空许诺画图。现在必须进模糊工具意图。"""
    from whale_core.core.intent_router import _FUZZY_TOOLISH
    assert _FUZZY_TOOLISH.search("同人也行，我要看娜娜奇和米蒂的同人")
    assert _FUZZY_TOOLISH.search("我想看她们俩的同人图")
    assert _FUZZY_TOOLISH.search("有她们的同人画吗")
    # 纯闲聊/阅读语境不触发画图路由
    assert not _FUZZY_TOOLISH.search("我在看漫画")
    assert not _FUZZY_TOOLISH.search("今天天气真不错")
