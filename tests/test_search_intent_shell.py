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


def test_weather_without_location_still_searches_user_text():
    """无位置时仍走搜索（允许口述城市）；不硬编码上海。"""
    router = SearchIntentRouter(tools=None, enable_llm=False)
    plan = json.loads(router.plan(
        "成都天气", context="当前位置：未知（未提供城市或坐标）"))
    assert plan["kind"] == "task"
    assert plan["slots"]["query"] == "成都天气"
    assert "上海" not in plan["slots"]["query"]
