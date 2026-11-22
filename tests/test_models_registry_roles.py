"""裁决①②③ 后端测试：registry 装配消费点 / 唯一事实源 / 逐角色测试。

- ① profiles 装配注册表优先（llm.chat 与角色），未命中回落既有路径（不回归）；
- ② read_models_view 读穿注册表；POST /settings models{} 写穿注册表
  （config 文件 models 不再写——防双写漂移）；
- ③ POST /models/roles/{role}/test 逐角色连通（unbound/config/未知角色）。
"""
from __future__ import annotations

import json
from http.client import HTTPConnection

import pytest

from whale_core.api.server import ApiContext, WhaleApiServer
from whale_core.configuration import read_models_view
from whale_core.life.model_registry import (
    DEFAULT_REGISTRY,
    get_roles,
    registry_entries,
    set_role,
    upsert_entry,
)

TOKEN = "mgmt-test-token"


@pytest.fixture()
def store():
    from whale_core.memory.store import InMemoryStore
    return InMemoryStore()


@pytest.fixture()
def api(store):
    ctx = ApiContext(store=store, profile="demo")
    server = WhaleApiServer(ctx, port=0, token=TOKEN)
    server.start()
    yield server
    server.stop()


def _req(server, method, path, *, body=None):
    conn = HTTPConnection("127.0.0.1", server.port, timeout=15)
    headers = {"Authorization": f"Bearer {TOKEN}"}
    payload = json.dumps(body) if body is not None else None
    if payload:
        headers["Content-Type"] = "application/json"
    conn.request(method, path, body=payload, headers=headers)
    resp = conn.getresponse()
    raw = resp.read()
    conn.close()
    return resp.status, json.loads(raw.decode("utf-8"))


# 注册表测试 env：agentscope 链给 key（resolve_brain 才能装配）
ENV = {
    "WHALE_ASR_BASE_URL": "https://platform.agentscope.io/v1",
    "WHALE_ASR_API_KEY": "asr-key",
    "QQ_INTENT_BASE_URL": "https://platform.agentscope.io/v1",
    "QQ_INTENT_API_KEY": "qq-key",
    "DEEPSEEK_BASE_URL": "https://api.deepseek.com/v1",
    "DEEPSEEK_API_KEY": "ds-key",
    "DEEPSEEK_MODEL": "deepseek-flash",
    "WHALE_REPLY_MODEL": "v4-pro-0813",
    "WHALE_VISION_MODEL": "qwen4-max",
    "QQ_INTENT_MODEL": "kimi-k3",
}


class TestRoleMap:
    def test_seed_roles_from_env(self, store):
        roles = get_roles(store, ENV)
        assert roles["reply_fast"] == "v4-pro-0813"
        assert roles["vision"] == "qwen4-max"
        assert roles["intent"] == "kimi-k3"
        assert "reply_slow" not in roles  # 无 WHALE_SLOW_MODEL 不绑
        assert "executor" not in roles    # 无 WHALE_EXECUTOR_MODEL 不绑

    def test_set_role_validates_membership(self, store):
        assert set_role(store, "reply_slow", "kimi-k3") is True
        assert set_role(store, "nope", "kimi-k3") is False
        assert set_role(store, "intent", "not-in-registry") is False
        assert get_roles(store)["reply_slow"] == "kimi-k3"
        assert set_role(store, "reply_slow", None) is True
        assert "reply_slow" not in get_roles(store)


class TestAssemblyRegistryFirst:
    """① profiles 装配：注册表优先，回落不回归。"""

    def test_reply_brain_from_registry(self, store):
        from whale_core.profiles import assemble_runtime
        bundle = assemble_runtime("cloud", store=store, env=dict(ENV),
                                  use_saved_config=False, start_workers=False)
        from whale_core.plugins.model_provider import require_chat_model
        model = require_chat_model(bundle.registry)
        # 注册表 v4-pro-0813 → deepseek-v4-pro-0813 @ agentscope
        assert model.model == "deepseek-v4-pro-0813"
        assert "agentscope" in (model.base_url or "")

    def test_fallback_when_registry_unresolvable(self, store):
        from whale_core.profiles import assemble_runtime
        env = dict(ENV)
        env["WHALE_REPLY_MODEL"] = "not-in-registry"  # 未命中注册表
        # 裸 id 回落：resolve_brain 裸 id 路径仍可装配（走 agentscope/qq 链）
        bundle = assemble_runtime("cloud", store=store, env=env,
                                  use_saved_config=False, start_workers=False)
        from whale_core.plugins.model_provider import require_chat_model
        assert require_chat_model(bundle.registry) is not None

    def test_intent_role_from_registry(self, store):
        from whale_core.profiles import assemble_runtime
        bundle = assemble_runtime("cloud", store=store, env=dict(ENV),
                                  use_saved_config=False, start_workers=False)
        from whale_core.plugins.model_provider import resolve_role_model
        intent = resolve_role_model(bundle.registry, "intent")
        assert intent is not None and intent.model == "kimi-k3"


class TestSingleSourceView:
    """② read_models_view 读穿注册表 + 写穿。"""

    def test_view_reads_registry_when_bound(self, store, tmp_path):
        # 绑定 intent → kimi-k3（注册表），视图应反映注册表
        set_role(store, "intent", "kimi-k3")
        view = read_models_view(str(tmp_path), env=ENV, store=store)
        assert view["intent"]["model"] == "kimi-k3"
        assert view["intent"]["registry_entry"] == "kimi-k3"
        assert view["intent"]["api_key_configured"] is True  # qq-key 链命中
        # 未绑定角色（executor）回落旧径
        assert "registry_entry" not in view["executor"]

    def test_view_legacy_fallback_unbound(self, store, tmp_path):
        view = read_models_view(str(tmp_path), env=ENV, store=store)
        # get_roles 首读按 env 播种：reply_fast ← WHALE_REPLY_MODEL=v4-pro-0813
        # → 读穿注册表，视图反映注册表条目的真实模型 id（deepseek-v4-pro-0813）
        assert view["chat"]["model"] == "deepseek-v4-pro-0813"
        assert view["chat"]["registry_entry"] == "v4-pro-0813"


