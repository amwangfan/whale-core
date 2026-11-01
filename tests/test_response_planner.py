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


def test_multiple_follow_up_markers_all_stripped():
    """2026-11-01 生产实证：模型写两个 [[稍后补充]]，search 只剥第一个，
    第二个原样发进群。finditer+sub 全剥。"""
    plan = plan_reply(
        "我去画。[[稍后补充:生图结果出来后发出来]]◆ 等下哦[[稍后补充:再说]]",
        is_group=True, max_segments=3)
    assert plan.follow_up == "生图结果出来后发出来"
    assert all("[[" not in b and "]]" not in b for b in plan.bubbles)
    assert plan.bubbles == ("我去画。", "等下哦")


def test_unknown_double_bracket_marker_stripped():
    """任何残留 [[…]] 内部标记都不进气泡（纵深防御，不认标记名）。"""
    plan = plan_reply("甲。[[新标记:xyz]] 乙。", is_group=True, max_segments=2)
    assert plan.bubbles and all("[[" not in b for b in plan.bubbles)
    assert "甲。" in plan.bubbles[0] and "乙。" in plan.bubbles[0]


def test_marker_only_bubble_drops_to_silent_or_clean():
    plan = plan_reply("◆[[稍后补充:等任务]]", is_group=True, max_segments=2)
    assert all(b.strip() for b in plan.bubbles)
    assert all("[[" not in b for b in plan.bubbles)


def test_media_refs_become_image_placeholder():
    """2026-11-01「恶意链接」事件：模型把签名 OSS 链接/本地产物引用贴进正文，
    QQ 弹恶意链接警告。正文里的媒体引用一律转 [图片]（真图由系统另发）。"""
    oss = ("https://dashscope-463f.oss-accelerate.aliyuncs.com/1d/44/x.png"
           "?Expires=1&OSSAccessKeyId=LTAI&Signature=ab%2Fc")
    plan = plan_reply(f"画好啦 ◆ 给你看\n![小鲸鱼]({oss})",
                      is_group=True, max_segments=3)
    assert len(plan.bubbles) == 2
    assert "http" not in plan.bubbles[1] and "[图片]" in plan.bubbles[1]
    # 裸 OSS 链接与本地 artifact 引用同样净化
    plan2 = plan_reply(f"图在这 {oss} 看", is_group=True, max_segments=2)
    assert "http" not in plan2.bubbles[0] and "[图片]" in plan2.bubbles[0]
    plan3 = plan_reply("![图](/api/v1/artifacts/abc123)", is_group=True,
                       max_segments=2)
    assert plan3.bubbles == ("[图片]",)


def test_code_fence_keeps_markers_and_links():
    """代码围栏内的 [[…]]/链接是代码内容，不剥。"""
    text = "看这段：\n```\nx = '[[稍后补充:演示]]'\n```"
    plan = plan_reply(text, is_group=False, max_segments=3)
    assert any("[[稍后补充:等任务]]" not in b and "[[稍后补充:下载" not in b
               for b in plan.bubbles)
    assert any("x = '[[稍后补充:演示]]'" in b for b in plan.bubbles)


def test_mid_line_marker_also_separates():
    """句中夹带的 ◆ 同样是分隔（模型只从规划提示学到这个符号，出现即标记；
    「我在呢。◆ 八点起」生产实证形态）。"""
    plan = plan_reply("链路正常，我在呢。◆ 八点起还回学校", is_group=False,
                      max_segments=3)
    assert plan.planner == "model"
    assert plan.bubbles == ("链路正常，我在呢。", "八点起还回学校")
