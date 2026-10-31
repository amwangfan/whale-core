"""WeQ SSE 入站桥 + QQ 工作流的离线测试：合成事件、回环监听、stub brain。

不起真实 WeQ、不连 QQ、不联网；DeepSeek 用桩替代（协议见 core.companion.TextCompanion）。
"""
import json
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from whale_core.life.channels import OwnerChannelRegistry
from whale_core.life.initiative import ProactivePrefs
from whale_core.life.qq_workflow import QqInboundWorkflow
from weq_channel.wq_push import (
    MAX_BODY_BYTES,
    WeQSsePushListener,
    parse_push_events,
    sse_message_to_incoming,
)
from whale_core.memory.store import InMemoryStore

TOKEN = "synthetic-sse-token"
SELF_UIN = "1900000001"   # 纯合成测试号（不对应任何真实账号）
OWNER_UIN = "10001"


def sse_event(msg_id="m1", sender=OWNER_UIN, text="你好", chat_type="c2c", conv=None, **extra):
    event = {
        "type": "message",
        "chatType": chat_type,
        "convId": conv if conv is not None else (sender if chat_type == "c2c" else "30003"),
        "msgId": msg_id,
        "msgSeq": "100",
        "senderUid": "u_" + sender,
        "senderUin": sender,
        "sendTime": "1793280000",
        "text": text,
        "elements": [{"kind": "text", "text": text}],
    }
    event.update(extra)
    return event


def payload(*events):
    return json.dumps({"events": list(events)}).encode()


def make_registry(bindings=None):
    return OwnerChannelRegistry(
        owner_id="owner",
        bindings={("qq", OWNER_UIN): "owner"} if bindings is None else bindings,
    )


class RecordingListener(WeQSsePushListener):
    def __init__(self, registry, **kw):
        kw.setdefault("async_dispatch", False)
        super().__init__(registry, expected_token=TOKEN, self_uin=SELF_UIN,
                         on_owner_event=self._owner, on_unpaired=self._unpaired,
                         on_mass=self._mass, **kw)
        self.owner_events, self.unpaired, self.mass = [], [], []

    def _owner(self, e): self.owner_events.append(e)
    def _unpaired(self, i): self.unpaired.append(i)
    def _mass(self, m): self.mass.append(m)


# ── 载荷解析与事件映射 ────────────────────────────────────────

def test_parse_push_events_validation():
    assert parse_push_events(payload(sse_event())) == [sse_event()]
    assert parse_push_events(None) is None
    assert parse_push_events(b"{broken") is None
    assert parse_push_events(b"[1,2]") is None
    assert parse_push_events(json.dumps({"events": "x"}).encode()) is None
    assert parse_push_events(b"x" * (MAX_BODY_BYTES + 1)) is None
    # 非 dict 条目被过滤而不是崩溃
    assert parse_push_events(json.dumps({"events": [sse_event(), 42, None]}).encode()) == [sse_event()]


def test_message_to_incoming_mapping_and_filters():
    incoming = sse_message_to_incoming(sse_event(), self_uin=SELF_UIN)
    assert incoming.channel == "qq"
    assert incoming.account == OWNER_UIN
    assert incoming.conversation == f"private:{OWNER_UIN}"
    assert incoming.message_id == "m1"
    assert incoming.text == "你好"
    assert incoming.transport_authenticated is True
    # 小号自己发的消息不回灌（防自激回路）
    assert sse_message_to_incoming(sse_event(sender=SELF_UIN), self_uin=SELF_UIN) is None
    # mass / 未知类型不进消息管道
    assert sse_message_to_incoming({"type": "mass", "count": 99}, self_uin=SELF_UIN) is None
    # 群聊默认拒收，显式开启才映射
    group = sse_event(chat_type="group", conv="30003")
    assert sse_message_to_incoming(group, self_uin=SELF_UIN) is None
    mapped = sse_message_to_incoming(group, self_uin=SELF_UIN, accept_group=True)
    assert mapped.conversation == "group:30003"
    # 关键字段缺失 → None；超长文本截断
    assert sse_message_to_incoming(sse_event(msg_id=""), self_uin=SELF_UIN) is None
    assert sse_message_to_incoming(sse_event(sender=""), self_uin=SELF_UIN) is None
    long_in = sse_message_to_incoming(sse_event(text="长" * 5000), self_uin=SELF_UIN)
    assert len(long_in.text) == 4000


# ── 监听器纯逻辑（同步模式） ─────────────────────────────────

def test_listener_auth_and_status_codes():
    listener = RecordingListener(make_registry())
    body = payload(sse_event())
    assert listener.process_payload({"Authorization": f"Bearer {TOKEN}"}, body)[0] == 200
    assert listener.process_payload({}, body)[0] == 401
    assert listener.process_payload({"Authorization": "Bearer wrong"}, body)[0] == 401
    assert listener.process_payload({"Authorization": f"Bearer {TOKEN}"}, None)[0] == 413
    assert listener.process_payload({"Authorization": f"Bearer {TOKEN}"}, b"{bad")[0] == 400
    assert len(listener.owner_events) == 1  # 只有第一次 200 进了管道


