"""G3 合批 / epoch / 分段测试（下一阶段计划 §5、MSG-01/02）。"""

from __future__ import annotations

import time

from whale_core.life.batching import (
    MessageBatcher, bump_epoch, current_epoch, epoch_fresh)
from whale_core.life.channels import Incoming
from whale_core.life.segments import split_reply
from whale_core.memory.store import InMemoryStore


def _inc(conv, sender, text, mid):
    return Incoming(channel="qq", account=sender, conversation=conv,
                    message_id=mid, text=text, transport_authenticated=True)


# ---------------------------------------------------------------------------
# 合批
# ---------------------------------------------------------------------------


def test_same_sender_burst_merged():
    got = []
    b = MessageBatcher(got.append, window_private_s=0.15)
    b.offer(_inc("private:u1", "10001", "第一句", "m1"))
    b.offer(_inc("private:u1", "10001", "第二句", "m2"))
    b.offer(_inc("private:u1", "10001", "第三句", "m3"))
    time.sleep(0.5)
    assert len(got) == 1
    batch = got[0]
    assert batch.texts == ("第一句", "第二句", "第三句")
    assert batch.message_ids == ("m1", "m2", "m3")
    assert batch.text == "第一句\n第二句\n第三句"
    b.stop()


def test_different_senders_never_merged():
    got = []
    b = MessageBatcher(got.append, window_group_s=0.12)
    b.offer(_inc("group:1", "10001", "A 说", "m1"))
    b.offer(_inc("group:1", "10002", "B 说", "m2"))
    time.sleep(0.4)
    assert len(got) == 2
    senders = {batch.sender_id for batch in got}
    assert senders == {"10001", "10002"}
    b.stop()


def test_batcher_stop_flushes_nothing_lost():
    got = []
    b = MessageBatcher(got.append, window_private_s=60)
    b.offer(_inc("private:u1", "10001", "别急", "m1"))
    assert b.flush_all() == 1
    assert len(got) == 1
    b.stop()


# ---------------------------------------------------------------------------
# epoch
# ---------------------------------------------------------------------------


def test_epoch_supersede():
    store = InMemoryStore()
    e1 = bump_epoch(store, "qq:qq_group:group:1")
    assert epoch_fresh(store, "qq:qq_group:group:1", e1)
    bump_epoch(store, "qq:qq_group:group:1")  # 新消息到达
    assert not epoch_fresh(store, "qq:qq_group:group:1", e1)  # 旧计划失效
    assert epoch_fresh(store, "qq:qq_group:group:1", current_epoch(store, "qq:qq_group:group:1"))


# ---------------------------------------------------------------------------
# 分段
# ---------------------------------------------------------------------------


def test_split_reply_defaults_single():
    assert split_reply("一句话") == ("一句话",)
    assert split_reply("") == ()


def test_split_reply_paragraphs_capped():
    text = "第一段。\n\n第二段。\n\n第三段。\n\n第四段。"
    assert len(split_reply(text)) <= 3
    assert len(split_reply(text, is_group=True)) <= 2
    joined = "".join(split_reply(text))
    for part in ("第一段", "第二段", "第三段", "第四段"):
        assert part in joined  # 内容不丢


def test_split_never_breaks_code_or_url():
    code = "看这个：\n\n```python\nprint(1)\n\nprint(2)\n```"
    assert split_reply(code) == (code,)
    url = "链接 https://example.com/" + "a" * 200 + " 在这里\n\n别的一段"
    assert split_reply(url) == (url,)


def test_split_long_single_paragraph():
    text = "。".join(f"第{i}句是一个很长的句子用来凑长度" for i in range(20)) + "。"
    assert len(text) > 240
    segs = split_reply(text)
    assert 1 < len(segs) <= 3
    assert "".join(segs).replace("\n\n", "") == text


# ---------------------------------------------------------------------------
# issue #17：epoch 入队即生效 / flush 异常可观测 / stop 排水不丢消息
# ---------------------------------------------------------------------------


def _ev(conv, sender, text, mid):
    return Incoming(channel="qq", conversation=conv, account=sender,
                    text=text, message_id=mid)


def test_epoch_bumps_at_accept_not_flush():
    """消息接受即 bump epoch（尚在合批窗口内也生效）——旧回复基线立即过期。"""
    from whale_core.life.batching import current_epoch
    store = InMemoryStore()
    seen = []
    b = MessageBatcher(seen.append, window_private_s=0.2, store=store)
    assert current_epoch(store, "private:u1") == 0
    b.offer(_ev("private:u1", "u1", "第一句", "m1"))
    # 未 flush，epoch 已生效
    assert current_epoch(store, "private:u1") == 1
    b.offer(_ev("private:u1", "u1", "第二句", "m2"))
    assert current_epoch(store, "private:u1") == 2
    b.stop(drain=True)
    assert len(seen) == 1                        # 同发送者合为一回合
    assert seen[0].epoch == 2                    # 批次 epoch = 冲刷时刻当前值
    assert seen[0].texts == ("第一句", "第二句")


def test_flush_callback_error_is_observable_not_silent():
    """回调抛错：不炸线程、不静默——on_error 收到（宿主可接死信/指标）。"""
    errors = []

    def bad(_batch):
        raise RuntimeError("boom")

    b = MessageBatcher(bad, window_private_s=0.1,
                       on_error=lambda key, exc: errors.append((key, exc)))
    b.offer(_ev("private:u1", "u1", "hi", "m1"))
    deadline = time.time() + 3
    while not errors and time.time() < deadline:
        time.sleep(0.02)
    assert errors and isinstance(errors[0][1], RuntimeError)
    assert errors[0][0] == ("private:u1", "u1")
    b.stop(drain=False)


def test_stop_drains_pending_by_default():
    """stop() 默认先排水：停机不静默丢消息。"""
    store = InMemoryStore()
    seen = []
    b = MessageBatcher(seen.append, window_private_s=30.0, store=store)
    b.offer(_ev("private:u1", "u1", "别丢我", "m1"))
    b.stop()  # drain=True 默认
    assert len(seen) == 1 and seen[0].texts == ("别丢我",)


def test_stop_without_drain_is_explicit_bounded_cancel():
    """drain=False 显式有界取消：清空待处理，之后 offer 不再接受。"""
    seen = []
    b = MessageBatcher(seen.append, window_private_s=30.0)
    b.offer(_ev("private:u1", "u1", "x", "m1"))
    b.stop(drain=False)
    assert seen == []
    b.offer(_ev("private:u1", "u1", "y", "m2"))
    time.sleep(0.05)
    assert seen == []
