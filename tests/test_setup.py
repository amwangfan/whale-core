import json
import unittest

import httpx
from starlette.testclient import TestClient

from whale import cli, setup as setup_mod
from whale.server.http import create_server

from helpers import make_app


def fake_provider(models, reply="在线"):
    """A provider that lists ``models`` and answers chat completions."""
    seen = {"auth": []}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"].append(request.headers.get("authorization"))
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": m} for m in models]})
        body = json.loads(request.content)
        seen["model"] = body["model"]
        seen["temperature"] = body.get("temperature", "absent")
        return httpx.Response(200, json={"choices": [{"message": {"content": reply}, "finish_reason": "stop"}],
                                         "model": body["model"]})

    return httpx.MockTransport(handler), seen


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.app = make_app()
        self.client = TestClient(create_server(self.app))
        self.client.__enter__()
        self.h = {"Authorization": "Bearer " + self.app.config.api_token}

    def tearDown(self):
        self.client.__exit__(None, None, None)

    def rpc(self, method, params=None, status=200):
        r = self.client.post("/api/rpc/" + method, json=params or {}, headers=self.h)
        self.assertEqual(r.status_code, status, r.text)
        return r.json()

    def test_status_and_presets(self):
        st = self.rpc("setup.status")["result"]
        self.assertFalse(st["completed"])
        self.assertEqual(st["steps"], {"model": False, "persona": False, "phone": False, "qq": False})
        presets = {p["id"]: p for p in self.rpc("models.presets")["result"]["presets"]}
        self.assertIn("deepseek", presets)
        self.assertFalse(presets["ollama"]["needs_key"])
        self.assertTrue(presets["qwen"]["needs_key"])
        self.assertNotIn("prefer", presets["qwen"])

    def test_quick_model_picks_available_models_and_tests(self):
        transport, seen = fake_provider(["deepseek-v4-pro", "deepseek-flash", "deepseek-reasoner"])
        self.app.http_transport = transport
        res = self.rpc("setup.quick_model", {"preset": "deepseek", "api_key": "sk-test"})["result"]
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["chosen"], {"chat": "deepseek-flash", "fast": "deepseek-flash"})
        self.assertEqual(seen["auth"][0], "Bearer sk-test")
        self.assertFalse(res["status"]["offline"])
        cfg = self.rpc("config.get")["result"]
        self.assertNotIn("sk-test", json.dumps(cfg))
        self.assertTrue(self.rpc("setup.status")["result"]["steps"]["model"])
        self.rpc("setup.complete", {"name": "小鲸"})
        st = self.rpc("setup.status")["result"]
        self.assertTrue(st["completed"])
        self.assertEqual(self.rpc("config.get")["result"]["agent"]["name"], "小鲸")

    def test_renamed_models_are_discovered(self):
        transport, seen = fake_provider(["qwen3.9-plus", "qwen-flash", "text-embedding-v4", "qwen3-vl-plus"])
        self.app.http_transport = transport
        res = self.rpc("setup.quick_model", {"preset": "qwen", "api_key": "k"})["result"]
        self.assertEqual(res["chosen"]["chat"], "qwen3.9-plus")
        self.assertEqual(res["chosen"]["fast"], "qwen-flash")
        self.assertEqual(res["chosen"]["vision"], "qwen3-vl-plus")
        self.assertEqual(res["chosen"]["embedding"], "text-embedding-v4")

    def test_kimi_sends_no_temperature(self):
        transport, seen = fake_provider(["kimi-k3"])
        self.app.http_transport = transport
        res = self.rpc("setup.quick_model", {"preset": "kimi", "api_key": "k"})["result"]
        self.assertTrue(res["ok"], res)
        self.assertEqual(seen["temperature"], "absent")

    def test_key_required_and_ollama_without_models(self):
        bad = self.rpc("setup.quick_model", {"preset": "openai"}, status=400)
        self.assertEqual(bad["error"]["code"], "invalid_params")
        transport, _ = fake_provider([])
        self.app.http_transport = transport
        res = self.rpc("setup.quick_model", {"preset": "ollama"})["result"]
        self.assertFalse(res["ok"])
        self.assertIn("Ollama", res["test"]["error"])

    def test_network_lan_toggle_and_restart(self):
        self.app.setup.bound = ("127.0.0.1", 8080)
        net = self.rpc("system.network")["result"]
        self.assertFalse(net["lan_enabled"])
        self.assertEqual(net["urls"], [])
        net = self.rpc("system.set_lan", {"enabled": True})["result"]
        self.assertTrue(net["lan_enabled"])
        self.assertTrue(net["restart_required"])
        self.assertEqual(self.app.config.value.server.host, "0.0.0.0")
        err = self.rpc("system.restart", status=503)
        self.assertEqual(err["error"]["code"], "unavailable")
        calls = []
        self.app.setup.restart_hook = lambda: calls.append(1)
        self.assertTrue(self.rpc("system.restart")["result"]["restarting"])

    def test_pick(self):
        self.assertEqual(setup_mod.pick([], "a", [r"b"]), "a")
        self.assertEqual(setup_mod.pick(["x", "a"], "a", [r"x"]), "a")
        self.assertEqual(setup_mod.pick(["bge-m3", "llama3"], "", [r"."]), "llama3")  # never an embedding model
        self.assertEqual(setup_mod.pick(["qwen3-vl-plus", "qwen3.9-plus"], "x", [r"plus"]), "qwen3.9-plus")
        self.assertEqual(setup_mod.pick(["qwen3-vl-plus", "qwen3.9-plus"], "x", [r"vl"], vision=True), "qwen3-vl-plus")


class CliTests(unittest.TestCase):
    def test_plain_whale_means_serve(self):
        self.assertEqual(cli.normalize_argv([]), ["serve"])
        self.assertEqual(cli.normalize_argv(["--no-open"]), ["serve", "--no-open"])
        self.assertEqual(cli.normalize_argv(["--data", "D:/w", "--port", "9000"]), ["--data", "D:/w", "serve", "--port", "9000"])
        self.assertEqual(cli.normalize_argv(["token", "--rotate"]), ["token", "--rotate"])


if __name__ == "__main__":
    unittest.main()
