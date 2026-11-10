"""2026-11-10 引用/近期图可见性（_quoted_context / _recent_peer_image）单元测试。

owner 实证：机器人看不到「引用的是哪句」、没进上下文的图只有 [pic]。
修复：引用标注进文本 + 被引用/上一条带图消息的图片经 msgimg 索引读回。
"""
from __future__ import annotations

import time

from whale_core.life.channels import OwnerEvent
from whale_core.life.qq_workflow import QqInboundWorkflow
from whale_core.memory.store import InMemoryStore
from whale_core.memory.timeline import GroupTimelineStore

GROUP = "group:1124744261"
PNG = b"\x89PNG-fake-bytes"


class _Brain:
    def reply(self, text, **kw):
        return "收到"


def _wf(store):
    wf = QqInboundWorkflow(store, _Brain())
    wf._artifact_reader = lambda aid: (_Rec(), store.get(f"blob:{aid}"))
    return wf


class _Rec:
    content_type = "image/png"


def _plant_image(store, mid):
    """模拟 intake 持久化：msgimg:<mid> → artifact 索引 + 字节仓。"""
    store.put(f"blob:a-{mid}", PNG)
    store.put(f"msgimg:{mid}", {"artifact": f"a-{mid}", "mime": "image/png"})


def _ev(**kw):
    base = dict(owner_id="owner", channel="qq", conversation=GROUP,
                message_id="m-cur", text="看看这个", sender_id="10001",
                sender_uid="u_a", send_time=str(time.time()))
    base.update(kw)
    return OwnerEvent(**base)


def test_group_quote_resolves_text_and_image():
    store = InMemoryStore()
    tl = GroupTimelineStore(store)
    _plant_image(store, "m-old")
    ts = str(time.time() - 30)
    tl.append(GROUP, message_id="m-old", sender_id="10002", text="[pic]",
              send_time=ts)
    store.put("namecache:g1124744261:10002", {"name": "霜桐", "at": time.time()})
    wf = _wf(store)
    ev = _ev(reply_to=("10002", "u_b", "m-old", "9"))
    note, images = wf._quoted_context(ev)
    assert note == "（引用了霜桐的消息：「[pic]」）"
    assert images == ((PNG, "image/png"),)


def test_group_quote_miss_is_silent():
    store = InMemoryStore()
    wf = _wf(store)
    note, images = wf._quoted_context(_ev(reply_to=("1", "u", "m-gone", "1")))
    assert note == "" and images == ()


def test_private_quote_resolves_from_recent_ring():
    store = InMemoryStore()
    conv = "private:u_x"
    store.put(f"recentmsg:{conv}", [
        {"mid": "m1", "sender": "2440542747", "text": "第一句话", "ts": 1},
        {"mid": "m2", "sender": "2440542747", "text": "第二句话", "ts": 2},
    ])
    wf = _wf(store)
    ev = _ev(conversation=conv, sender_id="2440542747",
             reply_to=("2440542747", "u_x", "m1", "3"))
    note, images = wf._quoted_context(ev)
    assert note == "（引用了你的消息：「第一句话」）"
    assert images == ()


def test_private_quote_of_bot_falls_back_to_last_assistant():
    store = InMemoryStore()
    conv = "private:u_x"
    from whale_core.memory.history import append_turn
    append_turn(store, conv, "问", "我之前的回答")
    wf = _wf(store)
    ev = _ev(conversation=conv, sender_id="3563425362",
             reply_to=("3899713055", "u_bot", "m-bot", "3"))  # 引用机器人
    note, _ = wf._quoted_context(ev)
    assert note == "（引用了我的消息：「我之前的回答」）"


def test_recent_peer_image_attaches_within_window():
    """无引用时：同一发送者 3 分钟内的上一条带图消息补进 images。"""
    store = InMemoryStore()
    tl = GroupTimelineStore(store)
    _plant_image(store, "m-pic")
    ts = str(time.time() - 60)
    tl.append(GROUP, message_id="m-pic", sender_id="10001", text="[pic]",
              send_time=ts)
    tl.append(GROUP, message_id="m-other", sender_id="10002", text="哈哈哈",
              send_time=str(time.time() - 10))
    wf = _wf(store)
    images = wf._recent_peer_image(_ev())
    assert images == ((PNG, "image/png"),)


