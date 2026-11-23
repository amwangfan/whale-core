"""WeQ backend 测试（channels.qq backend="weq"）。

照 DEVELOPMENT §3：ScriptedModel 注入模型，TestClient 打 webhook，WeQ Web tRPC
用 httpx.MockTransport 假实现（登录/cookie/sendElements/memberBriefs），
回执门闩（sequence>0）与端点校验逐项断言。全部占位符 id。
"""

import json
import time
import unittest

import httpx
from starlette.testclient import TestClient

from whale.channels.weq import WeQChannel, accept_send_outcome, validate_weq_url, weq_event_to_qq
from whale.kernel.errors import WhaleError
from whale.server.http import create_server

from helpers import make_app, scripted

WEQ_CFG = {"kind": "qq", "backend": "weq", "weq_web_url": "http://127.0.0.1:19000",
           "owner_qq": "10001", "bot_qq": "99999", "bot_uid": "u_bot",
           "accept_group": True, "allowed_groups": ["777"], "bubble_delay_ms": 0,
           "group_mode": "mention", "judge_interval_s": 0, "smart_cooldown_s": 120,
           "fullmode_grace_s": 0}


class FakeWeQWeb:
    """WeQ 内置 Web tRPC 的假实现：登录发 cookie，发送记调用，回执带真 sequence。
    完整模式窗口：默认就绪（allowed=ready=True）；setAutoAttachQq 写 False 后变关窗态。"""

    def __init__(self):
        self.sent = []
        self.seq = 0
        self.fail = None
        self.brief_calls = 0
        self.fm_allowed = True
        self.fm_ready = True
        self.fm_attach_calls = []       # setAutoAttachQq 的 enabled 序列
        self.fm_state_calls = 0
        self.fm_fail_attach = False     # True → setAutoAttachQq 回 500
        self.fm_never_ready = False     # True → attach 后 ready 仍 False（就绪超时场景）

    def handler(self, request):
        path = request.url.path
        if path == "/_auth/login":
            return httpx.Response(200, json={"ok": True}, headers={"set-cookie": "weq_session=s1; Path=/"})
        if path == "/trpc/bootstrap.getFullModeState":
            self.fm_state_calls += 1
            return httpx.Response(200, json={"result": {"data": {"json": {
                "allowed": self.fm_allowed, "ready": self.fm_ready}}}})
        if path == "/trpc/bootstrap.setAutoAttachQq":
            body = json.loads(request.content)
            if self.fm_fail_attach:
                return httpx.Response(500, json={"error": {"json": {"message": "boom"}}})
            enabled = bool(body["json"]["enabled"])
            self.fm_attach_calls.append(enabled)
            self.fm_allowed = enabled
            self.fm_ready = enabled and not self.fm_never_ready
            return httpx.Response(200, json={"result": {"data": {"json": {"ok": True}}}})
        if path == "/trpc/account.memberBriefs":
            self.brief_calls += 1
            return httpx.Response(200, json={"result": {"data": {"json": {"members": [
                {"uin": "30003", "card": "群名片A", "nick": "昵称A"}]}}}})
        if path == "/trpc/account.sendElements":
            body = json.loads(request.content)
            self.sent.append(body["json"])
            if self.fail == "timeout":
                raise httpx.ReadTimeout("slow", request=request)
            if self.fail == "seq0":
                out = {"ok": True}      # 假成功：无 sequence
            else:
                self.seq += 1
                out = {"ok": True, "privateSequence": self.seq, "groupSequence": self.seq}
            return httpx.Response(200, json={"result": {"data": {"json": out}}})
        return httpx.Response(404, json={})

    def texts(self):
        return ["".join(el.get("textContent", "") for el in s["elements"] if el.get("kind") == "text")
                for s in self.sent]


def c2c(uin="10001", uid="u_owner", text="在吗", mid="m1"):
    return {"type": "message", "chatType": "c2c", "convId": uid, "senderUin": uin, "senderUid": uid,
            "msgId": mid, "text": text, "sendTime": "2026-11-23T10:00:00"}


def group_msg(gid="777", uin="30003", uid="u_30003", text="你在吗", mid="g1", at_bot=True):
    elements = []
    if at_bot:
        elements.append({"type": "at", "data": {"atTargetUid": "u_bot"}})
    return {"type": "message", "chatType": "group", "convId": gid, "senderUin": uin, "senderUid": uid,
            "msgId": mid, "text": text, "elements": elements, "sendTime": ""}


