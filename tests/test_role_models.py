"""分角色模型矩阵 + 安全审核钩子测试（全离线）。"""

from __future__ import annotations

from whale_core.plugins.model_provider import resolve_role_model
from whale_core.plugins.registry import PluginRegistry
from whale_core.profiles import assemble_runtime
from whale_core.life.channels import OwnerEvent
from whale_core.life.initiative import ProactivePrefs
from whale_core.life.qq_workflow import QqInboundWorkflow
from whale_core.memory.store import InMemoryStore


def test_role_model_registration_and_resolution():
    """WHALE_INTENT_* 配齐 → llm.intent 可解析；未配的角色回落 llm.chat。"""
    bundle = assemble_runtime("local", env={
        "WHALE_INTENT_BASE_URL": "http://127.0.0.1:9999/v1",
        "WHALE_INTENT_MODEL": "qwen3.8-flash",
        "WHALE_INTENT_API_KEY": "k-intent",
    })
    registry = bundle.registry
    intent = resolve_role_model(registry, "intent")
    assert intent is not None and intent.model == "qwen3.8-flash"
    assert intent.api_key == "k-intent"
    # executor 未配置 → 回落 llm.chat；safety 跟随意图（合并角色）
    assert resolve_role_model(registry, "executor").model == "local-model"
    assert resolve_role_model(registry, "safety").model == "qwen3.8-flash"


def test_role_model_same_endpoint_inherits_credentials():
    """只配模型名（无 base_url）→ 复用主端点与凭据（意图=审核合并角色）。"""
    bundle = assemble_runtime("cloud", env={
        "DEEPSEEK_API_KEY": "k-main",
        "WHALE_INTENT_MODEL": "deepseek-v4-pro-0813",
    })
    intent = resolve_role_model(bundle.registry, "intent")
    assert intent.model == "deepseek-v4-pro-0813"
    assert intent.api_key == "k-main"  # 复用主端点凭据
    # safety 与 intent 是同一角色（合并）
    assert resolve_role_model(bundle.registry, "safety").model == "deepseek-v4-pro-0813"


class _FakeBrain:
    def reply(self, text, *, persona="", memories=(), history=()):
        return "回复文本"


def _workflow(reviewer):
    prefs = ProactivePrefs(enabled=True,
                           allowed_channels=frozenset({"direct_message"}),
                           passive_daily_limit=500,
                           passive_reply_in_quiet=True)  # 不卡静默时段，专注审核逻辑
    return QqInboundWorkflow(InMemoryStore(), _FakeBrain(), prefs=prefs,
                             safety_reviewer=reviewer)


def _event():
    return OwnerEvent(owner_id="o", channel="qq", conversation="c1",
                      message_id="m1", text="你好")


def test_safety_reviewer_pass_through():
    wf = _workflow(lambda reply: None)  # 审核通过
    result = wf.handle_owner_event(_event())
    assert "safety_concern" not in result  # 审核无顾虑不打标
    assert "安全审核" not in result["reason"]


def test_safety_reviewer_concern_marks_for_human():
    """有顾虑：结果打 safety_concern 标（runner 据此不自动发送，留人工）。"""
    wf = _workflow(lambda reply: "可能泄漏隐私")
    result = wf.handle_owner_event(_event())
    assert result["safety_concern"] == "可能泄漏隐私"
    assert "安全审核" in result["reason"]


def test_safety_reviewer_none_default_unchanged():
    wf = _workflow(None)
    result = wf.handle_owner_event(_event())
    assert "安全审核" not in result.get("reason", "")


def test_settings_models_roundtrip(tmp_path):
    """设置 API 的 models 矩阵：写入 → 脱敏视图 → 不落盘明文 key。"""
    from whale_core.configuration import SettingsController, read_models_view
    bundle = assemble_runtime("local", env={}, store=InMemoryStore())
    ctl = SettingsController(tmp_path, bundle, env={})
    ctl.update({"models": {"intent": {"base_url": "http://127.0.0.1:9999/v1",
                                      "model": "qwen3.8-flash", "api_key": "sekrit"}}})
    view = read_models_view(tmp_path)
    assert view["intent"]["model"] == "qwen3.8-flash"
    assert view["intent"]["api_key_configured"] is True
    # key 本体不落公共配置
    import json
    raw = (tmp_path / "config.json").read_text(encoding="utf-8")
    assert "sekrit" not in raw
    import json as _j
    private = _j.loads((tmp_path / "secrets" / "settings.json").read_text(encoding="utf-8"))
    assert private["private"]["models_intent_api_key"] == "sekrit"


def test_settings_models_unknown_role_rejected(tmp_path):
    from whale_core.configuration import SettingsController
    bundle = assemble_runtime("local", env={}, store=InMemoryStore())
    ctl = SettingsController(tmp_path, bundle, env={})
    try:
        ctl.update({"models": {"yolo": {"model": "x"}}})
    except ValueError:
        return
    raise AssertionError("unknown role must be rejected")