def test_recent_peer_image_skips_when_current_has_attachment_or_expired():
    store = InMemoryStore()
    tl = GroupTimelineStore(store)
    _plant_image(store, "m-pic")
    tl.append(GROUP, message_id="m-pic", sender_id="10001", text="[pic]",
              send_time=str(time.time() - 60))
    wf = _wf(store)
    # 当前消息自带图 → 不补
    ev = _ev(attachments=({"kind": "pic", "file_path": "/x.png"},))
    assert wf._recent_peer_image(ev) == ()
    # 超出 3 分钟窗口 → 不补
    store2 = InMemoryStore()
    tl2 = GroupTimelineStore(store2)
    _plant_image(store2, "m-old")
    tl2.append(GROUP, message_id="m-old", sender_id="10001", text="[pic]",
               send_time=str(time.time() - 600))
    wf2 = _wf(store2)
    assert wf2._recent_peer_image(_ev()) == ()


def test_no_artifact_reader_degrades_silently():
    """未注入 artifact reader（旧装配）→ 图片解析静默缺席，引用文本仍可用。"""
    store = InMemoryStore()
    tl = GroupTimelineStore(store)
    tl.append(GROUP, message_id="m-old", sender_id="10002", text="原文",
              send_time=str(time.time()))
    wf = QqInboundWorkflow(store, _Brain())  # 不注入 reader
    note, images = wf._quoted_context(_ev(reply_to=("10002", "u", "m-old", "1")))
    assert "原文" in note and images == ()
    assert wf._recent_peer_image(_ev()) == ()


def test_quote_id_space_mismatch_falls_back_to_sender_recency():
    """2026-11-10 实证：reply 元素 origMsgId 与 SSE message_id 是两套编号
    （72057595837904458 vs 7706526182405080679）——id 查不到时按
    「被引用者 uin + 最近一条」回落，图片同回落。"""
    store = InMemoryStore()
    tl = GroupTimelineStore(store)
    _plant_image(store, "7706525627976383913")  # SSE 侧 id
    tl.append(GROUP, message_id="7706525627976383913", sender_id="3510294135",
              text="[pic]", send_time=str(time.time() - 30))
    store.put("namecache:g1124744261:3510294135", {"name": "霜桐", "at": time.time()})
    wf = _wf(store)
    # reply_to 带的是另一套编号 → 直接 id 查不到 → uin+就近回落命中
    ev = _ev(reply_to=("3510294135", "u_iZ8", "72057595837904458", "11805"))
    note, images = wf._quoted_context(ev)
    assert "霜桐" in note and images == ((PNG, "image/png"),)


def test_quote_fallback_picks_latest_image_from_quoted_sender():
    """被引用者发过多张图：回落取最近的一张。"""
    store = InMemoryStore()
    _plant_image(store, "m-a")
    _plant_image(store, "m-b")
    store.put("msgimg:m-a", {"artifact": "a-m-a", "mime": "image/png",
                             "conv": GROUP, "sender": "10002", "at": time.time() - 50})
    store.put("msgimg:m-b", {"artifact": "a-m-b", "mime": "image/png",
                             "conv": GROUP, "sender": "10002", "at": time.time() - 10})
    # 另一个会话的图不混进来
    store.put("msgimg:m-c", {"artifact": "a-m-c", "mime": "image/png",
                             "conv": "group:999", "sender": "10002",
                             "at": time.time()})
    tl = GroupTimelineStore(store)
    tl.append(GROUP, message_id="m-a", sender_id="10002", text="[pic]",
              send_time=str(time.time() - 50))
    tl.append(GROUP, message_id="m-b", sender_id="10002", text="[pic]",
              send_time=str(time.time() - 10))
    store.put("blob:a-m-a", b"OLD")
    store.put("blob:a-m-b", b"NEW")
    store.put("blob:a-m-c", b"OTHER")
    wf = _wf(store)
    note, images = wf._quoted_context(
        _ev(reply_to=("10002", "u_b", "72057000000000001", "1")))
    assert images == ((b"NEW", "image/png"),)
    assert "[pic]" in note or "图片" in note  # 文本回落命中原文占位即可