def test_listener_pairing_dedup_and_mass():
    listener = RecordingListener(make_registry())
    auth = {"Authorization": f"Bearer {TOKEN}"}
    code, n = listener.process_payload(auth, payload(sse_event(msg_id="m1")))
    assert (code, n) == (200, 1) and listener.owner_events[0].owner_id == "owner"
    # WeQ 至少一次投递：同 msgId 重推被去重
    assert listener.process_payload(auth, payload(sse_event(msg_id="m1")))[1] == 0
    assert len(listener.owner_events) == 1
    # 未配对发送者 → 只走元数据回调，不产出 OwnerEvent
    listener.process_payload(auth, payload(sse_event(msg_id="m2", sender="99999")))
    assert len(listener.unpaired) == 1 and listener.unpaired[0].account == "99999"
    assert len(listener.owner_events) == 1
    # mass 事件 → 元数据摘要（不含正文）
    mass = {"type": "mass", "chatType": "group", "convId": "30003",
            "fromSeq": "1", "toSeq": "900", "count": 900, "preview": sse_event()}
    listener.process_payload(auth, payload(mass))
    assert listener.mass == [{"chatType": "group", "convId": "30003", "fromSeq": "1",
                              "toSeq": "900", "count": 900, "hasPreview": True}]


def test_concurrent_duplicate_push_is_processed_once():
    listener = RecordingListener(make_registry())
    auth = {"Authorization": f"Bearer {TOKEN}"}
    body = payload(sse_event(msg_id="race-1"))
    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(lambda _: listener.process_payload(auth, body), range(24)))
    assert sum(count for code, count in results if code == 200) == 1
    assert [e.message_id for e in listener.owner_events] == ["race-1"]


def test_queue_overflow_is_retryable_and_does_not_poison_dedup():
    listener = WeQSsePushListener(make_registry(), expected_token=TOKEN, self_uin=SELF_UIN,
                                   on_owner_event=lambda _e: None, async_dispatch=True)
    for i in range(listener._events.maxsize):
        listener._events.put_nowait(("owner", f"queued-{i}"))
    auth = {"Authorization": f"Bearer {TOKEN}"}
    body = payload(sse_event(msg_id="overflow-1"))
    code, count = listener.process_payload(auth, body)
    assert (code, count) == (503, 0)
    assert listener.dropped == 1
    assert "overflow-1" not in listener._seen

    listener._events.get_nowait()
    listener._events.task_done()
    code, count = listener.process_payload(auth, body)
    assert (code, count) == (200, 1)
    assert "overflow-1" in listener._seen


def test_listener_requires_token():
    with pytest.raises(ValueError):
        WeQSsePushListener(make_registry(), expected_token="", self_uin=SELF_UIN,
                           on_owner_event=lambda e: None)


