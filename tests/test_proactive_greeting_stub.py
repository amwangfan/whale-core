from whale_core.life.proactive_greeting import (
    ProactiveGreetingConfig, ProactiveGreetingLoop, AGENTSCOPE_BASE_URL)


def test_default_off():
    cfg = ProactiveGreetingConfig.from_env({})
    assert cfg.enabled is False
    assert cfg.interval_s == 180
    assert cfg.judge_base_url == AGENTSCOPE_BASE_URL
    tick = ProactiveGreetingLoop(config=cfg).tick()
    assert tick.should_speak is False
    assert tick.reason == "disabled"


def test_enabled_without_judge():
    cfg = ProactiveGreetingConfig.from_env({"WHALE_PROACTIVE_GREETING": "1"})
    assert cfg.enabled is True
    tick = ProactiveGreetingLoop(config=cfg).tick()
    assert tick.should_speak is False
    assert tick.reason == "no_judge"
