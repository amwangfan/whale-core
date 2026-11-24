"""Channels 层测试：Channel 抽象、OneBot 渠道（core sink 托管）、昵称目录、端点校验。

照 DEVELOPMENT §3：模型用 ScriptedModel 注入，HTTP 用 Starlette TestClient，
OneBot 协议端用假实现（httpx.MockTransport），断言真实状态而非假成功。
QQ 插件行为契约的完整矩阵在 test_plugins.py；这里覆盖 channels/ 自己的面。
"""

import asyncio
import json
import time
import unittest

import httpx
from starlette.testclient import TestClient

from whale.capabilities.spec import ToolContext
from whale.channels import NameDirectory, clean_name, validate_endpoints
from whale.kernel.errors import WhaleError
from whale.server.http import create_server

from helpers import make_app, scripted

QQ_CFG = {"api_base": "http://onebot.local:3000", "owner_qq": "10001", "bubble_delay_ms": 0,
          "group_mode": "mention", "judge_interval_s": 0, "smart_cooldown_s": 120}


class FakeOneBot:
    def __init__(self):
        self.calls = []
        self.fail = None

    def handler(self, request):
        action = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content or b"{}")
        self.calls.append((action, body, request.headers.get("authorization")))
        if action == "get_group_member_info":
            return httpx.Response(200, json={"status": "ok", "retcode": 0,
                                             "data": {"card": "群名片", "nickname": "群昵称"}})
        if action == "get_stranger_info":
            return httpx.Response(200, json={"status": "ok", "retcode": 0, "data": {"nickname": "陌生人"}})
        if self.fail == "retcode":
            return httpx.Response(200, json={"status": "failed", "retcode": 100, "wording": "发送失败"})
        if self.fail == "timeout":
            raise httpx.ReadTimeout("slow", request=request)
        return httpx.Response(200, json={"status": "ok", "retcode": 0, "data": {"message_id": 5001}})

    def sent(self):
        return [(a, b) for a, b, _ in self.calls if a.startswith("send_")]


def private(uid, message, mid=1, nick="小明"):
    return {"post_type": "message", "message_type": "private", "self_id": 99999, "user_id": uid,
            "message_id": mid, "message": message, "sender": {"user_id": uid, "nickname": nick}}


def group(gid, uid, message, mid=1, nick="群友A"):
    return {"post_type": "message", "message_type": "group", "self_id": 99999, "group_id": gid,
            "user_id": uid, "message_id": mid, "message": message, "sender": {"user_id": uid, "card": nick}}


# -- endpoint validation ------------------------------------------------------
class EndpointValidationTests(unittest.TestCase):
    def test_ok_endpoints(self):
        self.assertEqual(validate_endpoints("http://127.0.0.1:3000/", ""), ("http://127.0.0.1:3000", ""))
        self.assertEqual(validate_endpoints("https://ob.example.com", "ws://127.0.0.1:3001"),
                         ("https://ob.example.com", "ws://127.0.0.1:3001"))
        self.assertEqual(validate_endpoints("", "wss://ob.example.com/ws"), ("", "wss://ob.example.com/ws"))
        self.assertEqual(validate_endpoints("", ""), ("", ""))

    def test_rejects_bad_scheme_userinfo_query(self):
        with self.assertRaises(WhaleError):
            validate_endpoints("ftp://onebot.local")
        with self.assertRaises(WhaleError) as ctx:      # 凭据不得内嵌进 URL
            validate_endpoints("http://user:secret@onebot.local:3000")
        self.assertNotIn("secret", ctx.exception.message)
        with self.assertRaises(WhaleError):
            validate_endpoints("http://onebot.local:3000/?access_token=x")
        with self.assertRaises(WhaleError):
            validate_endpoints("", "http://not-a-socket")
        with self.assertRaises(WhaleError):
            validate_endpoints("", "ws://user:secret@onebot.local")


