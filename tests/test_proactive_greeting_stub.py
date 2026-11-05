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


def test_quota_blocks_real_send():
    from whale_core.life.proactive_greeting import (
        ProactiveGreetingConfig, ProactiveGreetingLoop, record_send, LAST_SENT_KEY)

    class Mem:
        def __init__(self):
            self.d = {}
        def get(self, k):
            return self.d.get(k)
        def put(self, k, v):
            self.d[k] = v

    class J:
        def reply(self, text, **kw):
            return "yes"
    class R:
        def reply(self, text, **kw):
            return "嗨"

    store = Mem()
    record_send(store, now=1_000_000.0)
    sent = []
    cfg = ProactiveGreetingConfig.from_env({
        "WHALE_PROACTIVE_GREETING": "1",
        "WHALE_PROACTIVE_GREETING_DRY_RUN": "0",
        "WHALE_PROACTIVE_GREETING_COOLDOWN_S": "3600",
    })
    tick = ProactiveGreetingLoop(
        config=cfg, judge=J(), reply_model=R(), send=lambda s: sent.append(s),
        store=store, now_fn=lambda: 1_000_000.0 + 10,
    ).tick()
    assert tick.sent is False and tick.reason == "cooldown"
    assert sent == []
    assert store.get(LAST_SENT_KEY) == 1_000_000.0


def test_dry_run_skips_quota_consumption():
    from whale_core.life.proactive_greeting import (
        ProactiveGreetingConfig, ProactiveGreetingLoop, QUOTA_KEY_PREFIX)

    class Mem:
        def __init__(self):
            self.d = {}
        def get(self, k):
            return self.d.get(k)
        def put(self, k, v):
            self.d[k] = v

    class J:
        def reply(self, text, **kw):
            return "yes"
    class R:
        def reply(self, text, **kw):
            return "草稿"

    store = Mem()
    cfg = ProactiveGreetingConfig.from_env({"WHALE_PROACTIVE_GREETING": "1"})
    ProactiveGreetingLoop(
        config=cfg, judge=J(), reply_model=R(), send=lambda s: None, store=store,
    ).tick()
    assert not any(k.startswith(QUOTA_KEY_PREFIX) for k in store.d)


def test_send_failure_does_not_record_or_mark_sent():
    """回归：发送失败不得消耗额度/冷却，且 sent 必须为 False。"""
    from whale_core.life.proactive_greeting import (
        ProactiveGreetingConfig, ProactiveGreetingLoop,
        LAST_SENT_KEY, QUOTA_KEY_PREFIX)

    class Mem:
        def __init__(self):
            self.d = {}
        def get(self, k):
            return self.d.get(k)
        def put(self, k, v):
            self.d[k] = v

    class J:
        def reply(self, text, **kw):
            return "yes"

    class R:
        def __init__(self):
            self.n = 0
        def reply(self, text, **kw):
            self.n += 1
            return "嗨，在忙吗"

    store = Mem()
    reply = R()
    attempts = []

    def send_fail(draft):
        attempts.append(draft)
        return False

    cfg = ProactiveGreetingConfig.from_env({
        "WHALE_PROACTIVE_GREETING": "1",
        "WHALE_PROACTIVE_GREETING_DRY_RUN": "0",
    })
    tick = ProactiveGreetingLoop(
        config=cfg, judge=J(), reply_model=reply, send=send_fail,
        store=store, now_fn=lambda: 2_000_000.0,
    ).tick()
    assert tick.should_speak is True
    assert tick.sent is False
    assert tick.reason == "send_failed"
    assert attempts == ["嗨，在忙吗"]
    assert store.get(LAST_SENT_KEY) is None
    assert not any(k.startswith(QUOTA_KEY_PREFIX) for k in store.d)


def test_quota_exhausted_skips_reply_model():
    """回归：额度耗尽时提前返回，不调用回复模型浪费 token。"""
    from whale_core.life.proactive_greeting import (
        ProactiveGreetingConfig, ProactiveGreetingLoop, record_send)

    class Mem:
        def __init__(self):
            self.d = {}
        def get(self, k):
            return self.d.get(k)
        def put(self, k, v):
            self.d[k] = v

    class J:
        def reply(self, text, **kw):
            return "yes"

    class R:
        def __init__(self):
            self.n = 0
        def reply(self, text, **kw):
            self.n += 1
            return "不该生成"

    store = Mem()
    # 把日额度打满
    cfg = ProactiveGreetingConfig.from_env({
        "WHALE_PROACTIVE_GREETING": "1",
        "WHALE_PROACTIVE_GREETING_DRY_RUN": "0",
        "WHALE_PROACTIVE_GREETING_DAILY_QUOTA": "1",
        "WHALE_PROACTIVE_GREETING_COOLDOWN_S": "60",
    })
    now = 3_000_000.0
    record_send(store, now=now - 120)  # 冷却已过，但日额度已用完
    reply = R()
    sent = []
    tick = ProactiveGreetingLoop(
        config=cfg, judge=J(), reply_model=reply,
        send=lambda s: sent.append(s) or True,
        store=store, now_fn=lambda: now,
    ).tick()
    assert tick.sent is False
    assert tick.reason == "daily_quota"
    assert tick.draft == ""
    assert reply.n == 0
    assert sent == []


def test_daily_quota_zero_means_unlimited():
    """owner 2026-11-05：DAILY_QUOTA=0 → 不限（仍受冷却）。"""
    from whale_core.life.proactive_greeting import (
        ProactiveGreetingConfig, check_send_budget, record_send)

    class Mem:
        def __init__(self):
            self.d = {}
        def get(self, k):
            return self.d.get(k)
        def put(self, k, v):
            self.d[k] = v

    store = Mem()
    cfg = ProactiveGreetingConfig.from_env({
        "WHALE_PROACTIVE_GREETING": "1",
        "WHALE_PROACTIVE_GREETING_DRY_RUN": "0",
        "WHALE_PROACTIVE_GREETING_DAILY_QUOTA": "0",
        "WHALE_PROACTIVE_GREETING_COOLDOWN_S": "60",
    })
    assert cfg.daily_quota == 0
    now = 5_000_000.0
    # 即使已记过多次发送，quota=0 也不拦
    record_send(store, now=now - 120)
    record_send(store, now=now - 120)
    ok, why = check_send_budget(store, cfg, now=now)
    assert ok and why == "ok_unlimited"
    # 冷却仍生效
    record_send(store, now=now - 10)
    ok2, why2 = check_send_budget(store, cfg, now=now)
    assert not ok2 and why2 == "cooldown"
