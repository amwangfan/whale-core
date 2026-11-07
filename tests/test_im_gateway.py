"""IM Gateway + QQ dry-run provider 测试（规划 §7）。

覆盖：入站解析→路由→命令识别；私聊绑定与 /new 重建；群聊审批拒绝；
审批全生命周期（创建→批准→一次性核销→旧 id 复用拒绝→过期）；
投递重试与 dead-letter；dryrun provider 全程零真实 HTTP（socket 守护）。
"""
from __future__ import annotations

import json
import socket
from datetime import datetime, timedelta, timezone

import pytest

from whale_core.integrations.im import (
    DECISION_APPROVED,
    DECISION_EXPIRED,
    DECISION_REJECTED,
    ApprovalBridge,
    DeliveryService,
    ImGateway,
    OutboundMessage,
)
from whale_core.integrations.im.models import attachment_dict, parse_command
from whale_core.integrations.im.providers import (
    QqDryRunProvider,
    RecordingOneBotTransport,
)
from whale_core.integrations.im.router import InMemorySessionBindingStore, SessionRouter

BOT_ID = "999999"
OWNER_UIN = 10001
OTHER_UIN = 20002
GROUP_ID = 30003


# ---------------------------------------------------------------------------
# 测试道具
# ---------------------------------------------------------------------------

class FakeClock:
    """可推进时钟（审批过期测试用）。"""

    def __init__(self) -> None:
        self.t = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += timedelta(seconds=seconds)


class FakeTaskControl:
    """TaskControlPort 测试替身：记录调用、返回固定答复。"""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def stop(self, conversation_id: str) -> str:
        self.calls.append(("stop", conversation_id))
        return "已停止当前任务"

    def steer(self, conversation_id: str, instruction: str) -> str:
        self.calls.append(("steer", conversation_id, instruction))
        return "已按指示调整"

    def status(self, conversation_id: str) -> str:
        self.calls.append(("status", conversation_id))
        return "当前任务运行中"

    def new_session(self, target_id: str, conversation_id: str) -> str:
        self.calls.append(("new_session", target_id, conversation_id))
        return "新会话已建立"

    def set_model(self, conversation_id: str, model: str) -> str:
        self.calls.append(("set_model", conversation_id, model))
        return f"模型已切换为 {model}"


class World:
    """一套装配好的 gateway + provider + 记录替身。"""

    def __init__(self, *, salt: str = "testsalt", with_approval: bool = True,
                 clock: FakeClock | None = None,
                 approval_jsonl=None) -> None:
        self.provider = QqDryRunProvider(bot_id=BOT_ID, salt=salt)
        self.task_control = FakeTaskControl()
        self.handled: list[tuple] = []  # (ImMessage, conversation_id)
        self.gateway = ImGateway(
            task_control=self.task_control,
            message_handler=lambda msg, cid: self.handled.append((msg, cid)))
        self.gateway.register_provider(self.provider)
        self.resolutions: list = []
        self.clock = clock
        if with_approval:
            self.approvals = ApprovalBridge(
                self.gateway.delivery,
                owner_target_id=self.owner_target_id(),
                jsonl_path=approval_jsonl,
                now_fn=clock if clock is not None else None)
            self.approvals.add_listener(self.resolutions.append)
            self.gateway.approval_bridge = self.approvals
        else:
            self.approvals = None
        self._msg_seq = 0

    # -- 事件构造与驱动 --------------------------------------------------------

    def event(self, text, *, user_id=OWNER_UIN, message_type="private",
              group_id=None, self_id=int(BOT_ID), as_segments=False):
        """构造一条 OneBot v11 message 事件 dict。"""
        self._msg_seq += 1
        ev = {
            "post_type": "message",
            "message_type": message_type,
            "self_id": self_id,
            "user_id": user_id,
            "message_id": self._msg_seq,
            "message": ([{"type": "text", "data": {"text": text}}]
                        if as_segments else text),
            "time": 1794052801,
        }
        if message_type == "group":
            ev["group_id"] = group_id if group_id is not None else GROUP_ID
        return ev

    def say(self, text, **kwargs):
        """注入一条入站消息并 pump；返回产出的 ImMessage（被丢弃则为 None）。"""
        captured: list = []
        old_handler = self.provider._handler
        self.provider.set_inbound_handler(
            lambda msg: (captured.append(msg), old_handler(msg)))
        self.provider.feed(self.event(text, **kwargs))
        self.provider.pump()
        self.provider.set_inbound_handler(old_handler)
        return captured[0] if captured else None

    def owner_target_id(self) -> str:
        return self.provider._alias(f"private:{OWNER_UIN}")

    def other_target_id(self) -> str:
        return self.provider._alias(f"private:{OTHER_UIN}")

    def group_target_id(self) -> str:
        return self.provider._alias(f"group:{GROUP_ID}")

    def sent_texts(self) -> list[str]:
        """已记录出站调用的纯文本拼接列表。"""
        out = []
        for call in self.provider.transport.calls:
            for seg in call["params"].get("message", []):
                if seg.get("type") == "text":
                    out.append(seg["data"]["text"])
        return out


