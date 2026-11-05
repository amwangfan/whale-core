"""瞬时时间过滤：报时不进长期记忆；习惯/计划保留。"""
from whale_core.memory.transient import (
    is_time_query, is_transient_time_only, scrub_transient_time,
    scrub_episode_summary,
)


def test_time_query_detection():
    assert is_time_query("现在几点？")
    assert is_time_query("现在几点了")
    assert is_time_query("今天几号")
    assert not is_time_query("帮我查下明天天气？")
    assert not is_time_query("我一般凌晨一点睡")


def test_scrub_keeps_habits_and_plans():
    assert scrub_transient_time("用户常熬夜，喜欢猫。") == "用户常熬夜，喜欢猫。"
    assert "开会" in scrub_transient_time("下午三点开会别忘了")
    assert scrub_transient_time("我每天七点起床") == "我每天七点起床"


def test_scrub_drops_clock_statements():
    assert scrub_transient_time("凌晨快一点啦，零点四十九\n这么晚还没睡呀") == ""
    kept = scrub_transient_time("两点十五啦，凌晨…你怎么还没睡呀")
    assert "还没睡" in kept
    assert "两点十五" not in kept
    morning = scrub_transient_time(
        "早上好呀……等等，现在都快一点了，你这时差有点乱哦")
    assert "早上好" in morning
    assert "一点" not in morning


def test_episode_summary_drops_time_only_turns():
    assert scrub_episode_summary(
        "用户：现在几点？\nEnxi：凌晨快一点啦，零点四十九") == ""
    kept = scrub_episode_summary(
        "用户：早上好啊！\nEnxi：早上好呀……等等，现在都快一点了，你这时差有点")
    assert kept.startswith("用户：早上好")
    assert "一点" not in kept


def test_is_transient_time_only():
    assert is_transient_time_only("现在几点？")
    assert is_transient_time_only("凌晨快一点啦\n这么晚还没睡呀")
    assert not is_transient_time_only("用户常熬夜")
