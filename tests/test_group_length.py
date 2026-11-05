"""群聊长度软约束：peer 中位 + 句界软裁（不为了短而短）。"""

from whale_core.life.expression import derive_policy
from whale_core.life.group_length import (
    DEFAULT_MEDIAN, derive_group_length_norm, median_int, peer_char_lengths,
    soft_ceiling_for, soft_trim_bubbles, soft_trim_text,
)
from whale_core.life.response_planner import plan_reply


def _entry(text, *, is_self=False):
    return {"text": text, "is_self": is_self, "message_id": "x"}


def test_peer_lengths_skip_self_and_outliers():
    entries = [
        _entry("嗯"),  # too short
        _entry("今天天气不错啊"),
        _entry("哈哈哈哈"),
        _entry("我自己说的", is_self=True),
        _entry("http://example.com/very/long/path/only"),  # url stripped → empty/skip
        _entry("好的，那咱们晚点再聊这个事情吧。"),
    ]
    lens = peer_char_lengths(entries)
    assert all(n >= 2 for n in lens)
    assert len(lens) >= 3


def test_median_and_ceiling_sane():
    assert median_int([10, 20, 30]) == 20
    assert soft_ceiling_for(20) >= 72
    assert soft_ceiling_for(50) <= 160
    assert soft_ceiling_for(50) >= soft_ceiling_for(20)


def test_norm_defaults_when_few_samples():
    norm = derive_group_length_norm([_entry("嗨"), _entry("好")])
    assert norm.from_default is True
    assert norm.median == DEFAULT_MEDIAN
    assert "中位" in norm.hint()


def test_norm_uses_peer_median():
    entries = [_entry("短句一下")] * 3 + [_entry("这是稍微长一点的群聊消息内容")] * 4
    norm = derive_group_length_norm(entries)
    assert norm.from_default is False
    assert norm.sample_n >= 5
    assert 4 <= norm.median <= 40
    assert norm.soft_ceiling >= norm.median


def test_soft_trim_keeps_short_untouched():
    assert soft_trim_text("嗯嗯好的", 72) == "嗯嗯好的"


def test_soft_trim_cuts_at_sentence():
    long = "第一句说完了。第二句还很长很长很长很长很长很长很长很长很长很长。"
    out = soft_trim_text(long, 20)
    assert out.endswith("。")
    assert "第二句" not in out
    assert len(out) <= 20 or out == "第一句说完了。"


def test_soft_trim_bubbles_deflates_list():
    text = (
        "建议如下：\n"
        "- 第一点内容比较长\n"
        "- 第二点内容也比较长\n"
        "- 第三点还要再说\n"
        "- 第四点继续废话"
    )
    bubbles = soft_trim_bubbles((text,), soft_ceiling=80, max_segments=2)
    assert len(bubbles) == 1
    assert len(bubbles[0]) <= 80
    assert bubbles[0].count("\n") <= 1


def test_plan_reply_applies_soft_ceiling_in_group():
    essay = (
        "关于这个问题我想详细展开说一下。"
        "首先第一方面涉及很多细节需要慢慢解释。"
        "其次第二方面也同样复杂不能一笔带过。"
        "最后第三方面还要补充若干背景资料。"
    )
    plan = plan_reply(essay, is_group=True, max_segments=2, soft_ceiling=72)
    assert not plan.silent
    assert plan.bubbles
    assert all(len(b) <= 72 for b in plan.bubbles)


def test_plan_reply_private_ignores_soft_ceiling_by_default():
    essay = "私聊可以稍长一些。" * 20
    plan = plan_reply(essay, is_group=False, max_segments=3, soft_ceiling=0)
    assert "".join(plan.bubbles) == essay or len("".join(plan.bubbles)) >= 100


def test_expression_group_uses_peer_median_hint():
    policy = derive_policy("今天天气不错", is_group=True, peer_median=24)
    rendered = policy.render()
    assert "24" in rendered
    assert "中位" in rendered
    assert "像真人" not in rendered
    assert "碎" in rendered  # 别故意碎句


def test_expression_group_without_median_still_soft():
    policy = derive_policy("今天天气不错", is_group=True)
    assert "长文" in policy.render() or "平时的长度" in policy.render()
    assert "像真人" not in policy.render()


def test_expression_private_unchanged_default_empty():
    assert derive_policy("你好").render() == ""


def test_expression_group_gentle_tone_and_no_ai_dash():
    """owner 选 B：群侧温柔体贴，去客服/破折号腔；不堆「像真人」。"""
    policy = derive_policy("今晚吃啥", is_group=True, peer_median=18)
    rendered = policy.render()
    assert "温柔体贴" in rendered
    assert "少攻击" in rendered or "不互损" in rendered
    assert "客服" in rendered
    assert "破折号" in rendered
    assert "像真人" not in rendered


def test_expression_health_care_hint():
    policy = derive_policy("我感冒了好难受", is_group=True)
    assert policy.care_health is True
    assert "身体不适" in policy.render() or "关心" in policy.render()
    # 私聊生病也关心，并可略亲密
    priv = derive_policy("我发烧了", is_group=False)
    assert priv.care_health is True
    assert "温柔" in priv.render()