class TestPerRoleTest:
    """③ 逐角色连通测试端点。"""

    def test_unknown_role_404(self, api):
        status, body = _req(api, "POST", "/api/v1/models/roles/nope/test")
        assert status == 404

    def test_unbound_role(self, api):
        status, body = _req(api, "POST", "/api/v1/models/roles/reply_slow/test")
        assert status == 200
        assert body["result"]["ok"] is False
        assert body["result"]["error_kind"] == "unbound"

    def test_bound_role_config_fail_closed(self, api):
        # 绑定 intent → kimi-k3，但无 key（InMemoryStore 无 providers，env 无 key）
        set_role(api.ctx.store, "intent", "kimi-k3")
        status, body = _req(api, "POST", "/api/v1/models/roles/intent/test")
        assert status == 200
        assert body["result"]["ok"] is False
        assert body["result"]["error_kind"] == "config"

    def test_registry_view(self, api):
        status, body = _req(api, "GET", "/api/v1/models/registry")
        assert status == 200
        assert isinstance(body["result"]["entries"], list)
        assert isinstance(body["result"]["roles"], dict)
        # 默认注册表三条
        names = [e["name"] for e in body["result"]["entries"]]
        assert set(names) == {e["name"] for e in DEFAULT_REGISTRY}


class TestTwoTierNoRegression:
    """① 硬约束：现网两档回复逐字一致——生产 env 经 resolve_brain 的装配结果
    与现网一致（env 链优先，entry.base_url 仅作回落）。"""

    PROD_ENV = {
        "WHALE_ASR_BASE_URL": "https://platform.agentscope.io/v1",
        "WHALE_ASR_API_KEY": "asr-key",
        "QQ_INTENT_BASE_URL": "https://platform.agentscope.io/v1",
        "QQ_INTENT_API_KEY": "qq-key",
        "DEEPSEEK_BASE_URL": "https://api.deepseek.com/v1",
        "DEEPSEEK_API_KEY": "ds-key",
        "WHALE_REPLY_MODEL": "v4-pro-0813",
        "WHALE_VISION_MODEL": "qwen4-max",
        "QQ_INTENT_MODEL": "kimi-k3",
    }

    def test_reply_brain_byte_identical(self, store):
        from whale_core.life.model_registry import resolve_brain
        brain = resolve_brain("v4-pro-0813", env=self.PROD_ENV, store=store)
        assert brain is not None
        # 现网值逐字一致：模型 id 与 agentscope 端点
        assert brain.model == "deepseek-v4-pro-0813"
        assert brain.base_url == "https://platform.agentscope.io/v1"

    def test_slow_tier_brain_from_registry(self, store):
        from whale_core.life.model_registry import resolve_brain
        brain = resolve_brain("kimi-k3", env=self.PROD_ENV, store=store)
        assert brain is not None and brain.model == "kimi-k3"


class TestWriteThrough:
    """② POST /settings models{} 写穿注册表。"""

    def test_settings_write_goes_to_registry(self, api):
        # 经 SettingsController 写 models.intent
        from whale_core.configuration import SettingsController
        from whale_core.profiles import assemble_runtime
        import tempfile
        with tempfile.TemporaryDirectory() as cfg:
            bundle = assemble_runtime("cloud", store=api.ctx.store, env=dict(ENV),
                                      use_saved_config=False, start_workers=False)
            ctl = SettingsController(cfg, bundle, env=dict(ENV))
            ctl.update({"models": {"intent": {"model": "kimi-k3-0905",
                                              "base_url": "https://platform.agentscope.io/v1"}}})
            entries = {e["name"]: e for e in registry_entries(api.ctx.store)}
            assert "role-intent" in entries
            assert entries["role-intent"]["model"] == "kimi-k3-0905"
            assert get_roles(api.ctx.store)["intent"] == "role-intent"

class TestBindAndUpsert:
    """③ 补充：角色绑定与注册表 upsert 端点（真实 REST 面）。"""

    def test_bind_and_unbind(self, api):
        status, body = _req(api, "POST", "/api/v1/models/roles/reply_slow",
                            body={"name": "kimi-k3"})
        assert status == 200 and body["result"]["name"] == "kimi-k3"
        assert get_roles(api.ctx.store)["reply_slow"] == "kimi-k3"
        status, body = _req(api, "POST", "/api/v1/models/roles/reply_slow",
                            body={"name": None})
        assert status == 200
        assert "reply_slow" not in get_roles(api.ctx.store)

    def test_bind_unknown_role_404(self, api):
        status, _ = _req(api, "POST", "/api/v1/models/roles/nope", body={"name": "x"})
        assert status == 404

    def test_bind_entry_not_in_registry_400(self, api):
        status, body = _req(api, "POST", "/api/v1/models/roles/intent",
                            body={"name": "ghost-entry"})
        assert status == 400

    def test_upsert_entry(self, api):
        status, body = _req(api, "POST", "/api/v1/models/registry",
                            body={"name": "qwen4-turbo", "provider": "agentscope",
                                  "model": "qwen4-turbo-0901", "effort": "medium"})
        assert status == 200 and body["result"]["name"] == "qwen4-turbo"
        names = [e["name"] for e in registry_entries(api.ctx.store)]
        assert "qwen4-turbo" in names
