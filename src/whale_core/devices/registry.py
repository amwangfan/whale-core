"""D1：DeviceRegistry —— 设备注册 / 配对码 / 吊销 / scope 上限。

协议锚点：whale-mobile contracts/v1/PROTOCOL.md §3——
设备身份 = Ed25519 密钥对；``device_id`` = 公钥 SHA-256 前 16 字节 hex
（仅标识，不构成认证）。配对：设备提交公钥得 6 位配对码（10 min 有效），
主人在管理端批准后设备转 active；吊销即时生效。

持久化对齐 ``memory/spaces.py`` 的 kv 模式：设备元数据存
``device:{device_id}``，InMemoryStore 与 SqliteMemoryStore 双后端天然一致。
审计对齐 studio.py 的 ``audit:studio:`` 约定：只记元数据（action/actor/
outcome/device_id 等），不记公钥全文之外的任何正文——公钥本身即公开信息。
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import time
from datetime import datetime, timezone
from typing import Any, Iterable, Optional
from uuid import uuid4

from whale_core.memory.store import MemoryStore

#: V1 能力全集（§5 能力命名；与 contracts/v1/messages 中 enum 保持一致）。
DEVICE_CAPABILITIES: tuple[str, ...] = (
    "screen.context.read", "screen.history.search", "screen.evidence.read",
    "screen.live.subscribe", "phone.notification.post", "phone.notification.read",
    "phone.call.ring", "phone.call.audio", "phone.device.safe_action",
)

PAIR_CODE_TTL_S = 600
DEVICE_KEY_PREFIX = "device:"
AUDIT_PREFIX = "audit:studio:"  # 对齐 api/studio.py 约定

_PUBKEY_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_DEVICE_ID_RE = re.compile(r"^[0-9a-f]{32}$")


def now_ms() -> int:
    return int(time.time() * 1000)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def device_id_from_pubkey_hex(pubkey_hex: str) -> str:
    """device_id = SHA-256(32 字节公钥) 前 16 字节的小写 hex（§3）。"""
    pub = bytes.fromhex(pubkey_hex)
    if len(pub) != 32:
        raise ValueError("Ed25519 公钥必须为 32 字节（64 hex 字符）")
    return hashlib.sha256(pub).digest()[:16].hex()


class DeviceRegistryError(ValueError):
    """设备注册表操作被拒绝（携带适合回给管理端的 code）。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class DeviceRegistry:
    """设备元数据 CRUD + 配对状态机（pending → active → revoked）。"""

    def __init__(self, store: MemoryStore, *, actor: str = "owner:api-token") -> None:
        self.store = store
        self.actor = actor

    # ---- 审计（对齐 audit:studio:*；只元数据，INV-10） ----

    def audit(self, action: str, *, outcome: str = "ok", reason: str = "",
              **meta: Any) -> None:
        safe: dict[str, Any] = {}
        for key, value in meta.items():
            if isinstance(value, (str, int, float, bool)) or value is None:
                safe[key] = value
            elif isinstance(value, (list, tuple)):
                safe[key] = [str(x)[:80] for x in value][:20]
            else:
                safe[key] = str(value)[:120]
        event = {"at": _now_iso(), "action": action, "actor": self.actor,
                 "outcome": outcome, "reason": reason, **safe}
        self.store.put(f"{AUDIT_PREFIX}{time.time():.6f}:{uuid4().hex[:8]}", event)

    # ---- CRUD ----

    def _key(self, device_id: str) -> str:
        return f"{DEVICE_KEY_PREFIX}{device_id}"

    def get(self, device_id: str) -> Optional[dict[str, Any]]:
        meta = self.store.get(self._key(device_id))
        if not isinstance(meta, dict) or meta.get("device_id") != device_id:
            return None
        return dict(meta)

    def list_devices(self) -> list[dict[str, Any]]:
        out = []
        for key in self.store.keys():
            if not key.startswith(DEVICE_KEY_PREFIX):
                continue
            meta = self.store.get(key)
            if isinstance(meta, dict) and isinstance(meta.get("device_id"), str):
                out.append(dict(meta))
        return sorted(out, key=lambda m: m.get("created_at", 0))

    @staticmethod
    def public_view(meta: dict[str, Any]) -> dict[str, Any]:
        """回给管理端的视图：隐去配对码，其余字段（含公钥）本就非秘密。"""
        view = {k: v for k, v in meta.items()
                if k not in ("pairing_code", "code_expires_at")}
        return view

    def _validate_scopes(self, scopes: Iterable[str]) -> tuple[str, ...]:
        result = tuple(dict.fromkeys(scopes))
        unknown = [s for s in result if s not in DEVICE_CAPABILITIES]
        if unknown:
            raise DeviceRegistryError("bad_request", f"未知 scope: {unknown}")
        return result

    # ---- 配对状态机 ----

    def request_pairing(self, *, label: str, pubkey_hex: str,
                        scopes: Iterable[str] | None = None,
                        code_ttl_s: int = PAIR_CODE_TTL_S) -> dict[str, Any]:
        """设备提交公钥 → 登记 pending 并返回 6 位配对码。

        active 设备重复配对、revoked 设备直接重配对都被拒绝（后者须先删除
        记录——与 dev_gateway 语义一致）。
        """
        if not isinstance(label, str) or not (1 <= len(label.strip()) <= 64):
            raise DeviceRegistryError("bad_request", "label 须为 1..64 字符")
        if not isinstance(pubkey_hex, str) or not _PUBKEY_RE.match(pubkey_hex):
            raise DeviceRegistryError(
                "bad_request", "pubkey_hex 须为 Ed25519 公钥的 64 hex 字符")
        pubkey_hex = pubkey_hex.lower()
        device_id = device_id_from_pubkey_hex(pubkey_hex)
        scope_tuple = self._validate_scopes(scopes or DEVICE_CAPABILITIES)
        existing = self.get(device_id)
        if existing and existing.get("status") == "active":
            raise DeviceRegistryError(
                "conflict", f"设备 {device_id} 已是 active，无需重复配对")
        if existing and existing.get("status") == "revoked":
            raise DeviceRegistryError(
                "conflict", f"设备 {device_id} 已 revoked；如需重配对请先删除记录")
        code = f"{secrets.randbelow(1_000_000):06d}"
        # 配对码全局唯一（撞码时重摇，避免把别人的设备批准进来）
        for _ in range(8):
            clash = any(d.get("pairing_code") == code for d in self.list_devices())
            if not clash:
                break
            code = f"{secrets.randbelow(1_000_000):06d}"
        now = now_ms()
        meta = {
            "device_id": device_id,
            "pubkey_hex": pubkey_hex,
            "label": label.strip(),
            "scopes": list(scope_tuple),
            "status": "pending",
            "pairing_code": code,
            "code_expires_at": now + code_ttl_s * 1000,
            "created_at": (existing or {}).get("created_at", now),
            "updated_at": now,
        }
        self.store.put(self._key(device_id), meta)
        self.audit("device.pair.request", device_id=device_id, label=meta["label"],
                   scopes=scope_tuple)
        return {"code": code, "device_id": device_id, "expires_in": code_ttl_s}

    def approve(self, code: str) -> dict[str, Any]:
        """主人在管理端批准配对码 → 设备转 active。"""
        if not isinstance(code, str) or not re.fullmatch(r"\d{6}", code):
            raise DeviceRegistryError("bad_request", "配对码须为 6 位数字")
        target = None
        for meta in self.list_devices():
            if meta.get("pairing_code") == code:
                target = meta
                break
        if target is None or target.get("status") != "pending":
            self.audit("device.pair.approve", outcome="denied",
                       reason="code_not_found")
            raise DeviceRegistryError("bad_request", "配对码不存在或已被处理")
        if target.get("code_expires_at") is not None \
                and target["code_expires_at"] <= now_ms():  # 到期瞬间即失效
            self.audit("device.pair.approve", outcome="denied",
                       reason="code_expired", device_id=target["device_id"])
            raise DeviceRegistryError("expired", "配对码已过期，请重新 pair")
        target["status"] = "active"
        target["pairing_code"] = None
        target["code_expires_at"] = None
        target["updated_at"] = now_ms()
        self.store.put(self._key(target["device_id"]), target)
        self.audit("device.pair.approve", device_id=target["device_id"])
        return dict(target)

    def revoke(self, device_id: str) -> dict[str, Any]:
        """吊销设备：状态即时生效；踢在线连接由调用方（API 层）触发。"""
        meta = self.get(device_id)
        if meta is None:
            raise DeviceRegistryError("not_found", f"未知设备 {device_id}")
        if meta.get("status") != "revoked":
            meta["status"] = "revoked"
            meta["pairing_code"] = None
            meta["code_expires_at"] = None
            meta["updated_at"] = now_ms()
            self.store.put(self._key(device_id), meta)
        self.audit("device.revoke", device_id=device_id)
        return dict(meta)

    def status_of(self, device_id: str) -> str:
        meta = self.get(device_id)
        return str(meta.get("status")) if meta else "unknown"

    def scopes_of(self, device_id: str) -> tuple[str, ...]:
        meta = self.get(device_id)
        if meta is None:
            return ()
        return tuple(str(s) for s in meta.get("scopes") or ())

    # json 序列化助手（列表端点用）
    @staticmethod
    def dumps_scopes(meta: dict[str, Any]) -> str:
        return json.dumps(meta.get("scopes") or [], ensure_ascii=False)