# -- name directory -------------------------------------------------------------
class NameDirectoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_cache_negative_cache_and_failure_fallback(self):
        app = make_app()
        calls = []

        async def fetch(kind, uid, gid=""):
            calls.append((kind, uid, gid))
            if uid == "boom":
                raise WhaleError("unavailable", "OneBot down")
            return "  [群] 名片|A  " if kind == "group" else "陌生昵称"

        names = NameDirectory(app.db, fetch)
        self.assertEqual(await names.resolve("group", "100", "777"), "群 名片A")   # 净化 + 截断
        self.assertEqual(await names.resolve("group", "100", "777"), "群 名片A")   # 正缓存命中
        self.assertEqual(len(calls), 1)
        self.assertEqual(await names.resolve("private", "200"), "陌生昵称")
        self.assertEqual(await names.resolve("group", "boom", "777"), "")          # 失败回退空串
        self.assertEqual(await names.resolve("group", "boom", "777"), "")          # 负缓存命中
        self.assertEqual(len([c for c in calls if c[1] == "boom"]), 1)
        self.assertEqual(clean_name("a\x00[b]|c\\d" + "x" * 40), "abcd" + "x" * 28)  # 截断 32 字
        app.db.close()

    async def test_directory_used_when_event_has_no_name(self):
        app = make_app(channels={"qq": dict(QQ_CFG, enabled=True)})
        app.config.set_secret("channel.qq.access_token", "tok")
        bot = FakeOneBot()
        app.http_transport = httpx.MockTransport(bot.handler)
        app.memory.start()
        scripted(app, "你好呀")
        await app.channels.start()
        channel = app.channels.get("qq")
        ev = private("20002", "你好", mid=1)
        ev["sender"] = {"user_id": "20002"}                                        # 事件没带名片
        self.assertEqual(await channel.handle_event(ev), "replied")
        conv = app.conversations.get("qq:private:20002")
        self.assertEqual(conv["title"], "QQ · 陌生人")                              # get_stranger_info 解析
        self.assertTrue(any(a == "get_stranger_info" for a, _, _ in bot.calls))
        await app.channels.stop()
        await app.memory.stop()
        app.db.close()


