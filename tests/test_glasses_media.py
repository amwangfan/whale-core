"""眼镜 QQ 语音接线独立模块测试（api/glasses_media.py）。

全部离线：真实 configure_optional_capabilities 安装（fake MiniMax key +
monkeypatch 传输层，同 test_optional_capabilities 先例）、真实 ArtifactStore
临时目录、真实 qq_workflow 回执提取；投递用假回调与 glasses.build_delegates
的真实 deliver_result（在线假 port）。不触网、不外发。
"""
from __future__ import annotations

import io
import json
import pytest

from whale_core.api.glasses import build_delegates
from whale_core.api.glasses_media import (
    deliver_audio_from_receipts, extract_audio_artifact_ids,
    install_media_capabilities)
from whale_core.core.runtime import ToolRegistry
from whale_core.memory.store import InMemoryStore
from whale_core.plugins import provider_http
from whale_core.plugins.artifacts import ArtifactStore
from whale_core.plugins.registry import PluginRegistry

_MINIMAX_ENV = {"WHALE_MINIMAX_API_KEY": "fake-key"}
_WAV = b"RIFF" + b"\x00" * 40


class _FakeDeliver:
    """glasses deliver_result 同签名假件：记录调用；返回值/异常可配置。"""

    def __init__(self, result=None, raises=None):
        self.calls = []
        self.result = result if result is not None else {
            "ok": True, "count": 1, "channels": ["qq_c2c"],
            "receipts": [{"channel": "qq_c2c", "ok": True, "detail": "sent"}]}
        self.raises = raises

    def __call__(self, items, *, conversation="", summary="",
                 prefer_owner_dm=True):
        self.calls.append({"items": list(items), "conversation": conversation,
                           "summary": summary,
                           "prefer_owner_dm": prefer_owner_dm})
        if self.raises is not None:
            raise self.raises
        return self.result


def _minimax_tts_transport(monkeypatch):
    """假 MiniMax t2a_v2 传输层：hex 音频（test_optional_capabilities 同款）。"""
    monkeypatch.setattr(
        provider_http, "open_request",
        lambda req, **kw: io.BytesIO(json.dumps(
            {"data": {"audio": _WAV.hex()},
             "base_resp": {"status_code": 0}}).encode()))


def _audio_receipt(artifact) -> str:
    """TTS 工具同款成功回执（tool:<name>:ok:<json>，含 artifact_id+audio/*）。"""
    payload = artifact.as_dict()
    payload["model"] = "speech-02-hd"
    return "tool:synthesize_speech:ok:" + json.dumps(payload, ensure_ascii=False)


class _OnlineVoicePort:
    """QQ 在线 + 记录 send_voice_to 的假 port（test_glasses_notification 同款）。"""

    def __init__(self, voice_ok=True):
        self.voice_ok = voice_ok
        self.voice_calls = []
        self.last_receipt = {"ok": True}

    def _get(self, path, *, auth=True):
        return 200, {"result": {"data": {"json": {"online": True}}}}

    def send_voice_to(self, peer_type, target, wav_bytes, *,
                      filename="voice.wav"):
        self.voice_calls.append((peer_type, target, bytes(wav_bytes), filename))
        return self.voice_ok

    def send_c2c(self, target, text):
        return True


def test_install_real_factory_registers_tts_and_keeps_artifacts(tmp_path,
                                                                monkeypatch):
    """真实工厂薄封装：TTS 工具真实可用（假传输层），产物落 artifacts。"""
    bundle = install_media_capabilities(
        InMemoryStore(), env=_MINIMAX_ENV, artifact_dir=tmp_path / "art")
    assert bundle.artifacts is not None
    specs = bundle.tools.for_capability("tts.synthesize")
    assert specs and specs[0].name == "synthesize_speech"
    assert bundle.registry.manifest("speech_tts") is not None
    assert any("语音" in n for n in bundle.notes)
    _minimax_tts_transport(monkeypatch)
    result = json.loads(specs[0].handler({"text": "你好"}))
    assert result["content_type"] == "audio/mpeg"
    assert bundle.artifacts.read(result["artifact_id"])[1] == _WAV


def test_install_merges_into_existing_registry_and_tools(tmp_path):
    """传入 glasses 现有 registry/tools → 并入同一对象（同编排器可见）。"""
    registry, tools = PluginRegistry(), ToolRegistry()
    bundle = install_media_capabilities(
        InMemoryStore(), env=_MINIMAX_ENV, artifact_dir=tmp_path / "art",
        registry=registry, tools=tools)
    assert bundle.registry is registry and bundle.tools is tools
    assert tools.for_capability("tts.synthesize")
    # 工厂原始返回透传（video_manager 等字段不丢）
    assert bundle.optional is not None
    assert bundle.optional.artifacts is bundle.artifacts


