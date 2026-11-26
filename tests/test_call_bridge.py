"""VoIP 呼出一期：东京呼叫信令桥（devices/callbridge.py + phone.call.start/stop 能力）。

照 DEVELOPMENT §3：MockTransport 假 ring_hub（记录请求、可编排回执），全占位符 id，
断言真实三态（delivered→ok / queued→unknown / 非2xx→error）与秘密卫生（token 不入错误文本）。
"""

import unittest
from urllib.parse import parse_qs

import httpx

from whale.capabilities.spec import ToolContext
from whale.devices.callbridge import BridgeSignalClient, accept_ring_outcome, validate_bridge_url
from whale.kernel.errors import WhaleError

from helpers import make_app

BRIDGE_CFG = {"call_bridge": {"url": "http://192.0.2.10:18799", "device_id": "dev-test-01"}}


class FakeRingHub:
    """ring_hub 假实现：录请求，按 mode 编排回执；鉴权头不对回 401。"""

    def __init__(self):
        self.calls = []
        self.mode = "delivered"

    def handler(self, request):
        self.calls.append(request)
        if request.headers.get("authorization") != "Bearer sig-tok":
            return httpx.Response(401, json={"error": {"message": "unauthorized"}})
        path = request.url.path
        if path == "/v1/ring":
            if self.mode == "delivered":
                out = {"ok": True, "delivered": True, "queued": False, "device_id": "dev-test-01", "msg": ""}
            elif self.mode == "queued":
                out = {"ok": True, "delivered": False, "queued": True, "device_id": "dev-test-01", "msg": ""}
            else:
                out = {"ok": False, "msg": "device unknown"}
            return httpx.Response(200, json=out)
        if path == "/v1/hangup":
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404, json={})


class BridgeUrlTests(unittest.TestCase):
    def test_url_validation(self):
        self.assertEqual(validate_bridge_url("http://192.0.2.10:18799/"), "http://192.0.2.10:18799")
        self.assertEqual(validate_bridge_url("https://bridge.example.com"), "https://bridge.example.com")
        # tailnet CGNAT 段（100.64.0.0/10）放行：wireguard 自带加密（生产实测：
        # ipaddress.is_private 对 CGNAT 段为 False，曾误拒 tailnet 桥地址）
        self.assertEqual(validate_bridge_url("http://100.64.0.1:18800"), "http://100.64.0.1:18800")
        with self.assertRaises(WhaleError):
            validate_bridge_url("")                                      # 空
        with self.assertRaises(WhaleError):
            validate_bridge_url("http://8.8.8.8:18799")                  # 公网明文 http 拒
        with self.assertRaises(WhaleError) as ctx:
            validate_bridge_url("https://user:s3cr3t-pw@bridge.example.com")
        self.assertNotIn("s3cr3t-pw", ctx.exception.message)             # 内嵌凭据不进错误文本
        with self.assertRaises(WhaleError):
            validate_bridge_url("https://bridge.example.com/?token=x")   # 不带 query

    def test_outcome_gate(self):
        self.assertEqual(accept_ring_outcome({"ok": True, "delivered": True})["status"], "ok")
        q = accept_ring_outcome({"ok": True, "queued": True})
        self.assertEqual((q["status"], q["queued"]), ("unknown", True))  # 排队如实 unknown
        self.assertEqual(accept_ring_outcome({"ok": False, "msg": "device unknown"})["status"], "error")
        self.assertEqual(accept_ring_outcome({"ok": True})["status"], "error")   # 既不投递也不排队=异常如实
        self.assertEqual(accept_ring_outcome(None)["status"], "error")