# ---------------------------------------------------------------------------
# 入站解析 → 路由 → 命令识别
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("command,expected_call,expected_reply", [
    ("/stop", ("stop",), "已停止当前任务"),
    ("/status", ("status",), "当前任务运行中"),
    ("/steer 改成分三步做", ("steer", "改成分三步做"), "已按指示调整"),
    ("/model deepseek-v4", ("set_model", "deepseek-v4"), "模型已切换为 deepseek-v4"),
])
def test_commands_dispatch_to_task_control(command, expected_call, expected_reply):
    world = World()
    msg = world.say(command)
    assert msg is not None and msg.text == command
    assert len(world.task_control.calls) == 1
    call = world.task_control.calls[0]
    assert call[0] == expected_call[0]
    assert call[1] == world.gateway.router.binding_for(msg.target_id)
    if len(expected_call) > 1:
        assert call[2] == expected_call[1]
    assert expected_reply in world.sent_texts()


def test_command_new_replies_and_rebinds():
    world = World()
    msg = world.say("hello")  # 先建立绑定
    first = world.gateway.router.binding_for(msg.target_id)
    assert first
    world.say("/new")
    names = [c[0] for c in world.task_control.calls]
    assert names == ["new_session"]
    assert "新会话已建立" in world.sent_texts()


def test_unknown_command_gets_hint():
    world = World()
    world.say("/dance")
    assert any("未知命令 /dance" in t for t in world.sent_texts())
    assert world.task_control.calls == []


def test_command_missing_args_gets_usage():
    world = World()
    world.say("/steer")
    world.say("/model")
    texts = world.sent_texts()
    assert any("用法：/steer" in t for t in texts)
    assert any("用法：/model" in t for t in texts)
    assert world.task_control.calls == []


def test_plain_message_routed_with_binding():
    world = World()
    msg = world.say("今天天气怎么样")
    assert len(world.handled) == 1
    got_msg, conversation_id = world.handled[0]
    assert got_msg is msg
    assert conversation_id == world.gateway.router.binding_for(msg.target_id)
    assert world.task_control.calls == []


def test_parse_reuse_drops_self_and_non_message():
    world = World()
    # bot 自己发的消息：parse_onebot_message 一律丢弃
    assert world.say("hello", user_id=int(BOT_ID)) is None
    # 非 message 事件直接喂队列验证 pump 不计数
    world.provider.feed({"post_type": "notice", "notice_type": "group_upload"})
    assert world.provider.pump() == 0
    # 段数组形式也能解析
    msg = world.say("段数组文本", as_segments=True)
    assert msg is not None and msg.text == "段数组文本"


def test_target_id_alias_hides_platform_id():
    world = World()
    msg = world.say("hi")
    assert msg.target_id.startswith("tgt_")
    assert str(OWNER_UIN) not in msg.target_id
    # provider 内部可反查（出站寻址依赖它）
    assert world.provider.resolve(msg.target_id) == f"private:{OWNER_UIN}"


# ---------------------------------------------------------------------------
# 私聊绑定与 /new 重建
# ---------------------------------------------------------------------------