def test_end_to_end_tts_receipt_to_owner_qq_voice(tmp_path, monkeypatch):
    """全链：真实安装 → 真实工具产 artifact → 回执提取 → 真实 deliver_result
    （glasses build_delegates）→ 在线假 port 收到语音；目标恒 owner c2c。"""
    port = _OnlineVoicePort()
    delegates = build_delegates(
        {"WHALE_GLASSES_TOKEN": "t",
         "WHALE_STORE": str(tmp_path / "s.sqlite"),
         "OWNER_UID": "owner-uid"},
        brain_factory=lambda: None,
        send_port_factory=lambda: port)
    store = delegates["store"]
    try:
        bundle = install_media_capabilities(
            store, env=_MINIMAX_ENV, artifact_dir=tmp_path / "art")
        _minimax_tts_transport(monkeypatch)
        spec = bundle.tools.for_capability("tts.synthesize")[0]
        receipts = ["tool:synthesize_speech:ok:" + spec.handler({"text": "你好"})]
        out = deliver_audio_from_receipts(
            receipts, artifacts=bundle.artifacts,
            deliver_result=delegates["deliver_result"], delivered=set())
        assert out["ok"] is True and out["delivered"] == 1
        assert out["errors"] == [] and out["duplicates"] == 0
        # 真实回执原样透传（供响应/时序如实展示）：qq_c2c accepted
        assert out["receipts"] and all(
            r["ok"] and r["channel"] == "qq_c2c" for r in out["receipts"])
        # 目标纪律：owner 默认 QQ 私聊；扩展名随真实 content_type
        assert port.voice_calls == [("c2c", "owner-uid", _WAV, "voice.mp3")]
    finally:
        store.close()


def test_only_confirmed_success_receipts_are_delivered(tmp_path):
    """无法确认成功不投递：失败/无关回执 no-op（不调回调、不假报）。"""
    store = InMemoryStore()
    artifacts = ArtifactStore(tmp_path / "art", store)
    art = artifacts.save_bytes(_WAV, "audio/mpeg")
    fake = _FakeDeliver()
    receipts = [
        "tool-fail:synthesize_speech:SpeechError",
        "tool:synthesize_speech:fail:" + json.dumps(art.as_dict()),
        "capability-unavailable:tts.synthesize",
        "task-limit:max-tool-rounds",
        'tool:web_search:ok:{"results": []}',
    ]
    assert extract_audio_artifact_ids(receipts) == ()
    out = deliver_audio_from_receipts(
        receipts, artifacts=artifacts, deliver_result=fake, delivered=set())
    assert out == {"ok": True, "delivered": 0, "duplicates": 0,
                   "audio_ids": [], "errors": [], "receipts": []}
    assert fake.calls == []
    # 追加唯一成功回执 → 只提取该 artifact
    assert extract_audio_artifact_ids(receipts + [_audio_receipt(art)]) == (
        art.artifact_id,)


def test_once_per_turn_shared_claim_set(tmp_path):
    """turn 级认领集合（同步/异步 _settled 闭包共享）：同一回执只投一次。"""
    store = InMemoryStore()
    artifacts = ArtifactStore(tmp_path / "art", store)
    art = artifacts.save_bytes(_WAV, "audio/mpeg")
    receipts = [_audio_receipt(art)]
    fake = _FakeDeliver()
    turn_delivered: set = set()
    first = deliver_audio_from_receipts(
        receipts, artifacts=artifacts, deliver_result=fake,
        delivered=turn_delivered)
    second = deliver_audio_from_receipts(
        receipts, artifacts=artifacts, deliver_result=fake,
        delivered=turn_delivered)  # 异步 _settled 再看同一份 receipts
    assert first["delivered"] == 1 and first["duplicates"] == 0
    assert second["delivered"] == 0 and second["duplicates"] == 1
    assert second["ok"] is True  # 重复≠失败：不产生二次外发也不假报错误
    assert len(fake.calls) == 1
    # delivered=None 一次性路径：同一调用内重复回执也只投一条
    fake2 = _FakeDeliver()
    out = deliver_audio_from_receipts(
        receipts + receipts, artifacts=artifacts, deliver_result=fake2)
    assert out["delivered"] == 1 and len(fake2.calls) == 1
    assert len(fake2.calls[0]["items"]) == 1