class WeQValidationTests(unittest.TestCase):
    def test_url_validation(self):
        self.assertEqual(validate_weq_url("http://127.0.0.1:19000/"), "http://127.0.0.1:19000")
        self.assertEqual(validate_weq_url("https://weq.example.com"), "https://weq.example.com")
        with self.assertRaises(WhaleError):
            validate_weq_url("http://weq.example.com")          # 远程必须 HTTPS
        with self.assertRaises(WhaleError) as ctx:
            validate_weq_url("https://user:secret@weq.example.com")
        self.assertNotIn("secret", ctx.exception.message)
        with self.assertRaises(WhaleError):
            validate_weq_url("https://weq.example.com/?token=x")

    def test_receipt_gate(self):
        self.assertTrue(accept_send_outcome({"ok": True, "privateSequence": 3}, peer_type="c2c")["ok"])
        self.assertFalse(accept_send_outcome({"ok": True}, peer_type="c2c")["ok"])       # sequence 门闩
        self.assertFalse(accept_send_outcome({"ok": True, "groupSequence": 0}, peer_type="group")["ok"])
        self.assertFalse(accept_send_outcome(None, peer_type="c2c")["ok"])

    def test_event_normalization(self):
        ev = weq_event_to_qq(c2c(), bot_uin="99999", bot_uid="u_bot", accept_group=True,
                             allowed_groups=frozenset())
        self.assertEqual((ev.kind, ev.uin, ev.uid, ev.text), ("private", "10001", "u_owner", "在吗"))
        self.assertIsNone(weq_event_to_qq(c2c(uin="99999"), bot_uin="99999", bot_uid="u_bot",
                                          accept_group=True, allowed_groups=frozenset()))  # 自发不回灌
        g = weq_event_to_qq(group_msg(), bot_uin="99999", bot_uid="u_bot", accept_group=True,
                            allowed_groups=frozenset({"777"}))
        self.assertTrue(g.mentioned)
        self.assertIsNone(weq_event_to_qq(group_msg(gid="888"), bot_uin="99999", bot_uid="u_bot",
                                          accept_group=True, allowed_groups=frozenset({"777"})))
        self.assertIsNone(weq_event_to_qq(group_msg(), bot_uin="99999", bot_uid="u_bot",
                                          accept_group=False, allowed_groups=frozenset({"777"})))
        self.assertIsNone(weq_event_to_qq({"type": "mass"}, bot_uin="99999", bot_uid="u_bot",
                                          accept_group=True, allowed_groups=frozenset()))


