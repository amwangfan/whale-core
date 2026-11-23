import asyncio
import base64
import io
import json
import tempfile
import time
import unittest
import wave

import httpx
from starlette.testclient import TestClient

from whale.app import App
from whale.capabilities.spec import ToolContext
from whale.models.base import Completion
from whale.plugins.manager import BUILTIN_DIR, PluginContext, PluginRecord
from whale.plugins.builtin.media import plugin as media_plugin
from whale.plugins.builtin.qq import plugin as qq_plugin
from whale.plugins.builtin.qweather import plugin as qweather_plugin
from whale.server.http import create_server

from helpers import make_app, scripted

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32


async def load(app, pid, module, secrets=None):
    """Set a built-in plugin up the way PluginManager.enable does (without importlib)."""
    manifest = json.loads((BUILTIN_DIR / pid / "plugin.json").read_text(encoding="utf-8"))
    for key, value in (secrets or {}).items():
        app.config.set_secret(f"plugin.{pid}.{key}", value)
    rec = PluginRecord(pid, BUILTIN_DIR / pid, manifest)
    app.plugins.records[pid] = rec
    result = module.setup(PluginContext(app, rec))
    if asyncio.iscoroutine(result):
        result = await result
    rec.enabled = True
    rec.status = "active" if rec.capabilities or rec.stop_hooks else "needs_config"
    return rec, result


def invoke(app, name, args):
    return app.capabilities.invoke(name, args, ToolContext(app=app))


class ManifestTests(unittest.TestCase):
    def test_manifests_are_complete(self):
        for pid in ("qweather", "qq", "media"):
            m = json.loads((BUILTIN_DIR / pid / "plugin.json").read_text(encoding="utf-8"))
            self.assertEqual(m["id"], pid)
            for key in ("name", "version", "description", "icon", "entry"):
                self.assertTrue(m[key], key)
            for key, spec in m["config"].items():
                self.assertIn(spec["type"], ("string", "integer", "boolean", "array"), key)
                self.assertIn("title", spec)
                self.assertIn("default", spec)
            self.assertTrue(m["secrets"])