def test_target_defaults_never_from_user_text(tmp_path):
    """回调只收到空 conversation + 默认 prefer_owner_dm=True（owner 私聊）；
    本模块没有「发给谁」参数可传——目标不可能来自用户话语。"""
    store = InMemoryStore()
    artifacts = ArtifactStore(tmp_path / "art", store)
    art = artifacts.save_bytes(_WAV, "audio/wav")
    fake = _FakeDeliver()
    out = deliver_audio_from_receipts(
        [_audio_receipt(art)], artifacts=artifacts, deliver_result=fake,
        delivered=set(), summary="glasses:tts")
    assert out["delivered"] == 1
    call = fake.calls[0]
    assert call["conversation"] == "" and call["prefer_owner_dm"] is True
    assert call["summary"] == "glasses:tts"
    assert call["items"][0]["filename"] == "voice.wav"  # 扩展名随真实类型


def test_failure_receipts_are_real(tmp_path):
    """失败回执真实：回调拒绝/抛异常 → ok=False + errors，绝不假成功。"""
    store = InMemoryStore()
    artifacts = ArtifactStore(tmp_path / "art", store)
    art = artifacts.save_bytes(_WAV, "audio/mpeg")
    receipts = [_audio_receipt(art)]

    # 回调拒绝（QQ send_failed）→ 原始回执原样透传
    rejected = _FakeDeliver(result={
        "ok": False, "count": 1, "channels": ["qq_c2c"],
        "receipts": [{"channel": "qq_c2c", "ok": False,
                      "detail": "send_failed"}]})
    out = deliver_audio_from_receipts(
        receipts, artifacts=artifacts, deliver_result=rejected, delivered=set())
    assert out["ok"] is False and out["errors"] == ["deliver-rejected"]
    assert out["receipts"][0]["detail"] == "send_failed"

    # 回调抛异常 → 如实记异常类型；认领后不重投（防重复外发）
    raised = _FakeDeliver(raises=RuntimeError("boom"))
    turn: set = set()
    out = deliver_audio_from_receipts(
        receipts, artifacts=artifacts, deliver_result=raised, delivered=turn)
    assert out["ok"] is False and out["errors"] == ["deliver-raised:RuntimeError"]
    out2 = deliver_audio_from_receipts(
        receipts, artifacts=artifacts, deliver_result=raised, delivered=turn)
    assert out2["delivered"] == 0 and out2["duplicates"] == 1
    assert len(raised.calls) == 1


def test_missing_artifact_and_missing_store_reported_honestly(tmp_path):
    """artifact 缺失/artifacts 未建 → 诚实错误，不调回调不假成功。"""
    store = InMemoryStore()
    artifacts = ArtifactStore(tmp_path / "art", store)
    fake = _FakeDeliver()
    ghost_id = "a" * 32
    ghost = "tool:synthesize_speech:ok:" + json.dumps(
        {"artifact_id": ghost_id, "content_type": "audio/mpeg"})
    out = deliver_audio_from_receipts(
        [ghost], artifacts=artifacts, deliver_result=fake, delivered=set())
    assert out["ok"] is False and fake.calls == []
    assert out["errors"] == [f"artifact-read-failed:{ghost_id}:KeyError"]

    # 回执指向图像 artifact → 提取器只认 audio/*，无投递、无错误（no-op）
    img = artifacts.save_bytes(b"png-bytes", "image/png")
    out = deliver_audio_from_receipts(
        ["tool:generate_image:ok:" + json.dumps(img.as_dict())],
        artifacts=artifacts, deliver_result=fake, delivered=set())
    assert out["ok"] is True and out["delivered"] == 0 and fake.calls == []

    # artifacts=None（安装未建 ArtifactStore 却拿到 audio 回执）→ 诚实报错
    real = artifacts.save_bytes(_WAV, "audio/mpeg")
    out = deliver_audio_from_receipts(
        [_audio_receipt(real)], artifacts=None, deliver_result=fake,
        delivered=set())
    assert out["ok"] is False and out["errors"] == ["artifact-store-missing"]
    assert fake.calls == []


# ---- 真实编排集成（父代理已集成 glasses.py：_cached media / _finish_audio /
# ---- timing.audioDelivery / trustedphone resolver；此处只测不改） ----------

from whale_core.core.intent_router import SearchIntentRouter  # noqa: E402

_TTS_TEXT = "用语音说你好"
_TTS_PLAN = json.dumps({
    "goal": "把这句话合成语音发给主人", "kind": "task",
    "slots": {"text": "你好呀"},
    "capabilities": ["tts.synthesize"],
    "reason": "测试打桩：语音任务"}, ensure_ascii=False)
_CHAT_PLAN = json.dumps({"goal": "闲聊", "kind": "chat"}, ensure_ascii=False)