def test_private_binding_stable_and_isolated_per_target():
    world = World()
    a1 = world.say("第一条")
    a2 = world.say("第二条")
    b1 = world.say("别人的", user_id=OTHER_UIN)
    router = world.gateway.router
    assert router.binding_for(a1.target_id) == router.binding_for(a2.target_id)
    assert router.binding_for(b1.target_id) != router.binding_for(a1.target_id)


def test_new_rebuilds_binding_and_task_control_notified():
    world = World()
    msg = world.say("hello")
    old = world.gateway.router.binding_for(msg.target_id)
    world.say("/new")
    new = world.gateway.router.binding_for(msg.target_id)
    assert new and new != old
    name, target_id, conversation_id = world.task_control.calls[0]
    assert (name, target_id, conversation_id) == ("new_session", msg.target_id, new)
    # /new 之后的普通消息走新会话
    world.say("新会话第一条")
    assert world.handled[-1][1] == new


def test_router_custom_store_and_id_factory():
    store = InMemorySessionBindingStore()
    ids = iter(["conv-A", "conv-B"])
    router = SessionRouter(store, id_factory=lambda: next(ids))
    assert router.resolve("t1") == "conv-A"
    assert router.resolve("t1") == "conv-A"  # 幂等
    assert router.reset("t1") == "conv-B"
    assert store.get("t1") == "conv-B"
    assert router.binding_for("t2") is None  # 只查不建


# ---------------------------------------------------------------------------
# 群聊纪律
# ---------------------------------------------------------------------------

def test_group_message_marked_group_and_routed():
    world = World()
    msg = world.say("群里唠嗑", message_type="group")
    assert msg is not None
    assert msg.chat_type == "group" and msg.is_group
    assert world.handled[-1][0] is msg  # 群消息可正常路由（通知/对话）


def test_group_command_rejected_to_private():
    world = World()
    world.say("/status", message_type="group")
    assert world.task_control.calls == []
    last = world.provider.transport.calls[-1]
    assert last["action"] == "send_group_msg"
    assert any("请私聊" in t for t in world.sent_texts())


def test_group_approval_reply_rejected_and_not_consumed():
    world = World()
    request = world.approvals.create_request(
        task_id="task_1", device_id="d" * 32, action="device.shell",
        summary="在设备上执行 ls")
    world.say("批准", message_type="group")  # 群里的批准必须被拒
    assert any("审批请私聊" in t for t in world.sent_texts())
    assert world.resolutions == []                      # 未核销
    assert world.approvals.pending_for(request.target_id) != []  # 仍 pending


# ---------------------------------------------------------------------------
# 审批全生命周期
# ---------------------------------------------------------------------------

def test_approval_create_delivers_card_to_owner_private():
    world = World()
    request = world.approvals.create_request(
        task_id="task_1", device_id="d" * 32, action="device.shell",
        summary="在设备上执行 ls")
    assert request.approval_id.startswith("aprv_")
    assert request.target_id == world.owner_target_id()
    call = world.provider.transport.calls[-1]
    assert call["action"] == "send_private_msg"
    assert call["params"]["user_id"] == OWNER_UIN
    text = world.sent_texts()[-1]
    assert "在设备上执行 ls" in text and request.approval_id in text
    assert "task_1" in text and "批准" in text and "拒绝" in text


def test_approval_approve_then_one_time_consumed():
    world = World()
    request = world.approvals.create_request(
        task_id="task_1", device_id="d" * 32, action="device.shell",
        summary="执行 ls")
    world.say("批准")
    assert len(world.resolutions) == 1
    resolution = world.resolutions[0]
    assert resolution.approval_id == request.approval_id
    assert resolution.decision == DECISION_APPROVED
    assert resolution.resolved_by == world.owner_target_id()
    assert any("已批准" in t for t in world.sent_texts())
    # 重复回复第二次 → 「已处理」；resolver 不再触发
    world.say("批准")
    assert len(world.resolutions) == 1
    assert any("已处理或已过期" in t for t in world.sent_texts())


def test_approval_reject_flow():
    world = World()
    world.approvals.create_request(task_id="task_2", device_id="d" * 32,
                                   action="device.install", summary="安装应用")
    world.say("拒绝")
    assert world.resolutions[0].decision == DECISION_REJECTED
    assert any("已拒绝" in t for t in world.sent_texts())


