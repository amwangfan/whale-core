"""devices/registry.py：注册 / 配对码过期 / 批准 / 吊销（纯内存 store，不触网）。"""

from __future__ import annotations

import hashlib

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from whale_core.devices.registry import (
    DEVICE_CAPABILITIES,
    PAIR_CODE_TTL_S,
    DeviceRegistry,
    DeviceRegistryError,
    device_id_from_pubkey_hex,
)
from whale_core.memory.store import InMemoryStore


def _pubkey_hex() -> str:
    return Ed25519PrivateKey.generate().public_key().public_bytes_raw().hex()


@pytest.fixture()
def registry():
    return DeviceRegistry(InMemoryStore())


def _pair(reg, **kw):
    return reg.request_pairing(label="pixel-8", pubkey_hex=_pubkey_hex(), **kw)


# ---------- device_id / 注册 ----------

def test_device_id_is_pubkey_fingerprint(registry):
    pub = _pubkey_hex()
    expected = hashlib.sha256(bytes.fromhex(pub)).digest()[:16].hex()
    result = registry.request_pairing(label="a", pubkey_hex=pub)
    assert result["device_id"] == expected
    assert device_id_from_pubkey_hex(pub) == expected


def test_pairing_code_shape_and_ttl(registry):
    result = _pair(registry)
    assert len(result["code"]) == 6 and result["code"].isdigit()
    assert result["expires_in"] == PAIR_CODE_TTL_S
    meta = registry.get(result["device_id"])
    assert meta["status"] == "pending"
    assert meta["scopes"] == list(DEVICE_CAPABILITIES)  # 默认全量上限


def test_invalid_pubkey_rejected(registry):
    with pytest.raises(DeviceRegistryError) as ei:
        registry.request_pairing(label="a", pubkey_hex="zz")
    assert ei.value.code == "bad_request"


def test_unknown_scope_rejected(registry):
    with pytest.raises(DeviceRegistryError) as ei:
        _pair(registry, scopes=["screen.context.read", "shell.exec"])
    assert ei.value.code == "bad_request"


def test_scopes_upper_bound_narrowed(registry):
    result = _pair(registry, scopes=["screen.context.read"])
    meta = registry.get(result["device_id"])
    assert meta["scopes"] == ["screen.context.read"]


# ---------- 批准 ----------

def test_approve_activates_and_clears_code(registry):
    result = _pair(registry)
    meta = registry.approve(result["code"])
    assert meta["status"] == "active"
    assert meta["pairing_code"] is None
    assert registry.status_of(result["device_id"]) == "active"


def test_approve_unknown_code_rejected(registry):
    with pytest.raises(DeviceRegistryError) as ei:
        registry.approve("000000")
    assert ei.value.code == "bad_request"


def test_approve_expired_code_rejected(registry):
    result = _pair(registry, code_ttl_s=0)  # 立即过期
    with pytest.raises(DeviceRegistryError) as ei:
        registry.approve(result["code"])
    assert ei.value.code == "expired"
    assert registry.status_of(result["device_id"]) == "pending"


def test_approve_twice_rejected(registry):
    result = _pair(registry)
    registry.approve(result["code"])
    with pytest.raises(DeviceRegistryError):
        registry.approve(result["code"])


def test_repair_active_device_rejected(registry):
    pub = _pubkey_hex()
    result = registry.request_pairing(label="a", pubkey_hex=pub)
    registry.approve(result["code"])
    with pytest.raises(DeviceRegistryError) as ei:
        registry.request_pairing(label="a", pubkey_hex=pub)
    assert ei.value.code == "conflict"


def test_repair_revoked_device_rejected(registry):
    pub = _pubkey_hex()
    result = registry.request_pairing(label="a", pubkey_hex=pub)
    registry.approve(result["code"])
    registry.revoke(result["device_id"])
    with pytest.raises(DeviceRegistryError) as ei:
        registry.request_pairing(label="a", pubkey_hex=pub)
    assert ei.value.code == "conflict"


# ---------- 吊销 ----------

def test_revoke_immediate(registry):
    result = _pair(registry)
    registry.approve(result["code"])
    meta = registry.revoke(result["device_id"])
    assert meta["status"] == "revoked"
    assert registry.status_of(result["device_id"]) == "revoked"


def test_revoke_unknown_device(registry):
    with pytest.raises(DeviceRegistryError) as ei:
        registry.revoke("0" * 32)
    assert ei.value.code == "not_found"


def test_revoke_idempotent(registry):
    result = _pair(registry)
    registry.revoke(result["device_id"])
    assert registry.revoke(result["device_id"])["status"] == "revoked"


# ---------- 列表 / 审计 ----------

def test_list_and_public_view_hides_pairing_code(registry):
    result = _pair(registry)
    devices = registry.list_devices()
    assert [d["device_id"] for d in devices] == [result["device_id"]]
    view = DeviceRegistry.public_view(devices[0])
    assert "pairing_code" not in view and "code_expires_at" not in view
    assert view["pubkey_hex"]  # 公钥非秘密，保留


def test_audit_trail(registry):
    result = _pair(registry)
    registry.approve(result["code"])
    registry.revoke(result["device_id"])
    store = registry.store
    audits = [store.get(k) for k in store.keys() if k.startswith("audit:studio:")]
    actions = [a["action"] for a in audits]
    assert actions == ["device.pair.request", "device.pair.approve", "device.revoke"]
    assert all(a["outcome"] == "ok" and a["actor"] for a in audits)
    # 审计不含正文/秘密字段
    assert all("summary" not in a and "signature" not in a for a in audits)


def test_denied_approve_audited(registry):
    with pytest.raises(DeviceRegistryError):
        registry.approve("123456")
    store = registry.store
    audits = [store.get(k) for k in store.keys() if k.startswith("audit:studio:")]
    assert audits[0]["action"] == "device.pair.approve"
    assert audits[0]["outcome"] == "denied"
