"""issue #52：设备动作回执/幂等记录的有界保留与清理测试（离线）。"""
from __future__ import annotations

import threading

from whale_core.devices.gateway import DeviceGateway
from whale_core.memory.store import InMemoryStore

DAY = 24 * 3600 * 1000
NOW = 10 * DAY


def _gateway(store: InMemoryStore) -> DeviceGateway:
    """sweep 只用 store——绕过 WS 监听构造（其它测试覆盖协议面）。"""
    gw = DeviceGateway.__new__(DeviceGateway)
    gw.store = store
    gw.lock = threading.RLock()
    return gw


def _put_action(store, device, idem, *, state, created, expires=NOW, action_id=None):
    action_id = action_id or f"aid-{idem}"
    store.put(f"device:action:{device}:{idem}", {
        "device_id": device, "idempotency_key": idem, "action_id": action_id,
        "capability": "camera.capture", "created_at": created,
        "state": state, "expires_at": expires,
    })
    store.put(f"device:action-id:{device}:{action_id}", idem)
    return action_id


def test_terminal_records_older_than_retention_deleted():
    store = InMemoryStore()
    gw = _gateway(store)
    aid = _put_action(store, "dev1", "idem-old", state="executed",
                      created=NOW, action_id="a1")
    # 让记录变老：直接改写 created_at 超过 7 天保留窗
    key = "device:action:dev1:idem-old"
    rec = store.get(key)
    rec["created_at"] = NOW - 8 * DAY
    store.put(key, rec)
    store.put(f"device:receipts:dev1:{aid}", [{"state": "executed", "ts": 1}])
    stats = gw.sweep_action_retention(now=NOW)
    assert stats["deleted"] == 1
    assert store.get(key) is None
    assert store.get("device:action-id:dev1:a1") is None      # 反向索引同清
    assert store.get(f"device:receipts:dev1:{aid}") is None   # 回执流水同清


def test_terminal_within_retention_kept_for_reconciliation():
    store = InMemoryStore()
    gw = _gateway(store)
    _put_action(store, "dev1", "idem-new", state="executed", created=NOW - DAY)
    stats = gw.sweep_action_retention(now=NOW)
    assert stats["deleted"] == 0
    assert store.get("device:action:dev1:idem-new") is not None


def test_pending_within_retry_window_never_touched():
    """有效重试窗口内（未过 expires_at+宽限）的幂等键绝不提前释放。"""
    store = InMemoryStore()
    gw = _gateway(store)
    _put_action(store, "dev1", "idem-pend", state="pending",
                created=NOW - 2 * DAY)
    rec = store.get("device:action:dev1:idem-pend")
    rec["expires_at"] = NOW + 1000  # 还没到设备侧 deadline
    store.put("device:action:dev1:idem-pend", rec)
    stats = gw.sweep_action_retention(now=NOW)
    assert stats["deleted"] == 0 and stats["marked_unknown"] == 0
    assert store.get("device:action:dev1:idem-pend")["state"] == "pending"


def test_pending_past_grace_marked_unknown_not_deleted():
    """过宽限的非终态先落 unknown 过期标记（可核对），下轮才按终态清理。"""
    store = InMemoryStore()
    gw = _gateway(store)
    _put_action(store, "dev1", "idem-stale", state="received",
                created=NOW - 2 * DAY)
    rec = store.get("device:action:dev1:idem-stale")
    rec["expires_at"] = NOW - 25 * 3600 * 1000  # 过 24h 宽限
    store.put("device:action:dev1:idem-stale", rec)
    stats = gw.sweep_action_retention(now=NOW)
    assert stats["marked_unknown"] == 1 and stats["deleted"] == 0
    rec = store.get("device:action:dev1:idem-stale")
    assert rec["state"] == "unknown"
    # 幂等键仍在：重复提交仍命中查重（不提前释放）
    # （再老化超过 7 天保留窗后才删除）
    rec["created_at"] = NOW - 8 * DAY
    store.put("device:action:dev1:idem-stale", rec)
    stats = gw.sweep_action_retention(now=NOW)
    assert stats["deleted"] == 1


def test_orphan_receipt_streams_cleaned():
    store = InMemoryStore()
    gw = _gateway(store)
    store.put("device:receipts:dev1:ghost", [{"state": "executed", "ts": 1}])
    stats = gw.sweep_action_retention(now=NOW)
    assert store.get("device:receipts:dev1:ghost") is None
    assert stats["deleted"] == 1


def test_delete_cap_bounds_single_pass():
    store = InMemoryStore()
    gw = _gateway(store)
    for i in range(5):
        _put_action(store, "dev1", f"idem-{i}", state="executed",
                    created=NOW - 30 * DAY, action_id=f"a{i}")
    stats = gw.sweep_action_retention(now=NOW, max_deletes=2)
    assert stats["deleted"] == 2
    # 下轮继续清
    stats = gw.sweep_action_retention(now=NOW, max_deletes=10)
    assert stats["deleted"] == 3