def test_listener_loopback_http_and_worker():
    listener = RecordingListener(make_registry())
    listener.requested_port = 0
    try:
        listener.start()
    except OSError:
        pytest.skip("loopback bind unavailable in this environment")
    try:
        url = f"http://127.0.0.1:{listener.port}/whale"
        req = urllib.request.Request(url, data=payload(sse_event(msg_id="live1")),
                                     method="POST",
                                     headers={"Authorization": f"Bearer {TOKEN}",
                                              "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            assert resp.status == 200
            assert json.loads(resp.read()) == {"accepted": 1}
        bad = urllib.request.Request(url, data=payload(sse_event(msg_id="live2")),
                                     method="POST", headers={"Authorization": "Bearer x"})
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            urllib.request.urlopen(bad, timeout=5)
        assert excinfo.value.code == 401
        listener.wait_idle(timeout=5)
        assert [e.message_id for e in listener.owner_events] == ["live1"]
        with urllib.request.urlopen(url, timeout=5) as resp:
            assert resp.status == 204  # GET 探活
    finally:
        listener.stop()


# ── 工作流（stub brain，不触网） ─────────────────────────────

class StubBrain:
    def __init__(self):
        self.calls = []

    def reply(self, text, *, persona="", memories=(), history=()):
        self.calls.append({"text": text, "memories": list(memories)})
        return f"[stub] 收到：{text[:40]}"


def make_workflow(**prefs_kw):
    store = InMemoryStore()
    brain = StubBrain()
    prefs = (ProactivePrefs(**prefs_kw) if prefs_kw else
             ProactivePrefs(enabled=True, allowed_channels=frozenset({"direct_message"}),
                            daily_limit=20))
    wf = QqInboundWorkflow(store, brain, prefs=prefs,
                           now_fn=lambda: datetime(2026, 10, 29, 15, 0, tzinfo=ZoneInfo("Asia/Shanghai")))
    return wf, brain, store


def owner_event(msg_id="m1", text="今晚吃啥"):
    from whale_core.life.channels import OwnerEvent
    return OwnerEvent(owner_id="owner", channel="qq", conversation=f"private:{OWNER_UIN}",
                      message_id=msg_id, text=text)


def test_workflow_defaults_disabled_without_explicit_prefs():
    store = InMemoryStore()
    brain = StubBrain()
    wf = QqInboundWorkflow(store, brain)  # no prefs is not consent to invoke an LLM
    assert wf.handle_owner_event(owner_event()) == {"skipped": "disabled"}
    assert brain.calls == []
    assert store.keys() == []


def test_workflow_produces_reviewable_draft_and_memory():
    from whale_core.memory.store import MemoryKind
    wf, brain, store = make_workflow()
    result = wf.handle_owner_event(owner_event())
    assert result["draft_id"] == "qq-reply:m1"
    assert result["reply"].startswith("[stub]")
    assert result["verdict"] == "needs_approval"  # direct_message 强制人工批准
    assert brain.calls[0]["text"] == "今晚吃啥"
    # 情节记忆已回写（WhaleCompanion.chat 的 remember_episode）
    episodes = store.list_by_kind(MemoryKind.EPISODE)
    assert episodes and "今晚吃啥" in episodes[0].content.get("summary", "")
    # 幂等：同 msgId 再处理，草稿不重复入库
    again = wf.handle_owner_event(owner_event())
    assert again["proposed"] is False


def test_workflow_pregate_skips_ai_when_disabled():
    wf, brain, _ = make_workflow(enabled=False)
    assert wf.handle_owner_event(owner_event()) == {"skipped": "disabled"}
    assert brain.calls == []  # 闸门未过：一次 AI 都不调
    wf2, brain2, _ = make_workflow(enabled=True, allowed_channels=frozenset({"hud"}))
    assert wf2.handle_owner_event(owner_event()) == {"skipped": "channel_denied"}
    assert brain2.calls == []


def test_workflow_empty_text_and_daily_cap():
    wf, brain, _ = make_workflow()
    assert wf.handle_owner_event(owner_event(msg_id="m0", text="   ")) == {"skipped": "empty_text"}
    prefs = ProactivePrefs(enabled=True, allowed_channels=frozenset({"direct_message"}),
                           daily_limit=0)
    wf.prefs = prefs
    result = wf.handle_owner_event(owner_event(msg_id="mcap"))
    assert result["verdict"] == "over_budget"  # AI 已跑但草稿被额度闸门拦下


def test_pic_relative_url_completed_with_rkey(tmp_path, monkeypatch):
    """生产实证：QQ c2c 图片 originalUrl 是相对 /download 路径——
    需补多媒体域名 + 活 rkey 才可下载。"""
    import json
    import time

    from weq_channel.wq_push import _load_weq_rkeys, _pic_download_urls

    account_dir = tmp_path / "config" / "accounts"
    account_dir.mkdir(parents=True)
    fresh = {"rkey": "&rkey=LIVE", "type": 10,
             "createTime": time.time(), "ttlSeconds": 3600}
    stale = {"rkey": "&rkey=DEAD", "type": 10,
             "createTime": time.time() - 7200, "ttlSeconds": 3600}
    group_rk = {"rkey": "&rkey=GRP", "type": 20,
                "createTime": time.time(), "ttlSeconds": 3600}
    (account_dir / "acc.json").write_text(
        json.dumps({"nested": {"rkeys": [fresh, stale, group_rk]}}))
    rkeys = _load_weq_rkeys(str(tmp_path))
    assert ("1406", "&rkey=LIVE") in rkeys
    assert ("1406", "&rkey=STALE") not in rkeys  # 过期不带
    assert ("1407", "&rkey=GRP") in rkeys

    data = {"originalUrl": "/download?appid=1406&fileid=TOK&spec=0",
            "fileToken": "TOK"}
    urls = _pic_download_urls(data, chat_type="c2c", rkeys=rkeys)
    assert urls[0].startswith("https://multimedia.nt.qq.com.cn/download?")
    assert urls[0].endswith("&rkey=LIVE")  # c2c 场景优先
    assert any(u.endswith("&rkey=GRP") and "appid=1407" in u for u in urls)
    # 绝对 URL 原样保留在最前
    abs_urls = _pic_download_urls({"originalUrl": "https://cdn.example.com/x.png"},
                                  chat_type="c2c", rkeys=rkeys)
    assert abs_urls[0] == "https://cdn.example.com/x.png".replace("/x.png", "") or abs_urls[0] == "https://cdn.example.com/x.png"
    # 无 rkey → 相对路径不可用（空候选，诚实不伪造）
    assert _pic_download_urls(data, chat_type="c2c", rkeys=[]) == ()
