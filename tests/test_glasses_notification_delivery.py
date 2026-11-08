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