# -- qweather ----------------------------------------------------------------------------------
class QWeatherTests(unittest.IsolatedAsyncioTestCase):
    def mock(self, geo_code="200"):
        self.requests = []

        def handler(request):
            self.requests.append(request)
            path = request.url.path
            if path == "/geo/v2/city/lookup":
                if geo_code != "200":
                    return httpx.Response(200, json={"code": geo_code})
                return httpx.Response(200, json={"code": "200", "location": [{
                    "id": "101210101", "name": "杭州", "adm2": "杭州", "adm1": "浙江省", "lat": "30.25",
                    "lon": "120.16"}]})
            if path == "/v7/weather/now":
                return httpx.Response(200, json={"code": "200", "updateTime": "2026-11-23T10:00+08:00", "now": {
                    "text": "多云", "temp": "14", "feelsLike": "12", "windDir": "东北风", "windScale": "3",
                    "humidity": "71", "precip": "0.0", "obsTime": "2026-11-23T09:50+08:00"}})
            if path == "/v7/weather/3d":
                return httpx.Response(200, json={"code": "200", "daily": [
                    {"fxDate": "2026-11-23", "textDay": "多云", "textNight": "小雨", "tempMin": "9", "tempMax": "16",
                     "precip": "1.2"},
                    {"fxDate": "2026-11-24", "textDay": "晴", "textNight": "晴", "tempMin": "7", "tempMax": "15",
                     "precip": "0.0"},
                    {"fxDate": "2026-11-25", "textDay": "阴", "textNight": "多云", "tempMin": "8", "tempMax": "13"}]})
            return httpx.Response(404, json={"code": "404"})

        self.app.http_transport = httpx.MockTransport(handler)

    async def test_needs_config_registers_nothing(self):
        self.app = make_app(plugins={"qweather": {"api_host": "abc.re.qweatherapi.com"}})
        rec, _ = await load(self.app, "qweather", qweather_plugin)
        self.assertEqual(rec.status, "needs_config")
        self.assertFalse(self.app.capabilities.has("weather.now"))

    async def test_now_and_forecast_parsing_and_cache(self):
        self.app = make_app(plugins={"qweather": {"api_host": "https://abc.re.qweatherapi.com/"}})
        self.mock()
        rec, _ = await load(self.app, "qweather", qweather_plugin, {"key": "qw-secret"})
        self.assertEqual(rec.status, "active")
        res = await invoke(self.app, "weather.now", {"location": "杭州"})
        self.assertEqual(res.status, "ok", res.error)
        self.assertEqual(res.data["now"]["temp"], "14")
        self.assertEqual(res.data["now"]["feels_like"], "12")
        self.assertEqual(len(res.data["forecast"]), 3)
        self.assertEqual(res.data["forecast"][0]["text_night"], "小雨")
        self.assertIn("杭州", res.data["summary"])
        self.assertIn("多云转小雨", res.data["summary"])
        self.assertEqual(self.requests[0].url.host, "abc.re.qweatherapi.com")
        self.assertEqual(self.requests[0].headers["x-qw-api-key"], "qw-secret")
        self.assertNotIn("qw-secret", res.to_model_text())
        n = len(self.requests)
        await invoke(self.app, "weather.now", {"location": "杭州"})
        self.assertEqual(len(self.requests), n)  # cached

    async def test_default_city_coords_and_errors(self):
        self.app = make_app(plugins={"qweather": {"api_host": "abc.re.qweatherapi.com"}})
        self.mock(geo_code="401")
        await load(self.app, "qweather", qweather_plugin, {"key": "k"})
        res = await invoke(self.app, "weather.now", {})
        self.assertEqual(res.status, "error")
        self.assertIn("城市", res.error)
        res = await invoke(self.app, "weather.now", {"location": "杭州"})
        self.assertEqual(res.status, "error")
        self.assertIn("鉴权", res.error)
        self.assertEqual(qweather_plugin.parse_coords("30.25,120.16"), "120.16,30.25")
        self.assertEqual(qweather_plugin.parse_coords("116.41, 39.92"), "116.41,39.92")
        self.assertIsNone(qweather_plugin.parse_coords("北京"))


# -- QQ ----------------------------------------------------------------------------------------
class FakeOneBot:
    def __init__(self):
        self.calls = []
        self.fail = None
        self.messages = {}
        self.n = 0

    def handler(self, request):
        action = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content or b"{}")
        self.calls.append((action, body, request.headers.get("authorization")))
        if action == "get_msg":
            msg = self.messages.get(str(body.get("message_id")))
            if msg is None:
                return httpx.Response(200, json={"status": "failed", "retcode": 100, "data": None})
            return httpx.Response(200, json={"status": "ok", "retcode": 0, "data": msg})
        if self.fail == "retcode":
            return httpx.Response(200, json={"status": "failed", "retcode": 100, "wording": "发送失败"})
        if self.fail == "timeout":
            raise httpx.ReadTimeout("slow", request=request)
        self.n += 1
        return httpx.Response(200, json={"status": "ok", "retcode": 0, "data": {"message_id": 5000 + self.n}})

    def sent(self):
        return [(a, b) for a, b, _ in self.calls if a.startswith("send_")]

    def texts(self):
        return ["".join(seg["data"].get("text", "") for seg in b["message"] if seg["type"] == "text")
                for _, b in self.sent()]


def private(uid, message, mid=1, nick="小明"):
    return {"post_type": "message", "message_type": "private", "self_id": 99999, "user_id": uid,
            "message_id": mid, "message": message, "sender": {"user_id": uid, "nickname": nick}}


def group(gid, uid, message, mid=1, nick="群友A"):
    return {"post_type": "message", "message_type": "group", "self_id": 99999, "group_id": gid, "user_id": uid,
            "message_id": mid, "message": message, "sender": {"user_id": uid, "card": nick}}


