"""owner_fact_v1 宿主接线（装配侧）：授权提供者 / observe 入口 / 遗忘抑制 /
显式命令窄分派。

领域语义（受理、提交、更正、遗忘的事务与 generation fence）全部在
``memory/owner_facts.py``；本模块只做宿主侧接线，**不写第二套领域逻辑**、
不直接改写 offact: 记录（唯一例外是遗忘抑制里对 chat 情节/短历史的既有
治理动作，那是本模块职责范围内的装配侧数据）。

纪律（冻结方案 §4/§5.1/§6 + Astra 红队 2026-11-12 + 协调约束）：

- ``context=None`` 的兼容调用绝不自动采集（无可信授权即无记忆）；
- 授权**实时**解析（space 元数据 / pause 开关 / 归属），不捕获旧
  ResolvedContext；``authority_version`` 由可被事务观察的字段派生，
  服务 commit 在事务内比对避免 TOCTOU；
- 遗忘抑制严格按「来源绑定索引」限域：只清/遮蔽该 owner 自有会话的短
  history 与 chat 情节，绝不按字符串猜测去删群会话或他人数据；
- 显式「忘掉」只做确定性精确匹配：唯一命中才删，多义必须澄清，
  禁止相似度 top-1 盲删；
- 日志/诊断不含用户原文。
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Mapping, Sequence

from whale_core.memory.store import MemoryKind, MemoryStore

#: 记录判别（tag/前缀与 memory/owner_facts.py 冻结值一致；用字面量避免
#: 窄分派路径对该模块的硬依赖——未装配时旧 API 仍能识别并拒绝）。
OWNER_FACT_TAG = "owner_fact_v1"
OWNER_FACT_KEY_PREFIX = "offact:"

#: 宿主侧会话来源索引（我的 kv 前缀，与领域 ofgen/ofq/ofjob/ofev/ofidx 不冲突）：
#: forget 跨会话抑制只允许扫这里登记过的 owner 自有会话。
#: **持久分页、不截断**（协调约束 2026-11-12：截断会让第 65 个会话之后的
#: 历史无法被遗忘抑制；来源索引不是资源预算，宁多页不丢源）：
#: - ``ofhost:convs:{owner}``        → {"pages": n, "count": m}
#: - ``ofhost:convs:{owner}:p{i}``   → [conversation_id, ...]（每页 ≤64）
_OWNER_CONVS_PREFIX = "ofhost:convs:"
_CONVS_PAGE_SIZE = 64

#: 可信身份来源登记（宿主 composition root 注册；authorize_now 逐条经
#: OwnerRegistry.resolve **实时**回查——解绑即失效，commit 事务内可比对）：
#: ``ofhost:authsrc:{owner_id}`` → [{"channel","sender_id"}, ...]（去重，
#: 身份通道天然少量；满 32 拒绝新增而不淘汰旧绑定）。
_OWNER_AUTHSRC_PREFIX = "ofhost:authsrc:"
_AUTHSRC_CAP = 32

#: 修订链快照深度上限（防环/防超长链）。
_CHAIN_DEPTH = 16

#: 本机管理通道（API/控制台，持本地 token）的规范主体：与 profiles 装配的
#: pairing {("api","console"): "owner"} 一致；管理面窄分派经
#: authorize_principal_now 用它做真实来源核查（登记+绑定后有效）。
CONSOLE_PRINCIPAL = "paired:api:console"


# ---------------------------------------------------------------------------
# 记录判别（供旧 API 窄分派；不 import 领域模块）
# ---------------------------------------------------------------------------


def is_owner_fact_record(record: Any) -> bool:
    """按冻结 tag 判别 owner_fact_v1 记录（墓碑同样命中——墓碑受保护）。"""
    if record is None:
        return False
    tags = tuple(getattr(record, "tags", ()) or ())
    key = str(getattr(record, "key", "") or "")
    return OWNER_FACT_TAG in tags or key.startswith(OWNER_FACT_KEY_PREFIX)


def owner_fact_status(record: Any) -> str:
    """记录状态（accepted/pending/superseded/expired/deleted；非本命名空间=''）。"""
    if not is_owner_fact_record(record):
        return ""
    return str((getattr(record, "content", None) or {}).get("status") or "")


def owner_fact_id(record: Any) -> str:
    if not is_owner_fact_record(record):
        return ""
    return str((getattr(record, "content", None) or {}).get("fact_id") or "")


# ---------------------------------------------------------------------------
# 可信身份来源登记 + 授权提供者（实时；不捕获旧 context）
# ---------------------------------------------------------------------------


def register_owner_source(store: MemoryStore, owner_id: str, channel: str,
                          sender_id: str) -> bool:
    """宿主 composition root 登记 owner 的可信 (channel, sender) 身份来源。

    登记做两件事（都幂等）：

    1. 写入来源清单 ``ofhost:authsrc:{owner_id}``（authorize 时逐条回查）；
    2. 经 ``OwnerRegistry.bind`` 把该来源**物化**进身份 kv（宿主已在配置层
       完成认证——env/配对表；物化后才存在可实时核查、可撤销的绑定）。

    解绑（OwnerRegistry.unbind / identity:channel:* 删除）后 authorize_now /
    authorize_principal_now 立即失效——commit 事务内回查同样拒绝（其他来源
    仍活跃**不能替代**本来源，无 any-source 后门）。满 ``_AUTHSRC_CAP``
    拒绝新增（返回 False）而不淘汰旧绑定。
    """
    owner_id = str(owner_id or "").strip()
    channel = str(channel or "").strip()
    sender_id = str(sender_id or "").strip()
    if not owner_id or not channel or not sender_id:
        return False
    from whale_core.life.identity import OwnerRegistry

    try:
        OwnerRegistry(store).bind(owner_id, channel, sender_id)
    except ValueError:
        return False  # 已绑定到其它 owner：拒绝登记（先解绑再改绑）
    key = f"{_OWNER_AUTHSRC_PREFIX}{owner_id}"
    rows = store.get(key)
    rows = [dict(r) for r in rows if isinstance(r, Mapping)] if isinstance(rows, list) else []
    for row in rows:
        if (str(row.get("channel") or "") == channel
                and str(row.get("sender_id") or "") == sender_id):
            return True  # 已登记（幂等）
    if len(rows) >= _AUTHSRC_CAP:
        return False
    rows.append({"channel": channel, "sender_id": sender_id})
    store.put(key, rows)
    return True


def owner_sources(store: MemoryStore, owner_id: str) -> tuple[dict, ...]:
    rows = store.get(f"{_OWNER_AUTHSRC_PREFIX}{str(owner_id or '')}")
    if not isinstance(rows, list):
        return ()
    return tuple(dict(r) for r in rows if isinstance(r, Mapping))


class LiveAuthorityProvider:
    """``owner_facts.AuthorityProvider`` 的宿主实现（协调修订 2026-11-12）。

    首批仅 owner 私聊/眼镜的 owner_working_copy 空间。**每次调用实时核查**
    （不捕获旧 ResolvedContext，不接受任意 owner 字符串）：

    1. space 元数据：存在、未注销、类型 = owner_working_copy、归属与
       owner_id 一致（空间存在 ≠ 任意 owner 可用）；
    2. 空间级「停止记录」暂停开关；
    3. **OwnerRegistry 身份绑定**：宿主登记的可信 (channel, sender) 来源中
       至少一条当前 ``resolve(channel, sender) == owner_id``——解绑后即使
       空间仍在，authorize_now 也返回 None，worker 在途 job 于 commit 事务
       内被拒（authority_version 同步变化，双保险防 TOCTOU）；
    4. 宿主可选附加闸（extra_gate）。

    ``principal_id`` 语义（Kimi 窄鉴权接口 2026-11-12 冻结）：领域侧
    observe/commit/correct/forget 一律经 ``authorize_principal_now`` 按
    **真实来源 principal**（``paired:{channel}:{sender}``，服务端 resolver
    产物）精确核查——解绑该来源即拒绝，其他来源活跃不能替代，绝不把
    evidence 改写成 canonical/字典序来源。``authorize_now``（canonical
    主体）仅服务 retrieve 等只读管理面与测试假件回落。
    """

    SPACE_TYPE = "owner_working_copy"

    def __init__(self, store: MemoryStore, *, policy_version: str = "1",
                 extra_gate: Callable[[str, str], bool] | None = None) -> None:
        self.store = store
        self.policy_version = str(policy_version)
        # 宿主可选附加闸（如运行期全局停记开关）；缺省不启用。
        self._extra_gate = extra_gate

    # -- 内部：实时状态读取 ------------------------------------------------

    def _space_state(self, owner_id: str, space_id: str) -> dict[str, Any]:
        meta = self.store.get(f"space:{space_id}")
        meta = dict(meta) if isinstance(meta, Mapping) else {}
        paused_kv = self.store.get(f"space:paused:{space_id}")
        paused = bool(isinstance(paused_kv, Mapping) and paused_kv.get("paused"))
        return {
            "exists": bool(meta),
            "type": str(meta.get("type") or ""),
            "owner": str(meta.get("owner") or ""),
            "version": int(meta.get("version") or 0),
            "retired_at": meta.get("retired_at"),
            "paused": paused,
        }

    def _active_principals(self, owner_id: str) -> tuple[str, ...]:
        """当前经 OwnerRegistry 实时解析仍绑定到该 owner 的规范主体列表。"""
        out: list[str] = []
        for src in owner_sources(self.store, owner_id):
            if self._source_active(owner_id, src):
                out.append("paired:{}:{}".format(
                    src.get("channel"), src.get("sender_id")))
        return tuple(sorted(out))

    def _source_active(self, owner_id: str, src: Mapping[str, Any]) -> bool:
        """单条登记来源的实时绑定核查（OwnerRegistry.resolve 现查）。"""
        from whale_core.life.identity import OwnerRegistry

        channel = str(src.get("channel") or "")
        sender = str(src.get("sender_id") or "")
        if not channel or not sender:
            return False
        try:
            return OwnerRegistry(self.store).resolve(channel, sender) == owner_id
        except Exception:
            return False  # 证据源故障 = 未认证（fail closed）

    def _principal_active(self, owner_id: str, principal_id: str) -> bool:
        """**具体来源**的实时绑定核查：``paired:{channel}:{sender}`` 逐段
        解析后经 OwnerRegistry.resolve 现查（其他来源活跃不能替代本来源；
        展示名/昵称永不参与——身份只认显式配对 kv）。"""
        parts = str(principal_id or "").split(":", 2)
        if len(parts) != 3 or parts[0] != "paired":
            return False
        return self._source_active(owner_id,
                                   {"channel": parts[1], "sender_id": parts[2]})

    def authorize_principal_now(self, owner_id: str, space_id: str,
                                principal_id: str):
        """Kimi 窄鉴权接口（2026-11-12 冻结）：按**真实来源 principal** 授权。

        observe/commit/correct/forget 全部经此精确核查该条证据的来源绑定
        仍活跃 + 空间面可用；不回落 canonical、不接受 any-source。
        """
        state = self._space_state(owner_id, space_id)
        if not self._space_ok(owner_id, space_id, state):
            return None
        if not self._principal_active(owner_id, principal_id):
            return None
        if self._extra_gate is not None and not self._extra_gate(owner_id, space_id):
            return None
        try:
            from whale_core.memory.owner_facts import MemoryAuthorization
        except Exception:
            return None
        return MemoryAuthorization(
            owner_id=owner_id,
            space_id=space_id,
            principal_id=str(principal_id),  # 真实来源主体，不改写
            policy_version=self.policy_version,
            authority_version=self.authority_version(owner_id, space_id),
            readable_space_ids=(space_id,),
        )

    def _space_ok(self, owner_id: str, space_id: str,
                  state: Mapping[str, Any]) -> bool:
        """空间面核查（不含身份绑定）：存在/类型/未注销/未暂停/归属一致。

        owner 侧「停止记录」= 空间级 pause 开关（issue #38 同闸）或宿主
        WHALE_OWNER_FACT_MEMORY=0；MemberConsent opt-out 是群成员语义，
        对 owner 不适用——不声称检查了不存在的东西。
        """
        if not space_id or not state.get("exists"):
            return False
        if state.get("type") != self.SPACE_TYPE:
            return False  # 首批仅 owner 工作副本；群/成员/原始库一律拒绝
        if state.get("retired_at"):
            return False
        if state.get("paused"):
            return False
        if str(state.get("owner") or "") != owner_id:
            return False  # 空间归属必须与主体严格一致（不接受任意 owner 串）
        return True

    def _usable(self, owner_id: str, space_id: str,
                state: Mapping[str, Any], principals: Sequence[str]) -> bool:
        if not self._space_ok(owner_id, space_id, state):
            return False
        if not principals:
            return False  # 无任何当前有效的可信身份绑定（解绑即失效）
        if self._extra_gate is not None and not self._extra_gate(owner_id, space_id):
            return False
        return True

    # -- AuthorityProvider 协议 --------------------------------------------

    def authorize_now(self, owner_id: str, space_id: str):
        """当前授权快照；任一条件不满足返回 None。只读、无副作用。"""
        state = self._space_state(owner_id, space_id)
        principals = self._active_principals(owner_id)
        if not self._usable(owner_id, space_id, state, principals):
            return None
        try:
            from whale_core.memory.owner_facts import MemoryAuthorization
        except Exception:
            return None
        return MemoryAuthorization(
            owner_id=owner_id,
            space_id=space_id,
            principal_id=principals[0],  # 规范主体（活跃来源字典序最小）
            policy_version=self.policy_version,
            authority_version=self.authority_version(owner_id, space_id),
            readable_space_ids=(space_id,),
        )

    def authority_version(self, owner_id: str, space_id: str) -> str:
        """事务内可观察字段的短 hash：space 元数据 + 暂停位 + **当前活跃
        身份绑定集合** + 策略版本。解绑/pause/空间变更任一发生即变化，
        服务 commit 在事务内重算比对（TOCTOU 防护）。"""
        state = self._space_state(owner_id, space_id)
        principals = self._active_principals(owner_id)
        material = "|".join([
            str(owner_id), str(space_id), str(state.get("type")),
            str(state.get("owner")), str(state.get("version")),
            str(state.get("retired_at") or ""), str(bool(state.get("paused"))),
            ",".join(principals), str(self.policy_version),
        ])
        return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# observe 入口（主模型调用前；宿主适配器共用）
# ---------------------------------------------------------------------------


def remember_owner_conversation(store: MemoryStore, owner_id: str,
                                conversation_id: str) -> None:
    """把 owner 自有会话登记进来源绑定索引（forget 跨会话抑制的限域依据）。

    持久分页、**不截断**：索引丢失会让后续会话的遗忘抑制失效，这与队列
    资源预算不同，宁多页不丢源。每页 ≤64 个会话 id；去重后追加。
    读-改-写在**单个事务**内完成（Astra 2026-11-12：多 get/put 会丢更新）。
    """
    owner_id = str(owner_id or "").strip()
    cid = str(conversation_id or "").strip()
    if not owner_id or not cid:
        return
    meta_key = _OWNER_CONVS_PREFIX + owner_id

    def _tx(tx) -> None:
        meta = tx.get(meta_key)
        pages = int(meta.get("pages") or 0) if isinstance(meta, Mapping) else 0
        count = int(meta.get("count") or 0) if isinstance(meta, Mapping) else 0
        for i in range(pages):
            rows = tx.get(f"{meta_key}:p{i}")
            if isinstance(rows, list) and cid in rows:
                return  # 已登记（幂等）
        if pages == 0:
            tx.put(f"{meta_key}:p0", [cid])
            tx.put(meta_key, {"pages": 1, "count": 1})
            return
        last_key = f"{meta_key}:p{pages - 1}"
        last = tx.get(last_key)
        last = ([str(c) for c in last if isinstance(c, str)]
                if isinstance(last, list) else [])
        if len(last) < _CONVS_PAGE_SIZE:
            last.append(cid)
            tx.put(last_key, last)
            tx.put(meta_key, {"pages": pages, "count": count + 1})
        else:
            tx.put(f"{meta_key}:p{pages}", [cid])
            tx.put(meta_key, {"pages": pages + 1, "count": count + 1})

    store.transact(_tx)


def owner_conversations(store: MemoryStore, owner_id: str) -> tuple[str, ...]:
    """来源绑定索引内的全部 owner 自有会话（分页读取；不扫全库）。"""
    meta_key = _OWNER_CONVS_PREFIX + str(owner_id or "")
    meta = store.get(meta_key)
    pages = int(meta.get("pages") or 0) if isinstance(meta, Mapping) else 0
    out: list[str] = []
    for i in range(pages):
        rows = store.get(f"{meta_key}:p{i}")
        if isinstance(rows, list):
            out.extend(str(c) for c in rows if isinstance(c, str) and c)
    return tuple(out)


def resolve_owner_authorization(store: MemoryStore, provider, context,
                                conversation_id: str):
    """从服务端 ResolvedContext 解析 (owner_id, space_id, auth)；不合格返回 None。

    首批闸门：context 必须存在；principal.is_owner；非群会话；授权空间里有
    可写工作副本；provider 实时复核。任一不满足 = 不采集（fail closed）。
    """
    if provider is None or context is None:
        return None  # 无上下文兼容调用绝不自动采集
    try:
        if str(conversation_id or "").startswith("group:"):
            return None
        from whale_core.life.conversation_context import ConversationKind
        if getattr(context.conversation, "kind", None) is ConversationKind.QQ_GROUP:
            return None
        principal = getattr(context, "principal", None)
        if principal is None or not getattr(principal, "is_owner", False):
            return None
        spaces = getattr(context, "allowed_memory_spaces", None)
        if spaces is None:
            return None
        from whale_core.memory.spaces import default_write_space
        space_id = default_write_space(tuple(spaces))
        if not space_id:
            return None
        meta = store.get(f"space:{space_id}")
        owner_id = (str(meta.get("owner")) if isinstance(meta, Mapping)
                    and meta.get("owner") else "owner")
        auth = provider.authorize_now(owner_id, space_id)
        if auth is None:
            return None
        return owner_id, space_id, auth
    except Exception:
        return None


def observe_owner_text(service, provider, store: MemoryStore, *, context,
                       conversation_id: str, channel: str, text: str,
                       evidence_id: str,
                       source_message_id: str | None = None,
                       received_at: datetime | None = None,
                       timezone_name: str | None = None):
    """主回答模型调用前的快速持久受理（fail soft；返回 ObserveResult|None）。

    - ``text`` 必须是**用户原话**（未拼接引用注释/系统提示/助手内容）；
    - ``evidence_id`` 宿主生成且稳定：平台消息 ID 优先，缺省用入口生成的
      server UUID，主/重模型/补发同一轮复用（服务侧按 evidence_id 幂等）；
    - sudo 口令等已由渠道前置拦截（wq_push/qq_workflow 先于一切持久化），
      敏感内容由服务 observe 内部 contains_sensitive 再挡一层；
    - 任何异常吞掉并返回 None——采集缺席永远不阻塞聊天。
    """
    if service is None or provider is None:
        return None
    resolved = resolve_owner_authorization(
        store, provider, context, conversation_id)
    if resolved is None:
        return None
    owner_id, space_id, auth = resolved
    raw = str(text or "")
    if not raw.strip():
        return None
    try:
        from whale_core.memory.owner_facts import (
            ObserveStatus, UserEvidence)

        stamp = received_at or datetime.now(timezone.utc)
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        # 真实来源主体（paired:{channel}:{sender}，服务端 resolver 产物）：
        # **绝不改写成 canonical/字典序来源**（Astra 2026-11-12）。入口经
        # authorize_principal_now 精确核查该来源当前仍绑定（OwnerRegistry
        # 现查）；provider 未实现窄接口时才回落 canonical auth（Kimi 域侧
        # 按 principal 严格相等校验，仅测试假件走该路径）。
        real_principal = str(getattr(getattr(context, "principal", None),
                                     "principal_id", "") or "")
        authorize_principal = getattr(provider, "authorize_principal_now", None)
        if callable(authorize_principal) and real_principal:
            auth = authorize_principal(owner_id, space_id, real_principal)
            if auth is None:
                return None  # 本来源解绑/空间不可用：fail closed
            principal_id = real_principal
        else:
            principal_id = str(getattr(auth, "principal_id", "")
                               or real_principal or f"owner:{owner_id}")
        evidence = UserEvidence(
            evidence_id=str(evidence_id),
            owner_id=owner_id,
            principal_id=principal_id,
            conversation_id=str(conversation_id or "")[:160],
            channel=str(channel or ""),
            space_id=space_id,
            text=raw,
            received_at=stamp.astimezone(timezone.utc),
            role="user",
            timezone=timezone_name,
            source_message_id=(str(source_message_id)
                               if source_message_id else None),
            schema_version=1,
        )
        result = service.observe(evidence, auth)
        if result is not None and result.status is ObserveStatus.QUEUED:
            remember_owner_conversation(store, owner_id, conversation_id)
        return result
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 遗忘抑制（forget 成功后同步执行；来源绑定索引限域）
# ---------------------------------------------------------------------------


def _norm(text: str) -> str:
    """匹配用规范化：压缩空白与标点、小写（与 companion._norm_mem 同口径）。"""
    return re.sub(r"[\s\W]+", "", str(text or "").lower())


def snapshot_fact_texts(service, fact_ids: Iterable[str]) -> tuple[str, ...]:
    """forget **前**沿修订链快照事实的匹配文本（summary==quote 原句 +
    结构化 value）。墓碑化后正文即消失，抑制匹配必须先取快照（协调约束4）。

    value（如「年糕」）用于捕获**跨会话助手 echo**（"你的猫叫年糕"不含
    完整 quote，但含 value）；这是事实自身的结构化取值，不是近义猜测。
    """
    texts: list[str] = []
    seen: set[str] = set()
    queue = [str(f) for f in (fact_ids or ()) if str(f)]
    depth = 0
    while queue and depth < _CHAIN_DEPTH * 4:
        fid = queue.pop(0)
        depth += 1
        if fid in seen:
            continue
        seen.add(fid)
        try:
            fact = service.get_fact(fid)
        except Exception:
            fact = None
        if fact is None:
            continue
        for field in ("summary", "value"):
            text = str(getattr(fact, field, "") or "").strip()
            # 过短 value（单字）不参与匹配，防误伤无关内容
            if text and (field == "summary" or len(_norm(text)) >= 2):
                texts.append(text)
        for linked in (getattr(fact, "supersedes", None),
                       getattr(fact, "superseded_by", None)):
            if linked and str(linked) not in seen:
                queue.append(str(linked))
    return tuple(texts)


def suppress_after_forget(store: MemoryStore, report, fact_texts: Sequence[str],
                          *, owner_id: str, space_id: str = "") -> dict[str, int]:
    """遗忘后的召回抑制（装配侧；同步执行，返回可观测计数）。

    Astra 2026-11-12 最终隐私闭环（不依赖 quote/value 词面匹配）：

    - 短历史：来源索引内该 owner 的**全部登记会话**保守整清（含源会话；
      clear_history 原子 bump 代际 → 在途迟到写回全部失效）；
    - chat 情节：来源索引内登记会话、同 space、带 chat 标签的情节
      **整条删除**——value 缺失/助手改写句式（无完整 quote）也不可能经
      episode_retrieval 复召；这是「清除受管理会话的本地聊天记忆」，
      不是撤回已发送的平台消息（回复文案如实告知）；
    - InnerState scoped 未完话题：登记 owner 会话**整清**（与 history/
      episodes 同口径，不做词面/语义匹配；全局人格/兴趣与他人 scope 不动）；
    - 保留：其他结构化事实、其他 owner/成员空间、群会话、未归属旧数据
      （不在来源索引内的会话一律不碰）。

    ``fact_texts`` 保留为接口/审计入参（整清语义下不再参与匹配）。
    """
    stats = {"histories_cleared": 0, "episodes_deleted": 0,
             "topics_cleared": 0}
    source_cids: set[str] = set()
    for ref in tuple(getattr(report, "affected_evidence", ()) or ()):
        cid = str(getattr(ref, "conversation_id", "") or "")
        if cid:
            source_cids.add(cid)
    owner_cids = set(owner_conversations(store, owner_id)) | source_cids
    if not owner_cids:
        return stats

    from whale_core.memory.history import clear_history

    # 1) owner 登记会话短历史：保守整清（echo 无法逐句判别；短历史是
    #    owner 自有数据，整清安全且与情节整删口径一致）
    for cid in sorted(owner_cids):
        clear_history(store, cid)
        stats["histories_cleared"] += 1

    # 2) chat 情节整条删除：登记会话 + 同 space + chat 标签（不再依赖
    #    quote/value 词面命中——value=None 时助手改写句同样清除）。
    #    未登记会话（群/他人/未归属旧数据）绝不触碰。
    for record in store.list_by_kind(MemoryKind.EPISODE):
        if "chat" not in (record.tags or ()):
            continue
        if is_owner_fact_record(record):
            continue  # 领域记录由服务事务处理，这里不碰
        cid = str((record.content or {}).get("conversation_id") or "")
        if cid not in owner_cids:
            continue
        if space_id and record.space_id != space_id:
            continue  # 只清同一授权空间下的聊天情节
        store.delete(record.key)
        stats["episodes_deleted"] += 1

    # 3) InnerState scoped 未完话题：登记 owner 会话**整清**（Astra
    #    2026-11-12：与 history/episodes 同口径——value=None 时旧话题
    #    「年糕最近的体重」词面匹配不到「我养的猫叫年糕」，下一轮 persona
    #    注入又会提名。宁整清不做语义匹配；全局人格/兴趣与他人 scope 不动）。
    try:
        from whale_core.life.state import InnerStateStore
        for cid in sorted(owner_cids):
            scoped = InnerStateStore(store, scope=cid)
            topics = scoped.load().unfinished_topics
            if topics:
                scoped.reset()
                stats["topics_cleared"] += len(topics)
    except Exception:
        pass  # InnerState 是增强层：抑制失败不影响遗忘主语义

    return stats


# ---------------------------------------------------------------------------
# 装配栈（宿主 composition root 共用；单宿主单 worker）
# ---------------------------------------------------------------------------


class BoundedModelReply:
    """``owner_fact_extraction.ModelReply`` 适配：复用宿主已配置文本脑。

    - 提炼脑 = 已有 intent 脑或主文本脑（不加新密钥/新服务）；
    - ``complete(prompt, *, timeout)``：经 life.bounded_call.BoundedCall 单
      worker 门做**真实超时**；超时/异常/忙碌一律抛 RuntimeError——绝不
      返回空串冒充成功（那会被当成「无可提炼」静默丢事实）；
    - ``busy`` 属性暴露内层门状态：内层超时后底层 LLM 调用可能仍在收尾，
      期间 busy 保持 True——宿主组合 gate 据此禁止 worker 领取新 job
      （Astra 2026-11-12：防快速重试耗尽、丢弃非故障用户证据）。
    """

    def __init__(self, reply: Callable[..., str], *, floor_timeout_s: float = 2.0):
        from whale_core.life.bounded_call import BoundedCall

        self._reply = reply
        self._floor = max(0.5, float(floor_timeout_s))
        self._bounded = BoundedCall(self._call, default_timeout_s=30.0)

    def _call(self, prompt: str) -> str:
        return str(self._reply(prompt) or "")

    @property
    def busy(self) -> bool:
        return self._bounded.busy

    def complete(self, prompt: str, *, timeout: float) -> str:
        out = self._bounded(prompt, default=None,
                            timeout_s=max(self._floor, float(timeout)))
        if out is None:
            raise RuntimeError("owner_fact_model_timeout_or_busy")
        return str(out)


class CompositeCallGate:
    """worker 注入的**组合调用门**（Astra 2026-11-12，host 侧实现，
    不改 Kimi 领域文件）。

    问题：worker 外层 BoundedCall(extractor.extract) 与 ModelReply 内层
    BoundedCall 双 gate——内层超时抛异常会让外层立刻解除 busy，但底层
    LLM 调用仍在内层收尾（内层 busy）。若 worker 此时领取下一 job，内层
    立即 busy 短路 → 秒失败 → 重试快速耗尽，把一批**非故障**用户证据丢掉。

    方案：``busy = extract 门 busy OR 模型内层门 busy``。worker 主循环领取
    前与 drain barrier 都读本属性（owner_fact_worker 已按 gate.busy 判断），
    底层忙时不领取新 job；``__call__`` 仅委托 extract 门（唯一实际调用门）。
    """

    def __init__(self, fn: Callable, *, default_timeout_s: float,
                 extra_busy: Sequence[Any] = ()):
        from whale_core.life.bounded_call import BoundedCall

        self._gate = BoundedCall(fn, default_timeout_s=default_timeout_s)
        self._extra = tuple(extra_busy)

    @property
    def busy(self) -> bool:
        if self._gate.busy:
            return True
        return any(bool(getattr(b, "busy", False)) for b in self._extra)

    def __call__(self, *args, default=None, timeout_s: float = 0.0, **kw):
        return self._gate(*args, default=default, timeout_s=timeout_s, **kw)

    def close(self) -> None:
        self._gate.close()  # extra_busy sources are injected, not owned by this gate


class OwnerFactStack:
    """一次装配的完整栈。``service is None`` = 未装配（宿主必须打印 note）。"""

    def __init__(self, *, service=None, authority=None, extractor=None,
                 worker=None, call_gate=None, enabled: bool = False, note: str = "") -> None:
        self.service = service
        self.authority = authority
        self.extractor = extractor
        self.worker = worker
        self._call_gate = call_gate
        self.enabled = bool(enabled and service is not None)
        self.note = note

    def shutdown(self, *, drain: bool = False, timeout: float = 5.0) -> None:
        try:
            if self.worker is not None:
                quiesce = getattr(self.worker, "quiesce", None)
                if drain and callable(quiesce):
                    try:
                        self.worker.stop(drain=True, timeout=timeout)
                    finally:
                        quiesce(timeout=0)  # fence any result that outlived the graceful wait
                elif callable(quiesce):
                    quiesce(timeout=timeout)
                else:
                    self.worker.stop(drain=drain, timeout=timeout)
        except Exception:
            pass
        finally:
            if self._call_gate is not None:
                self._call_gate.close()


def owner_fact_memory_enabled(env: Mapping[str, Any] | None = None) -> bool:
    """紧急开关：WHALE_OWNER_FACT_MEMORY=0/false/no/off 关闭（缺省开启）。"""
    import os
    source = os.environ if env is None else env
    raw = str(source.get("WHALE_OWNER_FACT_MEMORY") or "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


def build_owner_fact_stack(store: MemoryStore, model_reply, *,
                           env: Mapping[str, Any] | None = None,
                           policy_version: str = "1",
                           start_worker: bool = True,
                           worker_id: str | None = None,
                           model_timeout: float | None = None) -> OwnerFactStack:
    """宿主 composition root 的统一装配入口（约束：不允许静默全 skip）。

    超时/租约（2026-11-12 实测修订，依据 docs/memory-real-model-eval-20261112.md）：
    - ``WHALE_OWNER_FACT_MODEL_TIMEOUT_S`` 覆盖模型超时，缺省 **20s**——真实
      模型 20 case 实测 avg 7.87s / max 13.01s，旧的 10s 缺省会把 13s 的
      正常候选当超时丢弃重试；
    - lease_seconds 自动取 ``max(60, model_timeout + 30)``（必须 > timeout，
      且覆盖慢调用收尾，防 lease 过期被并发重领）；
    - 模型调用经 BoundedCall 单 worker 门：超时后 busy 保持到底层调用真正
      收尾，期间不领取新 job、不积线程；后台提炼永不阻塞主回复。

    返回 OwnerFactStack；``note`` 必须由宿主打印到启动日志——开关关闭、
    领域模块缺失、装配失败都各有明确文案，绝不无声降级。
    """
    import os
    source_env = os.environ if env is None else env
    if not owner_fact_memory_enabled(source_env):
        return OwnerFactStack(note="owner_fact_v1=off（WHALE_OWNER_FACT_MEMORY 显式关闭）")
    if model_timeout is None:
        try:
            model_timeout = float(source_env.get("WHALE_OWNER_FACT_MODEL_TIMEOUT_S")
                                  or 20.0)
        except (TypeError, ValueError):
            model_timeout = 20.0
        if model_timeout <= 0:
            model_timeout = 20.0
    if model_reply is None:
        return OwnerFactStack(
            note="owner_fact_v1=未装配（宿主无可复用文本脑；自动提炼关闭，聊天不受影响）")
    try:
        from whale_core.memory.owner_fact_extraction import OwnerFactExtractor
        from whale_core.memory.owner_facts import OwnerFactService
        from whale_core.memory.owner_fact_worker import OwnerFactWorker
    except Exception as exc:  # 领域模块缺失：显式报告，不静默
        return OwnerFactStack(
            note=f"owner_fact_v1=未装配（领域模块不可用：{type(exc).__name__}）")
    try:
        authority = LiveAuthorityProvider(store, policy_version=policy_version)
        service = OwnerFactService(store, authority)
        extractor = OwnerFactExtractor(model_reply, timeout_s=model_timeout)
        # worker 不自建线程池：CallGate 由宿主注入（Kimi 2026-11-12 修订）。
        # **组合门**（Astra 2026-11-12）：busy = extract 门 OR 模型内层门——
        # 内层超时后底层 LLM 仍在收尾时，worker 不领取新 job（防快速重试
        # 耗尽、误丢非故障证据）；drain barrier 同读组合 busy。
        gate = CompositeCallGate(extractor.extract,
                                 default_timeout_s=model_timeout,
                                 extra_busy=(model_reply,))
        # lease 必须 > model_timeout 且覆盖慢调用收尾（防 lease 过期被并发
        # 重领）：max(60, timeout+30)，与文档口径一致。
        lease_seconds = max(60.0, model_timeout + 30.0)
        worker = OwnerFactWorker.create(
            store, service, extractor, call_gate=gate, worker_id=worker_id,
            lease_seconds=lease_seconds, model_timeout=model_timeout)
        if start_worker:
            worker.start()
        state = "worker已启动" if start_worker else "worker未启动(按需)"
        return OwnerFactStack(service=service, authority=authority,
                              extractor=extractor, worker=worker, call_gate=gate, enabled=True,
                              note=f"owner_fact_v1=on（真实装配：{state}，"
                                   f"提炼脑复用宿主文本模型，model_timeout={model_timeout:g}s）")
    except Exception as exc:
        return OwnerFactStack(
            note=f"owner_fact_v1=装配失败（{type(exc).__name__}；聊天不受影响，"
                 "自动提炼未启用——请检查而非视为已接线）")


# ---------------------------------------------------------------------------
# 显式「忘掉」窄分派（确定性匹配；禁止 top-1 模糊盲删）
# ---------------------------------------------------------------------------


def _forget_reason_text(code: str) -> str:
    mapping = {
        "policy_rejected": "当前授权不可用（空间暂停/解绑），没有删。",
        "revision_mismatch": "这条记忆刚被更新过，请再说一次「忘掉」。",
        "forbidden": "这条记忆不属于当前授权空间，没有删。",
    }
    for prefix, text in mapping.items():
        if str(code or "").startswith(prefix):
            return text
    if str(code or "").startswith("not_found"):
        return "没有找到相关的记忆诶"
    return "没有删成（内部原因已记录），请稍后再试。"


def explicit_forget_owner_fact(store: MemoryStore, service, provider, *,
                               owner_id: str, space_id: str,
                               clue: str, principal_id: str = "",
                               ) -> tuple[str | None, bool]:
    """「忘掉：<线索>」对 owner_fact_v1 的窄分派。

    返回 (回复文本, 是否已处理)。``handled=False`` 时调用方回落既有
    curated 遗忘路径（旧行为不变）。纪律：

    - 只做确定性匹配（规范化相等 / 完整包含）；
    - 唯一命中 → forget（含修订链）+ 抑制；多条命中 → 列出让用户说全，
      **绝不**相似度 top-1 盲删；零命中 → 回落旧路径；
    - ``principal_id`` 为发起会话的真实来源主体（paired:{channel}:{sender}）：
      经 authorize_principal_now 精确核查，其他来源活跃不能替代。
    """
    if service is None or provider is None:
        return None, False
    clue_norm = _norm(clue)
    if not clue_norm:
        return None, False
    try:
        authorize_principal = getattr(provider, "authorize_principal_now", None)
        if callable(authorize_principal) and principal_id:
            auth = authorize_principal(owner_id, space_id, principal_id)
        else:
            auth = provider.authorize_now(owner_id, space_id)
        if auth is None:
            return None, False
        facts = tuple(service.retrieve(auth, str(clue), limit=8) or ())
    except Exception:
        return None, False
    exact = [f for f in facts if _norm(getattr(f, "summary", "")) == clue_norm]
    partial = [f for f in facts
               if clue_norm in _norm(getattr(f, "summary", ""))
               or _norm(getattr(f, "summary", "")) in clue_norm]
    candidates = exact if exact else partial
    # 同 summary 多修订只留最新一条代表（forget 会自动扩展修订链）
    dedup: dict[str, Any] = {}
    for fact in candidates:
        dedup.setdefault(_norm(getattr(fact, "summary", "")), fact)
    candidates = list(dedup.values())
    if not candidates:
        return None, False  # 回落旧 curated 路径
    if len(candidates) > 1:
        lines = "\n".join(
            f"· {str(getattr(f, 'summary', ''))[:40]}" for f in candidates[:5])
        return ("你想忘掉的是哪一条？把内容说全一点我再删：\n" + lines), True
    fact = candidates[0]
    fact_id = str(getattr(fact, "fact_id", ""))
    summary = str(getattr(fact, "summary", ""))
    texts = snapshot_fact_texts(service, [fact_id]) or (summary,)
    try:
        report = service.forget([fact_id], auth,
                                {fact_id: int(getattr(fact, "revision", 0))})
    except Exception:
        return "没有删成（内部原因已记录），请稍后再试。", True
    if not getattr(report, "ok", False):
        return _forget_reason_text(getattr(report, "reason_code", "")), True
    suppress_after_forget(store, report, texts, owner_id=owner_id,
                          space_id=space_id)
    return f"已忘掉：{summary[:50]}", True
