"""模型面增量：reasoning_effort 透传 + 回复档位（reply_gear 慢档缓冲）。

照 DEVELOPMENT §3：ScriptedModel 注入 + httpx.MockTransport 截获请求体。
"""

import json
import unittest

import httpx

from helpers import make_app, scripted


class ReasoningEffortTests(unittest.IsolatedAsyncioTestCase):
    async def test_temperature_omitted_when_null(self):
        """kimi-k3 纪律：temperature=null → 请求体不下发该参数（实测 0.7 会被 kimi-k3 400 拒）。"""
        seen = []

        def handler(request):
            seen.append(json.loads(request.content))
            data = {"choices": [{"delta": {"content": "好"}, "finish_reason": "stop"}]}
            return httpx.Response(200, content=("data: " + json.dumps(data) + "\n\ndata: [DONE]\n\n").encode(),
                                  headers={"content-type": "text/event-stream"})

        app = make_app(models={"providers": [{"id": "kimi", "kind": "openai",
                                              "base_url": "https://api.moonshot.cn/v1", "label": "Kimi"}],
                               "chat": {"provider": "kimi", "model": "kimi-k3", "temperature": None}})
        app.config.set_secret("provider.kimi", "sk-test")
        app.http_transport = httpx.MockTransport(handler)
        out = await app.models.test("chat")
        self.assertTrue(out["ok"], out)
        self.assertNotIn("temperature", seen[-1])
        app2 = make_app(models={"providers": [{"id": "ds", "kind": "openai", "base_url": "https://api.deepseek.com/v1"}],
                                "chat": {"provider": "ds", "model": "deepseek-chat"}})
        app2.config.set_secret("provider.ds", "sk-test")
        app2.http_transport = httpx.MockTransport(handler)
        await app2.models.test("chat")
        self.assertEqual(seen[-1].get("temperature"), 0.7)   # 默认行为不变
        app.db.close()
        app2.db.close()

    async def test_reasoning_effort_passed_only_when_set(self):
        seen = []

        def handler(request):
            seen.append(json.loads(request.content))
            body = {"choices": [{"message": {"content": "在线"}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1}}
            # 流式响应（SSE）
            data = {"choices": [{"delta": {"content": "在线"}, "finish_reason": "stop"}]}
            return httpx.Response(200, content=("data: " + json.dumps(data) + "\n\ndata: [DONE]\n\n").encode(),
                                  headers={"content-type": "text/event-stream"})

        app = make_app(models={"providers": [{"id": "kimi", "kind": "openai",
                                              "base_url": "https://api.moonshot.cn/v1", "label": "Kimi"}],
                               "chat": {"provider": "kimi", "model": "kimi-k3", "reasoning_effort": "high"},
                               "fast": {"provider": "kimi", "model": "kimi-k3"}})  # fast 不设强度
        app.config.set_secret("provider.kimi", "sk-test")
        app.http_transport = httpx.MockTransport(handler)
        out = await app.models.test("chat")
        self.assertTrue(out["ok"], out)
        self.assertEqual(seen[-1].get("reasoning_effort"), "high")       # chat 角色透传 high
        await app.models.test("fast")
        self.assertNotIn("reasoning_effort", seen[-1])                   # 未设即不下发该参数
        app.db.close()

    async def test_reasoning_effort_400_falls_back_to_omitted(self):
        """服务商 400 拒 reasoning 参数 → 自动去掉重试一次（降级为不下发），不重炸调用方。"""
        seen = []

        def handler(request):
            body = json.loads(request.content)
            seen.append(body)
            if "reasoning_effort" in body:
                return httpx.Response(400, json={"error": {"message": "unknown field reasoning_effort"}})
            data = {"choices": [{"delta": {"content": "在线"}, "finish_reason": "stop"}]}
            return httpx.Response(200, content=("data: " + json.dumps(data) + "\n\ndata: [DONE]\n\n").encode(),
                                  headers={"content-type": "text/event-stream"})

        app = make_app(models={"providers": [{"id": "kimi", "kind": "openai",
                                              "base_url": "https://api.moonshot.cn/v1"}],
                               "chat": {"provider": "kimi", "model": "kimi-k3", "reasoning_effort": "minimal"}})
        app.config.set_secret("provider.kimi", "sk-test")
        app.http_transport = httpx.MockTransport(handler)
        out = await app.models.test("chat")
        self.assertTrue(out["ok"], out)
        self.assertEqual(len(seen), 2)                                   # 恰好重试一次
        self.assertEqual(seen[0].get("reasoning_effort"), "minimal")
        self.assertNotIn("reasoning_effort", seen[1])
        app.db.close()


class ReplyGearTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_gear_buffers_deltas(self):
        app = make_app()
        scripted(app, "一句完整的话")
        deltas = []
        sub = app.bus.stream(["run.delta"])

        async def collect():
            while True:
                ev = await sub.get()
                deltas.append(ev.data["delta"])

        import asyncio
        task = asyncio.create_task(collect())
        from whale.agent.runtime import RunRequest
        out = await app.agent.run(RunRequest(text="说点什么", channel="console"))
        await asyncio.sleep(0.05)
        task.cancel()
        sub.close()
        self.assertEqual(out.text, "一句完整的话")
        self.assertTrue(deltas)                                          # 默认快档：有流式增量

        app.config.update({"agent": {"reply_gear": "slow"}})
        scripted(app, "想完再一次给")
        deltas.clear()
        task = asyncio.create_task(collect())
        out = await app.agent.run(RunRequest(text="再来", channel="console"))
        await asyncio.sleep(0.05)
        task.cancel()
        self.assertEqual(out.text, "想完再一次给")                        # 回复完整
        self.assertEqual(deltas, [])                                     # 慢档：零增量事件
        app.db.close()

    async def test_reply_gear_validation(self):
        app = make_app()
        from whale.kernel.errors import WhaleError
        with self.assertRaises(WhaleError):
            app.config.update({"agent": {"reply_gear": "turbo"}})        # 非法档位拒绝
        app.db.close()


if __name__ == "__main__":
    unittest.main()
