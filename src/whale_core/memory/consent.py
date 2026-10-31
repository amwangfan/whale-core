"""G2：群友个人记忆授权（MemberConsent）。

不变量（下一阶段计划 §4 / §11）：

- **MEM-02**：member_private 默认不采集；必须成员**本人** consent。
  群主/管理者不能替其他成员授权——``granted_by`` 必须等于 consent 主体
  （``principal_id``），授权入口只能是本人已认证私聊流程或管理通道代为
  提交本人意愿（API 层再做结构性校验）。
- 授权按 ``(principal_id, source_group)`` 维度记录：允许来源群、用途
  scope、版本、授予/撤销时间、TTL。
- 撤销与过期即时生效：``is_valid`` 失败即停止新写入与召回。
- 本人管理权（查看/纠正/删除）**不依赖** consent 有效——撤销后仍可
  清理已有记忆（隐私权不随授权状态消失）。

存储：kv ``member:consent:{principal_id}:{source_group}``，双后端一致。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Optional, Tuple

from whale_core.life.conversation_context import MemorySpaceRef, MemorySpaceType
from whale_core.memory.store import MemoryStore

#: 授权默认有效期：180 天（0 = 不过期，需显式传入）。
DEFAULT_CONSENT_TTL_S = 180 * 86400

#: 默认可授权 scope（首版只有「本人记忆同步」一种用途）。
CONSENT_SCOPE_SELF_MEMORY = "self_memory"
KNOWN_CONSENT_SCOPES = frozenset({CONSENT_SCOPE_SELF_MEMORY})

_PREFIX = "member:consent:"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(ts: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(ts))
    except (ValueError, TypeError):
        return None


class ConsentError(ValueError):
    """授权操作被拒绝（代授权 / 参数非法）。"""


@dataclass(frozen=True)
class MemberConsent:
    """一条成员授权记录（§2.1 权威领域对象）。"""

    principal_id: str      # consent 主体（成员本人）
    source_group: str      # 允许的来源群 conversation_id（如 "group:12345"）
    scope: str             # 用途/保存类型范围
    version: int           # 授权版本：重复 grant 递增
    granted_at: str
    revoked_at: Optional[str]
    ttl_s: float           # 授权有效期（秒）；0 = 不过期
    granted_by: str        # 必须 == principal_id（群主不能代授权）
    granted_via: str       # 授权入口证据（本人私聊 conversation_key）


class MemberConsentStore:
    """MemberConsent 的 kv 存储与校验。"""

    def __init__(self, store: MemoryStore):
        self.store = store

    @staticmethod
    def _key(principal_id: str, source_group: str) -> str:
        return f"{_PREFIX}{principal_id}:{source_group}"

    # -- 查询 --------------------------------------------------------------

    def get(self, principal_id: str, source_group: str) -> Optional[MemberConsent]:
        data = self.store.get(self._key(principal_id, source_group))
        if not isinstance(data, dict):
            return None
        try:
            return MemberConsent(
                principal_id=str(data["principal_id"]),
                source_group=str(data["source_group"]),
                scope=str(data.get("scope") or ""),
                version=int(data.get("version") or 1),
                granted_at=str(data.get("granted_at") or ""),
                revoked_at=(str(data["revoked_at"])
                            if data.get("revoked_at") else None),
                ttl_s=float(data.get("ttl_s") or 0.0),
                granted_by=str(data.get("granted_by") or ""),
                granted_via=str(data.get("granted_via") or ""),
            )
        except (TypeError, ValueError, KeyError):
            return None  # 污染数据 fail closed

    def list_for(self, principal_id: str) -> Tuple[MemberConsent, ...]:
        prefix = f"{_PREFIX}{principal_id}:"
        out: list[MemberConsent] = []
        for key in self.store.keys():
            if not key.startswith(prefix):
                continue
            # G6 红队中-3：前缀碰撞防护——回校验记录内的 principal_id，
            # 防异形 principal（如 anon:qq:1:x）的键被 anon:qq:1 前缀误吞。
            consent = self.get(principal_id, key[len(prefix):])
            if consent is not None and consent.principal_id == principal_id:
                out.append(consent)
        return tuple(sorted(out, key=lambda c: c.source_group))

    def is_valid(self, consent: Optional[MemberConsent],
                 *, now: Optional[datetime] = None) -> bool:
        """授权当前有效：存在、未撤销、未过期。"""
        if consent is None or consent.revoked_at:
            return False
        if consent.ttl_s > 0:
            granted = _parse(consent.granted_at)
            if granted is None:
                return False
            current = now or _now()
            if (current - granted).total_seconds() > consent.ttl_s:
                return False
        return True

    def valid_for_group(self, principal_id: str, source_group: str,
                        *, now: Optional[datetime] = None
                        ) -> Optional[MemberConsent]:
        """该成员对某个来源群的有效授权（提炼管道的写入闸门）。"""
        consent = self.get(principal_id, source_group)
        return consent if self.is_valid(consent, now=now) else None

    _OPTOUT_PREFIX = "member:optout:"

    def opt_out(self, principal_id: str) -> None:
        """owner 决策 2026-11-01：成员记忆默认开启（私聊即同意），
        「别记我了」= 持久退出标记。"""
        self.store.put(f"{self._OPTOUT_PREFIX}{principal_id}",
                       {"opted_out": True, "at": _now().isoformat()})
        self.revoke_all(principal_id)

    def opt_in(self, principal_id: str) -> None:
        self.store.delete(f"{self._OPTOUT_PREFIX}{principal_id}")

    def is_opted_out(self, principal_id: str) -> bool:
        rec = self.store.get(f"{self._OPTOUT_PREFIX}{principal_id}")
        return isinstance(rec, dict) and bool(rec.get("opted_out"))

    def has_valid(self, principal_id: str, *,
                  now: Optional[datetime] = None) -> bool:
        """该成员是否存在任一有效授权（私聊召回的下发闸门）。"""
        return any(self.is_valid(c, now=now)
                   for c in self.list_for(principal_id))

    # -- 写入 --------------------------------------------------------------

    def grant(self, principal_id: str, source_group: str, *,
              scope: str = CONSENT_SCOPE_SELF_MEMORY,
              ttl_s: float = DEFAULT_CONSENT_TTL_S,
              granted_by: str, granted_via: str) -> MemberConsent:
        """授予/续期授权。``granted_by`` 必须是成员本人（MEM-02）。

        重复 grant 递增 version（重新授权 = 新版本，不静默复用旧版本）。
        """
        if not principal_id or not source_group:
            raise ConsentError("principal_id 与 source_group 必填")
        if granted_by != principal_id:
            raise ConsentError(
                "consent_actor_mismatch: 群主/他人不能替成员授权；"
                "granted_by 必须等于 consent 主体")
        if scope not in KNOWN_CONSENT_SCOPES:
            raise ConsentError(f"未知授权 scope: {scope!r}")
        if ttl_s < 0:
            raise ConsentError("ttl_s 不能为负")
        old = self.get(principal_id, source_group)
        version = (old.version + 1) if old is not None else 1
        consent = MemberConsent(
            principal_id=principal_id, source_group=source_group,
            scope=scope, version=version, granted_at=_now().isoformat(),
            revoked_at=None, ttl_s=float(ttl_s),
            granted_by=granted_by, granted_via=granted_via[:220])
        self.store.put(self._key(principal_id, source_group), {
            "principal_id": consent.principal_id,
            "source_group": consent.source_group,
            "scope": consent.scope,
            "version": consent.version,
            "granted_at": consent.granted_at,
            "revoked_at": consent.revoked_at,
            "ttl_s": consent.ttl_s,
            "granted_by": consent.granted_by,
            "granted_via": consent.granted_via,
        })
        return consent

    def revoke(self, principal_id: str, source_group: str) -> bool:
        """撤销一个来源群的授权（保留记录与审计痕迹，状态置 revoked）。"""
        consent = self.get(principal_id, source_group)
        if consent is None or consent.revoked_at:
            return False
        data = dict(self.store.get(self._key(principal_id, source_group)))
        data["revoked_at"] = _now().isoformat()
        self.store.put(self._key(principal_id, source_group), data)
        return True

    def revoke_all(self, principal_id: str) -> int:
        """撤销该成员全部授权；返回撤销条数。"""
        count = 0
        for consent in self.list_for(principal_id):
            if self.revoke(principal_id, consent.source_group):
                count += 1
        return count


class MemberMemoryDirectory:
    """resolver 侧查询：principal → member_private 空间（绑定 + 有效 consent 双门）。

    实现 context_resolver 的 MemberMemoryLookup 协议。三道闸门全部满足才
    下发空间：PrincipalMemoryBinding 存在 → 空间存活且类型为 member_private
    → 该成员存在任一有效 consent。撤销 consent 的下一秒召回即停止。
    """

    def __init__(self, bindings, spaces, consents: MemberConsentStore):
        self._bindings = bindings
        self._spaces = spaces
        self._consents = consents

    def member_space_for(self, principal_id: str,
                         *, now: Optional[datetime] = None
                         ) -> Optional[MemorySpaceRef]:
        if not principal_id:
            return None
        binding = self._bindings.member_binding_for(principal_id)
        if binding is None:
            return None
        ref = self._spaces.ref(str(binding.get("space_id") or ""))
        if ref is None or ref.type is not MemorySpaceType.MEMBER_PRIVATE:
            return None
        # owner 决策 2026-11-01：私聊即同意，退出用「别记我了」；
        # consent 仍是撤销/审计证据链，但不再作为默认门槛。
        if self._consents.is_opted_out(principal_id):
            return None
        # 空间本身可写（提炼管道/本人管理操作），但 member_private 不在
        # spaces.WRITABLE_TYPES 中——会话闲聊的自动写回不会落进成员空间。
        return replace(ref, can_write=True)

    def space_for_member_management(self, principal_id: str
                                    ) -> Optional[MemorySpaceRef]:
        """本人管理路径（list/correct/delete）：只看绑定与空间，不看 consent。

        撤销授权后成员仍须能查看/删除自己的存量记忆（隐私权不随授权消失）。
        """
        binding = self._bindings.member_binding_for(principal_id)
        if binding is None:
            return None
        ref = self._spaces.ref(str(binding.get("space_id") or ""))
        if ref is None or ref.type is not MemorySpaceType.MEMBER_PRIVATE:
            return None
        return ref