class WeQChannelTests(unittest.IsolatedAsyncioTestCase):
    async def setup_weq(self, **extra):
        self.app = make_app(channels={"qq": {**WEQ_CFG, **extra, "enabled": True}})
        self.app.config.set_secret("channel.qq.access_token", "weq-tok")
        self.weq = FakeWeQWeb()
        self.app.http_transport = httpx.MockTransport(self.weq.handler)
        self.app.memory.start()
        await self.app.channels.start()
        return self.app.channels.get("qq")

    async def teardown(self):
        await self.app.channels.stop()
        await self.app.memory.stop()
        self.app.db.close()

    async def test_fail_closed_without_token_or_url(self):
        self.app = make_app(channels={"qq": {**WEQ_CFG, "enabled": True}})
        await self.app.channels.start()                    # 无 token → fail closed，状态可见
        listed = (await self.app.rpc.call("channels.list", {}))["channels"]
        self.assertEqual(listed[0]["last_error"].split(":")[0], "invalid_params")
        await self.app.channels.stop()
        app2 = make_app(channels={"qq": {k: v for k, v in WEQ_CFG.items() if k != "weq_web_url"} |
                                  {"enabled": True}})
        app2.config.set_secret("channel.qq.access_token", "t")
        await app2.channels.start()
        listed = (await app2.rpc.call("channels.list", {}))["channels"]
        self.assertIn("weq_web_url", listed[0]["last_error"])
        await app2.channels.stop()
        app2.db.close()

    async def test_backend_selection_and_status(self):
        ch = await self.setup_weq()
        self.assertIsInstance(ch, WeQChannel)
        listed = (await self.app.rpc.call("channels.list", {}))["channels"]
        self.assertEqual((listed[0]["backend"], listed[0]["kind"]), ("weq", "weq"))
        app2 = make_app(channels={"qq": {"kind": "qq", "enabled": False}})   # 默认 backend=onebot
        from whale.channels.onebot import OneBotChannel
        from whale.channels.manager import resolve_class
        self.assertIs(resolve_class("qq", app2.config.value.channels["qq"]), OneBotChannel)
        app2.db.close()
        await self.teardown()

    async def test_private_reply_and_dedupe_and_uid_learning(self):
        ch = await self.setup_weq()
        model = scripted(self.app, "在的主人～")
        decision = await ch.ingest(weq_event_to_qq(c2c(), bot_uin="99999", bot_uid="u_bot",
                                                   accept_group=True, allowed_groups=frozenset()))
        self.assertEqual(decision, "replied")
        self.assertEqual(self.weq.texts(), ["在的主人～"])
        body = self.weq.sent[0]
        self.assertEqual((body["peerType"], body["targetId"]), ("c2c", "u_owner"))
        self.assertIn("对方：主人", model.calls[0]["messages"][0].text)
        conv = self.app.conversations.get("qq:private:10001")
        self.assertEqual((conv["channel"], conv["peer"]), ("qq", "owner"))
        again = await ch.ingest(weq_event_to_qq(c2c(), bot_uin="99999", bot_uid="u_bot",
                                                accept_group=True, allowed_groups=frozenset()))
        self.assertEqual(again, "duplicate")
        self.assertEqual(len(self.weq.sent), 1)
        # uid 已学习：capability 直调可达
        res = await self.app.capabilities.invoke("qq.send_private", {"user_id": 10001, "text": "主动说一句"},
                                                 __import__("whale.capabilities.spec", fromlist=["ToolContext"])
                                                 .ToolContext(app=self.app))
        self.assertEqual(res.status, "ok")
        self.assertEqual(self.weq.sent[-1]["targetId"], "u_owner")
        await self.teardown()

    async def test_capability_without_uid_mapping_is_truthful(self):
        await self.setup_weq()
        from whale.capabilities.spec import ToolContext
        res = await self.app.capabilities.invoke("qq.send_private", {"user_id": 40004, "text": "hi"},
                                                 ToolContext(app=self.app))
        self.assertEqual(res.status, "error")
        self.assertIn("uid", res.error)
        self.assertEqual(self.weq.sent, [])
        await self.teardown()

    async def test_receipt_gate_and_timeout_truthfulness(self):
        ch = await self.setup_weq()
        self.weq.fail = "seq0"
        res = await ch.send_text("group", "777", "", "大家好")
        self.assertEqual(res["status"], "error")
        self.assertIn("sequence", res["error"])
        self.weq.fail = "timeout"
        res = await ch.send_text("group", "777", "", "还在吗")
        self.assertEqual(res["status"], "unknown")        # 超时=可能已发出，不重试
        await self.teardown()

    async def test_group_mention_replies_with_at(self):
        ch = await self.setup_weq()
        scripted(self.app, "我在！")
        ev = weq_event_to_qq(group_msg(), bot_uin="99999", bot_uid="u_bot", accept_group=True,
                             allowed_groups=frozenset({"777"}))
        self.assertEqual(await ch.ingest(ev), "replied")
        body = self.weq.sent[0]
        self.assertEqual(body["peerType"], "group")
        self.assertEqual(body["elements"][0]["kind"], "at")
        self.assertEqual(body["elements"][0]["atTargetUid"], "u_30003")
        conv = self.app.conversations.get("qq:group:777")
        self.assertEqual(conv["channel"], "qq")
        await self.teardown()

    async def test_name_directory_via_member_briefs(self):
        ch = await self.setup_weq()
        model = scripted(self.app, "嗯嗯")
        ev = group_msg(text="[CQ:at]" , at_bot=True)
        ev["text"] = "你在吗"
        qev = weq_event_to_qq(ev, bot_uin="99999", bot_uid="u_bot", accept_group=True,
                              allowed_groups=frozenset({"777"}))
        qev.nick = ""                                        # 事件没带名片 → 目录解析
        await ch.ingest(qev)
        self.assertEqual(self.weq.brief_calls, 1)
        self.assertIn("群名片A", model.calls[0]["messages"][-1].text)
        qev2 = weq_event_to_qq(group_msg(mid="g2"), bot_uin="99999", bot_uid="u_bot", accept_group=True,
                               allowed_groups=frozenset({"777"}))
        qev2.nick = ""
        self.app.models.overrides["chat"].script.append("还在")
        await ch.ingest(qev2)
        self.assertEqual(self.weq.brief_calls, 1)            # 正缓存命中，不再查
        await self.teardown()

    async def test_notifier_to_owner(self):
        await self.setup_weq()
        await self.app.channels.get("qq").ingest(
            weq_event_to_qq(c2c(), bot_uin="99999", bot_uid="u_bot", accept_group=True,
                            allowed_groups=frozenset()))     # 先学 owner 的 uid
        self.weq.sent.clear()
        delivered = await self.app.hub.notify("提醒", "该喝水了", {})
        self.assertIn("qq:owner", delivered)
        self.assertEqual(self.weq.texts(), ["【提醒】该喝水了"])
        await self.teardown()


