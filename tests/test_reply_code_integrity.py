"""Code examples must survive the complete reply planning/length pipeline."""

import pytest

from whale_core.life.group_length import soft_trim_text
from whale_core.life.message_text import fenced_parts, has_code_fence
from whale_core.life.response_planner import clean_reply_text, plan_reply
from whale_core.life.segments import split_reply


@pytest.mark.parametrize("fence", ["```", "~~~", "````"])
@pytest.mark.parametrize("closed", [True, False])
def test_code_markers_do_not_become_reply_intents(fence, closed):
    code = (f"{fence}text\n◆\n[[稍后补充:示例]]\n[[表情:开心]]\n"
            "[[沉默]]\n\n/api/v1/artifacts/example\n")
    code += fence if closed else "末尾"
    plan = plan_reply(code, is_group=True, max_segments=3, soft_ceiling=20)
    assert plan.bubbles == (code,)
    assert plan.planner == "fallback"
    assert not plan.silent
    assert plan.follow_up == plan.reaction_tag == ""
    assert clean_reply_text(code) == code


@pytest.mark.parametrize("fence", ["```", "~~~", "````"])
def test_real_markers_outside_code_still_work(fence):
    # 真实围栏： opener 独占一行（0-3 空格缩进），块内 ◆/[[…]] 都是代码内容。
    code = f"{fence}text\n◆ [[稍后补充:示例]]\n{fence}"
    text = f"开始◆\n{code}\n◆结束[[稍后补充:真实任务]][[表情:开心]]"
    plan = plan_reply(text, is_group=True, max_segments=3)
    assert plan.bubbles == ("开始", code, "结束")
    assert plan.planner == "model"
    assert plan.follow_up == "真实任务"
    assert plan.reaction_tag == "开心"


def test_inline_backtick_fence_after_marker_still_supported():
    """反引号保留原内联兼容：◆ 后同行的 ``` 仍是代码围栏（既有模型输出形态）。"""
    code = "```text\n◆ [[稍后补充:示例]]\n```"
    text = f"开始◆{code}\n◆结束"
    plan = plan_reply(text, is_group=True, max_segments=3)
    assert plan.bubbles == ("开始", code, "结束")
    assert plan.follow_up == ""
    assert plan.planner == "model"


def test_inline_tilde_after_marker_is_not_a_fence():
    """2026-11-17 PR#89 审查结论：◆ 后同行的 ~~~ 是普通语气符，不再当围栏。"""
    text = "好呀◆~~~只是语气~~~◆真的"
    assert not has_code_fence(text)
    plan = plan_reply(text, is_group=True, max_segments=3)
    assert plan.planner == "model"
    assert plan.bubbles == ("好呀", "~~~只是语气~~~", "真的")


def test_tilde_opener_requires_line_start():
    """has_code_fence 与 fenced_parts 共用同一起始规则：~~~ 只在物理行首
    （允许 0-3 空格缩进）才算代码围栏 opener；行中语气符不算。"""
    assert not has_code_fence("好呀~~~")
    assert not has_code_fence("a ~~~ b")
    assert not has_code_fence("好呀◆~~~只是语气")
    assert not has_code_fence("文字\n    ~~~\nx")   # 4 空格缩进不认
    assert has_code_fence("~~~")
    assert has_code_fence("文字\n~~~python\nx = 1")
    assert has_code_fence("文字\n   ~~~\nx = 1")     # 0-3 空格缩进认
    # 同一识别规则：fenced_parts 与 has_code_fence 判定一致（软裁不再误跳）
    assert [code for code, _ in fenced_parts("好呀~~~")] == [False]
    parts = list(fenced_parts("文字\n   ~~~\nx = 1"))
    assert [code for code, _ in parts] == [False, True]
    assert "".join(p for _, p in parts) == "文字\n   ~~~\nx = 1"


def test_plain_tilde_tone_text_is_not_a_code_fence():
    """PR#89 审查最小复现：普通语气 ~~~ 不得吞掉分气泡/标记剥离/媒体净化。"""
    text = ("好呀~~~ ◆ 另外一件事 [[稍后补充:待会]] "
            "https://demo.oss-cn-test.aliyuncs.com/demo.png")
    assert not has_code_fence(text)
    plan = plan_reply(text, is_group=True, max_segments=3)
    assert plan.planner == "model"
    assert plan.follow_up == "待会"
    assert len(plan.bubbles) == 2
    assert plan.bubbles[0] == "好呀~~~"
    assert plan.bubbles[1].startswith("另外一件事")
    assert "[图片]" in plan.bubbles[1] and "http" not in plan.bubbles[1]
    assert clean_reply_text(text).split("\n")[0] == "好呀~~~"