QQ_CFG = {"api_base": "http://onebot.local:3000", "owner_qq": "10001", "bubble_delay_ms": 0,
          "group_mode": "mention", "judge_interval_s": 0, "smart_cooldown_s": 120}


class QQTests(unittest.IsolatedAsyncioTestCase):
    async def setup_qq(self, **cfg):
        self.app = make_app(plugins={"qq": {**QQ_CFG, **cfg}})
        self.app.memory.start()
        self.bot = FakeOneBot()
        self.app.http_transport = httpx.MockTransport(self.bot.handler)
        self.rec, self.bridge = await load(self.app, "qq", qq_plugin, {"access_token": "tok"})

    async def asyncTearDown(self):
        if getattr(self, "app", None) is not None:
            if self.rec.enabled:
                await self.app.plugins.disable("qq", persist=False)
            await self.app.memory.stop()

    async def test_needs_config_without_endpoint(self):
        self.app = make_app()
        self.app.memory.start()
        self.rec, bridge = await load(self.app, "qq", qq_plugin)
        self.assertIsNone(bridge)
        self.assertEqual(self.rec.status, "needs_config")
        self.assertFalse(self.app.capabilities.has("qq.send_private"))

    async def test_owner_private_message_runs_agent_and_replies(self):
        await self.setup_qq()
        model = scripted(self.app, "在的主人～")
        decision = await self.bridge.handle_event(private(10001, "在吗"))
        self.assertEqual(decision, "replied")
        sent = self.bot.sent()
        self.assertEqual(sent[0][0], "send_private_msg")
        self.assertEqual(sent[0][1]["user_id"], 10001)
        self.assertEqual(self.bot.texts(), ["在的主人～"])
        self.assertEqual(self.bot.calls[0][2], "Bearer tok")
        conv = self.app.conversations.get("qq:private:10001")
        self.assertEqual((conv["channel"], conv["peer"]), ("qq", "owner"))
        self.assertIn("对方：主人", model.calls[0]["messages"][0].text)
        run = self.app.runs.list(limit=1)[0]
        self.assertEqual((run["channel"], run["status"]), ("qq", "completed"))
        self.assertEqual(self.rec.status, "active")

    async def test_private_from_stranger_uses_qq_peer(self):
        await self.setup_qq()
        model = scripted(self.app, "你好呀")
        self.assertEqual(await self.bridge.handle_event(private(20002, "你好", nick="路人")), "replied")
        self.assertEqual(self.app.conversations.get("qq:private:20002")["peer"], "qq:20002")
        system = model.calls[0]["messages"][0].text
        self.assertIn("qq:20002", system)
        self.assertIn("不是主人", system)
        self.assertEqual(self.bot.sent()[0][1]["user_id"], 20002)

    async def test_images_are_passed_to_the_run(self):
        await self.setup_qq()
        model = scripted(self.app, "好可爱的猫")
        msg = [{"type": "text", "data": {"text": "看我家猫"}},
               {"type": "image", "data": {"file": "a.jpg", "url": "https://multimedia.nt.qq.com.cn/a.jpg"}}]
        self.assertEqual(await self.bridge.handle_event(private(10001, msg)), "replied")
        last = model.calls[0]["messages"][-1]
        self.assertIsInstance(last.content, list)
        urls = [p["image_url"]["url"] for p in last.content if p["type"] == "image_url"]
        self.assertEqual(urls, ["https://multimedia.nt.qq.com.cn/a.jpg"])

    async def test_dedupe_by_message_id(self):
        await self.setup_qq()
        scripted(self.app, "一次", "两次")
        ev = private(10001, "hi", mid=42)
        self.assertEqual(await self.bridge.handle_event(ev), "replied")
        self.assertEqual(await self.bridge.handle_event(dict(ev)), "duplicate")
        self.assertEqual(len(self.bot.sent()), 1)
        self.assertEqual(await self.bridge.handle_event(private(99999, "self echo", mid=43)), "ignored:self")

    async def test_group_mention_mode(self):
        await self.setup_qq()
        model = scripted(self.app, "我在！")
        self.assertEqual(await self.bridge.handle_event(group(777, 30003, "今天好冷", mid=1)), "skip:not_mentioned")
        self.assertEqual(model.calls, [])
        self.assertEqual(self.bot.sent(), [])
        decision = await self.bridge.handle_event(group(777, 30003, "[CQ:at,qq=99999] 你在吗", mid=2))
        self.assertEqual(decision, "replied")
        action, body = self.bot.sent()[0]
        self.assertEqual((action, body["group_id"]), ("send_group_msg", 777))
        self.assertEqual(body["message"][0], {"type": "reply", "data": {"id": "2"}})
        user_text = model.calls[0]["messages"][-1].text
        self.assertIn("群友A：你在吗", user_text)
        system = model.calls[0]["messages"][0].text
        self.assertIn("今天好冷", system)  # earlier group chatter as background
        conv = self.app.conversations.get("qq:group:777")
        self.assertEqual(conv["channel"], "qq")

    async def test_group_reply_to_bot_counts_as_directed(self):
        await self.setup_qq()
        scripted(self.app, "第一句", "接着说")
        await self.bridge.handle_event(group(777, 30003, "[CQ:at,qq=99999]讲个笑话", mid=10))
        bot_mid = self.bridge.sent_ids[-1]
        decision = await self.bridge.handle_event(group(777, 30003, f"[CQ:reply,id={bot_mid}]再来一个", mid=11))
        self.assertEqual(decision, "replied")
        # a reply to an older bot message not in memory: resolved through get_msg
        self.bot.messages["8888"] = {"sender": {"user_id": 99999}, "message": "很久以前的话"}
        scripted(self.app, "记得记得")
        decision = await self.bridge.handle_event(group(777, 30003, "[CQ:reply,id=8888]还记得吗", mid=12))
        self.assertEqual(decision, "replied")
        self.bot.messages["7777"] = {"sender": {"user_id": 30004}, "message": "别人的话"}
        decision = await self.bridge.handle_event(group(777, 30003, "[CQ:reply,id=7777]同意", mid=13))
        self.assertEqual(decision, "skip:not_mentioned")

    async def test_group_allowlist_and_off(self):
        await self.setup_qq(allowed_groups=["111"])
        self.assertEqual(await self.bridge.handle_event(group(222, 1, "[CQ:at,qq=99999]hi")), "skip:not_allowed")
        self.bridge.group_mode = "off"
        self.assertEqual(await self.bridge.handle_event(group(111, 1, "[CQ:at,qq=99999]hi", mid=2)),
                         "skip:group_off")
        res = await invoke(self.app, "qq.send_group", {"group_id": 222, "text": "x"})
        self.assertEqual(res.status, "error")

    async def test_smart_mode_judge_skip_reply_and_cooldown(self):
        await self.setup_qq(group_mode="smart")
        fast = scripted(self.app, '{"decision": "skip", "reason": "群友闲聊"}', role="fast")
        chat = scripted(self.app, "哈哈我也觉得食堂今天不错")
        self.assertEqual(await self.bridge.handle_event(group(777, 1, "你们中午吃的啥", mid=1)), "skip:judge_skip")
        self.assertTrue(fast.calls[0]["json_mode"])
        self.assertEqual(fast.calls[0]["reasoning_effort"], "minimal")   # judge 快分类：不深推理
        self.assertEqual(fast.calls[0]["max_tokens"], 200)               # 留余量防推理吃光预算
        self.assertIn("你们中午吃的啥", fast.calls[0]["messages"][-1].text)
        self.assertEqual(chat.calls, [])
        fast.script.append('{"decision": "reply", "reason": "可以接话"}')
        self.assertEqual(await self.bridge.handle_event(group(777, 2, "食堂今天的红烧肉绝了", mid=2)), "replied")
        body = self.bot.sent()[0][1]
        self.assertEqual(body["message"][0]["type"], "text")  # unsolicited: no quote
        self.assertIn("主动插话", chat.calls[0]["messages"][0].text)
        self.assertEqual(await self.bridge.handle_event(group(777, 3, "确实确实好吃", mid=3)), "skip:cooldown")
        self.assertEqual(len(fast.calls), 2)
        # mentions bypass the judge and the cooldown
        chat.script.append("叫我干嘛")
        self.assertEqual(await self.bridge.handle_event(group(777, 3, "[CQ:at,qq=99999] 鲸鱼", mid=4)), "replied")
        self.assertEqual(len(fast.calls), 2)

    async def test_smart_mode_fails_closed_and_skips_media(self):
        await self.setup_qq(group_mode="smart")
        fast = scripted(self.app, "我觉得应该回复吧", role="fast")
        self.assertEqual(await self.bridge.handle_event(group(777, 1, "有人知道这个怎么修吗", mid=1)),
                         "skip:judge_error")
        self.assertEqual(await self.bridge.handle_event(group(777, 1, "[CQ:face,id=1]", mid=2)), "skip:media_only")
        self.assertEqual(len(fast.calls), 1)
        self.bridge.judge_interval = 60
        fast.script.append('{"decision": "reply"}')
        self.assertEqual(await self.bridge.handle_event(group(777, 1, "真的没人知道吗", mid=3)), "skip:rate_limited")

    async def test_agent_can_stay_silent_in_smart_join(self):
        await self.setup_qq(group_mode="smart")
        scripted(self.app, '{"decision": "reply"}', role="fast")
        scripted(self.app, "[SKIP]")
        self.assertEqual(await self.bridge.handle_event(group(777, 1, "今天周五了耶", mid=1)), "skip:agent_silent")
        self.assertEqual(self.bot.sent(), [])

    async def test_bubble_splitting_and_multi_send(self):
        await self.setup_qq()
        split = qq_plugin.split_bubbles
        self.assertEqual(split("短句子"), ["短句子"])
        long = "\n\n".join(["第一段" * 30, "第二段" * 30, "第三段" * 30, "第四段" * 30])
        parts = split(long)
        self.assertEqual(len(parts), 3)
        self.assertIn("第四段", parts[-1])
        self.assertEqual(split("一行\n二行\n三行\n四行", group=True), ["一行", "二行", "三行\n四行"])
        code = "看代码：\n\n```py\nprint(1)\n```\n\n就这样"
        self.assertEqual(split(code), [code])
        self.assertEqual(len(split("见 https://example.com/a\n\n" + "后面" * 200)), 1)
        self.assertEqual(qq_plugin.plain("## 标题\n**重点**内容"), "标题\n重点内容")
        scripted(self.app, "第一件事说完了，" * 12 + "\n\n" + "第二件事也说一下，" * 12)
        self.assertEqual(await self.bridge.handle_event(private(10001, "讲讲")), "replied")
        self.assertEqual(len(self.bot.sent()), 2)
        self.bridge.reply_split = False
        receipt = await self.bridge.send("private", "10001", "甲\n\n乙")
        self.assertEqual(receipt["total"], 1)

    async def test_cq_and_segment_parsing(self):
        parse = qq_plugin.parse_message
        p = parse("[CQ:reply,id=123][CQ:at,qq=99999] 看看这个&#91;图&#93;"
                  "[CQ:image,file=abc.jpg,url=https://x.cn/a.jpg?a=1&amp;b=2][CQ:face,id=5]", "99999")
        self.assertTrue(p.mentioned)
        self.assertEqual(p.reply_id, "123")
        self.assertEqual(p.images, ["https://x.cn/a.jpg?a=1&b=2"])
        self.assertEqual(p.text, "看看这个[图]")
        p = parse([{"type": "at", "data": {"qq": "123", "name": "小明"}}, {"type": "text", "data": {"text": "你好"}},
                   {"type": "record", "data": {"file": "x.amr"}}, {"type": "at", "data": {"qq": "all"}}], "99999")
        self.assertFalse(p.mentioned)
        self.assertEqual(p.text, "@小明 你好[语音]@全体成员")
        self.assertEqual(p.mentions, ["123", "all"])
        self.assertEqual(qq_plugin.parse_cq("纯文本"), [{"type": "text", "data": {"text": "纯文本"}}])

    async def test_delivery_failures_are_truthful(self):
        await self.setup_qq()
        res = await invoke(self.app, "qq.send_private", {"user_id": "20002", "text": "hello"})
        self.assertEqual(res.status, "ok", res.error)
        self.assertEqual(res.data["message_ids"], ["5001"])
        self.bot.fail = "retcode"
        res = await invoke(self.app, "qq.send_group", {"group_id": 777, "text": "hello"})
        self.assertEqual(res.status, "error")
        self.assertIn("retcode=100", res.error)
        self.bot.fail = "timeout"
        res = await invoke(self.app, "qq.send_private", {"user_id": 20002, "text": "hello"})
        self.assertEqual(res.status, "unknown")
        self.assertEqual(self.app.capabilities.get("qq.send_private").risk.value, "external")

    async def test_owner_gets_notified_when_the_run_fails(self):
        await self.setup_qq()

        class Broken:
            name = "broken"

            async def complete(self, *a, **k):
                from whale.models.base import ModelError
                raise ModelError("HTTP 500", status=500)

        self.app.models.overrides["chat"] = Broken()
        self.assertEqual(await self.bridge.handle_event(private(10001, "在吗")), "failed:failed")
        self.assertIn("没能回复", self.bot.texts()[0])
        self.assertEqual(await self.bridge.handle_event(private(20002, "在吗", mid=2)), "failed:failed")
        self.assertEqual(len(self.bot.sent()), 1)  # strangers are not told about internals

    async def test_notifier_and_disable_cleanup(self):
        await self.setup_qq()
        delivered = await self.app.hub.notify("提醒", "该喝水了", {})
        self.assertIn("qq:owner", delivered)
        self.assertEqual(self.bot.texts(), ["【提醒】该喝水了"])
        self.assertEqual(self.bot.sent()[0][1]["user_id"], 10001)
        self.bot.fail = "retcode"
        self.assertNotIn("qq:owner", await self.app.hub.notify("提醒", "再喝一杯", {}))
        self.assertIn(("qq", "onebot"), self.app.hooks)
        before = len(self.app.hub.notifiers)
        await self.app.plugins.disable("qq", persist=False)
        self.assertEqual(len(self.app.hub.notifiers), before - 1)  # only QQ's notifier is removed
        self.assertNotIn("qq:owner", await self.app.hub.notify("提醒", "第三杯", {}))
        self.assertNotIn(("qq", "onebot"), self.app.hooks)
        self.assertFalse(self.app.capabilities.has("qq.send_group"))

    async def test_forward_websocket_inbound_and_outbound(self):
        frames = asyncio.Queue()
        sent = []

        class FakeWS:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            def __aiter__(self):
                return self

            async def __anext__(self):
                return await frames.get()

            async def send(self, raw):
                msg = json.loads(raw)
                sent.append(msg)
                frames.put_nowait(json.dumps({"status": "ok", "retcode": 0, "echo": msg["echo"],
                                              "data": {"message_id": 1}}))

        seen_headers = []

        async def connect(url, headers):
            seen_headers.append((url, headers))
            return FakeWS()

        self.app = make_app(plugins={"qq": {"ws_url": "ws://127.0.0.1:3001", "owner_qq": "10001",
                                            "bubble_delay_ms": 0}})
        self.app.memory.start()
        self.app.ws_connect = connect
        scripted(self.app, "WS 收到")
        self.rec, bridge = await load(self.app, "qq", qq_plugin, {"access_token": "tok"})
        frames.put_nowait(json.dumps(private(10001, "通过 WS 来的")))
        for _ in range(200):
            await asyncio.sleep(0.01)
            if sent:
                break
        await bridge.drain()
        self.assertEqual(seen_headers[0], ("ws://127.0.0.1:3001", {"Authorization": "Bearer tok"}))
        self.assertEqual(sent[0]["action"], "send_private_msg")
        self.assertEqual(sent[0]["params"]["message"][0]["data"]["text"], "WS 收到")