def test_old_approval_id_cannot_approve_new_action():
    world = World()
    first = world.approvals.create_request(
        task_id="task_1", device_id="d" * 32, action="a.one", summary="动作一")
    world.say("批准")  # 核销 first
    second = world.approvals.create_request(
        task_id="task_2", device_id="d" * 32, action="a.two", summary="动作二")
    # 显式引用旧 id：只得到「已处理」，新审批不受影响
    world.say(f"批准 {first.approval_id}")
    assert [r.approval_id for r in world.resolutions] == [first.approval_id]
    assert any("旧审批号不能重复使用" in t for t in world.sent_texts())
    assert world.approvals.pending_for(second.target_id) == [second]
    # 新动作要用自己的新 id 另行批准
    world.say(f"批准 {second.approval_id}")
    assert [r.approval_id for r in world.resolutions] == [
        first.approval_id, second.approval_id]


def test_approval_unknown_id_and_cross_target_refused():
    world = World()
    request = world.approvals.create_request(
        task_id="task_1", device_id="d" * 32, action="a.one", summary="动作一")
    world.say("批准 aprv_0123456789ABCDEFGHJKMNPQ")  # 不存在的 id
    assert any("审批号无效" in t for t in world.sent_texts())
    # 别人私聊显式引用主人的审批号：不允许跨会话核销
    world.say(f"批准 {request.approval_id}", user_id=OTHER_UIN)
    assert any("不属于当前会话" in t for t in world.sent_texts())
    assert world.resolutions == []


def test_approval_lazy_expiry_on_reply():
    clock = FakeClock()
    world = World(clock=clock)
    world.approvals.create_request(task_id="task_1", device_id="d" * 32,
                                   action="a.one", summary="会过期的动作")
    clock.advance(301)  # 默认 ttl 300s
    world.say("批准")
    assert world.resolutions[0].decision == DECISION_EXPIRED
    assert any("已过期" in t for t in world.sent_texts())


def test_approval_expire_overdue_sweep():
    clock = FakeClock()
    world = World(clock=clock)
    request = world.approvals.create_request(
        task_id="task_1", device_id="d" * 32, action="a.one", summary="动作一")
    assert world.approvals.expire_overdue() == []   # 未到期不清
    clock.advance(301)
    assert world.approvals.expire_overdue() == [request.approval_id]
    assert world.resolutions[0].decision == DECISION_EXPIRED
    assert world.approvals.expire_overdue() == []   # 幂等


def test_approval_jsonl_audit(tmp_path):
    audit = tmp_path / "approvals.jsonl"
    world = World(approval_jsonl=audit)
    world.approvals.create_request(task_id="task_1", device_id="d" * 32,
                                   action="a.one", summary="动作一")
    world.say("批准")
    rows = [json.loads(line) for line in audit.read_text().splitlines()]
    assert [r["event"] for r in rows] == ["created", "resolved"]
    assert rows[1]["decision"] == DECISION_APPROVED


# ---------------------------------------------------------------------------
# 投递层：重试与死信
# ---------------------------------------------------------------------------

def test_delivery_retries_with_backoff_then_succeeds():
    attempts: list[int] = []
    sleeps: list[float] = []

    def flaky(message):
        attempts.append(1)
        if len(attempts) < 3:
            raise RuntimeError("boom")

    delivery = DeliveryService(flaky, sleep_fn=sleeps.append)
    assert delivery.send_text("tgt_x", "hello") is True
    assert len(attempts) == 3                 # 第 3 次成功
    assert sleeps == [0.5, 1.0]               # 指数退避
    assert delivery.dead_letters() == []


def test_delivery_dead_letter_after_bounded_retries():
    sleeps: list[float] = []

    def always_fail(message):
        raise ConnectionError("down")

    delivery = DeliveryService(always_fail, sleep_fn=sleeps.append)
    ok = delivery.send(OutboundMessage(target_id="tgt_x", text="hi"))
    assert ok is False
    assert sleeps == [0.5, 1.0]               # 3 次尝试、2 次退避
    letters = delivery.dead_letters()
    assert len(letters) == 1
    assert letters[0].attempts == 3
    assert letters[0].error == "ConnectionError"
    assert letters[0].message.text == "hi"
    assert delivery.clear_dead_letters() == 1
    assert delivery.dead_letters() == []