class BridgeClientTests(unittest.IsolatedAsyncioTestCase):
    async def setup_client(self, mode="delivered"):
        self.app = make_app()
        self.hub = FakeRingHub()
        self.hub.mode = mode
        self.app.http_transport = httpx.MockTransport(self.hub.handler)
        return BridgeSignalClient(self.app, BRIDGE_CFG["call_bridge"]["url"], "sig-tok")

    async def test_ring_delivered(self):
        client = await self.setup_client("delivered")
        out = await client.ring("dev-test-01", name="测试鱼")
        self.assertEqual(out["status"], "ok")
        req = self.hub.calls[0]
        self.assertEqual(req.url.path, "/v1/ring")
        self.assertEqual(req.headers.get("authorization"), "Bearer sig-tok")
        q = parse_qs(req.url.query.decode())
        self.assertEqual(q["device_id"], ["dev-test-01"])
        self.assertEqual(q["name"], ["测试鱼"])
        await client.close()
        self.app.db.close()

    async def test_ring_queued_is_unknown_truthful(self):
        client = await self.setup_client("queued")
        out = await client.ring("dev-test-01")
        self.assertEqual(out["status"], "unknown")        # 离线排队不谎报已响
        self.assertIn("排队", out["error"])
        await client.close()
        self.app.db.close()

    async def test_ring_unauthorized_keeps_token_out(self):
        app = make_app()
        self.hub = FakeRingHub()
        app.http_transport = httpx.MockTransport(self.hub.handler)
        client = BridgeSignalClient(app, BRIDGE_CFG["call_bridge"]["url"], "wrong-tok")
        with self.assertRaises(WhaleError) as ctx:
            await client.ring("dev-test-01")
        self.assertEqual(ctx.exception.code, "unauthorized")
        self.assertNotIn("wrong-tok", ctx.exception.message)
        await client.close()
        app.db.close()

    async def test_hangup(self):
        client = await self.setup_client()
        self.assertEqual((await client.hangup("dev-test-01"))["status"], "ok")
        self.assertEqual(self.hub.calls[-1].url.path, "/v1/hangup")
        await client.close()
        self.app.db.close()

    async def test_fail_closed_without_token(self):
        app = make_app()
        with self.assertRaises(WhaleError):
            BridgeSignalClient(app, BRIDGE_CFG["call_bridge"]["url"], "")
        app.db.close()


class CallCapabilityTests(unittest.IsolatedAsyncioTestCase):
    async def setup_app(self, **kw):
        self.app = make_app(devices=kw.get("devices", BRIDGE_CFG))
        self.hub = FakeRingHub()
        self.hub.mode = kw.get("mode", "delivered")
        self.app.http_transport = httpx.MockTransport(self.hub.handler)
        self.app.config.set_secret("devices.call_bridge.token", "sig-tok")
        return self.app

    async def teardown(self):
        self.app.db.close()

    async def test_call_start_end_to_end(self):
        app = await self.setup_app()
        self.assertTrue(app.capabilities.has("phone.call.start"))
        spec = app.capabilities.get("phone.call.start")
        self.assertTrue(spec.owner_only)                                # 只准主人被叫
        res = await app.capabilities.invoke("phone.call.start", {}, ToolContext(app=app))
        self.assertEqual(res.status, "ok")
        q = parse_qs(self.hub.calls[0].url.query.decode())
        self.assertEqual(q["device_id"], ["dev-test-01"])
        self.assertEqual(q["name"], ["鲸鱼"])                            # 默认来电显示名=智能体名
        await self.teardown()

    async def test_call_start_queued_is_unknown(self):
        app = await self.setup_app(mode="queued")
        res = await app.capabilities.invoke("phone.call.start", {}, ToolContext(app=app))
        self.assertEqual(res.status, "unknown")                         # 排队如实
        await self.teardown()

    async def test_call_stop(self):
        app = await self.setup_app()
        res = await app.capabilities.invoke("phone.call.stop", {}, ToolContext(app=app))
        self.assertEqual(res.status, "ok")
        self.assertEqual(self.hub.calls[0].url.path, "/v1/hangup")
        await self.teardown()

    async def test_unconfigured_is_truthful(self):
        self.app = make_app()                                           # 无 call_bridge 配置
        self.app.config.set_secret("devices.call_bridge.token", "sig-tok")
        res = await self.app.capabilities.invoke("phone.call.start", {}, ToolContext(app=self.app))
        self.assertEqual(res.status, "error")
        self.assertIn("call_bridge", res.error)
        await self.teardown()

    async def test_missing_token_fails_closed(self):
        self.app = make_app(devices=BRIDGE_CFG)                         # 有配置无 token
        res = await self.app.capabilities.invoke("phone.call.start", {}, ToolContext(app=self.app))
        self.assertEqual(res.status, "error")
        self.assertNotIn("sig-tok", res.error or "")
        await self.teardown()


if __name__ == "__main__":
    unittest.main()
