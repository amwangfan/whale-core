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


def _enabled_cfg(**extra):
    env = {"WHALE_PROACTIVE_GREETING": "1"}
    env.update(extra)
    return ProactiveGreetingConfig.from_env(env)


class _CaptureBrain:
    """记录 reply 收到的 kwargs 的假 brain。"""

    def __init__(self, answer="yes"):
        self.answer = answer
        self.calls = []

    def reply(self, text, **kw):
        self.calls.append(kw)
        return self.answer


def _owner_store():
    from whale_core.memory.store import InMemoryStore, PersonaContinuity
    store = InMemoryStore()
    store.put_persona_continuity(PersonaContinuity(
        persona_name="Enxi", tone="温柔简洁", address_user_as="主人"))
    return store


def test_persona_injected_from_store():
    """人格：store 里有人格快照时，judge/reply 都拿到非空 persona。"""
    store = _owner_store()
    judge, reply = _CaptureBrain("yes"), _CaptureBrain("晚上好")
    tick = ProactiveGreetingLoop(
        config=_enabled_cfg(), judge=judge, reply_model=reply,
        send=lambda s: None, store=store).tick()
    assert tick.reason == "dry_run"
    for brain in (judge, reply):
        persona = brain.calls[-1]["persona"]
        assert persona and "Enxi" in persona


def test_active_persona_card_injected():
    """当前激活角色卡（全局 active card）经 build_effective_persona 生效。"""
    from whale_core.ui.persona_cards import import_external_card, switch_card
    store = _owner_store()
    import_external_card(store, card_id="card-x", title="测试卡",
                         prompt_text="说话带一点猫的习惯。")
    switch_card(store, "card-x")
    reply = _CaptureBrain("喵，晚上好")
    tick = ProactiveGreetingLoop(
        config=_enabled_cfg(), judge=_CaptureBrain("yes"), reply_model=reply,
        send=lambda s: None, store=store).tick()
    assert tick.reason == "dry_run"
    persona = reply.calls[-1]["persona"]
    assert "说话带一点猫的习惯。" in persona


def test_authorized_owner_memories_injected_and_isolated():
    """授权空间内的记忆注入；未授权空间不出现；空授权 fail closed。"""
    from whale_core.memory.curation import MemoryCurator
    store = _owner_store()
    curator = MemoryCurator(store)
    curator.remember("主人晚饭想吃火锅", source="chat", evidence_id="ep1",
                     consent=True, space_id="sp_owner")
    curator.remember("别人的私密事情不应出现", source="chat", evidence_id="ep2",
                     consent=True, space_id="sp_other")
    judge, reply = _CaptureBrain("yes"), _CaptureBrain("要不要点火锅？")
    loop = ProactiveGreetingLoop(
        config=_enabled_cfg(), judge=judge, reply_model=reply,
        send=lambda s: None, store=store, curator=curator,
        allowed_spaces=("sp_owner",))
    tick = loop.tick(situation="晚饭时间到了")
    assert tick.reason == "dry_run"
    for brain in (judge, reply):
        memories = brain.calls[-1]["memories"]
        assert any("火锅" in m for m in memories)
        assert not any("私密" in m for m in memories)

    # fail closed：显式空授权 → 不注入任何记忆记录（人格是全局快照，仍可注入）
    reply2 = _CaptureBrain("嗨")
    ProactiveGreetingLoop(
        config=_enabled_cfg(), judge=_CaptureBrain("yes"), reply_model=reply2,
        send=lambda s: None, store=store, curator=curator,
        allowed_spaces=()).tick(situation="晚饭时间到了")
    assert reply2.calls[-1]["memories"] == []


def test_history_injected_for_conversation():
    """授权空间 + conversation_id 指向主人私聊时，brain 拿到同一份会话历史。"""
    from whale_core.memory.history import append_user_message, append_assistant_message
    store = _owner_store()
    append_user_message(store, "private:owner1", "今天加班好累")
    append_assistant_message(store, "private:owner1", "辛苦啦")
    judge, reply = _CaptureBrain("yes"), _CaptureBrain("还在忙吗？")
    ProactiveGreetingLoop(
        config=_enabled_cfg(), judge=judge, reply_model=reply,
        send=lambda s: None, store=store,
        allowed_spaces=("sp_owner",),
        conversation_id="private:owner1").tick()
    history = reply.calls[-1]["history"]
    assert {"role": "user", "content": "今天加班好累"} in history
    assert {"role": "assistant", "content": "辛苦啦"} in history