class QQWebhookTests(unittest.TestCase):
    def test_webhook_auth_and_end_to_end(self):
        app = make_app(plugins={"qq": dict(QQ_CFG)})
        bot = FakeOneBot()
        app.http_transport = httpx.MockTransport(bot.handler)
        scripted(app, "收到啦")
        asyncio.run(load(app, "qq", qq_plugin, {"access_token": "tok"}))
        with TestClient(create_server(app)) as client:
            url = "/api/hooks/qq/onebot"
            ev = private(10001, "webhook 你好", mid=77)
            self.assertEqual(client.post(url, json=ev).status_code, 401)
            self.assertEqual(client.post(url, json=ev, headers={"Authorization": "Bearer nope"}).status_code, 401)
            self.assertEqual(client.post(url, content=b"{bad", headers={"Authorization": "Bearer tok"}).status_code,
                             400)
            self.assertEqual(client.get(url).status_code, 204)
            r = client.post(url, json=ev, headers={"Authorization": "Bearer tok"})
            self.assertEqual(r.status_code, 204)
            for _ in range(300):
                if bot.sent():
                    break
                time.sleep(0.01)
            self.assertEqual(bot.texts(), ["收到啦"])
            # OneBot HMAC signature and query token are accepted too; duplicates are dropped
            import hashlib
            import hmac as hm
            raw = json.dumps(ev).encode()
            sig = "sha1=" + hm.new(b"tok", raw, hashlib.sha1).hexdigest()
            self.assertEqual(client.post(url, content=raw, headers={"X-Signature": sig}).status_code, 204)
            self.assertEqual(client.post(url + "?access_token=tok", json=ev).status_code, 204)
            time.sleep(0.2)
            self.assertEqual(len(bot.sent()), 1)


