"""S4：一次性验证口令 + sudo 式限域临时授权（PRD §6.3/§6.4）。

两类实体严格分账（INV-05）：

- **OneTimeAuthCode**：``uses_allowed=1``，成功验证即在同一事务里原子
  核销；只存校验值（高熵码用带服务端秘密的 HMAC，自定义短语用
  PBKDF2），明文仅创建时返回一次。
- **TemporaryGrant**：验证成功后签发的租约，绑定真实 sender + 确切私聊 +
  scopes + TTL + 操作次数；租约次数与口令次数分账，互不影响。

安全纪律：
- 限流防猜：同一 sender 连续失败计数与锁定；失败不消耗口令。
- 群聊禁止 sudo（``verify`` 拒绝非私聊会话类型）。
- 弱口令（低熵）只允许低风险 scope；高风险动作不在本模块放行清单。
- 口令消息经 ``maybe_intercept`` 在进入 LLM/记忆/日志之前截获。
- 本模块不 import core；由宿主装配进 resolver（GrantLookup 协议）。
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from typing import Any, Mapping, Optional, Tuple
from uuid import uuid4

from whale_core.life.conversation_context import (
    ConversationKind,
    TemporaryGrantRef,
    SCOPE_CHAT_BASIC,
    SCOPE_TASK_READ_SELF,
)
from whale_core.memory.store import MemoryStore

#: sudo 触发前缀（显式验证尝试才会被拦截；普通聊天不碰）
SUDO_PREFIXES: Tuple[str, ...] = ("sudo ", "sudo　", "SUDO ")

#: 低熵口令允许的最大 scope 集合（低风险）
LOW_ENTROPY_SCOPES = frozenset({SCOPE_CHAT_BASIC, SCOPE_TASK_READ_SELF})

#: 租约允许携带的 scope 上限（高风险动作永远不在此列——shell/支付/删数据
#  仍需既有审批链，INV-04）
GRANTABLE_SCOPES = frozenset({
    SCOPE_CHAT_BASIC, SCOPE_TASK_READ_SELF,
    "tool.web.search", "outbox.submit", "device.notify",
    # D3：设备面 scope 族（screen.* / phone.*）纳入可授予集合；
    # 实际能否生效仍由 context_resolver 矩阵裁决（screen.* 仅 owner
    # 私聊/API owner，群聊硬上限拒绝）——列入此处只是「允许租约携带」。
    "screen.context.read", "screen.history.search", "screen.evidence.read",
    "screen.live.subscribe", "phone.notification.post", "phone.notification.read",
    "phone.call.ring", "phone.call.audio", "phone.device.safe_action",
})

MAX_ACTIVE_CODES = 64
FAIL_LIMIT = 5
FAIL_LOCK_S = 600
PBKDF2_ITERATIONS = 100_000

#: F-08：常见弱口令黑名单（自定义短语额外拦截）
_WEAK_PHRASES = frozenset({
    "1234567890", "0123456789", "password", "password1", "qwertyuiop",
    "1111111111", "iloveyou", "adminadmin", "letmein123",
})


class TemporaryAuthError(ValueError):
    pass


def estimate_strength(text: str) -> str:
    """口令强度粗评：'high' / 'low'。低熵形式（短诗句/常见短语）→ low。"""
    t = (text or "").strip()
    if len(t) < 12:
        return "low"
    classes = sum([
        any(c.islower() for c in t),
        any(c.isupper() for c in t),
        any(c.isdigit() for c in t),
        any(not c.isalnum() for c in t),
    ])
    unique = len(set(t))
    # 长度足够 + 字符多样性 + 低重复度才算高熵
    if len(t) >= 20 and classes >= 3 and unique >= len(t) * 0.5:
        return "high"
    return "low"


class TemporaryAuthService:
    """口令生命周期 + 租约生命周期 + GrantLookup 协议实现。"""

    def __init__(self, store: MemoryStore, *, owner_id: str = "owner"):
        self.store = store
        self.owner_id = owner_id
        self._ensure_secret()

    # ---- 服务端秘密（HMAC key；只存于本库，0600 文件保护） ---------------

    def _ensure_secret(self) -> None:
        if not isinstance(self.store.get("otp:secret"), str):
            self.store.put("otp:secret", secrets.token_hex(32))

    def _secret(self) -> bytes:
        return str(self.store.get("otp:secret")).encode()

    # ---- 校验值 -----------------------------------------------------------

    def _verifier(self, code_text: str, strength: str, salt: str = "") -> str:
        if strength == "high":
            # 高熵随机码：HMAC（快）即可——抗离线猜测由熵保证
            return "hmac:" + hmac.new(
                self._secret(), code_text.encode(), hashlib.sha256).hexdigest()
        # 自定义短语：慢速哈希 + 独立盐
        digest = hashlib.pbkdf2_hmac(
            "sha256", code_text.encode(), bytes.fromhex(salt),
            PBKDF2_ITERATIONS)
        return f"pbkdf2:{salt}:{digest.hex()}"

    def _verify_value(self, code_text: str, record: Mapping[str, Any]) -> bool:
        verifier = str(record.get("verifier", ""))
        if verifier.startswith("hmac:"):
            expect = self._verifier(code_text, "high")
            return hmac.compare_digest(expect, verifier)
        if verifier.startswith("pbkdf2:"):
            _, salt, _ = verifier.split(":", 2)
            expect = self._verifier(code_text, "low", salt)
            return hmac.compare_digest(expect, verifier)
        return False

    # ---- 口令创建（可信管理入口调用） -------------------------------------

    def create_code(
        self,
        *,
        allowed_scopes: Tuple[str, ...] = (SCOPE_TASK_READ_SELF,),
        ttl_s: float = 86400,
        created_by: str = "owner",
        custom_phrase: str | None = None,
        grant_ttl_s: float = 600,
        grant_max_uses: int = 1,
        action_fingerprint: str | None = None,
    ) -> str:
        """创建口令，返回明文（仅这一次）。自定义短语过低熵时拒绝。"""
        return str(self.create_code_ex(
            allowed_scopes=allowed_scopes, ttl_s=ttl_s, created_by=created_by,
            custom_phrase=custom_phrase, grant_ttl_s=grant_ttl_s,
            grant_max_uses=grant_max_uses,
            action_fingerprint=action_fingerprint)["code_text"])

    def create_code_ex(
        self,
        *,
        allowed_scopes: Tuple[str, ...] = (SCOPE_TASK_READ_SELF,),
        ttl_s: float = 86400,
        created_by: str = "owner",
        custom_phrase: str | None = None,
        grant_ttl_s: float = 600,
        grant_max_uses: int = 1,
        action_fingerprint: str | None = None,
    ) -> dict:
        """create_code 的扩展返回版（S5 管理 API 用）：明文 + 元数据。

        返回 ``{"code_id", "code_text", "proof_strength", "allowed_scopes",
        "expires_at"}``；``code_text`` 明文仅在这一份返回值里出现，
        存储侧仍只有校验值（INV-05/INV-10）。
        """
        scopes = tuple(s for s in allowed_scopes if s in GRANTABLE_SCOPES)
        if not scopes:
            raise TemporaryAuthError("allowed_scopes 不含任何可授权 scope")
        if grant_max_uses < 1 or grant_ttl_s <= 0 or ttl_s <= 0:
            raise TemporaryAuthError("grant_max_uses/ttl 必须为正")
        if self._active_code_count() >= MAX_ACTIVE_CODES:
            raise TemporaryAuthError("活跃口令过多，请先撤销")

        if custom_phrase is not None:
            phrase = custom_phrase.strip()
            strength = estimate_strength(phrase)
            if strength == "low":
                if not set(scopes) <= LOW_ENTROPY_SCOPES:
                    raise TemporaryAuthError(
                        "低熵自定义口令只能承载低风险 scope；"
                        "请改用随机口令或叠加设备确认")
                # 低熵也要求最低长度，防纯秒猜
                # F-08（红队）：下限 10 + 常见弱口令黑名单
                if len(phrase) < 10:
                    raise TemporaryAuthError("自定义口令太短（至少 10 字符）")
                if phrase.casefold() in _WEAK_PHRASES:
                    raise TemporaryAuthError("该口令过于常见，请换一个")
            code_text = phrase
            salt = secrets.token_hex(16)
            verifier = self._verifier(code_text, strength, salt)
        else:
            code_text = secrets.token_urlsafe(18)  # ~144 bit
            verifier = self._verifier(code_text, "high")

        code_id = f"oc-{uuid4().hex[:16]}"
        expires_at = time.time() + ttl_s
        proof_strength = "high" if custom_phrase is None else estimate_strength(code_text)
        self.store.put(f"otp:code:{code_id}", {
            "code_id": code_id,
            "verifier": verifier,
            "allowed_scopes": list(scopes),
            "expires_at": expires_at,
            "consumed_at": None,
            "revoked_at": None,
            "failure_count": 0,
            "created_by": created_by,
            "grant_ttl_s": grant_ttl_s,
            "grant_max_uses": grant_max_uses,
            "action_fingerprint": action_fingerprint,
            "proof_strength": proof_strength,
        })
        return {
            "code_id": code_id,
            "code_text": code_text,
            "proof_strength": proof_strength,
            "allowed_scopes": list(scopes),
            "expires_at": expires_at,
        }

    def _active_code_count(self) -> int:
        now = time.time()
        n = 0
        for key in self.store.keys():
            if not key.startswith("otp:code:"):
                continue
            rec = self.store.get(key)
            if (isinstance(rec, dict) and not rec.get("consumed_at")
                    and not rec.get("revoked_at")
                    and float(rec.get("expires_at") or 0) > now):
                n += 1
        return n

    def list_codes(self) -> tuple[dict, ...]:
        """管理视图：元数据，永不返回 verifier/明文。"""
        out = []
        for key in sorted(self.store.keys()):
            if not key.startswith("otp:code:"):
                continue
            rec = self.store.get(key)
            if isinstance(rec, dict):
                out.append({k: v for k, v in rec.items() if k != "verifier"})
        return tuple(out)

    def revoke_code(self, code_id: str) -> bool:
        rec = self.store.get(f"otp:code:{code_id}")
        if not isinstance(rec, dict) or rec.get("consumed_at"):
            return False
        rec["revoked_at"] = time.time()
        self.store.put(f"otp:code:{code_id}", rec)
        return True

    # ---- 验证 + 原子核销 + 签发租约（INV-05 核心） ------------------------

    def verify(
        self,
        code_text: str,
        *,
        channel: str,
        sender_id: str,
        conversation_kind: ConversationKind,
        conversation_id: str,
        now: float | None = None,
    ) -> Optional[TemporaryGrantRef]:
        """校验口令；成功则同事务核销并签发租约。失败返回 None（不泄露细节）。

        - 群聊一律拒绝（口令禁止在群中提权）；
        - sender 限流：连续失败 FAIL_LIMIT 次锁 FAIL_LOCK_S 秒；
        - 仅验证失败不消耗口令，但计失败数。
        """
        now = now or time.time()
        if conversation_kind is not ConversationKind.QQ_PRIVATE:
            return None  # N05：首版仅在 QQ 私聊开放 sudo；群聊一律拒绝
        if not sender_id or not code_text or not code_text.strip():
            return None
        if self._locked_out(sender_id, now):
            return None

        candidate = self._find_code(code_text.strip())
        if candidate is None:
            self._record_failure(sender_id, now)
            return None

        grant = self._consume_and_issue(
            candidate, channel=channel, sender_id=sender_id,
            conversation_id=conversation_id, now=now)
        if grant is None:
            self._record_failure(sender_id, now)
            return None
        self._clear_failures(sender_id)
        return grant

    def _find_code(self, code_text: str) -> Optional[dict]:
        now = time.time()
        for key in self.store.keys():
            if not key.startswith("otp:code:"):
                continue
            rec = self.store.get(key)
            if not isinstance(rec, dict):
                continue
            if rec.get("consumed_at") or rec.get("revoked_at"):
                continue
            if float(rec.get("expires_at") or 0) <= now:
                continue
            if self._verify_value(code_text, rec):
                return rec
        return None

    def _consume_and_issue(self, code: Mapping[str, Any], *, channel: str,
                           sender_id: str, conversation_id: str,
                           now: float) -> Optional[TemporaryGrantRef]:
        """单事务：复查口令未消费 → 核销 → 落租约。并发只一人成功。"""
        code_id = str(code["code_id"])
        grant_id = f"tg-{uuid4().hex[:16]}"
        # 与 resolver 对未配对发送者的主体 ID 保持一致（租约证明在
        # proof_method/proof_strength 字段，主体 ID 必须是同一算法）。
        principal_id = f"anon:{channel}:{sender_id}"

        def _tx(tx):
            fresh = tx.get(f"otp:code:{code_id}")
            if not isinstance(fresh, dict):
                return None
            if (fresh.get("consumed_at") or fresh.get("revoked_at")
                    or float(fresh.get("expires_at") or 0) <= now):
                return None
            fresh["consumed_at"] = now
            tx.put(f"otp:code:{code_id}", fresh)
            grant = {
                "grant_id": grant_id,
                "principal_id": principal_id,
                "channel": channel,
                "sender_id": sender_id,
                "conversation_id": conversation_id,
                "scopes": list(fresh.get("allowed_scopes") or []),
                "issued_at": now,
                "expires_at": now + float(fresh.get("grant_ttl_s") or 600),
                "max_uses": int(fresh.get("grant_max_uses") or 1),
                "used_count": 0,
                "proof_method": "otp",
                "proof_strength": str(fresh.get("proof_strength") or "high"),
                "source_code_id": code_id,
                "action_fingerprint": fresh.get("action_fingerprint"),
                "revoked_at": None,
            }
            tx.put(f"otp:grant:{grant_id}", grant)
            return grant

        grant = self.store.transact(_tx)
        if grant is None:
            return None
        return TemporaryGrantRef(
            grant_id=grant_id,
            principal_id=principal_id,
            conversation_key=f"{channel}:{conversation_kind_private()}:{conversation_id}",
            scopes=tuple(grant["scopes"]),
            expires_at=float(grant["expires_at"]),
            max_uses=int(grant["max_uses"]),
            used_count=0,
            proof_method="otp",
        )

    # ---- 租约查询 / 消费 / 撤销（GrantLookup 协议） ------------------------

    def _load_grant(self, grant_id: str) -> Optional[dict]:
        rec = self.store.get(f"otp:grant:{grant_id}")
        return rec if isinstance(rec, dict) else None

    def active_grants(self, principal_id: str, conversation_key: str,
                      *, now: float) -> Tuple[TemporaryGrantRef, ...]:
        """resolver 消费：主体 + 会话严格匹配的有效租约。"""
        out = []
        for key in self.store.keys():
            if not key.startswith("otp:grant:"):
                continue
            rec = self.store.get(key)
            if not isinstance(rec, dict) or rec.get("revoked_at"):
                continue
            if float(rec.get("expires_at") or 0) <= now:
                continue
            if int(rec.get("used_count") or 0) >= int(rec.get("max_uses") or 1):
                continue
            if rec.get("principal_id") != principal_id:
                continue
            key_of = f"{rec.get('channel')}:{conversation_kind_private()}:{rec.get('conversation_id')}"
            if key_of != conversation_key:
                continue
            out.append(TemporaryGrantRef(
                grant_id=str(rec["grant_id"]),
                principal_id=principal_id,
                conversation_key=conversation_key,
                scopes=tuple(rec.get("scopes") or ()),
                expires_at=float(rec["expires_at"]),
                max_uses=int(rec.get("max_uses") or 1),
                used_count=int(rec.get("used_count") or 0),
                proof_method=str(rec.get("proof_method") or "otp"),
            ))
        return tuple(out)

    def consume_grant_use(self, grant_id: str, *,
                          action_fingerprint: str | None = None,
                          required_scope: str | None = None) -> bool:
        """租约操作次数原子消耗；模式 A（绑指纹）参数不符即拒。

        F-02（红队）：``required_scope`` 给定时，租约 scopes 必须覆盖之——
        只含 chat.basic 的低权限租约不能为工具动作放行。
        """
        def _tx(tx):
            rec = tx.get(f"otp:grant:{grant_id}")
            if not isinstance(rec, dict) or rec.get("revoked_at"):
                return False
            if float(rec.get("expires_at") or 0) <= time.time():
                return False
            if int(rec.get("used_count") or 0) >= int(rec.get("max_uses") or 1):
                return False
            if required_scope and required_scope not in tuple(
                    rec.get("scopes") or ()):
                return False  # F-02：scope 不覆盖即拒
            bound = rec.get("action_fingerprint")
            if bound and bound != action_fingerprint:
                return False  # N16：换参数即拒
            rec["used_count"] = int(rec.get("used_count") or 0) + 1
            tx.put(f"otp:grant:{grant_id}", rec)
            return True
        return bool(self.store.transact(_tx))

    def revoke_grant(self, grant_id: str) -> bool:
        rec = self._load_grant(grant_id)
        if rec is None:
            return False
        rec["revoked_at"] = time.time()
        self.store.put(f"otp:grant:{grant_id}", rec)
        return True

    def list_grants(self, *, active_only: bool = True) -> tuple[dict, ...]:
        """管理视图（S5）：租约元数据列表。

        只含元数据（id/主体/会话/scopes/次数/有效期/撤销状态），
        租约本体不含任何秘密材料，无需额外脱敏；``active_only=True``
        时排除已撤销 / 已过期 / 次数耗尽的租约。
        """
        now = time.time()
        out = []
        for key in sorted(self.store.keys()):
            if not key.startswith("otp:grant:"):
                continue
            rec = self.store.get(key)
            if not isinstance(rec, dict):
                continue
            revoked = bool(rec.get("revoked_at"))
            expired = float(rec.get("expires_at") or 0) <= now
            exhausted = int(rec.get("used_count") or 0) >= int(rec.get("max_uses") or 1)
            if active_only and (revoked or expired or exhausted):
                continue
            out.append({
                "grant_id": str(rec.get("grant_id") or ""),
                "principal_id": str(rec.get("principal_id") or ""),
                "channel": str(rec.get("channel") or ""),
                "sender_id": str(rec.get("sender_id") or ""),
                "conversation_id": str(rec.get("conversation_id") or ""),
                "scopes": list(rec.get("scopes") or []),
                "issued_at": float(rec.get("issued_at") or 0),
                "expires_at": float(rec.get("expires_at") or 0),
                "max_uses": int(rec.get("max_uses") or 1),
                "used_count": int(rec.get("used_count") or 0),
                "remaining_uses": max(
                    0, int(rec.get("max_uses") or 1) - int(rec.get("used_count") or 0)),
                "proof_method": str(rec.get("proof_method") or "otp"),
                "proof_strength": str(rec.get("proof_strength") or ""),
                "source_code_id": str(rec.get("source_code_id") or ""),
                "revoked": revoked,
                "expired": expired,
            })
        return tuple(out)

    # ---- 限流 ---------------------------------------------------------------

    def _fail_key(self, sender_id: str) -> str:
        return f"otp:fail:{hashlib.sha256(sender_id.encode()).hexdigest()[:24]}"

    def _locked_out(self, sender_id: str, now: float) -> bool:
        rec = self.store.get(self._fail_key(sender_id))
        return bool(isinstance(rec, dict) and float(rec.get("until") or 0) > now)

    def _record_failure(self, sender_id: str, now: float) -> None:
        key = self._fail_key(sender_id)
        rec = self.store.get(key)
        count = int(rec.get("count") or 0) + 1 if isinstance(rec, dict) else 1
        rec = {"count": count, "until": now + FAIL_LOCK_S if count >= FAIL_LIMIT else 0}
        self.store.put(key, rec)

    def _clear_failures(self, sender_id: str) -> None:
        self.store.put(self._fail_key(sender_id), {"count": 0, "until": 0})


def conversation_kind_private() -> str:
    """租约目前只在私聊签发（群聊不开放 sudo）。"""
    return ConversationKind.QQ_PRIVATE.value


def maybe_intercept_sudo(text: str) -> Optional[str]:
    """识别「sudo <口令>」验证尝试，返回口令文本；非验证消息返回 None。

    调用方（渠道宿主）必须在文本进入 LLM/记忆/历史/日志**之前**调用；
    返回非 None 的文本按验证请求处理，明文口令不再向后传递（INV-05/N17）。

    F-03（红队）：大小写与全角空格归一化——手机首字母自动大写
    （"Sudo …"）不得绕过拦截。
    """
    t = (text or "").strip()
    # 先整体归一化再判定（sudo 四字母为 ASCII，casefold 不改变其长度，
    # 因此原文第 5 位之后的切片即口令原文）。
    norm = t.casefold().replace("　", " ")
    if norm == "sudo":
        return ""  # 光秃 sudo：按验证请求处理（会得到失败回复），原文不外传
    if norm.startswith("sudo "):
        return t[5:].strip()
    return None