class _StubBrain:
    """回复模型桩：固定短文本（不含承诺词，不干扰并行链路判定）。"""

    def reply(self, text, **kwargs):
        return "[脑]收到"


class _StubQuickBrain:
    """轻量脑桩：投递结果交代一句（≤40 字单行，过 _delivery_note 护栏）；
    失败提示词下如实交代，不谎报成功。"""

    def reply(self, text, **kwargs):
        if "没有完成" in text:
            return "没发出去，我不谎报成功"
        return "语音发过去啦"


def _patch_intent_tts(monkeypatch, trigger=_TTS_TEXT):
    """monkey SearchIntentRouter.plan：触发词 → tts task，其余 → chat。"""
    def plan(self, text, *, context=""):
        return _TTS_PLAN if trigger in (text or "") else _CHAT_PLAN
    monkeypatch.setattr(SearchIntentRouter, "plan", plan)


def _media_delegates(tmp_path, monkeypatch, *, parallel=True, voice_ok=True):
    """无 search/weather/shell、仅 fake MiniMax 的真实 build_delegates。"""
    _minimax_tts_transport(monkeypatch)
    _patch_intent_tts(monkeypatch)
    port = _OnlineVoicePort(voice_ok=voice_ok)
    env = {
        "WHALE_GLASSES_TOKEN": "t",
        "WHALE_STORE": str(tmp_path / "s.sqlite"),
        "OWNER_UID": "owner-uid",
        "WHALE_MINIMAX_API_KEY": "fake-key",
    }
    if not parallel:
        env["WHALE_PARALLEL_REPLY"] = "0"
    delegates = build_delegates(
        env, brain_factory=_StubBrain, quick_brain_factory=_StubQuickBrain,
        send_port_factory=lambda: port)
    return delegates, port


def test_orchestrator_sync_task_delivers_voice_chat_does_not(tmp_path,
                                                             monkeypatch):
    """同步链路（WHALE_PARALLEL_REPLY=0）：真实编排器跑 tts 任务——工具真
    生成 artifact、只投递一次到 owner c2c；普通 chat 不产生任何投递。"""
    delegates, port = _media_delegates(tmp_path, monkeypatch, parallel=False)
    store = delegates["store"]
    try:
        # 普通 chat：意图 chat → audioDelivery no-op，不发语音
        reply, timing = delegates["companion_chat"](
            "晚上好呀", conversation_id="glasses:s-chat")
        assert reply == "[脑]收到"
        assert timing["audioDelivery"]["delivered"] == 0
        assert timing["audioDelivery"]["ok"] is True
        assert port.voice_calls == []

        # tts 任务：生成一次、投递一次；交代句替换回复
        reply, timing = delegates["companion_chat"](
            _TTS_TEXT, conversation_id="glasses:s-tts")
        delivery = timing["audioDelivery"]
        assert delivery["ok"] is True and delivery["delivered"] == 1
        assert delivery["errors"] == []
        assert port.voice_calls == [("c2c", "owner-uid", _WAV, "voice.mp3")]
        assert reply == "语音发过去啦"

        # 同一会话再说一次普通话：不会把上轮产物再投一遍
        reply, timing = delegates["companion_chat"](
            "谢谢", conversation_id="glasses:s-tts")
        assert timing["audioDelivery"]["delivered"] == 0
        assert len(port.voice_calls) == 1
    finally:
        store.close()


def test_orchestrator_parallel_poll_delivers_once(tmp_path, monkeypatch):
    """并行链路 + poll：第一条秒回不带语音；后台第二条真生成、_settled 只
    投递一次，followup_poll 如实带回 audioDelivery。"""
    delegates, port = _media_delegates(tmp_path, monkeypatch)
    store = delegates["store"]
    try:
        reply, timing = delegates["companion_chat"](
            _TTS_TEXT, conversation_id="glasses:p1", followup_mode="poll")
        assert reply == "[脑]收到"  # 第一条不等工具
        turn_id = timing["followup"]["turnId"]
        out = delegates["followup_poll"]("glasses:p1", turn_id, wait_ms=15000)
        assert out["status"] == "ready"
        assert out["reply"] == "语音发过去啦"
        assert out["audioDelivery"]["ok"] is True
        assert out["audioDelivery"]["delivered"] == 1
        assert port.voice_calls == [("c2c", "owner-uid", _WAV, "voice.mp3")]
        # 再 poll 同一 turnId：状态幂等，绝不二次投递
        out2 = delegates["followup_poll"]("glasses:p1", turn_id, wait_ms=0)
        assert out2["status"] == "ready"
        assert len(port.voice_calls) == 1
    finally:
        store.close()


