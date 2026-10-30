"""skills.py（Skill 层，roadmap §4.3）离线测试：Markdown 解析 + 模板 + 门控执行，全 fake。"""

from __future__ import annotations

import pytest

from whale_core.core.runtime import ToolRegistry, ToolSpec
from whale_core.skills import (
    Skill,
    SkillError,
    SkillResult,
    SkillStep,
    parse_skill,
    run_skill,
)

SEARCH_AND_SUMMARIZE = """\
# skill: search-and-summarize
description: 搜索并总结
## step: search
tool: search_web
args:
  query: "{input}"
save_as: results
## step: read
tool: fetch_url
args:
  url: "{steps.search.first_url}"
save_as: page
"""


def _tools(*specs: ToolSpec) -> ToolRegistry:
    registry = ToolRegistry()
    for spec in specs:
        registry.register(spec)
    return registry


# ---------- 解析 ----------

def test_parse_full_example():
    skill = parse_skill(SEARCH_AND_SUMMARIZE)
    assert skill.name == "search-and-summarize"
    assert skill.description == "搜索并总结"
    assert [s.name for s in skill.steps] == ["search", "read"]
    search, read = skill.steps
    assert search.tool == "search_web"
    assert search.args == {"query": "{input}"}  # 成对引号被剥掉
    assert search.save_as == "results"
    assert read.tool == "fetch_url"
    assert read.args == {"url": "{steps.search.first_url}"}
    assert read.save_as == "page"


def test_parse_dash_and_plain_arg_forms():
    skill = parse_skill(
        "# skill: two-forms\n"
        "## step: s\n"
        "tool: t\n"
        "args:\n"
        "  - a: \"1\"\n"
        "  b: '2'\n"
        "save_as: out\n"
    )
    assert skill.steps[0].args == {"a": "1", "b": "2"}
    assert skill.steps[0].save_as == "out"


def test_parse_missing_name_has_line_number():
    with pytest.raises(SkillError, match="line 1"):
        parse_skill("description: 没有名字\n## step: s\ntool: t\n")


def test_parse_empty_name_has_line_number():
    with pytest.raises(SkillError, match="line 1"):
        parse_skill("# skill:\n## step: s\ntool: t\n")


def test_parse_missing_tool_reports_step_line():
    text = "# skill: x\n## step: s\nargs:\n  q: \"1\"\n"
    with pytest.raises(SkillError) as excinfo:
        parse_skill(text)
    assert "line 2" in str(excinfo.value)  # step 头所在行
    assert "tool" in str(excinfo.value)


def test_parse_bad_structure_has_line_number():
    text = "# skill: x\n## step: s\ntool: t\nbogus line here\n"
    with pytest.raises(SkillError, match="line 4"):
        parse_skill(text)


def test_parse_bad_args_entry_has_line_number():
    text = "# skill: x\n## step: s\ntool: t\nargs:\n  justavalue\n"
    with pytest.raises(SkillError, match="line 5"):
        parse_skill(text)


def test_parse_duplicate_step_rejected():
    text = "# skill: x\n## step: s\ntool: t\n## step: s\ntool: t\n"
    with pytest.raises(SkillError, match="line 4"):
        parse_skill(text)


def test_parse_no_steps_rejected():
    with pytest.raises(SkillError, match="no steps"):
        parse_skill("# skill: x\ndescription: 空壳\n")


def test_parse_unexpected_content_before_first_step():
    with pytest.raises(SkillError, match="line 2"):
        parse_skill("# skill: x\n随便一句散文\n## step: s\ntool: t\n")


# ---------- 模板 ----------

def test_input_placeholder_substituted():
    seen: list[dict] = []
    tools = _tools(ToolSpec(name="echo", capability="test.echo",
                            handler=lambda a: seen.append(dict(a)) or "ok"))
    skill = parse_skill("# skill: s\n## step: e\ntool: echo\nargs:\n  q: \"{input}\"\n")
    result = run_skill(skill, tools, input_text="上海天气")
    assert result.status == "succeeded"
    assert seen == [{"q": "上海天气"}]


def test_steps_text_chaining():
    tools = _tools(
        ToolSpec(name="up", capability="test.up", handler=lambda a: "HELLO"),
        ToolSpec(name="wrap", capability="test.wrap", handler=lambda a: f"[{a['v']}]"),
    )
    skill = parse_skill(
        "# skill: chain\n"
        "## step: one\ntool: up\n"
        "## step: two\ntool: wrap\nargs:\n  v: \"{steps.one.text}\"\nsave_as: final\n"
    )
    result = run_skill(skill, tools)
    assert result.status == "succeeded"
    assert result.outputs["final"] == "[HELLO]"


