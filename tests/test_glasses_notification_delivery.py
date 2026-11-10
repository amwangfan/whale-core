from whale_core.api.glasses import build_delegates
from whale_core.notifications.gateway import NotificationGateway


class OfflineWeQPort:
    def _get(self, path, *, auth=True):
        return 200, {"result": {"data": {"json": {"online": False}}}}


def test_glasses_result_delivery_queues_whale_notification_when_qq_offline(tmp_path):
    device_id = "a" * 32
    env = {
        "WHALE_GLASSES_TOKEN": "test-glasses-token",
        "WHALE_STORE": str(tmp_path / "whale.sqlite"),
        "OWNER_UID": "owner",
    }
    delegates = build_delegates(
        env, brain_factory=lambda: None,
        send_port_factory=lambda: OfflineWeQPort())
    store = delegates["store"]
    try:
        store.put(f"device:{device_id}", {
            "device_id": device_id, "status": "active",
            "scopes": ["phone.notification.post"],
        })
        result = delegates["deliver_result"](
            [{"kind": "text", "text": "B站视频《FPGA 入门》"}],
            conversation="glasses:session-1",
            summary="exploration:BV1Abc123")
        assert result["ok"] is True
        assert "whale_notification" in result["channels"]

        queue = NotificationGateway(store).queue
        events = queue.replay_since(device_id)
        assert len(events) == 1
        assert events[0].category == "chat.message"
        assert "FPGA 入门" in events[0].body
    finally:
        store.close()



def test_ambiguous_gateway_post_does_not_enqueue_duplicate(tmp_path, monkeypatch):
    import urllib.request

    class UncertainOpener:
        def open(self, request, timeout):
            raise TimeoutError("response lost after sending")

    monkeypatch.setattr(urllib.request, "build_opener",
                        lambda *args: UncertainOpener())
    device_id = "b" * 32
    delegates = build_delegates({
        "WHALE_GLASSES_TOKEN": "test-glasses-token",
        "WHALE_STORE": str(tmp_path / "uncertain.sqlite"),
        "OWNER_UID": "owner",
        "WHALE_API_TOKEN": "test-core-token",
    }, brain_factory=lambda: None,
       send_port_factory=lambda: OfflineWeQPort())
    store = delegates["store"]
    try:
        store.put(f"device:{device_id}", {
            "device_id": device_id, "status": "active",
            "scopes": ["phone.notification.post"],
        })
        result = delegates["deliver_result"](
            [{"kind": "text", "text": "一条推荐"}],
            conversation="glasses:session-2", summary="exploration:test")
        assert result["ok"] is False
        assert result["receipts"][0]["channel"] == "whale_notification"
        assert "unknown_TimeoutError" in result["receipts"][0]["detail"]
        assert NotificationGateway(store).queue.replay_since(device_id) == []
    finally:
        store.close()


class OnlineVoicePort:
    """QQ 在线 + 记录 send_voice_to 调用的桩。"""

    def __init__(self):
        self.voice_calls = []
        self.last_receipt = {"ok": True}

    def _get(self, path, *, auth=True):
        return 200, {"result": {"data": {"json": {"online": True}}}}

    def send_voice_to(self, peer_type, target, wav_bytes, *, filename="voice.wav"):
        self.voice_calls.append((peer_type, target, len(wav_bytes), filename))
        return True

    def send_c2c(self, target, text):
        return True


def test_glasses_result_deliver_audio_goes_to_qq_voice(tmp_path):
    """2026-11-11：/v1/result/deliver 的 audio 载荷 → QQ 语音消息
    （眼镜→core→QQ 投递链加音频载体）。"""
    port = OnlineVoicePort()
    delegates = build_delegates(
        {"WHALE_GLASSES_TOKEN": "t", "WHALE_STORE": str(tmp_path / "s.sqlite"),
         "OWNER_UID": "owner-uid"},
        brain_factory=lambda: None,
        send_port_factory=lambda: port)
    store = delegates["store"]
    try:
        result = delegates["deliver_result"](
            [{"kind": "audio", "audio_bytes": b"RIFF-fake-wav", "filename": "hi.wav"}],
            conversation="", summary="语音问好")
        assert result["ok"] is True
        # summary 文本 + 语音两条回执
        assert result["channels"] == ["qq_c2c", "qq_c2c"]
        assert port.voice_calls and port.voice_calls[0][1] == "owner-uid"
        assert port.voice_calls[0][2] == len(b"RIFF-fake-wav")
    finally:
        store.close()


def test_delivery_audio_empty_rejected():
    from whale_core.life.result_delivery import DeliveryItem, ResultDelivery
    port = OnlineVoicePort()
    d = ResultDelivery(port, owner_uid="o", online_probe=lambda: True)
    receipts = d.deliver([DeliveryItem(kind="audio", audio_bytes=b"")])
    assert receipts[0].ok is False and receipts[0].detail == "empty-audio"


def test_delivery_note_persona_texts():
    """投递后眼镜侧的文字交代（2026-11-11 owner 决策）。"""
    from whale_core.api.glasses import _delivery_note
    assert _delivery_note({"ok": True}, [{"kind": "audio"}]) == "语音发过去啦，听听看~"
    assert _delivery_note({"ok": True}, [{"kind": "image"}]) == "图发你手机上了，看看~"
    assert _delivery_note({"ok": True}, [{"kind": "text"}]) == "说了哦，看看手机~"
    assert "没发出去" in _delivery_note({"ok": False}, [{"kind": "text"}])


def test_delivery_note_model_first_then_fallback():
    """owner 决策：note 由模型自主拟，模型失败/空/超长回落固定文案。"""
    from whale_core.api.glasses import _delivery_note
    # 模型给出合规短句 → 用模型的
    out = _delivery_note({"ok": True}, [{"kind": "audio"}],
                         say=lambda p: "发你 QQ 啦，戳开听听~")
    assert out == "发你 QQ 啦，戳开听听~"
    # 模型空返回 → 回落固定文案
    assert _delivery_note({"ok": True}, [{"kind": "audio"}],
                          say=lambda p: "") == "语音发过去啦，听听看~"
    # 模型超长/多行 → 回落
    assert _delivery_note({"ok": True}, [{"kind": "text"}],
                          say=lambda p: "长" * 60) == "说了哦，看看手机~"
    # 模型抛错 → 回落
    def _boom(p):
        raise RuntimeError("model down")
    assert _delivery_note({"ok": False}, [{"kind": "text"}],
                          say=_boom).startswith("刚才没发出去")
