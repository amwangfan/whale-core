"""ResultDelivery 路由：QQ 在线走 WeQ；离线走手机 stub。"""
from whale_core.life.result_delivery import (
    DeliveryItem, ResultDelivery, summarize_receipts)


class FakePort:
    def __init__(self):
        self.sent = []
        self.last_receipt = {"ok": True, "privateSequence": 11}

    def send_c2c(self, uid, text):
        self.sent.append(("c2c", uid, text))
        return True

    def send_image_to(self, peer_type, target, data, *, filename="result.png"):
        self.sent.append(("image", peer_type, target, len(data), filename))
        return True

    def send_group_text(self, group, text):
        self.sent.append(("group", group, text))
        return True


def test_online_prefers_owner_dm():
    port = FakePort()
    d = ResultDelivery(port, owner_uid="u_owner", online_probe=lambda: True)
    receipts = d.deliver([
        DeliveryItem(kind="text", text="摘要"),
        DeliveryItem(kind="image", image_bytes=b"PNG", filename="a.png"),
    ])
    assert all(r.ok for r in receipts)
    assert receipts[0].channel == "qq_c2c"
    assert port.sent[0] == ("c2c", "u_owner", "摘要")
    assert port.sent[1][0] == "image"
    assert summarize_receipts(receipts)["ok"] is True


def test_offline_uses_phone_stub(tmp_path, monkeypatch):
    port = FakePort()
    monkeypatch.setenv("WHALE_PHONE_GALLERY_STAGING", str(tmp_path))
    d = ResultDelivery(port, owner_uid="u_owner", online_probe=lambda: False)
    receipts = d.deliver([
        DeliveryItem(kind="image", image_bytes=b"\x89PNG", filename="x.png"),
    ], summary="offline-demo")
    assert len(receipts) == 1
    assert receipts[0].ok is True
    assert receipts[0].qq_online is False
    assert "stub:" in receipts[0].detail or receipts[0].channel in (
        "stub_log", "phone_gallery")
    assert list(tmp_path.iterdir()), "staging file expected"
    assert port.sent == []


def test_offline_text_only_logged():
    port = FakePort()
    d = ResultDelivery(port, owner_uid="u_owner", online_probe=lambda: False)
    receipts = d.deliver([DeliveryItem(kind="text", text="仅文字")])
    assert receipts[0].ok is True
    assert receipts[0].channel == "stub_log"
    assert port.sent == []