def test_first_url_extracted_from_output():
    seen: list[dict] = []
    tools = _tools(
        ToolSpec(name="search", capability="test.search",
                 handler=lambda a: "结果见 https://example.com/a 和 https://example.com/b"),
        ToolSpec(name="fetch", capability="test.fetch",
                 handler=lambda a: seen.append(dict(a)) or "page"),
    )
    skill = parse_skill(
        "# skill: s\n## step: f\ntool: search\n## step: g\ntool: fetch\n"
        "args:\n  url: \"{steps.f.first_url}\"\n"
    )
    result = run_skill(skill, tools)
    assert result.status == "succeeded"
    assert seen == [{"url": "https://example.com/a"}]  # 只取首个 URL


def test_unknown_step_reference_fails():
    tools = _tools(ToolSpec(name="t", capability="test.t", handler=lambda a: "x"))
    skill = parse_skill(
        "# skill: s\n## step: a\ntool: t\nargs:\n  v: \"{steps.ghost.text}\"\n"
    )
    result = run_skill(skill, tools)
    assert result.status == "failed"
    assert "template" in result.receipts[-1]
    assert "ghost" in result.receipts[-1]


def test_forward_reference_fails():  # 只能引用已执行完的前序步骤
    tools = _tools(ToolSpec(name="t", capability="test.t", handler=lambda a: "x"))
    skill = parse_skill(
        "# skill: s\n"
        "## step: a\ntool: t\nargs:\n  v: \"{steps.b.text}\"\n"
        "## step: b\ntool: t\n"
    )
    result = run_skill(skill, tools)
    assert result.status == "failed"
    assert "template" in result.receipts[0]


def test_first_url_missing_fails():
    tools = _tools(ToolSpec(name="t", capability="test.t", handler=lambda a: "没有链接"))
    skill = parse_skill(
        "# skill: s\n## step: a\ntool: t\n## step: b\ntool: t\n"
        "args:\n  v: \"{steps.a.first_url}\"\n"
    )
    result = run_skill(skill, tools)
    assert result.status == "failed"
    assert "no http(s) URL" in result.receipts[-1]


# ---------- 执行 ----------

def test_two_step_skill_runs_end_to_end():
    calls: list[str] = []
    tools = _tools(
        ToolSpec(name="t1", capability="test.one", handler=lambda a: calls.append("t1") or "r1"),
        ToolSpec(name="t2", capability="test.two", handler=lambda a: calls.append("t2") or "r2"),
    )
    skill = parse_skill(
        "# skill: two\n## step: s1\ntool: t1\nsave_as: o1\n## step: s2\ntool: t2\nsave_as: o2\n"
    )
    result = run_skill(skill, tools)
    assert result.status == "succeeded"
    assert calls == ["t1", "t2"]  # 严格串行
    assert result.outputs == {"o1": "r1", "o2": "r2"}
    assert result.receipts == (
        "skill:two:step:s1:tool:t1:ok",
        "skill:two:step:s2:tool:t2:ok",
    )


def test_unknown_tool_fails():
    skill = parse_skill("# skill: s\n## step: a\ntool: nope\n")
    result = run_skill(skill, _tools())
    assert result.status == "failed"
    assert result.receipts[-1].endswith(":fail:unknown-tool")


def test_read_only_tool_runs_directly_without_gateway():
    tools = _tools(ToolSpec(name="ro", capability="test.ro", handler=lambda a: "data"))
    skill = parse_skill("# skill: s\n## step: a\ntool: ro\nsave_as: o\n")
    result = run_skill(skill, tools)  # 无网关也能跑 read_only
    assert result.status == "succeeded"
    assert result.outputs == {"o": "data"}


def test_handler_exception_does_not_crash():
    def boom(args):
        raise OSError("网络断了")
    tools = _tools(ToolSpec(name="bad", capability="test.bad", handler=boom))
    skill = parse_skill("# skill: s\n## step: a\ntool: bad\n")
    result = run_skill(skill, tools)
    assert result.status == "failed"
    assert result.receipts[-1].endswith(":fail:OSError")


def test_first_failure_stops_remaining_steps():
    calls: list[str] = []

    def boom(args):
        raise RuntimeError("炸了")
    tools = _tools(
        ToolSpec(name="bad", capability="test.bad", handler=boom),
        ToolSpec(name="later", capability="test.later",
                 handler=lambda a: calls.append("later") or "x"),
    )
    skill = parse_skill("# skill: s\n## step: a\ntool: bad\n## step: b\ntool: later\n")
    result = run_skill(skill, tools)
    assert result.status == "failed"
    assert calls == []  # 后续步骤未执行


# ---------- 风险门控 ----------

def _transaction_tool() -> ToolSpec:
    return ToolSpec(name="pay", capability="shop.pay", risk_class="transaction",
                    plugin_id="shop", action="pay",
                    handler=lambda a: "不该直接执行")


def test_transaction_tool_without_gateway_denied():
    skill = parse_skill("# skill: s\n## step: p\ntool: pay\n")
    result = run_skill(skill, _tools(_transaction_tool()))  # 无网关
    assert result.status == "denied"
    assert result.receipts[-1].endswith(":denied:no-gateway")