# -- media -------------------------------------------------------------------------------------
def wav_bytes():
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * 160)
    return buf.getvalue()


class MediaTests(unittest.IsolatedAsyncioTestCase):
    async def test_needs_config_without_key(self):
        app = make_app(plugins={"media": {"provider": "openai"}})
        rec, svc = await load(app, "media", media_plugin)
        self.assertIsNone(svc)
        self.assertEqual(rec.status, "needs_config")
        self.assertFalse(app.capabilities.has("image.generate"))

    async def test_openai_image_is_stored_as_artifact(self):
        app = make_app(plugins={"media": {"provider": "openai"}})
        seen = []

        def handler(request):
            seen.append(request)
            return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(PNG).decode()}]})

        app.http_transport = httpx.MockTransport(handler)
        await load(app, "media", media_plugin, {"api_key": "sk-media"})
        res = await invoke(app, "image.generate", {"prompt": "一只在月亮上的猫", "size": "16:9"})
        self.assertEqual(res.status, "ok", res.error)
        body = json.loads(seen[0].content)
        self.assertEqual((body["model"], body["size"]), ("gpt-image-1", "1536x1024"))
        self.assertEqual(str(seen[0].url), "https://api.openai.com/v1/images/generations")
        self.assertEqual(seen[0].headers["authorization"], "Bearer sk-media")
        info, data = app.artifacts.read(res.data["artifact_id"])
        self.assertEqual(data, PNG)
        self.assertEqual(info["content_type"], "image/png")
        self.assertEqual(info["meta"]["plugin"], "media")
        self.assertEqual(res.data["url"], f"/api/artifacts/{res.data['artifact_id']}")
        self.assertNotIn("sk-media", res.to_model_text())

    async def test_dashscope_async_image_and_files_on_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = App(tmp, {"plugins": {"media": {"provider": "dashscope"}}})
            polls = []

            def handler(request):
                path = request.url.path
                if path.endswith("/multimodal-generation/generation"):
                    body = json.loads(request.content)
                    self.assertEqual(body["parameters"]["size"], "1328*1328")
                    return httpx.Response(200, json={"output": {"task_id": "t1", "task_status": "PENDING"}})
                if path.endswith("/tasks/t1"):
                    polls.append(1)
                    if len(polls) < 2:
                        return httpx.Response(200, json={"output": {"task_status": "RUNNING"}})
                    return httpx.Response(200, json={"output": {"task_status": "SUCCEEDED", "results": [
                        {"url": "https://oss.example.com/img.jpg"}]}})
                if request.url.host == "oss.example.com":
                    return httpx.Response(200, content=JPEG, headers={"content-type": "image/jpeg"})
                return httpx.Response(404)

            app.http_transport = httpx.MockTransport(handler)
            _, svc = await load(app, "media", media_plugin, {"api_key": "ds-key"})
            svc.poll_interval = 0
            res = await invoke(app, "image.generate", {"prompt": "水墨山水"})
            self.assertEqual(res.status, "ok", res.error)
            self.assertEqual(len(polls), 2)
            self.assertTrue(res.data["path"].startswith(tmp))
            with open(res.data["path"], "rb") as fh:
                self.assertEqual(fh.read(), JPEG)
            self.assertNotIn("oss.example.com", res.to_model_text())
            app.db.close()

    async def test_tts_openai_and_dashscope(self):
        app = make_app(plugins={"media": {"provider": "openai", "voice": "nova"}})
        bodies = []

        def handler(request):
            bodies.append(json.loads(request.content))
            return httpx.Response(200, content=b"ID3" + b"\x00" * 20, headers={"content-type": "audio/mpeg"})

        app.http_transport = httpx.MockTransport(handler)
        await load(app, "media", media_plugin, {"api_key": "k"})
        res = await invoke(app, "speech.synthesize", {"text": "你好，世界"})
        self.assertEqual(res.status, "ok", res.error)
        self.assertEqual(bodies[0]["voice"], "nova")
        self.assertEqual(app.artifacts.get(res.data["artifact_id"])["content_type"], "audio/mpeg")

        app2 = make_app(plugins={"media": {"provider": "dashscope"}})
        wav = wav_bytes()

        def handler2(request):
            body = json.loads(request.content)
            self.assertEqual(body["parameters"]["voice"], "longanfengyue")
            return httpx.Response(200, json={"output": {"audio": {"data": base64.b64encode(wav).decode()}}})

        app2.http_transport = httpx.MockTransport(handler2)
        await load(app2, "media", media_plugin, {"api_key": "k"})
        res = await invoke(app2, "speech.synthesize", {"text": "早上好"})
        self.assertEqual(res.status, "ok", res.error)
        info, data = app2.artifacts.read(res.data["artifact_id"])
        self.assertEqual(info["content_type"], "audio/wav")
        self.assertEqual(data[:4], b"RIFF")

    async def test_provider_errors_are_mapped(self):
        app = make_app(plugins={"media": {"provider": "openai"}})
        app.http_transport = httpx.MockTransport(
            lambda r: httpx.Response(401, json={"error": {"message": "bad key sk-xxx"}}))
        await load(app, "media", media_plugin, {"api_key": "sk-secret-1"})
        res = await invoke(app, "image.generate", {"prompt": "猫"})
        self.assertEqual(res.status, "error")
        self.assertIn("鉴权失败", res.error)
        self.assertNotIn("sk-secret-1", res.error)
        res = await invoke(app, "speech.synthesize", {"text": "x" * 5000})
        self.assertEqual(res.status, "error")
        res = await invoke(app, "image.generate", {"prompt": "猫", "size": "huge"})
        self.assertIn("size", res.error)
        self.assertEqual(media_plugin.normalize_size("dashscope", "1024x1024"), "1328*1328")
        self.assertEqual(media_plugin.normalize_size("openai", "9:16"), "1024x1536")


if __name__ == "__main__":
    unittest.main()