# -- manager lifecycle (CoreSink) ------------------------------------------------
class ChannelManagerTests(unittest.IsolatedAsyncioTestCase):
    async def setup_channel(self, **extra):
        self.app = make_app(channels={"qq": {**QQ_CFG, **extra, "enabled": True}})
        self.app.config.set_secret("channel.qq.access_token", "tok")
        self.bot = FakeOneBot()
        self.app.http_transport = httpx.MockTransport(self.bot.handler)
        self.app.memory.start()
        await self.app.channels.start()
        return self.app.channels.get("qq")

    async def teardown(self):
        await self.app.channels.stop()
        await self.app.memory.stop()
        self.app.db.close()

    async def test_open_registers_surface_and_status(self):
        channel = await self.setup_channel()
        self.assertTrue(self.app.capabilities.has("qq.send_private"))
        self.assertEqual(self.app.capabilities.get("qq.send_private").provider, "channel:qq")
        self.assertIn(("channel-qq", "inbound"), self.app.hooks)
        listed = (await self.app.rpc.call("channels.list", {}))["channels"]
        self.assertEqual(listed[0]["id"], "qq")
        self.assertEqual(listed[0]["kind"], "onebot")
        self.assertTrue(listed[0]["connected"])
        self.assertIn("http_api", listed[0]["via"])
        self.assertIn("onebot", (await self.app.rpc.call("channels.list", {}))["kinds"])
        self.assertTrue(channel.connected)
        await self.teardown()

    async def test_inbound_runs_agent_and_replies(self):
        channel = await self.setup_channel()
        model = scripted(self.app, "在的主人～")
        decision = await channel.handle_event(private("10001", "在吗"))
        self.assertEqual(decision, "replied")
        action, body = self.bot.sent()[0]
        self.assertEqual((action, body["user_id"]), ("send_private_msg", 10001))
        conv = self.app.conversations.get("qq:private:10001")
        self.assertEqual((conv["channel"], conv["peer"]), ("qq", "owner"))
        run = self.app.runs.list(limit=1)[0]
        self.assertEqual((run["channel"], run["status"]), ("qq", "completed"))
        self.assertIn("对方：主人", model.calls[0]["messages"][0].text)
        await self.teardown()

    async def test_group_mention_and_truthful_delivery(self):
        channel = await self.setup_channel()
        scripted(self.app, "我在！")
        self.assertEqual(await channel.handle_event(group("777", "30003", "今天好冷", mid=1)),
                         "skip:not_mentioned")
        decision = await channel.handle_event(group("777", "30003", "[CQ:at,qq=99999] 你在吗", mid=2))
        self.assertEqual(decision, "replied")
        self.assertEqual(self.bot.sent()[0][0], "send_group_msg")
        self.bot.fail = "timeout"
        res = await self.app.capabilities.invoke("qq.send_group", {"group_id": 777, "text": "hi"},
                                                 ToolContext(app=self.app))
        self.assertEqual(res.status, "unknown")          # 超时如实记 unknown，不重试
        self.bot.fail = "retcode"
        res = await self.app.capabilities.invoke("qq.send_private", {"user_id": "20002", "text": "hi"},
                                                 ToolContext(app=self.app))
        self.assertEqual(res.status, "error")
        await self.teardown()

    async def test_group_send_exempt_from_approval_by_owner_decree(self):
        """主人令 2026-11-24：群发送免审核（write→allow）；私聊维持 external→ask 不动。"""
        await self.setup_channel()
        self.assertEqual(self.app.policy.decide(self.app.capabilities.get("qq.send_group")), "allow")
        self.assertEqual(self.app.policy.decide(self.app.capabilities.get("qq.send_private")), "ask")
        await self.teardown()

    async def test_group_history_persisted_and_restored_after_restart(self):
        """主人令 2026-11-24：群缓冲落 kv，重启（同数据目录新 App）后恢复；群间严格隔离。"""
        import tempfile

        from whale.app import App
        cfg = {"channels": {"qq": dict(QQ_CFG, enabled=True)}}
        with tempfile.TemporaryDirectory() as td:
            app1 = App(td, cfg)
            app1.config.set_secret("channel.qq.access_token", "tok")
            app1.memory.start()
            await app1.channels.start()
            ch1 = app1.channels.get("qq")
            await ch1.handle_event(group("777", "30003", "第一条", mid=1))
            await ch1.handle_event(group("777", "30003", "第二条", mid=2))
            await ch1.handle_event(group("888", "30004", "别群的话", mid=3))
            stored = app1.db.kv_all("qq_group_history")                      # 存
            self.assertEqual([t for _, t in stored["777"]["items"]], ["第一条", "第二条"])
            self.assertEqual([t for _, t in stored["888"]["items"]], ["别群的话"])
            await app1.channels.stop()
            await app1.memory.stop()
            app1.db.close()
            # 重启：新 App 同数据目录 → open 时恢复
            app2 = App(td, cfg)
            app2.config.set_secret("channel.qq.access_token", "tok")
            app2.memory.start()
            await app2.channels.start()
            ch2 = app2.channels.get("qq")
            self.assertEqual([t for _, t in ch2.history["777"]], ["第一条", "第二条"])
            self.assertEqual([t for _, t in ch2.history["888"]], ["别群的话"])
            self.assertNotIn("999", ch2.history)                             # 无消息群不存在缓冲
            self.assertEqual(ch2.history["777"].maxlen, 20)                  # 深度语义不变
            await app2.channels.stop()
            await app2.memory.stop()
            app2.db.close()

    async def test_group_history_capped_at_20(self):
        """20 深封顶：最旧挤出，kv 与内存一致。"""
        channel = await self.setup_channel()
        for n in range(25):
            await channel.handle_event(group("777", "30003", f"消息{n}", mid=100 + n))
        items = self.app.db.kv_get("qq_group_history", "777")["items"]
        self.assertEqual(len(items), 20)
        self.assertEqual(items[0][1], "消息5")                               # 最旧五条已挤出
        self.assertEqual(items[-1][1], "消息24")
        self.assertEqual(len(channel.history["777"]), 20)
        await self.teardown()

    async def test_group_history_restore_tolerates_corruption(self):
        """损坏条目跳过：好行留下、坏行丢弃、坏群不留，绝不阻断上线。"""
        self.app = make_app(channels={"qq": dict(QQ_CFG, enabled=True)})
        self.app.config.set_secret("channel.qq.access_token", "tok")
        self.app.db.kv_set("qq_group_history", "777", {"items": "not-a-list"})
        self.app.db.kv_set("qq_group_history", "888", {"items": [["群友A", "好"], ["坏行"], ["群友A", "行"]]})
        self.bot = FakeOneBot()
        self.app.http_transport = httpx.MockTransport(self.bot.handler)
        self.app.memory.start()
        await self.app.channels.start()
        channel = self.app.channels.get("qq")
        self.assertNotIn("777", channel.history)
        self.assertEqual([t for _, t in channel.history["888"]], ["好", "行"])
        await self.teardown()

    async def test_notifier_and_close_cleanup(self):
        await self.setup_channel()
        delivered = await self.app.hub.notify("提醒", "该喝水了", {})
        self.assertIn("qq:owner", delivered)
        before = len(self.app.hub.notifiers)
        await self.app.channels.stop()
        self.assertEqual(len(self.app.hub.notifiers), before - 1)
        self.assertNotIn(("channel-qq", "inbound"), self.app.hooks)
        self.assertFalse(self.app.capabilities.has("qq.send_private"))
        listed = (await self.app.rpc.call("channels.list", {}))["channels"]
        self.assertFalse(listed[0]["connected"])
        await self.app.memory.stop()
        self.app.db.close()

    async def test_bad_config_is_truthful_not_fatal(self):
        self.app = make_app(channels={"qq": {"kind": "onebot", "enabled": True,
                                             "api_base": "ftp://bad"},
                                      "mystery": {"kind": "nosuch", "enabled": True}})
        await self.app.channels.start()
        listed = {c["id"]: c for c in (await self.app.rpc.call("channels.list", {}))["channels"]}
        self.assertIn("invalid_params", listed["qq"]["last_error"])
        self.assertIn("unknown channel kind", listed["mystery"]["last_error"])
        self.assertFalse(self.app.capabilities.has("qq.send_private"))
        await self.app.channels.stop()
        self.app.db.close()

    async def test_disabled_channel_stays_down(self):
        self.app = make_app(channels={"qq": dict(QQ_CFG, enabled=False)})
        await self.app.channels.start()
        self.assertFalse(self.app.capabilities.has("qq.send_private"))
        listed = (await self.app.rpc.call("channels.list", {}))["channels"]
        self.assertEqual((listed[0]["enabled"], listed[0]["connected"]), (False, False))
        self.app.db.close()