def test_risky_tool_without_plugin_mapping_denied():
    spec = ToolSpec(name="mail", capability="email.send", risk_class="communicate",
                    handler=lambda a: "不该执行")
    skill = parse_skill("# skill: s\n## step: m\ntool: mail\n")
    result = run_skill(skill, _tools(spec), gateway=object())
    assert result.status == "denied"
    assert result.receipts[-1].endswith(":denied:risk-without-plugin-mapping")


def test_transaction_tool_via_gateway_ok():
    seen: list = []

    class OkGateway:
        def dispatch(self, intent):
            seen.append(intent)
            return "plugin:shop:pay:ok:已扣款 30 元"

    tools = _tools(_transaction_tool())
    skill = parse_skill(
        "# skill: buy\n## step: p\ntool: pay\nargs:\n  item: \"{input}\"\nsave_as: receipt\n"
    )
    result = run_skill(skill, tools, gateway=OkGateway(), input_text="咖啡")
    assert result.status == "succeeded"
    assert result.outputs == {"receipt": "已扣款 30 元"}  # 只保留 :ok: 之后的载荷
    intent = seen[0]
    assert intent.kind == "plugin_action"
    # plugin_id/action 从 ToolSpec 取，不是 Skill 文本自声明
    assert intent.payload["plugin_id"] == "shop"
    assert intent.payload["action"] == "pay"
    assert intent.payload["args"] == {"item": "咖啡"}


def test_gateway_hold_reported_as_denied():  # Skill 不等待审批，如实归入 denied
    class HoldGateway:
        def dispatch(self, intent):
            return "plugin-hold:shop:pay"

    skill = parse_skill("# skill: s\n## step: p\ntool: pay\n")
    result = run_skill(skill, _tools(_transaction_tool()), gateway=HoldGateway())
    assert result.status == "denied"
    assert ":denied:plugin-hold:shop:pay" in result.receipts[-1]


def test_gateway_denied_propagates():
    class DenyGateway:
        def dispatch(self, intent):
            return "plugin-denied:shop:pay:not-approved"

    skill = parse_skill("# skill: s\n## step: p\ntool: pay\n")
    result = run_skill(skill, _tools(_transaction_tool()), gateway=DenyGateway())
    assert result.status == "denied"
    assert "not-approved" in result.receipts[-1]


# ---------- 输出截断与 save_as ----------

def test_output_truncated_to_output_chars():
    tools = _tools(ToolSpec(name="big", capability="test.big", handler=lambda a: "x" * 5000))
    skill = parse_skill("# skill: s\n## step: a\ntool: big\nsave_as: o\n")
    result = run_skill(skill, tools, output_chars=100)
    assert result.status == "succeeded"
    assert len(result.outputs["o"]) == 100


def test_truncated_output_feeds_next_step_template():
    tools = _tools(
        ToolSpec(name="big", capability="test.big", handler=lambda a: "y" * 500),
        ToolSpec(name="echo", capability="test.echo", handler=lambda a: a["v"]),
    )
    skill = parse_skill(
        "# skill: s\n## step: a\ntool: big\n## step: b\ntool: echo\n"
        "args:\n  v: \"{steps.a.text}\"\nsave_as: o\n"
    )
    result = run_skill(skill, tools, output_chars=50)
    assert result.outputs["o"] == "y" * 50  # 链式引用拿到的是截断后的文本


def test_outputs_only_contain_save_as_steps():
    tools = _tools(
        ToolSpec(name="t1", capability="test.one", handler=lambda a: "r1"),
        ToolSpec(name="t2", capability="test.two", handler=lambda a: "r2"),
    )
    skill = parse_skill(
        "# skill: s\n## step: a\ntool: t1\n## step: b\ntool: t2\nsave_as: keep\n"
    )
    result = run_skill(skill, tools)
    assert result.status == "succeeded"
    assert result.outputs == {"keep": "r2"}  # 无 save_as 的步骤不进 outputs


# ---------- 端到端：搜索 → 抓取 ----------

def test_search_then_fetch_end_to_end():
    """真实双步场景：fake 搜索返回含 URL 文本 → fetch 断言收到了该 URL。"""
    fetched: list[str] = []
    tools = _tools(
        ToolSpec(name="search_web", capability="web.search",
                 description="搜索网页",
                 handler=lambda a: f"关于「{a['query']}」的结果：https://news.example.com/a1 （摘要）"),
        ToolSpec(name="fetch_url", capability="web.fetch",
                 description="抓取网页",
                 handler=lambda a: fetched.append(a["url"]) or "正文：今天上海晴"),
    )
    skill = parse_skill(SEARCH_AND_SUMMARIZE)
    result = run_skill(skill, tools, input_text="上海天气")
    assert result.status == "succeeded"
    assert fetched == ["https://news.example.com/a1"]  # 抓的正是搜索抽出的首个 URL
    assert result.outputs["results"].startswith("关于「上海天气」")
    assert result.outputs["page"] == "正文：今天上海晴"
    assert len(result.receipts) == 2
    assert all(r.endswith(":ok") for r in result.receipts)