@pytest.mark.parametrize("tilde", ["~~~", "~~~~"])
def test_tilde_tone_run_keeps_normal_processing(tilde):
    """普通语气 ~~~ / ~~~~：分气泡与 follow-up 提取照常。"""
    text = f"好呀{tilde} ◆ 另外一件事[[稍后补充:待会]]"
    plan = plan_reply(text, is_group=True, max_segments=3)
    assert plan.bubbles == (f"好呀{tilde}", "另外一件事")
    assert plan.follow_up == "待会"
    assert plan.planner == "model"


def test_tilde_tone_does_not_disable_group_soft_trim():
    """普通语气 ~~~ 不得让群聊长度软裁失效（has_code_fence 必须同规则）。"""
    text = "好呀~~~ " + "这是一段普通聊天。" * 30
    assert not has_code_fence(text)
    assert len(soft_trim_text(text, 72)) <= 72
    plan = plan_reply(text, is_group=True, max_segments=3, soft_ceiling=72)
    assert plan.bubbles and all(len(b) <= 72 for b in plan.bubbles)


@pytest.mark.parametrize("indent", ["", " ", "  ", "   "])
def test_tilde_fence_at_line_start_with_small_indent_still_protected(indent):
    """真实波浪线围栏：物理行首 0-3 空格缩进仍受保护，块内标记不剥。"""
    code = f"{indent}~~~python\n◆ [[稍后补充:示例]]\n{indent}~~~"
    text = f"看代码：\n{code}"
    assert has_code_fence(text)
    plan = plan_reply(text, is_group=True, max_segments=3)
    assert len(plan.bubbles) == 1
    assert code in plan.bubbles[0]
    assert plan.follow_up == ""
    assert "◆" in plan.bubbles[0]


def test_tilde_fence_indented_four_spaces_is_plain_text():
    """4 空格及以上缩进的 ~~~ 不是围栏 opener（与 0-3 空格规则对应）。"""
    text = "说明：\n    ~~~只是缩进的普通文本\n◆ [[稍后补充:甲]]"
    assert not has_code_fence(text)
    plan = plan_reply(text, is_group=True, max_segments=3)
    assert plan.follow_up == "甲"


def test_tilde_fence_crlf_block_still_protected():
    """CRLF 换行下的真实波浪线围栏：opener/闭合都在行首，块内标记保留。"""
    code = "~~~\r\n◆ [[稍后补充:示例]]\r\n~~~"
    text = f"开始◆\r\n{code}\r\n◆结束"
    assert has_code_fence(text)
    plan = plan_reply(text, is_group=True, max_segments=3)
    assert plan.follow_up == ""
    assert plan.bubbles[0] == "开始"
    assert any("[[稍后补充:示例]]" in b and "◆" in b for b in plan.bubbles)


def test_tilde_shorter_close_does_not_close_longer_opener():
    """长波浪线围栏只能被同字符且不短于 opener 的行闭合（与反引号同规则）。"""
    code = "~~~~text\n~~~\n◆ [[表情:开心]]\n~~~~"
    assert has_code_fence(code)
    plan = plan_reply(code, is_group=True, max_segments=3)
    assert plan.bubbles == (code,)
    assert plan.reaction_tag == ""


def test_short_inner_fence_does_not_close_long_outer_fence():
    code = "````markdown\n```\n◆ [[表情:开心]]\n```\n````"
    plan = plan_reply(code, is_group=True, max_segments=3)
    assert plan.bubbles == (code,)
    assert plan.reaction_tag == ""


@pytest.mark.parametrize("fence", ["```", "~~~"])
def test_group_soft_limit_does_not_cut_code(fence):
    code = f"{fence}python\n" + "print('hello')\n" * 30 + fence
    assert soft_trim_text(code, 72) == code
    assert split_reply(code, is_group=True) == (code,)
    plan = plan_reply(code, is_group=True, max_segments=3, soft_ceiling=72)
    assert plan.bubbles == (code,)


def test_normal_group_newlines_and_soft_limit_still_apply():
    assert split_reply("甲\n乙\n丙", is_group=True) == ("甲", "乙", "丙")
    assert len(soft_trim_text("这是一段普通聊天。" * 30, 72)) <= 72


def test_multiple_blocks_scrub_only_surrounding_text():
    first = "```\n[[表情:示例]]\n```"
    second = "~~~\n/api/v1/artifacts/example\n~~~"
    text = f"{first}\n[[表情:开心]]\n{second}\n/api/v1/artifacts/real"
    plan = plan_reply(text, is_group=False, max_segments=3)
    assert plan.reaction_tag == "开心"
    assert first in plan.bubbles[0] and second in plan.bubbles[0]
    assert plan.bubbles[0].endswith("[图片]")
