"""issue #23：ResponsePlanner 标记解析与回落行为。"""

from __future__ import annotations

from whale_core.life.response_planner import plan_reply


def test_plain_text_falls_back_to_rule_split():
    plan = plan_reply("一句话。", is_group=True, max_segments=2)
    assert plan.planner == "fallback" and plan.bubbles == ("一句话。",)


def test_model_bubble_separator_plans_multi_bubble():
    text = "先回这句\n◆\n再补一句\n◆\n最后一句"
    plan = plan_reply(text, is_group=True, max_segments=2)
    assert plan.planner == "model"
    # 群聊上限 2：尾部合并进最后一个气泡（内容不丢）
    assert len(plan.bubbles) == 2
    assert "再补一句" in plan.bubbles[1] and "最后一句" in plan.bubbles[1]


def test_silence_marker_only_honored_at_start():
    plan = plan_reply("[[沉默]] 不说了", is_group=False, max_segments=3)
    assert plan.silent and plan.bubbles == ()
    # 正文中间的 [[沉默]] 不生效（防伪造）
    plan2 = plan_reply("我觉得 [[沉默]] 这个说法不对", is_group=False,
                       max_segments=3)
    assert not plan2.silent and plan2.bubbles


def test_follow_up_marker_extracted_and_stripped():
    plan = plan_reply("先这样，结果出来我再说。[[稍后补充:下载任务完成]]",
                      is_group=False, max_segments=3)
    assert plan.follow_up == "下载任务完成"
    assert all("稍后补充" not in b for b in plan.bubbles)
    # 只有声明没有正文 → 沉默 + 意图保留
    plan2 = plan_reply("[[稍后补充:任务x完成]]", is_group=False, max_segments=3)
    assert plan2.silent and plan2.follow_up == "任务x完成"


def test_empty_and_whitespace_are_silent():
    assert plan_reply("", is_group=False, max_segments=3).silent
    assert plan_reply("  \n ", is_group=True, max_segments=2).silent


def test_long_text_without_markers_uses_fallback_caps():
    text = "。".join(f"第{i}句很长的句子用来凑长度" for i in range(20)) + "。"
    plan = plan_reply(text, is_group=True, max_segments=2)
    assert plan.planner == "fallback" and len(plan.bubbles) <= 2


def test_inline_bubble_marker_no_leak():
    """2026-11-01 事故形态：模型常写「◆ 接着说」（同行文本），旧解析只认
    独占一行 → 标记与空行泄漏进气泡。行首 ◆ 一律视为新气泡起点。"""
    plan = plan_reply("在呢在呢。\n\n◆ 现在是谁不理谁呀", is_group=False,
                      max_segments=3)
    assert plan.planner == "model"
    assert plan.bubbles == ("在呢在呢。", "现在是谁不理谁呀")
    assert all("◆" not in b and "\n\n" not in b for b in plan.bubbles)


def test_leading_bubble_marker_stripped():
    plan = plan_reply("◆ 开头就带标记\n◆\n第二句", is_group=False, max_segments=3)
    assert plan.bubbles == ("开头就带标记", "第二句")


def test_blank_lines_collapsed_but_code_fence_kept():
    plan = plan_reply("甲。\n\n◆ 乙\n\n\n丙", is_group=False, max_segments=3)
    assert plan.bubbles == ("甲。", "乙\n丙")
    code = "看这个：\n◆ ```python\nprint(1)\n\nprint(2)\n```"
    plan2 = plan_reply(code, is_group=False, max_segments=3)
    assert plan.bubbles and "\n\n" in plan2.bubbles[-1]  # 代码块空行保留


def test_mid_line_marker_is_not_separator():
    """行中间的 ◆ 不当分隔（非行首），原样保留。"""
    plan = plan_reply("这个符号 ◆ 在句子中间", is_group=False, max_segments=3)
    assert plan.planner == "fallback"
    assert plan.bubbles == ("这个符号 ◆ 在句子中间",)