def test_delivery_attachment_and_approval_entries():
    sent: list = []
    delivery = DeliveryService(sent.append, sleep_fn=lambda s: None)
    assert delivery.send_attachment("tgt_x", attachment_dict(
        attachment_id="att_1", type="image", mime="image/webp")) is True
    assert sent[0].attachments[0]["attachment_id"] == "att_1"
    world = World()
    request = world.approvals.create_request(
        task_id="task_9", device_id="d" * 32, action="a.b", summary="汇总入口")
    # send_approval 走统一入口（审批卡片文本化）
    assert delivery.send_approval("tgt_x", request) is True
    assert "审批请求" in sent[1].text


# ---------------------------------------------------------------------------
# dry-run provider：全程零真实 HTTP
# ---------------------------------------------------------------------------

def test_dryrun_provider_zero_network(monkeypatch):
    """socket 守护下跑完整链路：任何真实连接尝试都会炸掉测试。"""
    def forbidden(*args, **kwargs):
        raise AssertionError("dry-run provider 触网了！")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)

    world = World()
    world.say("/status")                                   # 命令 → 回复
    world.approvals.create_request(task_id="t", device_id="d" * 32,
                                   action="a", summary="审批投递")  # 出站
    world.say("批准")                                       # 核销 → 确认回复
    recorded = world.provider.drain_sent()
    assert len(recorded) == 3                               # 全部只被记录
    assert all(c["action"] == "send_private_msg" for c in recorded)
    assert world.provider.drain_sent() == []                # drain 清空


def test_recording_transport_jsonl_and_failure_injection(tmp_path):
    log = tmp_path / "sent.jsonl"
    transport = RecordingOneBotTransport(jsonl_path=log)
    provider = QqDryRunProvider(bot_id=BOT_ID, salt="s", transport=transport)
    gateway = ImGateway(message_handler=lambda m, c: None)
    gateway.register_provider(provider)
    provider.feed({"post_type": "message", "message_type": "private",
                   "self_id": int(BOT_ID), "user_id": OWNER_UIN,
                   "message_id": 1, "message": "hi", "time": 1794052801})
    provider.pump()
    target = provider._alias(f"private:{OWNER_UIN}")
    gateway.send_text(target, "落盘验证", reply_to="1")
    rows = [json.loads(line) for line in log.read_text().splitlines()]
    assert rows[0]["action"] == "send_private_msg"
    segs = rows[0]["params"]["message"]
    assert segs[0]["type"] == "reply" and segs[0]["data"]["id"] == "1"
    assert segs[1]["data"]["text"] == "落盘验证"

    # 发送失败注入：responder 抛错 → gateway 投递进死信
    def boom(action, params):
        raise RuntimeError("simulated send failure")

    failing = RecordingOneBotTransport(responder=boom)
    provider2 = QqDryRunProvider(bot_id=BOT_ID, salt="s", transport=failing)
    sleeps: list[float] = []
    delivery = DeliveryService(provider2.send, sleep_fn=sleeps.append)
    gateway2 = ImGateway(delivery=delivery)
    gateway2.register_provider(provider2)
    provider2.feed({"post_type": "message", "message_type": "private",
                    "self_id": int(BOT_ID), "user_id": OWNER_UIN,
                    "message_id": 1, "message": "hi"})
    provider2.pump()
    assert gateway2.send_text(provider2._alias(f"private:{OWNER_UIN}"), "x") is False
    assert len(delivery.dead_letters()) == 1
    # 失败的尝试也被记录（审计可见），但始终没有网络
    assert len(failing.calls) == 3


def test_provider_queue_shapes_and_drain():
    # 注入 list 队列也可用
    inbound = [{"post_type": "message", "message_type": "private",
                "self_id": int(BOT_ID), "user_id": OWNER_UIN,
                "message_id": 7, "message": "queued"}]
    provider = QqDryRunProvider(bot_id=BOT_ID, salt="s", inbound=inbound)
    seen: list = []
    provider.set_inbound_handler(seen.append)
    assert provider.pump() == 1
    assert seen[0].message_id == "7"
    assert provider.pump() == 0  # 空队列幂等