class WeQFullModeWindowTests(unittest.IsolatedAsyncioTestCase):
    """按需完整模式窗口纪律：发时开窗、空闲关窗、开窗失败不阻断、state_file 跨进程协调。"""

    async def setup_weq(self, tmp, **extra):
        cfg = {**WEQ_CFG, "fullmode_state_file": str(tmp / "w.ts"), **extra, "enabled": True}
        self.app = make_app(channels={"qq": cfg})
        self.app.config.set_secret("channel.qq.access_token", "weq-tok")
        self.weq = FakeWeQWeb()
        self.app.http_transport = httpx.MockTransport(self.weq.handler)
        self.app.memory.start()
        await self.app.channels.start()
        return self.app.channels.get("qq")

    async def teardown(self):
        await self.app.channels.stop()
        await self.app.memory.stop()
        self.app.db.close()

    async def test_startup_baseline_close(self):
        tmp = self._mk_tmp()
        ch = await self.setup_weq(tmp)
        self.assertEqual(self.weq.fm_attach_calls, [False])     # 启动即基线关窗（幂等）
        self.assertFalse((tmp / "w.ts").exists())
        self.assertIsNotNone(ch._closer_task)
        st = (await self.app.rpc.call("channels.list", {}))["channels"][0]
        self.assertEqual(st["fullmode"]["opens"], 0)
        await self.teardown()

    async def test_send_auto_opens_window_then_reuses(self):
        tmp = self._mk_tmp()
        ch = await self.setup_weq(tmp)
        self.weq.fm_attach_calls.clear()
        scripted(self.app, "在的主人～")
        ev = weq_event_to_qq(c2c(), bot_uin="99999", bot_uid="u_bot", accept_group=True,
                             allowed_groups=frozenset())
        self.assertEqual(await ch.ingest(ev), "replied")
        self.assertEqual(self.weq.fm_attach_calls, [True])      # 发送触发开窗
        self.assertTrue((tmp / "w.ts").exists())                # 落 state_file
        self.assertEqual(self.weq.texts(), ["在的主人～"])
        self.assertEqual(ch.win_opens, 1)
        # 第二次发送：窗口已就绪，不重复开
        self.app.models.overrides["chat"].script.append("还在")
        await ch.ingest(weq_event_to_qq(c2c(mid="m2"), bot_uin="99999", bot_uid="u_bot",
                                        accept_group=True, allowed_groups=frozenset()))
        self.assertEqual(self.weq.fm_attach_calls, [True])
        self.assertEqual(ch.win_opens, 1)
        await self.teardown()

    async def test_open_failure_does_not_block_send(self):
        tmp = self._mk_tmp()
        ch = await self.setup_weq(tmp)
        self.weq.fm_fail_attach = True                          # 开窗一律 500
        res = await ch.send_text("group", "777", "", "大家好")
        self.assertEqual(res["status"], "ok")                   # 红线：开窗失败不阻断发送
        self.assertEqual(ch.win_open_errors, 1)
        self.assertEqual(len(self.weq.sent), 1)
        self.assertTrue((tmp / "w.ts").exists())                # 失败发送也 touch 续期
        await self.teardown()

    async def test_ready_timeout_still_sends(self):
        tmp = self._mk_tmp()
        ch = await self.setup_weq(tmp, fullmode_ready_timeout_s=0.05)
        self.weq.fm_never_ready = True
        res = await ch.send_text("group", "777", "", "还在吗")
        self.assertEqual(res["status"], "ok")
        self.assertEqual(ch.win_open_errors, 1)
        self.assertEqual(len(self.weq.sent), 1)
        await self.teardown()

    async def test_closer_closes_after_idle(self):
        tmp = self._mk_tmp()
        ch = await self.setup_weq(tmp, fullmode_idle_min=10)
        await ch.send_text("group", "777", "", "大家好")
        self.assertTrue((tmp / "w.ts").exists())
        now = time.time()
        # 未越 idle：不关
        self.assertFalse(await ch._window_closer_step(now + 60))
        self.assertTrue((tmp / "w.ts").exists())
        # 越过 idle：关窗 + 清 state_file
        self.assertTrue(await ch._window_closer_step(now + 601))
        self.assertEqual(self.weq.fm_attach_calls[-1], False)
        self.assertFalse((tmp / "w.ts").exists())
        self.assertEqual(ch.win_closes, 1)
        # 已关：幂等不再关
        self.assertFalse(await ch._window_closer_step(now + 1200))
        self.assertEqual(ch.win_closes, 1)
        await self.teardown()

    async def test_closer_respects_fresh_touch_from_elsewhere(self):
        tmp = self._mk_tmp()
        ch = await self.setup_weq(tmp, fullmode_idle_min=10)
        await ch.send_text("group", "777", "", "大家好")
        later = time.time() + 500                               # 模拟另一进程刚 touch 过
        (tmp / "w.ts").write_text(str(int(later)), encoding="ascii")
        self.assertFalse(await ch._window_closer_step(later + 60))
        self.assertTrue((tmp / "w.ts").exists())
        await self.teardown()

    async def test_corrupt_state_file_is_safe(self):
        tmp = self._mk_tmp()
        ch = await self.setup_weq(tmp, fullmode_idle_min=10)
        (tmp / "w.ts").write_text("junk", encoding="ascii")
        self.assertEqual(ch.window.last_send_epoch(), 0.0)      # 损坏 → 0 不抛
        self.assertTrue(await ch._window_closer_step(time.time() + 601))
        await self.teardown()

    async def test_ondemand_disabled_sends_directly(self):
        tmp = self._mk_tmp()
        ch = await self.setup_weq(tmp, fullmode_ondemand=False)
        self.assertIsNone(ch.window)
        res = await ch.send_text("group", "777", "", "大家好")
        self.assertEqual(res["status"], "ok")
        self.assertEqual(self.weq.fm_attach_calls, [])           # 零窗口调用（旧行为）
        self.assertFalse((tmp / "w.ts").exists())
        await self.teardown()

    async def test_token_hygiene_in_state_and_errors(self):
        tmp = self._mk_tmp()
        ch = await self.setup_weq(tmp)
        await ch.send_text("group", "777", "", "大家好")
        content = (tmp / "w.ts").read_text(encoding="ascii")
        self.assertTrue(content.strip().isdigit())               # state_file 只有 epoch
        self.assertNotIn("weq-tok", content)
        self.weq.fm_fail_attach = True
        self.weq.fm_allowed = False
        self.weq.fm_ready = False
        res = await ch.send_text("group", "777", "", "再来")
        self.assertNotIn("weq-tok", res.get("error", ""))
        await self.teardown()

    def _mk_tmp(self):
        import tempfile as tf
        from pathlib import Path
        d = tf.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        return Path(d.name)


