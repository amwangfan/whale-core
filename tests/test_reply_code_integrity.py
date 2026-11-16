"""Code examples must survive the complete reply planning/length pipeline."""

import pytest

from whale_core.life.group_length import soft_trim_text
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
    code = f"{fence}text\n◆ [[稍后补充:示例]]\n{fence}"
    text = f"开始◆{code}\n◆结束[[稍后补充:真实任务]][[表情:开心]]"
    plan = plan_reply(text, is_group=True, max_segments=3)
    assert plan.bubbles == ("开始", code, "结束")
    assert plan.planner == "model"
    assert plan.follow_up == "真实任务"
    assert plan.reaction_tag == "开心"


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
