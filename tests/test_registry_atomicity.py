from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from whale_core.devices.registry import DeviceRegistry, DeviceRegistryError
from whale_core.memory.store import InMemoryStore
from whale_core.memory.sqlite_store import SqliteMemoryStore


@pytest.fixture(params=["memory", "sqlite"])
def stores(request, tmp_path):
    backends = ([InMemoryStore()] * 8 if request.param == "memory" else
                [SqliteMemoryStore(tmp_path / "devices.db") for _ in range(8)])
    yield backends
    if request.param == "sqlite":
        for store in backends:
            store.close()


def pair(reg, i=1, **kwargs):
    return reg.request_pairing(label="test", pubkey_hex=f"{i:064x}", **kwargs)


def concurrent(stores, operation):
    barrier = Barrier(len(stores), timeout=10)
    def run(i):
        barrier.wait()
        try:
            return operation(DeviceRegistry(stores[i]), i)
        except DeviceRegistryError as error:
            return error.code
    with ThreadPoolExecutor(max_workers=len(stores)) as pool:
        return list(pool.map(run, range(len(stores))))


def test_code_collision_exhaustion_never_creates_ambiguous_pair(stores, monkeypatch):
    monkeypatch.setattr("whale_core.devices.registry.secrets.randbelow", lambda n: 123456)
    results = concurrent(stores, lambda reg, i: pair(reg, i + 1))
    assert sum(isinstance(r, dict) for r in results) == 1
    assert results.count("conflict") == len(stores) - 1
    reg = DeviceRegistry(stores[0])
    assert len(reg.list_devices()) == 1
    assert reg.approve("123456")["status"] == "active"


def test_approval_consumed_once_across_registries(stores):
    result = pair(DeviceRegistry(stores[0]))
    results = concurrent(stores, lambda reg, i: reg.approve(result["code"]))
    assert sum(isinstance(r, dict) for r in results) == 1
    assert results.count("bad_request") == len(stores) - 1
    audits = [stores[0].get(k) for k in stores[0].keys() if k.startswith("audit:studio:")]
    approves = [a for a in audits if a["action"] == "device.pair.approve"]
    assert sum(a["outcome"] == "ok" for a in approves) == 1
    assert sum(a["outcome"] == "denied" for a in approves) == len(stores) - 1


def test_scopes_cannot_reactivate_concurrently_revoked_device(stores):
    reg = DeviceRegistry(stores[0])
    result = pair(reg)
    reg.approve(result["code"])
    concurrent(stores, lambda other, i: other.revoke(result["device_id"]) if i % 2 else
               other.set_scopes(result["device_id"], []))
    assert reg.status_of(result["device_id"]) == "revoked"


def test_empty_scope_pairing_does_not_grant_every_capability(stores):
    reg = DeviceRegistry(stores[0])
    result = pair(reg, scopes=[])
    assert reg.approve(result["code"])["scopes"] == []


def test_audit_failure_rolls_back_device_mutation(stores, monkeypatch):
    reg = DeviceRegistry(stores[0])
    def fail(*args, **kwargs):
        raise RuntimeError("audit unavailable")
    monkeypatch.setattr(DeviceRegistry, "audit", fail)
    with pytest.raises(RuntimeError, match="audit unavailable"):
        pair(reg)
    assert reg.list_devices() == []


def test_legacy_ambiguous_code_is_rejected(stores):
    reg = DeviceRegistry(stores[0])
    one, two = pair(reg), pair(reg, 2)
    meta = reg.get(two["device_id"])
    meta["pairing_code"] = one["code"]
    stores[0].put("device:" + two["device_id"], meta)
    with pytest.raises(DeviceRegistryError):
        reg.approve(one["code"])
    assert all(d["status"] == "pending" for d in reg.list_devices())


def test_device_listing_uses_prefix_pages(stores, monkeypatch):
    reg = DeviceRegistry(stores[0])
    for i in range(505):
        device_id = f"{i:032x}"
        stores[0].put("device:" + device_id, {"device_id": device_id, "created_at": i})
    stores[0].put("device:action:abc", {"device_id": "abc"})
    monkeypatch.setattr(stores[0], "keys", lambda: pytest.fail("full scan"))
    assert len(reg.list_devices()) == 505