class ChannelWebhookTests(unittest.TestCase):
    def test_webhook_auth_and_end_to_end(self):
        app = make_app(channels={"qq": dict(QQ_CFG, enabled=True)})
        app.config.set_secret("channel.qq.access_token", "tok")
        bot = FakeOneBot()
        app.http_transport = httpx.MockTransport(bot.handler)
        scripted(app, "收到啦")
        with TestClient(create_server(app)) as client:   # lifespan → app.start() → channels.start()
            url = "/api/hooks/channel-qq/inbound"
            ev = private("10001", "webhook 你好", mid=77)
            self.assertEqual(client.post(url, json=ev).status_code, 401)
            self.assertEqual(client.post(url, json=ev, headers={"Authorization": "Bearer nope"}).status_code, 401)
            self.assertEqual(client.post(url, content=b"{bad",
                                         headers={"Authorization": "Bearer tok"}).status_code, 400)
            self.assertEqual(client.get(url).status_code, 204)
            r = client.post(url, json=ev, headers={"Authorization": "Bearer tok"})
            self.assertEqual(r.status_code, 204)
            for _ in range(300):
                if bot.sent():
                    break
                time.sleep(0.01)
            self.assertEqual(bot.sent()[0][1]["user_id"], 10001)


if __name__ == "__main__":
    unittest.main()
