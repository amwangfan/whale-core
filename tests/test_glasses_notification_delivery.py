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