def test_no_trusted_scope_renders_persona_only():
    """宿主未给 allowed_spaces/context：只渲染全局人格，不读记录/历史。"""
    from whale_core.memory.continuity import remember_episode
    from whale_core.memory.history import append_user_message
    store = _owner_store()
    remember_episode(store, summary="用户：不该被读到的情节\nEnxi：嗯",
                     key="ep:chat:x", tags=("chat",),
                     extra={"conversation_id": "private:owner1"},
                     space_id="sp_owner")
    append_user_message(store, "private:owner1", "不该被读到的历史")
    judge, reply = _CaptureBrain("yes"), _CaptureBrain("嗨")
    tick = ProactiveGreetingLoop(
        config=_enabled_cfg(), judge=judge, reply_model=reply,
        send=lambda s: None, store=store,
        conversation_id="private:owner1").tick()
    assert tick.reason == "dry_run"
    assert "Enxi" in reply.calls[-1]["persona"]  # 全局人格仍注入
    assert reply.calls[-1]["memories"] == []
    assert tuple(reply.calls[-1]["history"]) == ()


def test_empty_spaces_fail_closed_no_memory_no_history():
    """授权空间已解析但为空元组：记忆与历史都不注入（人格是全局的，仍注入）。"""
    from whale_core.memory.continuity import remember_episode
    from whale_core.memory.history import append_user_message
    store = _owner_store()
    remember_episode(store, summary="用户：同一会话的情节\nEnxi：好",
                     key="ep:chat:y", tags=("chat",),
                     extra={"conversation_id": "private:owner1"},
                     space_id="sp_owner")
    append_user_message(store, "private:owner1", "同一会话的历史")
    reply = _CaptureBrain("嗨")
    ProactiveGreetingLoop(
        config=_enabled_cfg(), judge=_CaptureBrain("yes"), reply_model=reply,
        send=lambda s: None, store=store,
        allowed_spaces=(),
        conversation_id="private:owner1").tick()
    assert reply.calls[-1]["memories"] == []
    assert tuple(reply.calls[-1]["history"]) == ()
    assert reply.calls[-1]["persona"]


def test_history_conversation_mismatch_not_injected():
    """显式 conversation_id 与 context 权威会话冲突 → 不注入历史（fail closed）。"""
    from whale_core.memory.history import append_user_message
    store = _owner_store()
    append_user_message(store, "private:A", "A 会话的话")
    append_user_message(store, "private:B", "B 会话的话")

    class _Conv:
        conversation_id = "private:A"

    class Ctx:
        allowed_memory_spaces = ("sp_owner",)
        persona_id = None
        conversation = _Conv()

    reply = _CaptureBrain("嗨")
    ProactiveGreetingLoop(
        config=_enabled_cfg(), judge=_CaptureBrain("yes"), reply_model=reply,
        send=lambda s: None, store=store,
        context=Ctx(), conversation_id="private:B").tick()
    assert tuple(reply.calls[-1]["history"]) == ()

    # 不显式给 conversation_id 时，context 的权威会话生效
    reply2 = _CaptureBrain("嗨")
    ProactiveGreetingLoop(
        config=_enabled_cfg(), judge=_CaptureBrain("yes"), reply_model=reply2,
        send=lambda s: None, store=store,
        context=Ctx()).tick()
    assert {"role": "user", "content": "A 会话的话"} in reply2.calls[-1]["history"]