def test_orchestrator_parallel_legacy_merges_and_delivers_once(tmp_path,
                                                               monkeypatch):
    """旧客户端（无 followup=poll）：smart 策略同步等到第二条并合并回复；
    语音只生成一次、投递一次，audioDelivery 进 timing。"""
    delegates, port = _media_delegates(tmp_path, monkeypatch)
    store = delegates["store"]
    try:
        reply, timing = delegates["companion_chat"](
            _TTS_TEXT, conversation_id="glasses:l1")
        assert timing["followup"]["merged"] is True
        assert timing["followup"]["waited"] == "rule"
        assert reply == "[脑]收到\n语音发过去啦"
        assert timing["audioDelivery"]["ok"] is True
        assert timing["audioDelivery"]["delivered"] == 1
        assert port.voice_calls == [("c2c", "owner-uid", _WAV, "voice.mp3")]
    finally:
        store.close()


def test_orchestrator_delivery_failure_is_honest(tmp_path, monkeypatch):
    """失败如实：send_voice_to 拒发 → audioDelivery ok=False + 真实回执
    detail；回复文本交代失败，不谎报成功；失败后不自动重投。"""
    delegates, port = _media_delegates(tmp_path, monkeypatch, parallel=False,
                                       voice_ok=False)
    store = delegates["store"]
    try:
        reply, timing = delegates["companion_chat"](
            _TTS_TEXT, conversation_id="glasses:f1")
        delivery = timing["audioDelivery"]
        assert delivery["ok"] is False
        assert delivery["delivered"] == 1  # 如实尝试过一次
        assert delivery["errors"] == ["deliver-rejected"]
        assert delivery["receipts"][0]["ok"] is False
        assert delivery["receipts"][0]["detail"] == "send_failed"
        assert reply == "没发出去，我不谎报成功"
        assert len(port.voice_calls) == 1  # 失败不重投（无重复外发）
    finally:
        store.close()


@pytest.mark.parametrize("parallel", [False, True])
def test_bound_persona_and_final_delivery_wording_are_consistent(tmp_path, monkeypatch, parallel):
    from whale_core.memory.bindings import BindingStore
    from whale_core.memory.history import load_history
    from whale_core.memory.spaces import SpaceStore
    from whale_core.memory.store import MemoryKind
    from whale_core.ui.persona_cards import CARD_KEY_PREFIX, PersonaCard, switch_card

    _minimax_tts_transport(monkeypatch)
    _patch_intent_tts(monkeypatch)
    model_personas, quick_personas = [], []
    class Brain:
        def reply(self, text, **kwargs):
            model_personas.append(kwargs.get("persona", ""))
            return "尚未投递的旧措辞" if "不可信工具输出" in text else "第一条"
    class Quick:
        def reply(self, text, **kwargs):
            quick_personas.append(kwargs.get("persona", ""))
            return "语音已送出"
    delegates = build_delegates(
        {"WHALE_STORE": str(tmp_path / "persona.sqlite"), "OWNER_UID": "owner-uid",
         # This test observes conversational persona, not background extraction prompts.
         "WHALE_OWNER_FACT_MEMORY": "0",
         "WHALE_MINIMAX_API_KEY": "fake-key", "WHALE_PARALLEL_REPLY": "1" if parallel else "0"},
        brain_factory=Brain, quick_brain_factory=Quick, send_port_factory=_OnlineVoicePort)
    store = delegates["store"]
    try:
        for cid, name in (("global", "全局鲸鲸"), ("bound", "会话鲸鲸")):
            card = PersonaCard(card_id=cid, title=cid, name=name)
            store.put(CARD_KEY_PREFIX + cid, card.as_dict())
        switch_card(store, "global")
        BindingStore(store, SpaceStore(store)).bind_persona(
            "phone:voice:glasses:bound", "bound", updated_by="owner")
        reply, timing = delegates["companion_chat"](
            _TTS_TEXT, conversation_id="glasses:bound", followup_mode="poll" if parallel else "")
        if parallel:
            out = delegates["followup_poll"]("glasses:bound", timing["followup"]["turnId"], wait_ms=5000)
            assert out["status"] == "ready"
            reply = out["reply"]
        assert reply == "语音已送出"
        assert model_personas and quick_personas
        assert all("会话鲸鲸" in p and "全局鲸鲸" not in p for p in model_personas + quick_personas)
        history = load_history(store, "glasses:bound")
        assert "尚未投递的旧措辞" not in str(history)
        assert str(history).count("语音已送出") == 1
        assert all("尚未投递的旧措辞" not in str(r.content)
                   for r in store.list_by_kind(MemoryKind.EPISODE))
    finally:
        store.close()
