from whale_core.life.proactive_greeting import (
    ProactiveGreetingConfig, ProactiveGreetingLoop, AGENTSCOPE_BASE_URL)


def test_default_off():
    cfg = ProactiveGreetingConfig.from_env({})
    assert cfg.enabled is False
    assert cfg.dry_run is True
    assert cfg.interval_s == 180
    assert cfg.judge_base_url == AGENTSCOPE_BASE_URL
    tick = ProactiveGreetingLoop(config=cfg).tick()
    assert tick.should_speak is False
    assert tick.reason == "disabled"


def test_enabled_without_judge():
    cfg = ProactiveGreetingConfig.from_env({"WHALE_PROACTIVE_GREETING": "1"})
    assert cfg.enabled is True
    assert cfg.dry_run is True  # 开启默认 dry-run
    tick = ProactiveGreetingLoop(config=cfg).tick()
    assert tick.should_speak is False
    assert tick.reason == "no_judge"


def test_dry_run_does_not_send():
    class J:
        def reply(self, text, **kw):
            return "yes"
    class R:
        def reply(self, text, **kw):
            return "嗨，在吗？"
    sent = []
    cfg = ProactiveGreetingConfig.from_env({
        "WHALE_PROACTIVE_GREETING": "1",
        # DRY_RUN 缺省 → True
    })
    tick = ProactiveGreetingLoop(
        config=cfg, judge=J(), reply_model=R(), send=lambda s: sent.append(s)
    ).tick(situation="下午闲着")
    assert tick.should_speak and tick.reason == "dry_run" and tick.draft
    assert sent == []


def test_real_send_when_dry_run_off():
    class J:
        def reply(self, text, **kw):
            return "yes"
    class R:
        def reply(self, text, **kw):
            return "下午好"
    sent = []
    cfg = ProactiveGreetingConfig.from_env({
        "WHALE_PROACTIVE_GREETING": "1",
        "WHALE_PROACTIVE_GREETING_DRY_RUN": "0",
    })
    assert cfg.dry_run is False
    tick = ProactiveGreetingLoop(
        config=cfg, judge=J(), reply_model=R(), send=lambda s: sent.append(s)
    ).tick()
    assert tick.sent and sent == ["下午好"]


def test_background_start_requires_enabled():
    loop = ProactiveGreetingLoop(config=ProactiveGreetingConfig())
    assert loop.start_background() is False