class WeQWebhookTests(unittest.TestCase):
    def test_webhook_auth_and_end_to_end(self):
        app = make_app(channels={"qq": dict(WEQ_CFG, enabled=True)})
        app.config.set_secret("channel.qq.access_token", "weq-tok")
        weq = FakeWeQWeb()
        app.http_transport = httpx.MockTransport(weq.handler)
        scripted(app, "收到啦")
        with TestClient(create_server(app)) as client:
            url = "/api/hooks/channel-qq/inbound"
            ev = {"events": [c2c(mid="w1")]}
            self.assertEqual(client.post(url, json=ev).status_code, 401)
            self.assertEqual(client.post(url, json=ev,
                                         headers={"Authorization": "Bearer nope"}).status_code, 401)
            self.assertEqual(client.get(url).status_code, 204)
            r = client.post(url, content=b"{bad", headers={"Authorization": "Bearer weq-tok"})
            self.assertEqual(r.status_code, 400)
            r = client.post(url, json=ev, headers={"Authorization": "Bearer weq-tok"})
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json()["accepted"], 1)
            for _ in range(300):
                if weq.sent:
                    break
                time.sleep(0.01)
            self.assertEqual(weq.texts(), ["收到啦"])
            r = client.post(url, json=ev, headers={"Authorization": "Bearer weq-tok"})
            self.assertEqual(r.status_code, 200)             # 重推同 msgId：ACK 但不重复投递
            time.sleep(0.2)
            self.assertEqual(len(weq.sent), 1)


if __name__ == "__main__":
    unittest.main()