def test_no_curator_same_conversation_episodes_only():
    """有授权空间但无 curator：至多注入本会话 chat 情节，不取其它会话。"""
    from whale_core.memory.continuity import remember_episode
    store = _owner_store()
    remember_episode(store, summary="用户：今天加班好累\nEnxi：辛苦啦",
                     key="ep:chat:same", tags=("chat",),
                     extra={"conversation_id": "private:owner1"},
                     space_id="sp_owner")
    remember_episode(store, summary="用户：别的会话的秘密话题\nEnxi：嗯",
                     key="ep:chat:other", tags=("chat",),
                     extra={"conversation_id": "private:someone_else"},
                     space_id="sp_owner")
    remember_episode(store, summary="用户：同会话但非 chat 来源\nEnxi：哦",
                     key="ep:call:same", tags=("call",),
                     extra={"conversation_id": "private:owner1"},
                     space_id="sp_owner")
    reply = _CaptureBrain("还在忙吗？")
    ProactiveGreetingLoop(
        config=_enabled_cfg(), judge=_CaptureBrain("yes"), reply_model=reply,
        send=lambda s: None, store=store,
        allowed_spaces=("sp_owner",),
        conversation_id="private:owner1").tick()
    memories = reply.calls[-1]["memories"]
    assert any("加班" in m for m in memories)
    assert not any("秘密话题" in m for m in memories)
    assert not any("非 chat 来源" in m for m in memories)


def test_bound_persona_card_injected_to_both_models():
    """context.persona_id 绑定真实卡：judge 与 reply 两个模型都拿到卡人格。"""
    from whale_core.ui.persona_cards import import_external_card
    store = _owner_store()
    import_external_card(store, card_id="card-bound", title="绑定卡",
                         prompt_text="回答时自称小鲸。")

    class _Conv:
        conversation_id = "glasses:proactive"

    class Ctx:
        allowed_memory_spaces = ("sp_owner",)
        persona_id = "card-bound"
        conversation = _Conv()

    judge, reply = _CaptureBrain("yes"), _CaptureBrain("小鲸来啦")
    tick = ProactiveGreetingLoop(
        config=_enabled_cfg(), judge=judge, reply_model=reply,
        send=lambda s: None, store=store, context=Ctx()).tick()
    assert tick.reason == "dry_run"
    for brain in (judge, reply):
        assert "回答时自称小鲸。" in brain.calls[-1]["persona"]


def test_context_callable_resolves_spaces_and_bound_persona():
    """context 为 callable 时每 tick 现取授权空间与会话绑定卡。"""
    from whale_core.memory.curation import MemoryCurator
    store = _owner_store()
    curator = MemoryCurator(store)
    curator.remember("主人晚饭想吃火锅", source="chat", evidence_id="ep1",
                     consent=True, space_id="sp_owner")

    class Ctx:
        allowed_memory_spaces = ("sp_owner",)
        persona_id = None

    calls = {"n": 0}

    def ctx_provider():
        calls["n"] += 1
        return Ctx()

    reply = _CaptureBrain("饿了吧")
    tick = ProactiveGreetingLoop(
        config=_enabled_cfg(), judge=_CaptureBrain("yes"), reply_model=reply,
        send=lambda s: None, store=store, curator=curator,
        context=ctx_provider).tick(situation="晚饭时间到了")
    assert tick.reason == "dry_run"
    assert calls["n"] == 1
    assert any("火锅" in m for m in reply.calls[-1]["memories"])
    assert reply.calls[-1]["persona"]


def test_dry_run_with_context_still_not_sent():
    """有人格/记忆注入时 dry-run 仍不外发。"""
    from whale_core.memory.curation import MemoryCurator
    store = _owner_store()
    curator = MemoryCurator(store)
    curator.remember("主人怕辣", source="chat", evidence_id="ep1",
                     consent=True, stable_fact=True, space_id="sp_owner")
    sent = []
    tick = ProactiveGreetingLoop(
        config=_enabled_cfg(), judge=_CaptureBrain("yes"),
        reply_model=_CaptureBrain("吃点清淡的吧"),
        send=lambda s: sent.append(s), store=store, curator=curator,
        allowed_spaces=("sp_owner",)).tick()
    assert tick.should_speak and tick.reason == "dry_run" and tick.draft
    assert sent == []


def test_legacy_construction_keeps_empty_injection():
    """旧构造（无 store/curator/context/conversation_id）行为不变：空注入。"""
    judge, reply = _CaptureBrain("yes"), _CaptureBrain("嗨，在吗？")
    tick = ProactiveGreetingLoop(
        config=_enabled_cfg(), judge=judge, reply_model=reply,
        send=lambda s: None).tick(situation="下午闲着")
    assert tick.should_speak and tick.reason == "dry_run"
    assert judge.calls[-1]["persona"] == ""
    assert list(reply.calls[-1]["memories"]) == []
    assert tuple(reply.calls[-1]["history"]) == ()


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
