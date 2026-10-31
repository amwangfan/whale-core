"""S5：Whale Studio 关系图的服务端单一事实源 + 临时授权管理 API。

挂在既有 ``WhaleApiServer`` 的 Bearer 鉴权之后（token = 主人管理通道），
本模块不引入第二套鉴权；所有写操作在 ``ctx.lock`` 内串行化并带版本乐观锁，
拒绝原因用稳定机器码（``vault_not_bindable`` / ``scope_ceiling`` /
``version_conflict`` / ``not_found`` / ``invalid_request``）。

纪律（PRD §7.2/§9 与 goal-chain S5 的落地）：

- 画布上的边 = 服务端已批准的绑定；本模块是唯一写入点，前端不做安全决策。
- MEM-HARD-01：``protected_original`` 绑定请求一律 403，且不留下残边（INV-01）。
- 写路径全量审计到 kv ``audit:studio:{ts}:{id}``（只元数据，不存秘密/正文，
  INV-10）；授权与拒绝都留痕。
- Fork 不自动连线：创建副本与绑定会话是两个独立的已授权动作。
- 会话 key 形如 ``channel:kind:conversation_id``（ConversationRef.as_key），
  路由段做 URL decode + 长度/字符/结构校验。
- 写请求强制 ``Content-Type: application/json``（配合 Bearer 挡表单 CSRF）。

依赖装配：``StudioServices.from_store(ctx.store)`` 现场构建，与 S1 resolver
共用同一个 MemoryStore（profiles.py 的装配点）；也可由宿主显式挂到
``ApiContext.studio``（cli/main.py 这么做）。
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import unquote
from uuid import uuid4

from whale_core.life.context_resolver import (
    API_OWNER,
    GROUP_CEILING,
    PRIVATE_OWNER,
)
from whale_core.life.conversation_context import (
    ConversationKind,
    ConversationRef,
    MemorySpaceType,
    Principal,
    ResolvedContext,
    SCOPE_CHAT_BASIC,
    SCOPE_DEVICE_NOTIFY,
    SCOPE_OUTBOX_SUBMIT,
    SCOPE_TASK_READ_SELF,
    SCOPE_TOOL_SHELL_SAFE,
    SCOPE_TOOL_WEB_SEARCH,
    principal_matches_private_conversation,
)
from whale_core.life.rhythm import (
    ACTIVITY_MODES,
    MAX_SEGMENTS_LIMIT,
    ConversationRhythm,
    RhythmStore,
)
from whale_core.life.temporary_auth import (
    GRANTABLE_SCOPES,
    TemporaryAuthError,
    TemporaryAuthService,
)
from whale_core.memory.bindings import BindingError, BindingStore
from whale_core.memory.consent import (
    DEFAULT_CONSENT_TTL_S,
    KNOWN_CONSENT_SCOPES,
    ConsentError,
    MemberConsentStore,
    MemberMemoryDirectory,
)
from whale_core.memory.extraction import contains_sensitive
from whale_core.memory.fork import ForkError, ForkService
from whale_core.memory.governance import _purge_embedding, forget_gone_everywhere
from whale_core.memory.spaces import (
    FORKABLE_TARGET_TYPES,
    WRITABLE_TYPES,
    MemorySpaceError,
    SpaceStore,
)
from whale_core.memory.store import MemoryKind, MemoryStore
from whale_core.ui.persona_cards import (
    ACTIVE_CARD_KEY,
    CARD_KEY_PREFIX,
    MAX_EXTERNAL_PROMPT_CHARS,
    PersonaCard,
    PersonaCardLibrary,
    switch_card,
)

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

#: 会话 key 的字符白名单（URL decode 后校验）；conversation_id 内部允许冒号。
_KEY_RE = re.compile(r"^[A-Za-z0-9_.:\-]+$")
_KEY_MIN, _KEY_MAX = 3, 220

#: 能力白名单：本端点只管理能力 scope；memory.* 授权走 memories 绑定端点。
KNOWN_CAPABILITY_SCOPES = frozenset({
    SCOPE_CHAT_BASIC,
    SCOPE_OUTBOX_SUBMIT,
    SCOPE_TOOL_WEB_SEARCH,
    SCOPE_TOOL_SHELL_SAFE,
    SCOPE_TASK_READ_SELF,
    SCOPE_DEVICE_NOTIFY,
})

_SCOPE_RE = re.compile(r"^[a-z0-9_.:\-]+$")

#: code/grant id 的路径段白名单（oc-/tg- 前缀 + hex）。
_ID_RE = re.compile(r"^[A-Za-z0-9\-]{1,64}$")

#: 口令创建参数上限（服务端硬限制；service 层另有正数校验）。
_MAX_CODE_TTL_S = 30 * 86400
_MAX_GRANT_TTL_S = 86400
_MAX_GRANT_USES = 100
_MAX_RECORD_KEYS = 500

_AUDIT_PREFIX = "audit:studio:"
_MEMVER_PREFIX = "bind:memver:"
_CAPS_PREFIX = "bind:caps:"

#: 内置脱敏（首版占位回调）：手机号 / 邮箱。
_PHONE_RE = re.compile(r"(?<!\d)(1[3-9]\d)\d{4}(\d{4})(?!\d)")
_EMAIL_RE = re.compile(r"\b([A-Za-z0-9._%+\-]{1,64})@([A-Za-z0-9.\-]+\.[A-Za-z]{2,})\b")


def _owner_convs() -> frozenset:
    """owner 大号可直连 vault 的会话集合（owner 决策 2026-11-01）。"""
    import os
    uid = os.environ.get("OWNER_UID", "").strip()
    out = {"api:api:api", "cli:cli:local"}
    if uid:
        out.add(f"qq:qq_private:private:{uid}")
    return frozenset(out)


def builtin_redactor(content: dict) -> dict:
    """内置脱敏回调：递归掩码字符串字段中的手机号与邮箱。

    Fork preview 与 fork 共用同一回调，保证「预览所见即所得」。
    """
    def scrub(value: Any) -> Any:
        if isinstance(value, str):
            value = _PHONE_RE.sub(r"\1****\2", value)
            return _EMAIL_RE.sub(lambda m: m.group(1)[:2] + "***@" + m.group(2), value)
        if isinstance(value, dict):
            return {k: scrub(v) for k, v in value.items()}
        if isinstance(value, list):
            return [scrub(v) for v in value]
        return value
    return scrub(dict(content))


def _parse_redact_mode(raw: Any) -> tuple[str, Optional[Any]]:
    """解析 redact 选项：'builtin' / 'none' / true / false（默认 builtin）。"""
    if raw is None or raw is True or raw == "builtin":
        return "builtin", builtin_redactor
    if raw is False or raw == "none":
        return "none", None
    raise StudioError(422, "invalid_request",
                      "redact must be 'builtin' | 'none' | true | false")


# ---------------------------------------------------------------------------
# 服务装配
# ---------------------------------------------------------------------------


@dataclass
class StudioServices:
    """S5 依赖集合。全部是 MemoryStore 上的薄封装，现场构建即共享同一 store。"""

    spaces: SpaceStore
    bindings: BindingStore
    forks: ForkService
    auth: TemporaryAuthService
    personas: PersonaCardLibrary
    consents: MemberConsentStore
    directory: MemberMemoryDirectory

    @classmethod
    def from_store(cls, store: MemoryStore) -> "StudioServices":
        spaces = SpaceStore(store)
        bindings = BindingStore(store, spaces, owner_conversations=_owner_convs())
        consents = MemberConsentStore(store)
        return cls(
            spaces=spaces,
            bindings=bindings,
            forks=ForkService(store, spaces),
            auth=TemporaryAuthService(store),
            personas=PersonaCardLibrary(),
            consents=consents,
            directory=MemberMemoryDirectory(bindings, spaces, consents),
        )


def services_for(ctx) -> StudioServices:
    """取 ctx.studio；宿主未显式装配时从 ctx.store 现场构建（惰性、幂等）。"""
    svc = getattr(ctx, "studio", None)
    if isinstance(svc, StudioServices):
        return svc
    with ctx.lock:
        svc = getattr(ctx, "studio", None)
        if not isinstance(svc, StudioServices):
            svc = StudioServices.from_store(ctx.store)
            try:
                ctx.studio = svc
            except AttributeError:
                pass  # 宿主自定义 ctx：不缓存也行，下次重建即可
    return svc


def management_context() -> ResolvedContext:
    """管理通道自身的 owner ResolvedContext（供 graph 解释信息使用）。

    Bearer token 即主人管理通道；本对象仅用于解释/展示，不作为授权证据外传。
    """
    return ResolvedContext(
        conversation=ConversationRef("api", ConversationKind.API, "studio"),
        principal=Principal("paired:api:studio", paired=True, is_owner=True,
                            evidence="api:bearer-token"),
        persona_id=None,
        capability_scopes=API_OWNER,
    )


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


class StudioError(Exception):
    """handler 流程内可预期的拒绝；携带 HTTP 状态与稳定机器码。"""

    def __init__(self, status: int, code: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.extra = extra


def _ok(result: Any) -> bytes:
    return json.dumps({"result": result}, ensure_ascii=False).encode("utf-8")


def _err(code: str, message: str, extra: Optional[dict] = None) -> bytes:
    err: dict[str, Any] = {"code": code, "message": message}
    if extra:
        err.update(extra)
    return json.dumps({"error": err}, ensure_ascii=False).encode("utf-8")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _audit(ctx, action: str, *, outcome: str = "ok", reason: str = "",
           **meta: Any) -> None:
    """写一条 Studio 审计事件（只元数据：调用方不得传秘密/正文，INV-10）。"""
    safe: dict[str, Any] = {}
    for k, v in meta.items():
        if isinstance(v, (str, int, float, bool)) or v is None:
            safe[k] = v
        elif isinstance(v, (list, tuple)):
            safe[k] = [str(x)[:80] for x in v][:20]
        else:
            safe[k] = str(v)[:120]
    event = {
        "at": _now_iso(),
        "action": action,
        "actor": "owner:api-token",
        "outcome": outcome,
        "reason": reason,
        **safe,
    }
    ctx.store.put(f"{_AUDIT_PREFIX}{time.time():.6f}:{uuid4().hex[:8]}", event)


def _parse_conversation_key(raw: str) -> ConversationRef:
    """URL decode + 校验 ``channel:kind:conversation_id`` 结构。"""
    key = unquote(raw)
    if not (_KEY_MIN <= len(key) <= _KEY_MAX) or not _KEY_RE.match(key):
        raise StudioError(422, "invalid_request",
                          "conversation key 必须是 3..220 字符的 [A-Za-z0-9_.:-]")
    segments = key.split(":", 2)
    if len(segments) != 3:
        raise StudioError(422, "invalid_request",
                          "conversation key 必须是 channel:kind:conversation_id")
    channel, kind_s, cid = segments
    if not channel or not cid:
        raise StudioError(422, "invalid_request", "conversation key 结构不完整")
    try:
        kind = ConversationKind(kind_s)
    except ValueError:
        raise StudioError(422, "invalid_request",
                          f"未知会话类型 {kind_s!r}")
    return ConversationRef(channel, kind, cid)


def _require_json_body(handler) -> dict:
    """写请求强制 JSON Content-Type（配合 Bearer 挡住表单 CSRF）。"""
    ctype = (handler.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    if ctype != "application/json":
        raise StudioError(415, "invalid_request",
                          "Content-Type: application/json required")
    body = handler._body_json()
    if body is None:
        raise StudioError(400, "invalid_request", "JSON object body required")
    return body


def _ceiling_for(kind: ConversationKind) -> tuple[str, ...]:
    """会话类型的能力上限（与 context_resolver 的权限矩阵一致）。"""
    if kind is ConversationKind.QQ_GROUP:
        return GROUP_CEILING
    if kind is ConversationKind.API:
        return API_OWNER
    if kind in (ConversationKind.QQ_PRIVATE, ConversationKind.WEB,
                ConversationKind.VOICE, ConversationKind.PHONE):
        return PRIVATE_OWNER
    return (SCOPE_CHAT_BASIC,)


def _check_expected_version(body: dict, current: int) -> None:
    """乐观锁：携带 expected_version 且与当前不符 → 409 回显当前版本。"""
    if "expected_version" not in body or body.get("expected_version") is None:
        return
    try:
        expected = int(body["expected_version"])
    except (TypeError, ValueError):
        raise StudioError(422, "invalid_request", "expected_version 必须是整数")
    if expected != current:
        raise StudioError(409, "version_conflict",
                          "绑定已被并发修改；请读取最新版本后重试",
                          current_version=current)


def _binding_versions(ctx, ckey: str) -> tuple[int, int, int]:
    """(persona_version, memory_version, capabilities_version)。"""
    pdata = ctx.store.get(f"bind:persona:{ckey}")
    persona_version = int(pdata.get("version") or 0) if isinstance(pdata, dict) else 0
    memory_version = int(ctx.store.get(f"{_MEMVER_PREFIX}{ckey}") or 0)
    caps = ctx.store.get(f"{_CAPS_PREFIX}{ckey}")
    caps_version = int(caps.get("version") or 0) if isinstance(caps, dict) else 0
    return persona_version, memory_version, caps_version


# ---------------------------------------------------------------------------
# 会话 / 绑定视图
# ---------------------------------------------------------------------------


def _memory_binding_view(svc: StudioServices, row: dict) -> dict:
    """单条记忆绑定的脱敏视图（vault 永不带名称/计数等细节）。"""
    sid = str(row.get("space_id") or "")
    ref = svc.spaces.ref(sid)
    base = {
        "space_id": sid,
        "can_read": bool(row.get("can_read")),
        "can_write": bool(row.get("can_write")),
        "granted_by": str(row.get("granted_by") or ""),
        "created_at": str(row.get("created_at") or ""),
    }
    if ref is None:
        base["missing"] = True  # 空间已注销/不存在：展示残绑定以便清理
        return base
    if ref.type is MemorySpaceType.PROTECTED_ORIGINAL:
        # 绑定层本已拒绝 vault；这里是污染数据的纵深防御：只暴露存在与类型。
        return {"space_id": sid, "space_type": "protected_original",
                "restricted": True}
    base.update({
        "space_type": ref.type.value,
        "name": str((svc.spaces.get(sid) or {}).get("name") or ""),
        "space_version": ref.version,
    })
    return base


def _conversation_keys(ctx) -> tuple[str, ...]:
    """可见会话 = 有任何绑定/节奏的 key ∪ 已知渠道的历史会话。"""
    keys: set[str] = set()
    for store_key in ctx.store.keys():
        for prefix in ("bind:persona:", "bind:mem:", _CAPS_PREFIX,
                       "bind:rhythm:"):
            if store_key.startswith(prefix):
                keys.add(store_key[len(prefix):])
    for ref in _known_conversations(ctx):
        keys.add(ref.as_key())
    return tuple(sorted(keys))


def _known_conversations(ctx) -> tuple[ConversationRef, ...]:
    """从 chat:history:* 推断已知渠道会话（只认带可信前缀的 id）。"""
    out: list[ConversationRef] = []
    for key in ctx.store.keys():
        if not key.startswith("chat:history:"):
            continue
        cid = key[len("chat:history:"):]
        if cid.startswith("group:"):
            out.append(ConversationRef("qq", ConversationKind.QQ_GROUP, cid))
        elif cid.startswith("private:"):
            out.append(ConversationRef("qq", ConversationKind.QQ_PRIVATE, cid))
        elif cid in ("api", "console"):
            out.append(ConversationRef("api", ConversationKind.API, cid))
        elif cid.startswith("voice:"):
            out.append(ConversationRef("voice", ConversationKind.VOICE, cid))
        # 无法归因渠道的会话不做猜测（宁可少列，不错标渠道）
    return tuple(out)


def _conversation_entry(ctx, svc: StudioServices, key: str) -> dict:
    try:
        ref = _parse_conversation_key(key)
    except StudioError:
        # kv 里的异形 key：不参与画布（不伪造结构）
        return {"key": key, "malformed": True}
    pdata = ctx.store.get(f"bind:persona:{key}")
    persona_id = str(pdata.get("persona_id")) if isinstance(pdata, dict) \
        and pdata.get("persona_id") else None
    rows = ctx.store.get(f"bind:mem:{key}")
    rows = [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []
    caps = ctx.store.get(f"{_CAPS_PREFIX}{key}")
    scopes = sorted(str(s) for s in caps.get("scopes") or ()) \
        if isinstance(caps, dict) else []
    persona_version, memory_version, caps_version = _binding_versions(ctx, key)
    rhythm = RhythmStore(ctx.store).get(key)
    return {
        "key": key,
        "channel": ref.channel,
        "kind": ref.kind.value,
        "conversation_id": ref.conversation_id,
        "persona_id": persona_id,
        "persona_version": persona_version,
        "memory_spaces": [_memory_binding_view(svc, r) for r in rows],
        "memory_version": memory_version,
        "capabilities": scopes,
        "capabilities_version": caps_version,
        "ceiling": list(_ceiling_for(ref.kind)),
        # G4：会话节奏（只影响回复节奏，与能力/记忆授权无关）。
        "rhythm": {
            "activity": rhythm.activity,
            "max_segments": rhythm.max_segments,
            "dnd": rhythm.dnd,
        },
        "rhythm_version": rhythm.version,
    }


def _list_conversations(handler, ctx, svc: StudioServices) -> None:
    keys = _conversation_keys(ctx)
    handler._send(200, _ok({
        "conversations": [_conversation_entry(ctx, svc, k) for k in keys],
    }))


# ---------------------------------------------------------------------------
# 角色卡
# ---------------------------------------------------------------------------


def _persona_view(card: PersonaCard, *, builtin: bool) -> dict:
    """极简字段视图；**绝不回显** external_prompt / style_rules 等内部文本。"""
    return {
        "id": card.card_id,
        "title": card.title,
        "name": card.name,
        "summary": card.summary or card.description,
        "temperament": card.temperament,
        "relationship_style": card.relationship_style,
        "voice_hint": card.voice_hint or card.timbre_hint,
        "category": card.category,
        "author": card.author,
        "license": card.license,
        "source_url": card.source_url,
        "builtin": builtin,
        "has_external_prompt": bool(card.external_prompt),
    }


def _find_persona(ctx, svc: StudioServices, persona_id: str) -> Optional[PersonaCard]:
    card = svc.personas.get(persona_id)
    if card is not None:
        return card
    raw = ctx.store.get(f"{CARD_KEY_PREFIX}{persona_id}")
    if not isinstance(raw, dict):
        return None
    try:
        return PersonaCard.from_dict(raw)
    except (ValueError, TypeError):
        return None


def _list_personas(handler, ctx, svc: StudioServices) -> None:
    out = [_persona_view(svc.personas.require(cid), builtin=True)
           for cid in svc.personas.ids()]
    builtin_ids = set(svc.personas.ids())
    for key in sorted(ctx.store.keys()):
        if not key.startswith(CARD_KEY_PREFIX):
            continue
        cid = key[len(CARD_KEY_PREFIX):]
        if cid in builtin_ids:
            continue
        card = _find_persona(ctx, svc, cid)
        if card is not None:
            out.append(_persona_view(card, builtin=False))
    handler._send(200, _ok({"personas": out}))


# ---- 人物卡管理（owner 决策 2026-11-01：工作台可直接添加/修改人物卡）----

#: 自定义卡 id 白名单（小写 slug；内置卡 id 不可占用）。
_CARD_ID_RE = re.compile(r"^[a-z0-9][a-z0-9\-]{0,63}$")

#: 可编辑字符串字段（严格字段表，未知字段 422；与 PersonaCard 字段同名）。
_PERSONA_STR_FIELDS = frozenset({
    "title", "name", "summary", "description", "temperament",
    "relationship_style", "voice_hint", "category", "tone",
    "address_user_as", "timbre_hint", "artwork_ref",
    "author", "source_url", "license", "external_prompt",
})
_PERSONA_LIST_FIELDS = frozenset({"style_rules", "extra_taboos"})
_PERSONA_EDITABLE = _PERSONA_STR_FIELDS | _PERSONA_LIST_FIELDS | {"speaking_rate"}

#: 单字段长度硬上限（external_prompt 另由 PersonaCard 截断到
#: MAX_EXTERNAL_PROMPT_CHARS）；列表条数/单条长度上限。
_MAX_PERSONA_FIELD_CHARS = MAX_EXTERNAL_PROMPT_CHARS
_MAX_PERSONA_LIST_ITEMS = 50
_MAX_PERSONA_LIST_ITEM_CHARS = 300


def _persona_detail_view(card: PersonaCard, *, builtin: bool) -> dict:
    """编辑用完整视图。管理面仅 owner Bearer 可达，故含 external_prompt
    （列表视图 _persona_view 仍不回显内部文本）。"""
    view = _persona_view(card, builtin=builtin)
    view.update({
        "description": card.description,
        "tone": card.tone,
        "address_user_as": card.address_user_as,
        "style_rules": list(card.style_rules),
        "extra_taboos": list(card.extra_taboos),
        "timbre_hint": card.timbre_hint,
        "speaking_rate": card.speaking_rate,
        "artwork_ref": card.artwork_ref,
        "external_prompt": card.external_prompt,
        "channels": list(card.channels),
    })
    return view


def _get_persona_detail(handler, ctx, svc: StudioServices, card_id: str) -> None:
    if not _ID_RE.match(card_id):
        raise StudioError(404, "not_found", "unknown persona")
    card = _find_persona(ctx, svc, card_id)
    if card is None:
        raise StudioError(404, "not_found", f"人物卡不存在: {card_id!r}")
    handler._send(200, _ok({
        "persona": _persona_detail_view(
            card, builtin=svc.personas.get(card_id) is not None),
    }))


def _parse_persona_fields(body: dict) -> dict:
    """严格字段表 + 类型/长度校验；返回 PersonaCard 可接受的字段子集。"""
    _reject_unknown_fields(body, _PERSONA_EDITABLE | {"card_id"})
    fields: dict = {}
    for key in sorted(_PERSONA_STR_FIELDS):
        if key not in body:
            continue
        value = body[key]
        if not isinstance(value, str):
            raise StudioError(422, "invalid_request", f"{key} 必须是字符串")
        fields[key] = value[:_MAX_PERSONA_FIELD_CHARS]
    for key in sorted(_PERSONA_LIST_FIELDS):
        if key not in body:
            continue
        value = body[key]
        if not isinstance(value, list) \
                or not all(isinstance(v, str) for v in value):
            raise StudioError(422, "invalid_request", f"{key} 必须是字符串数组")
        cleaned = tuple(
            v.strip()[:_MAX_PERSONA_LIST_ITEM_CHARS] for v in value if v.strip())
        if len(cleaned) > _MAX_PERSONA_LIST_ITEMS:
            raise StudioError(422, "invalid_request",
                              f"{key} 最多 {_MAX_PERSONA_LIST_ITEMS} 条")
        fields[key] = cleaned
    if "speaking_rate" in body:
        rate = body["speaking_rate"]
        if isinstance(rate, bool) or not isinstance(rate, (int, float)):
            raise StudioError(422, "invalid_request", "speaking_rate 必须是数字")
        if not 0.5 <= float(rate) <= 2.0:
            raise StudioError(422, "invalid_request",
                              "speaking_rate 须在 [0.5, 2.0] 内")
        fields["speaking_rate"] = float(rate)
    return fields


def _create_persona(handler, ctx, svc: StudioServices) -> None:
    body = _require_json_body(handler)
    fields = _parse_persona_fields(body)
    title = (fields.get("title") or "").strip()
    if not title:
        raise StudioError(422, "invalid_request", "title 不能为空")
    fields["title"] = title
    cid = str(body.get("card_id") or "").strip().lower()
    if cid and not _CARD_ID_RE.match(cid):
        raise StudioError(422, "invalid_request",
                          "card_id 只能含小写字母/数字/连字符（≤64 字符）")
    if not cid:
        cid = f"custom-{uuid4().hex[:8]}"
    if svc.personas.get(cid) is not None:
        raise StudioError(409, "builtin_immutable",
                          f"card_id 与内置卡冲突: {cid!r}")
    with ctx.lock:
        if ctx.store.get(f"{CARD_KEY_PREFIX}{cid}"):
            raise StudioError(409, "duplicate_card", f"card_id 已存在: {cid!r}")
        card = PersonaCard(card_id=cid, **fields)
        ctx.store.put(f"{CARD_KEY_PREFIX}{cid}", card.as_dict())
        _audit(ctx, "persona.create", card_id=cid, title=title)
    handler._send(201, _ok({"persona": _persona_detail_view(card, builtin=False)}))


def _update_persona(handler, ctx, svc: StudioServices, card_id: str) -> None:
    if not _ID_RE.match(card_id):
        raise StudioError(404, "not_found", "unknown persona")
    body = _require_json_body(handler)
    fields = _parse_persona_fields(body)
    if not fields:
        raise StudioError(422, "invalid_request", "没有需要修改的字段")
    if "title" in fields and not str(fields["title"]).strip():
        raise StudioError(422, "invalid_request", "title 不能为空")
    if svc.personas.get(card_id) is not None:
        raise StudioError(403, "builtin_immutable",
                          "内置卡不可修改；请以其为底稿新建一张自定义卡")
    with ctx.lock:
        raw = ctx.store.get(f"{CARD_KEY_PREFIX}{card_id}")
        if not isinstance(raw, dict):
            raise StudioError(404, "not_found", f"人物卡不存在: {card_id!r}")
        data = dict(raw)
        data.update(fields)
        try:
            updated = PersonaCard.from_dict(data)
        except (ValueError, TypeError) as exc:
            raise StudioError(422, "invalid_request", f"字段值非法: {exc}")
        ctx.store.put(f"{CARD_KEY_PREFIX}{card_id}", updated.as_dict())
        _audit(ctx, "persona.update", card_id=card_id, fields=sorted(fields))
        # 若改的是当前活跃卡，立即重渲 PersonaContinuity 快照，
        # 让修改对下游（companion/voice 渲染）当轮生效。
        active = str((ctx.store.get(ACTIVE_CARD_KEY) or {}).get("card_id") or "")
        if active == card_id:
            switch_card(ctx.store, card_id, library=svc.personas)
    handler._send(200, _ok({
        "persona": _persona_detail_view(updated, builtin=False)}))


def _put_persona(handler, ctx, svc: StudioServices, raw_key: str) -> None:
    ref = _parse_conversation_key(raw_key)
    body = _require_json_body(handler)
    persona_raw = body.get("persona_id")
    if persona_raw is not None and not isinstance(persona_raw, str):
        raise StudioError(422, "invalid_request", "persona_id 必须是字符串或 null")
    persona_id = (persona_raw or "").strip()
    if persona_id and _find_persona(ctx, svc, persona_id) is None:
        raise StudioError(422, "invalid_request",
                          f"persona_id 不存在: {persona_id[:80]!r}")
    key = ref.as_key()
    store_key = f"bind:persona:{key}"
    with ctx.lock:
        old = ctx.store.get(store_key)
        old = old if isinstance(old, dict) else None
        current_version = int(old.get("version") or 0) if old else 0
        current_pid = str(old.get("persona_id") or "") if old else ""
        if persona_id == current_pid:
            # 幂等重放优先：同值重试（带着自己首次的版本号）是 200 no-op，
            # 不是 409——重试不算并发冲突（N13）。
            handler._send(200, _ok({
                "key": key, "persona_id": current_pid or None,
                "version": current_version, "changed": False,
            }))
            return
        try:
            _check_expected_version(body, current_version)
        except StudioError as exc:
            _audit(ctx, "persona.bind", outcome="denied", reason=exc.code,
                   conversation_key=key)
            raise
        if persona_id:
            data = svc.bindings.bind_persona(key, persona_id,
                                             updated_by="studio-api")
            version = int(data.get("version") or current_version + 1)
        else:
            ctx.store.delete(store_key)
            version = 0
        _audit(ctx, "persona.unbind" if not persona_id else "persona.bind",
               conversation_key=key, from_persona=current_pid,
               to_persona=persona_id, version=version)
    handler._send(200, _ok({
        "key": key, "persona_id": persona_id or None,
        "version": version, "changed": True,
    }))


# ---------------------------------------------------------------------------
# 记忆空间 / Fork
# ---------------------------------------------------------------------------


def _record_count(store: MemoryStore, space_id: str) -> int:
    return sum(len(store.list_by_kind(kind, space_ids=(space_id,)))
               for kind in MemoryKind)


def _space_view(ctx, svc: StudioServices, space_id: str) -> Optional[dict]:
    meta = svc.spaces.get(space_id)
    if meta is None:
        return None
    stype = str(meta.get("type") or "")
    if stype == MemorySpaceType.PROTECTED_ORIGINAL.value:
        # INV-08：原始库只暴露「存在 + 名称」，不给记录数/摘要/来源。
        return {
            "space_id": space_id,
            "space_type": stype,
            "name": str(meta.get("name") or ""),
            "version": int(meta.get("version") or 1),
            "restricted": True,
            "bindable": False,
        }
    ref = svc.spaces.ref(space_id)
    return {
        "space_id": space_id,
        "space_type": stype,
        "name": str(meta.get("name") or ""),
        "version": int(meta.get("version") or 1),
        "owner": str(meta.get("owner") or ""),
        "created_at": str(meta.get("created_at") or ""),
        "record_count": _record_count(ctx.store, space_id),
        "bindable": True,
        "writable": bool(ref and ref.type in WRITABLE_TYPES),
    }


def _list_memory_spaces(handler, ctx, svc: StudioServices) -> None:
    spaces = []
    for ref in svc.spaces.list_spaces(include_protected=True):
        view = _space_view(ctx, svc, ref.space_id)
        if view is not None:
            spaces.append(view)
    handler._send(200, _ok({"memory_spaces": spaces}))


def _parse_record_keys(body: dict) -> tuple[str, ...]:
    raw = body.get("record_keys")
    if not isinstance(raw, list) or not raw or len(raw) > _MAX_RECORD_KEYS:
        raise StudioError(422, "invalid_request",
                          f"record_keys 必须是 1..{_MAX_RECORD_KEYS} 个字符串")
    keys: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not item.strip() or len(item) > 300:
            raise StudioError(422, "invalid_request",
                              "record_keys 元素必须是 1..300 字符的字符串")
        keys.append(item.strip())
    return tuple(dict.fromkeys(keys))


def _parse_target_type(body: dict) -> MemorySpaceType:
    raw = body.get("target_type")
    try:
        ttype = MemorySpaceType(str(raw))
    except ValueError:
        raise StudioError(422, "invalid_request",
                          "target_type 必须是 shareable_fork/group_space/"
                          "owner_working_copy")
    if ttype not in FORKABLE_TARGET_TYPES:
        # Fork 永不产生新的 protected_original（MEM-HARD-01 纵深防御）
        raise StudioError(422, "invalid_request",
                          f"不允许 Fork 出 {ttype.value} 类型空间")
    return ttype


def _vault_records(handler, ctx, svc: StudioServices) -> None:
    """GET /studio/vault/records?space_id=…：可信管理通道的原始库记录浏览。

    仅接受 protected_original 空间；摘要经内置脱敏；上限 500 条防大包。
    这是 Fork 向导的选记录数据源（PRD §5.2「在可信管理界面选记录」）。
    注意：list_by_kind 存储层硬排除 vault（MEM-HARD-01），管理读取只能
    走 keys()+get_record 直读——这是刻意的管道分离，不是绕过。
    """
    space_id = str(handler.query.get("space_id", [""])[0] or "").strip() \
        or "vault-default"
    meta = svc.spaces.get(space_id)
    if meta is None or meta.get("type") != "protected_original":
        raise StudioError(404, "not_found", "受保护原始库不存在")
    items: list[dict] = []
    truncated = False
    for key in sorted(ctx.store.keys()):
        rec = ctx.store.get_record(key)
        if rec is None or rec.space_id != space_id:
            continue
        redacted = builtin_redactor(dict(rec.content))
        items.append({
            "key": rec.key,
            "kind": rec.kind.value,
            "tags": list(rec.tags),
            "summary": str(redacted.get("summary", ""))[:300],
            "created_at": rec.created_at.isoformat(),
        })
        if len(items) >= 500:
            truncated = True
            break
    else:
        truncated = False
    _audit(ctx, "vault.records", space_id=space_id, count=len(items))
    handler._send(200, _ok({"space_id": space_id, "count": len(items),
                            "truncated": truncated, "items": items}))


def _fork_preview(handler, ctx, svc: StudioServices) -> None:
    body = _require_json_body(handler)
    source = str(body.get("source_space") or "").strip()
    if not source:
        raise StudioError(422, "invalid_request", "source_space 必填")
    if svc.spaces.get(source) is None:
        raise StudioError(404, "not_found", "源空间不存在或已注销")
    keys = _parse_record_keys(body)
    mode, redactor = _parse_redact_mode(body.get("redact"))
    try:
        items = svc.forks.preview(source, keys, redact=redactor)
    except ForkError as exc:
        raise StudioError(422, "invalid_request", str(exc)[:200])
    _audit(ctx, "fork.preview", source_space=source,
           record_count=len(items), redact=mode)
    handler._send(200, _ok({
        "source_space": source,
        "redact": mode,
        "count": len(items),
        "items": list(items),
    }))


def _fork_create(handler, ctx, svc: StudioServices) -> None:
    body = _require_json_body(handler)
    if body.get("confirmed") is not True:
        _audit(ctx, "fork.create", outcome="denied", reason="unconfirmed")
        raise StudioError(422, "invalid_request",
                          "Fork 需要显式确认（confirmed=true）")
    source = str(body.get("source_space") or "").strip()
    if not source:
        raise StudioError(422, "invalid_request", "source_space 必填")
    if svc.spaces.get(source) is None:
        raise StudioError(404, "not_found", "源空间不存在或已注销")
    keys = _parse_record_keys(body)
    ttype = _parse_target_type(body)
    target_name = str(body.get("target_name") or "")[:120]
    mode, redactor = _parse_redact_mode(body.get("redact"))
    with ctx.lock:
        try:
            new_space = svc.forks.fork(
                source, keys, target_type=ttype, target_name=target_name,
                created_by="studio-api", redact=redactor, confirmed=True)
        except ForkError as exc:
            _audit(ctx, "fork.create", outcome="denied",
                   reason="invalid_request", source_space=source)
            raise StudioError(422, "invalid_request", str(exc)[:200])
        _audit(ctx, "fork.create", source_space=source,
               target_space=new_space, target_type=ttype.value,
               record_count=len(keys), redact=mode)
    view = _space_view(ctx, svc, new_space) or {"space_id": new_space}
    view["auto_bound"] = False  # Fork 不自动连线：绑定是独立授权动作
    handler._send(200, _ok({"fork": view}))


# ---------------------------------------------------------------------------
# 记忆绑定 / 能力配置
# ---------------------------------------------------------------------------


def _put_memories(handler, ctx, svc: StudioServices, raw_key: str) -> None:
    ref = _parse_conversation_key(raw_key)
    body = _require_json_body(handler)
    space_id = str(body.get("space_id") or "").strip()
    if not space_id:
        raise StudioError(422, "invalid_request", "space_id 必填")
    remove = body.get("remove") is True
    can_read = body.get("can_read") is not False  # 默认读
    can_write = body.get("can_write") is True     # 默认不写
    space_ref = svc.spaces.ref(space_id)
    if space_ref is None and not remove:
        raise StudioError(404, "not_found", "空间不存在或已注销")
    # remove=True 且空间已注销：放行——这是清理残绑定的管理路径。
    # MEM-HARD-01（owner 决策 2026-11-01 修订）：vault 仅可绑主人本人大号
    # 私聊/控制台会话（宿主配置的 owner_conversations）；其余一律 403。
    if space_ref is not None and space_ref.type is MemorySpaceType.PROTECTED_ORIGINAL:
        conv_key = ref.as_key()
        if conv_key not in svc.bindings.owner_conversations:
            _audit(ctx, "memory.unbind" if remove else "memory.bind",
                   outcome="denied", reason="vault_not_bindable",
                   conversation_key=conv_key, space_id=space_id)
            raise StudioError(403, "vault_not_bindable",
                              "个人原始库只能连接主人本人的私聊会话；"
                              "其余会话请先创建受控副本（Fork）")
    key = ref.as_key()
    ver_key = f"{_MEMVER_PREFIX}{key}"
    with ctx.lock:
        rows = ctx.store.get(f"bind:mem:{key}")
        rows = [r for r in rows if isinstance(r, dict)] \
            if isinstance(rows, list) else []
        current_row = next((r for r in rows if r.get("space_id") == space_id), None)
        current_version = int(ctx.store.get(ver_key) or 0)
        # 幂等重放优先于版本检查：同值重复 / 重复解绑都是 200 no-op（N13）。
        if remove and current_row is None:
            handler._send(200, _ok({"key": key, "space_id": space_id,
                                    "version": current_version,
                                    "changed": False, "removed": True}))
            return
        if (not remove and current_row is not None
                and bool(current_row.get("can_read")) == can_read
                and bool(current_row.get("can_write")) == can_write):
            handler._send(200, _ok({"key": key, "space_id": space_id,
                                    "version": current_version,
                                    "changed": False}))
            return
        try:
            _check_expected_version(body, current_version)
        except StudioError as exc:
            _audit(ctx, "memory.bind", outcome="denied", reason=exc.code,
                   conversation_key=key, space_id=space_id)
            raise
        try:
            if remove:
                svc.bindings.unbind_memory(key, space_id)
                row = None
            else:
                row = svc.bindings.bind_memory(
                    key, space_id, can_read=can_read, can_write=can_write,
                    granted_by="studio-api")
        except BindingError as exc:
            _audit(ctx, "memory.bind", outcome="denied",
                   reason="invalid_request", conversation_key=key,
                   space_id=space_id)
            raise StudioError(422, "invalid_request", str(exc)[:200])
        version = current_version + 1
        ctx.store.put(ver_key, version)
        _audit(ctx, "memory.unbind" if remove else "memory.bind",
               conversation_key=key, space_id=space_id,
               can_read=bool(can_read) if not remove else None,
               can_write=bool(can_write) if not remove else None,
               version=version)
    handler._send(200, _ok({
        "key": key, "space_id": space_id, "version": version, "changed": True,
        "removed": remove,
        "binding": _memory_binding_view(svc, row) if row else None,
    }))


def _put_capabilities(handler, ctx, svc: StudioServices, raw_key: str) -> None:
    ref = _parse_conversation_key(raw_key)
    body = _require_json_body(handler)
    raw_scopes = body.get("capabilities")
    if not isinstance(raw_scopes, list) or len(raw_scopes) > 32:
        raise StudioError(422, "invalid_request",
                          "capabilities 必须是最多 32 项的字符串数组")
    scopes: list[str] = []
    for item in raw_scopes:
        if not isinstance(item, str) or not _SCOPE_RE.match(item) \
                or len(item) > 80:
            raise StudioError(422, "invalid_request", f"非法 scope: {item!r}")
        if item not in KNOWN_CAPABILITY_SCOPES:
            raise StudioError(422, "invalid_request",
                              f"未知能力 scope {item!r}；memory.* 授权请使用 "
                              "conversations/{{key}}/memories 绑定端点")
        if item not in scopes:
            scopes.append(item)
    ceiling = _ceiling_for(ref.kind)
    over = [s for s in scopes if s not in ceiling]
    key = ref.as_key()
    if over:
        _audit(ctx, "capabilities.set", outcome="denied", reason="scope_ceiling",
               conversation_key=key, scopes=over)
        raise StudioError(403, "scope_ceiling",
                          "配置超过该会话类型的能力上限（群内不提权，INV-03）",
                          ceiling=list(ceiling), rejected=over)
    caps_key = f"{_CAPS_PREFIX}{key}"
    with ctx.lock:
        old = ctx.store.get(caps_key)
        old = old if isinstance(old, dict) else None
        current_version = int(old.get("version") or 0) if old else 0
        current_scopes = sorted(str(s) for s in old.get("scopes") or ()) if old else []
        # 幂等重放优先于版本检查（N13）
        if sorted(scopes) == current_scopes:
            handler._send(200, _ok({"key": key, "capabilities": current_scopes,
                                    "version": current_version,
                                    "changed": False}))
            return
        try:
            _check_expected_version(body, current_version)
        except StudioError as exc:
            _audit(ctx, "capabilities.set", outcome="denied", reason=exc.code,
                   conversation_key=key)
            raise
        version = current_version + 1
        ctx.store.put(caps_key, {
            "scopes": sorted(scopes), "version": version,
            "updated_by": "studio-api", "updated_at": _now_iso(),
        })
        _audit(ctx, "capabilities.set", conversation_key=key,
               scopes=sorted(scopes), version=version)
    handler._send(200, _ok({"key": key, "capabilities": sorted(scopes),
                            "version": version, "changed": True,
                            "ceiling": list(ceiling)}))


# ---------------------------------------------------------------------------
# G4：会话节奏（ConversationRhythm）
#
# 纪律：节奏卡**只影响回复节奏**（活跃模式/分段上限/免打扰），不得携带
# 任何工具权限或记忆授权语义——严格字段表直接 422 掉未知字段（含
# capabilities/scopes/memory 之类越界字段）；写路径 ctx.lock + 版本乐观锁 +
# 全量审计（只元数据）。
# ---------------------------------------------------------------------------

_RHYTHM_FIELDS = frozenset({"activity", "max_segments", "dnd",
                            "expected_version"})


def _rhythm_view(key: str, rhythm: ConversationRhythm, *, configured: bool) -> dict:
    return {
        "key": key,
        "rhythm": {
            "activity": rhythm.activity,
            "max_segments": rhythm.max_segments,
            "dnd": rhythm.dnd,
        },
        "version": rhythm.version,
        "configured": configured,
    }


def _get_rhythm(handler, ctx, raw_key: str) -> None:
    ref = _parse_conversation_key(raw_key)
    key = ref.as_key()
    store = RhythmStore(ctx.store)
    handler._send(200, _ok(_rhythm_view(
        key, store.get(key), configured=store.configured(key))))


def _parse_rhythm_body(body: dict) -> ConversationRhythm:
    _reject_unknown_fields(body, set(_RHYTHM_FIELDS))
    activity = body.get("activity", "balanced")
    if not isinstance(activity, str) or activity not in ACTIVITY_MODES:
        raise StudioError(422, "invalid_request",
                          f"activity 必须是 {list(ACTIVITY_MODES)!r} 之一")
    max_segments = body.get("max_segments", 0)
    if not isinstance(max_segments, int) or isinstance(max_segments, bool) \
            or not 0 <= max_segments <= MAX_SEGMENTS_LIMIT:
        raise StudioError(422, "invalid_request",
                          f"max_segments 必须是 0..{MAX_SEGMENTS_LIMIT} 的整数"
                          "（0 = 跟随默认：群2/私3）")
    dnd = body.get("dnd", False)
    if not isinstance(dnd, bool):
        raise StudioError(422, "invalid_request", "dnd 必须是布尔值")
    return ConversationRhythm(activity=activity, max_segments=max_segments,
                              dnd=dnd)


def _put_rhythm(handler, ctx, raw_key: str) -> None:
    ref = _parse_conversation_key(raw_key)
    body = _require_json_body(handler)
    rhythm = _parse_rhythm_body(body)
    key = ref.as_key()
    store = RhythmStore(ctx.store)
    with ctx.lock:
        current = store.get(key)
        # 幂等重放优先于版本检查：同值重试是 200 no-op（N13）。
        if (rhythm.activity, rhythm.max_segments, rhythm.dnd) == (
                current.activity, current.max_segments, current.dnd):
            handler._send(200, _ok({
                **_rhythm_view(key, current, configured=store.configured(key)),
                "changed": False,
            }))
            return
        try:
            _check_expected_version(body, current.version)
        except StudioError as exc:
            _audit(ctx, "rhythm.set", outcome="denied", reason=exc.code,
                   conversation_key=key)
            raise
        version = store.put(key, rhythm)
        _audit(ctx, "rhythm.set", conversation_key=key,
               activity=rhythm.activity, max_segments=rhythm.max_segments,
               dnd=rhythm.dnd, version=version)
    handler._send(200, _ok({
        **_rhythm_view(key, store.get(key), configured=True),
        "changed": True,
    }))


# ---------------------------------------------------------------------------
# 关系图
# ---------------------------------------------------------------------------


def _graph(handler, ctx, svc: StudioServices) -> None:
    nodes: list[dict] = []
    edges: list[dict] = []
    explanations: dict[str, dict] = {}

    # 管理通道自身的解释信息（owner ResolvedContext，见模块 docstring）。
    mgmt = management_context()
    explanations["management_channel"] = {
        "code": "owner_channel",
        "message": "本 API 以 Bearer token 作为主人管理通道；"
                   f"管理主体能力：{', '.join(mgmt.capability_scopes)}。",
    }

    conv_keys = _conversation_keys(ctx)
    for key in conv_keys:
        entry = _conversation_entry(ctx, svc, key)
        node_id = f"conv:{key}"
        if entry.get("malformed"):
            continue
        nodes.append({
            "id": node_id, "kind": "conversation", "key": key,
            "label": f"{entry['channel']}:{entry['conversation_id']}",
            "channel": entry["channel"],
            "conversation_kind": entry["kind"],
            "capabilities": entry["capabilities"],
            "ceiling": entry["ceiling"],
        })
        if entry["kind"] == ConversationKind.QQ_GROUP.value:
            explanations[node_id] = {
                "code": "group_ceiling",
                "message": "群会话能力上限 "
                           f"{', '.join(GROUP_CEILING)}；主人在群内不提权（INV-03）。",
            }
        if entry.get("persona_id"):
            pid = entry["persona_id"]
            edges.append({
                "id": f"edge:persona:{key}", "kind": "persona",
                "from": node_id, "to": f"persona:{pid}",
                "version": entry["persona_version"],
                "dangling": _find_persona(ctx, svc, pid) is None,
            })
        for mb in entry["memory_spaces"]:
            sid = mb["space_id"]
            if mb.get("restricted"):
                continue  # vault 污染数据不进图（INV-01：图上无边）
            if mb.get("missing"):
                edges.append({
                    "id": f"edge:mem:{key}:{sid}", "kind": "memory",
                    "from": node_id, "to": f"space:{sid}", "missing": True,
                })
                continue
            edges.append({
                "id": f"edge:mem:{key}:{sid}", "kind": "memory",
                "from": node_id, "to": f"space:{sid}",
                "can_read": mb["can_read"], "can_write": mb["can_write"],
                "granted_by": mb["granted_by"], "created_at": mb["created_at"],
            })

    for view in (_persona_view(svc.personas.require(cid), builtin=True)
                 for cid in svc.personas.ids()):
        nodes.append({"id": f"persona:{view['id']}", "kind": "persona",
                      "label": view["title"] or view["name"], "builtin": True})
    for key in sorted(ctx.store.keys()):
        if not key.startswith(CARD_KEY_PREFIX):
            continue
        cid = key[len(CARD_KEY_PREFIX):]
        card = _find_persona(ctx, svc, cid)
        if card is not None and svc.personas.get(cid) is None:
            nodes.append({"id": f"persona:{cid}", "kind": "persona",
                          "label": card.title or card.name, "builtin": False})

    # issue #11：设备节点——事实源 = DeviceRegistry（非客户端自报）；
    # 只展示 标签/状态/能力/排队水位，绝不回显 token/公钥/配对码。
    from whale_core.devices.registry import DeviceRegistry
    _registry = DeviceRegistry(ctx.store)
    for meta in _registry.list_devices():
        did = str(meta.get("device_id") or "")
        if not did:
            continue
        queued = 0
        for k in ctx.store.keys():
            if not k.startswith(f"device:event:{did}:"):
                continue
            evid = k.rsplit(":", 1)[-1]
            if not ctx.store.get(f"device:processed:{did}:{evid}"):
                queued += 1
        nodes.append({
            "id": f"device:{did}", "kind": "device",
            "label": str(meta.get("label") or did),
            "status": str(meta.get("status") or "unknown"),
            "capabilities": list(meta.get("scopes") or []),
            "queued_events": queued,
            "created_at": meta.get("created_at"),
        })

    for ref in svc.spaces.list_spaces(include_protected=True):
        view = _space_view(ctx, svc, ref.space_id)
        if view is None:
            continue
        node_id = f"space:{ref.space_id}"
        node = {"id": node_id, "kind": "memory_space",
                "label": view["name"], "space_type": view["space_type"],
                "version": view["version"], "bindable": view["bindable"]}
        if view.get("restricted"):
            node["restricted"] = True  # 无记录数/摘要/owner 元数据（INV-08）
            explanations[node_id] = {
                "code": "vault_not_bindable",
                "message": "个人原始库永不允许连接任何会话（MEM-HARD-01）；"
                           "请通过 Fork 向导创建受控副本后再绑定。",
            }
        else:
            node["record_count"] = view["record_count"]
            node["writable"] = view["writable"]
            if view["space_type"] == MemorySpaceType.SHAREABLE_FORK.value:
                explanations[node_id] = {
                    "code": "read_only_fork",
                    "message": "共享副本默认只读；绑定时不允许 can_write。",
                }
        nodes.append(node)

    handler._send(200, _ok({
        "nodes": nodes, "edges": edges, "explanations": explanations,
        "generated_at": _now_iso(),
    }))


# ---------------------------------------------------------------------------
# 临时授权管理（S4 实体的管理视图）
# ---------------------------------------------------------------------------


def _create_code(handler, ctx, svc: StudioServices) -> None:
    body = _require_json_body(handler)
    # 严格字段表：未知字段静默忽略会掩盖配置错误（生产实证 2026-10-31：
    # 调用方传 allowed_scopes 被忽略，口令退化为默认 scope，租约搜索被拒）。
    allowed_fields = {"scopes", "custom_phrase", "ttl_s", "grant_ttl_s",
                      "grant_max_uses", "action_fingerprint"}
    unknown_fields = sorted(set(body) - allowed_fields)
    if unknown_fields:
        raise StudioError(422, "invalid_request",
                          f"未知字段: {unknown_fields!r}（允许: {sorted(allowed_fields)!r}）")
    raw_scopes = body.get("scopes")
    if raw_scopes is None:
        scopes = (SCOPE_TASK_READ_SELF,)
    elif isinstance(raw_scopes, list) and all(
            isinstance(s, str) for s in raw_scopes) and len(raw_scopes) <= 16:
        scopes = tuple(raw_scopes)
    else:
        raise StudioError(422, "invalid_request",
                          "scopes 必须是最多 16 项的字符串数组")
    unknown = [s for s in scopes if s not in GRANTABLE_SCOPES]
    if unknown:
        raise StudioError(422, "invalid_request",
                          f"scopes 超出可授权范围: {unknown!r}")
    custom = body.get("custom_phrase")
    if custom is not None and (not isinstance(custom, str)
                               or not custom.strip() or len(custom) > 300):
        raise StudioError(422, "invalid_request",
                          "custom_phrase 必须是 1..300 字符的字符串")

    def _num(name: str, default: float, lo: float, hi: float) -> float:
        v = body.get(name)
        if v is None:
            return default
        if not isinstance(v, (int, float)) or isinstance(v, bool) \
                or not (lo <= float(v) <= hi):
            raise StudioError(422, "invalid_request",
                              f"{name} 必须在 {lo}..{hi} 之间")
        return float(v)

    ttl_s = _num("ttl_s", 86400, 1, _MAX_CODE_TTL_S)
    grant_ttl_s = _num("grant_ttl_s", 600, 1, _MAX_GRANT_TTL_S)
    grant_max_uses = int(_num("grant_max_uses", 1, 1, _MAX_GRANT_USES))
    fingerprint = body.get("action_fingerprint")
    if fingerprint is not None and (not isinstance(fingerprint, str)
                                    or len(fingerprint) > 200):
        raise StudioError(422, "invalid_request",
                          "action_fingerprint 必须是最多 200 字符的字符串")
    with ctx.lock:
        try:
            created = svc.auth.create_code_ex(
                allowed_scopes=scopes, ttl_s=ttl_s, created_by="studio-api",
                custom_phrase=custom, grant_ttl_s=grant_ttl_s,
                grant_max_uses=grant_max_uses,
                action_fingerprint=fingerprint)
        except TemporaryAuthError as exc:
            _audit(ctx, "auth.code.create", outcome="denied",
                   reason="invalid_request")
            raise StudioError(422, "invalid_request", str(exc)[:200])
        # INV-10：审计只记元数据——明文/校验值一律不落审计。
        _audit(ctx, "auth.code.create", code_id=created["code_id"],
               scopes=list(created["allowed_scopes"]),
               proof_strength=created["proof_strength"])
    handler._send(200, _ok({
        "code_id": created["code_id"],
        "code": created["code_text"],  # 明文仅此一次出现在响应里
        "proof_strength": created["proof_strength"],
        "scopes": list(created["allowed_scopes"]),
        "expires_at": created["expires_at"],
        "note": "口令明文仅本次返回；服务端只保存校验值，无法找回。",
    }))


def _list_codes(handler, ctx, svc: StudioServices) -> None:
    # list_codes 已剔除 verifier；此处再剔除任何意外混入的明文字段。
    codes = []
    for rec in svc.auth.list_codes():
        codes.append({k: v for k, v in rec.items()
                      if k not in ("verifier", "code", "code_text", "phrase")})
    handler._send(200, _ok({"codes": codes}))


def _revoke_code(handler, ctx, svc: StudioServices, code_id: str) -> None:
    if not _ID_RE.match(code_id):
        raise StudioError(422, "invalid_request", "非法 code_id")
    with ctx.lock:
        done = svc.auth.revoke_code(code_id)
        if done:
            _audit(ctx, "auth.code.revoke", code_id=code_id)
        else:
            _audit(ctx, "auth.code.revoke", outcome="denied",
                   reason="not_found", code_id=code_id)
    if not done:
        raise StudioError(404, "not_found", "口令不存在或已核销（不可撤销）")
    handler._send(200, _ok({"code_id": code_id, "revoked": True}))


def _list_grants(handler, ctx, svc: StudioServices) -> None:
    handler._send(200, _ok({"grants": list(svc.auth.list_grants())}))


def _revoke_grant(handler, ctx, svc: StudioServices, grant_id: str) -> None:
    if not _ID_RE.match(grant_id):
        raise StudioError(422, "invalid_request", "非法 grant_id")
    with ctx.lock:
        done = svc.auth.revoke_grant(grant_id)
        if done:
            _audit(ctx, "auth.grant.revoke", grant_id=grant_id)
        else:
            _audit(ctx, "auth.grant.revoke", outcome="denied",
                   reason="not_found", grant_id=grant_id)
    if not done:
        raise StudioError(404, "not_found", "租约不存在")
    handler._send(200, _ok({"grant_id": grant_id, "revoked": True}))


# ---------------------------------------------------------------------------
# G2：成员本人授权与个人记忆管理（consent.* / memory.*-own）
#
# 纪律（下一阶段计划 §4 / §11 MEM-02/03/04）：
# - consent.grant 必须携带「本人已认证私聊」的结构性证据
#   （actor_conversation_key），其归属 principal 必须等于 consent 主体——
#   群主/管理通道替成员授权一律 403（member_actor_mismatch）。
# - 本人记忆管理（list/correct/delete）同样要求本人私聊证据；撤销 consent
#   后仍可管理存量记忆（隐私权不随授权消失）。
# - 管理通道（Bearer=主人）可以看到 consent 状态元数据，但看不到成员私人
#   记忆全文（list-own 对群主同样要求成员本人证据）。
# - 所有写操作严格字段表 + ctx.lock + 审计（只元数据，不存记忆正文）。
# ---------------------------------------------------------------------------

#: principal_id 形如 anon:qq:10001 / paired:qq:10001。
_PRINCIPAL_RE = re.compile(
    r"^(anon|paired):[A-Za-z0-9_.\-]{1,32}:[A-Za-z0-9_.:\-]{1,64}$")
_GROUP_ID_RE = re.compile(r"^group:[A-Za-z0-9_.\-]{1,64}$")
_MAX_CONSENT_TTL_S = 10 * 365 * 86400
_MEMBER_LIST_LIMIT = 200


def _require_member_actor(store: Any, principal_raw: Any, raw_key: Any) -> tuple[str, str]:
    """校验「成员本人私聊」证据；返回 (principal_id, conversation_key)。

    这是 MEM-02 的 API 关卡：consent 主体 == 会话归属主体。群主用自己的
    私聊、或任何群会话 key，都无法通过（结构性校验，不看昵称/正文）。

    G6 红队中-2 加固：形状自洽只是必要条件——另要求该私聊**真实发生过
    入站对话**（chat:history 存在成员发言记录），否则持管理 token 者可为
    从未说过话的任意 uin 代授 consent。
    """
    if not isinstance(principal_raw, str) or not _PRINCIPAL_RE.match(principal_raw):
        raise StudioError(422, "invalid_request",
                          "principal_id 形如 anon:qq:123 / paired:qq:123")
    if not isinstance(raw_key, str) or not raw_key.strip():
        raise StudioError(422, "invalid_request",
                          "actor_conversation_key 必填（成员本人已认证私聊）")
    ref = _parse_conversation_key(raw_key)
    if not principal_matches_private_conversation(
            ref.channel, ref.kind, ref.conversation_id, principal_raw):
        raise StudioError(403, "member_actor_mismatch",
                          "只有成员本人的已认证私聊可以发起该操作；"
                          "群主/他人代操作、群会话一律拒绝（MEM-02/03）")
    history = store.get(f"chat:history:{ref.conversation_id}")
    has_member_turn = isinstance(history, list) and any(
        isinstance(m, dict) and m.get("role") == "user" for m in history)
    if not has_member_turn:
        raise StudioError(403, "member_actor_no_evidence",
                          "该私聊尚无成员真实发言记录，不能代授 consent；"
                          "请让成员本人先私聊鲸鱼后再授权（MEM-02）")
    return principal_raw, ref.as_key()


def _reject_unknown_fields(body: dict, allowed: set[str]) -> None:
    unknown = sorted(set(body) - allowed)
    if unknown:
        raise StudioError(422, "invalid_request",
                          f"未知字段: {unknown!r}（允许: {sorted(allowed)!r}）")


def _consent_view(consent) -> dict:
    return {
        "principal_id": consent.principal_id,
        "source_group": consent.source_group,
        "scope": consent.scope,
        "version": consent.version,
        "granted_at": consent.granted_at,
        "revoked_at": consent.revoked_at,
        "ttl_s": consent.ttl_s,
        "granted_via": consent.granted_via,
    }


def _member_consent_grant(handler, ctx, svc: StudioServices) -> None:
    body = _require_json_body(handler)
    _reject_unknown_fields(body, {"principal_id", "source_group", "scope",
                                  "ttl_s", "actor_conversation_key"})
    try:
        principal_id, actor_key = _require_member_actor(
            ctx.store, body.get("principal_id"), body.get("actor_conversation_key"))
    except StudioError as exc:
        _audit(ctx, "member.consent.grant", outcome="denied", reason=exc.code)
        raise
    source_group = body.get("source_group")
    if not isinstance(source_group, str) or not _GROUP_ID_RE.match(source_group):
        raise StudioError(422, "invalid_request",
                          "source_group 必须是 group:<群号> 形式的会话 id")
    scope = body.get("scope", "self_memory")
    if scope not in KNOWN_CONSENT_SCOPES:
        raise StudioError(422, "invalid_request",
                          f"未知授权 scope: {scope!r}")
    ttl_raw = body.get("ttl_s")
    if ttl_raw is None:
        ttl_s = float(DEFAULT_CONSENT_TTL_S)
    elif isinstance(ttl_raw, (int, float)) and not isinstance(ttl_raw, bool) \
            and 0 <= float(ttl_raw) <= _MAX_CONSENT_TTL_S:
        ttl_s = float(ttl_raw)
    else:
        raise StudioError(422, "invalid_request",
                          f"ttl_s 必须在 0..{_MAX_CONSENT_TTL_S} 之间")
    with ctx.lock:
        try:
            consent = svc.consents.grant(
                principal_id, source_group, scope=scope, ttl_s=ttl_s,
                granted_by=principal_id, granted_via=actor_key)
            ref = svc.bindings.ensure_member_space(
                principal_id, conversation_key=actor_key,
                granted_by=principal_id)
        except (ConsentError, BindingError, MemorySpaceError) as exc:
            _audit(ctx, "member.consent.grant", outcome="denied",
                   reason="invalid_request", principal_id=principal_id)
            raise StudioError(422, "invalid_request", str(exc)[:200])
        _audit(ctx, "member.consent.grant", principal_id=principal_id,
               source_group=source_group, version=consent.version,
               space_id=ref.space_id)
    handler._send(200, _ok({"consent": _consent_view(consent),
                            "space_id": ref.space_id}))


def _member_consent_revoke(handler, ctx, svc: StudioServices) -> None:
    body = _require_json_body(handler)
    _reject_unknown_fields(body, {"principal_id", "source_group"})
    principal_raw = body.get("principal_id")
    if not isinstance(principal_raw, str) or not _PRINCIPAL_RE.match(principal_raw):
        raise StudioError(422, "invalid_request", "principal_id 非法")
    source_group = body.get("source_group")
    if source_group is not None and (not isinstance(source_group, str)
                                     or not _GROUP_ID_RE.match(source_group)):
        raise StudioError(422, "invalid_request", "source_group 非法")
    with ctx.lock:
        # owner 决策 2026-11-01：撤销语义 = opt-out（停止召回与写入）。
        # 管理通道撤销是安全正向操作，无需成员证据；grant 才要。
        if source_group:
            revoked = 1 if svc.consents.revoke(principal_raw, source_group) else 0
        else:
            revoked = svc.consents.revoke_all(principal_raw)
        svc.consents.opt_out(principal_raw)
        _audit(ctx, "member.consent.revoke", principal_id=principal_raw,
               source_group=source_group or "*", revoked=revoked)
    handler._send(200, _ok({"principal_id": principal_raw,
                            "revoked": revoked}))


def _member_consent_list(handler, ctx, svc: StudioServices) -> None:
    """consent 状态元数据视图（管理通道可见；不含任何记忆内容）。"""
    principal_id = str(handler.query.get("principal_id", [""])[0] or "")
    if not _PRINCIPAL_RE.match(principal_id):
        raise StudioError(422, "invalid_request", "principal_id 非法")
    rows = svc.consents.list_for(principal_id)
    handler._send(200, _ok({
        "principal_id": principal_id,
        "consents": [_consent_view(c) for c in rows],
        "active": svc.consents.has_valid(principal_id),
    }))


def _member_space_or_404(svc: StudioServices, principal_id: str) -> str:
    ref = svc.directory.space_for_member_management(principal_id)
    if ref is None:
        raise StudioError(404, "not_found", "该成员没有个人记忆空间（未授权过）")
    return ref.space_id


def _member_record_view(rec) -> dict:
    content = dict(rec.content)
    return {
        "key": rec.key,
        "kind": rec.kind.value,
        "summary": str(content.get("summary") or ""),
        "confidence": float(content.get("confidence") or 0.0),
        "source_group": str(content.get("source_group") or ""),
        "source_message_ids": list(content.get("source_message_ids") or []),
        "consent_version": int(content.get("consent_version") or 0),
        "observed_at": str(content.get("observed_at") or ""),
        "expires_at": content.get("expires_at"),
        "created_at": rec.created_at.isoformat(),
        "superseded": "superseded" in rec.tags,
    }


def _member_memory_list(handler, ctx, svc: StudioServices) -> None:
    """memory.list-own：本人查看自己的成员记忆（须本人私聊证据）。"""
    try:
        principal_id, _ = _require_member_actor(
            ctx.store,
            handler.query.get("principal_id", [""])[0],
            handler.query.get("actor_conversation_key", [""])[0])
    except StudioError as exc:
        _audit(ctx, "member.memory.list", outcome="denied", reason=exc.code)
        raise
    space_id = _member_space_or_404(svc, principal_id)
    items: list[dict] = []
    for kind in (MemoryKind.EPISODE, MemoryKind.PREFERENCE):
        if len(items) >= _MEMBER_LIST_LIMIT:
            break
        for rec in ctx.store.list_by_kind(kind, space_ids=(space_id,)):
            if "member" not in rec.tags or "superseded" in rec.tags:
                continue
            items.append(_member_record_view(rec))
            if len(items) >= _MEMBER_LIST_LIMIT:
                break
    _audit(ctx, "member.memory.list", principal_id=principal_id,
           count=len(items))
    handler._send(200, _ok({"principal_id": principal_id,
                            "space_id": space_id, "items": items}))


def _member_record_for_write(ctx, svc: StudioServices, principal_id: str,
                             key: str):
    """取出待纠正/删除的成员记录；越空间/越标签一律 404（不泄露存在性）。"""
    space_id = _member_space_or_404(svc, principal_id)
    rec = ctx.store.get_record(key)
    if rec is None or rec.space_id != space_id or "member" not in rec.tags:
        raise StudioError(404, "not_found", "记录不存在")
    return space_id, rec


def _plugin_registry(ctx):
    return getattr(getattr(ctx, "manager", None), "registry", None)


def _member_memory_correct(handler, ctx, svc: StudioServices) -> None:
    """memory.correct-own：本人更正一条记忆（旧记录 superseded，退出召回）。"""
    body = _require_json_body(handler)
    _reject_unknown_fields(body, {"principal_id", "actor_conversation_key",
                                  "key", "new_summary"})
    try:
        principal_id, _ = _require_member_actor(
            ctx.store, body.get("principal_id"), body.get("actor_conversation_key"))
    except StudioError as exc:
        _audit(ctx, "member.memory.correct", outcome="denied", reason=exc.code)
        raise
    key = str(body.get("key") or "").strip()
    new_summary = body.get("new_summary")
    if not isinstance(new_summary, str) or not (1 <= len(new_summary.strip())
                                                <= 1500):
        raise StudioError(422, "invalid_request",
                          "new_summary 必须是 1..1500 字符的字符串")
    if contains_sensitive(new_summary):
        _audit(ctx, "member.memory.correct", outcome="denied",
               reason="sensitive_content", principal_id=principal_id)
        raise StudioError(422, "invalid_request",
                          "更正内容含敏感信息（手机号/证件/地址/凭据类），已拒绝")
    with ctx.lock:
        space_id, old = _member_record_for_write(ctx, svc, principal_id, key)
        old_content = dict(old.content)
        old_summary = str(old_content.get("summary") or "")
        # 旧记录退出一切召回（superseded），新记录留在同一成员空间。
        old_content["layer"] = "superseded"
        old_content["superseded_at"] = _now_iso()
        ctx.store.put_record(type(old)(
            kind=old.kind, key=old.key, content=old_content,
            record_id=old.record_id, created_at=old.created_at,
            tags=tuple(set(old.tags) | {"superseded"}), space_id=old.space_id))
        _purge_embedding(_plugin_registry(ctx), old_summary)
        new_key = f"member-mem:corr-{uuid4().hex[:16]}"
        new_content = dict(old.content)
        new_content.update({
            "summary": new_summary.strip()[:1500],
            "source": "member-correct",
            "layer": "member",
            "corrected_from": old.key,
            "last_confirmed": _now_iso(),
        })
        ctx.store.put_record(type(old)(
            kind=old.kind, key=new_key, content=new_content,
            tags=tuple(t for t in old.tags if t != "superseded"),
            space_id=space_id))
        _audit(ctx, "member.memory.correct", principal_id=principal_id,
               old_key_tail=key[-8:], new_key_tail=new_key[-8:])
    handler._send(200, _ok({"corrected": True, "old_key": key,
                            "new_key": new_key}))


def _member_memory_delete(handler, ctx, svc: StudioServices) -> None:
    """memory.delete-own：真删 + embedding 缓存 forget_text 传播。"""
    body = _require_json_body(handler)
    _reject_unknown_fields(body, {"principal_id", "actor_conversation_key",
                                  "key"})
    try:
        principal_id, _ = _require_member_actor(
            ctx.store, body.get("principal_id"), body.get("actor_conversation_key"))
    except StudioError as exc:
        _audit(ctx, "member.memory.delete", outcome="denied", reason=exc.code)
        raise
    key = str(body.get("key") or "").strip()
    with ctx.lock:
        _member_record_for_write(ctx, svc, principal_id, key)
        deleted = forget_gone_everywhere(
            ctx.store, key, registry=_plugin_registry(ctx))
        _audit(ctx, "member.memory.delete", principal_id=principal_id,
               key_tail=key[-8:], deleted=bool(deleted))
    handler._send(200, _ok({"deleted": bool(deleted), "key": key}))


# ---------------------------------------------------------------------------
# 路由入口（server._dispatch 挂载）
# ---------------------------------------------------------------------------


def dispatch_studio(handler, method: str, parts: list[str], query: dict) -> None:
    """``/api/v1/studio/*`` 路由。"""
    handler.query = query  # GET 端点的查询参数（如 vault/records?space_id=）
    try:
        _dispatch_studio(handler, method, parts)
    except StudioError as exc:
        handler._send(exc.status, _err(exc.code, exc.message, exc.extra))


def _dispatch_studio(handler, method: str, parts: list[str]) -> None:
    ctx = handler.ctx
    svc = services_for(ctx)
    rest = parts[1:]
    if rest == ["conversations"] and method == "GET":
        _list_conversations(handler, ctx, svc)
    elif rest == ["personas"] and method == "GET":
        _list_personas(handler, ctx, svc)
    elif rest == ["personas"] and method == "POST":
        _create_persona(handler, ctx, svc)
    elif len(rest) == 2 and rest[0] == "personas":
        if method == "GET":
            _get_persona_detail(handler, ctx, svc, rest[1])
        elif method == "PUT":
            _update_persona(handler, ctx, svc, rest[1])
        else:
            raise StudioError(405, "method_not_allowed", "use GET or PUT")
    elif rest == ["memory-spaces"] and method == "GET":
        _list_memory_spaces(handler, ctx, svc)
    elif rest == ["graph"] and method == "GET":
        _graph(handler, ctx, svc)
    elif rest == ["forks", "preview"] and method == "POST":
        _fork_preview(handler, ctx, svc)
    elif rest == ["forks"] and method == "POST":
        _fork_create(handler, ctx, svc)
    elif rest == ["vault", "records"] and method == "GET":
        _vault_records(handler, ctx, svc)
    elif len(rest) == 3 and rest[0] == "conversations" and rest[2] == "persona":
        if method != "PUT":
            raise StudioError(405, "method_not_allowed", "use PUT")
        _put_persona(handler, ctx, svc, rest[1])
    elif len(rest) == 3 and rest[0] == "conversations" and rest[2] == "memories":
        if method != "PUT":
            raise StudioError(405, "method_not_allowed", "use PUT")
        _put_memories(handler, ctx, svc, rest[1])
    elif len(rest) == 3 and rest[0] == "conversations" and rest[2] == "capabilities":
        if method != "PUT":
            raise StudioError(405, "method_not_allowed", "use PUT")
        _put_capabilities(handler, ctx, svc, rest[1])
    elif len(rest) == 3 and rest[0] == "conversations" and rest[2] == "rhythm":
        if method == "GET":
            _get_rhythm(handler, ctx, rest[1])
        elif method == "PUT":
            _put_rhythm(handler, ctx, rest[1])
        else:
            raise StudioError(405, "method_not_allowed", "use GET or PUT")
    elif rest == ["members", "consent"]:
        if method == "POST":
            _member_consent_grant(handler, ctx, svc)
        elif method == "GET":
            _member_consent_list(handler, ctx, svc)
        else:
            raise StudioError(405, "method_not_allowed", "use GET or POST")
    elif rest == ["members", "consent", "revoke"]:
        if method != "POST":
            raise StudioError(405, "method_not_allowed", "use POST")
        _member_consent_revoke(handler, ctx, svc)
    elif rest == ["members", "memory"]:
        if method != "GET":
            raise StudioError(405, "method_not_allowed", "use GET")
        _member_memory_list(handler, ctx, svc)
    elif rest == ["members", "memory", "correct"]:
        if method != "POST":
            raise StudioError(405, "method_not_allowed", "use POST")
        _member_memory_correct(handler, ctx, svc)
    elif rest == ["members", "memory", "delete"]:
        if method != "POST":
            raise StudioError(405, "method_not_allowed", "use POST")
        _member_memory_delete(handler, ctx, svc)
    elif _path_known(rest):
        # 路径模板已知但方法不符（PUT 子资源已在上面拦截，这里只剩 GET 集合）
        raise StudioError(405, "method_not_allowed", "method not allowed")
    else:
        raise StudioError(404, "not_found", "unknown endpoint")


def _path_known(rest: list[str]) -> bool:
    """路径模板已知但方法不符 → 405（否则 404）。"""
    if rest in (["conversations"], ["personas"], ["memory-spaces"], ["graph"],
                ["forks"], ["forks", "preview"], ["vault", "records"],
                ["members", "consent"], ["members", "consent", "revoke"],
                ["members", "memory"], ["members", "memory", "correct"],
                ["members", "memory", "delete"]):
        return True
    if len(rest) == 2 and rest[0] == "personas":
        return True
    return len(rest) == 3 and rest[0] == "conversations" \
        and rest[2] in ("persona", "memories", "capabilities", "rhythm")


def dispatch_auth(handler, method: str, parts: list[str]) -> None:
    """``/api/v1/auth/temporary/*`` 路由（S4 实体管理面）。"""
    try:
        _dispatch_auth(handler, method, parts)
    except StudioError as exc:
        handler._send(exc.status, _err(exc.code, exc.message, exc.extra))


def _dispatch_auth(handler, method: str, parts: list[str]) -> None:
    ctx = handler.ctx
    svc = services_for(ctx)
    rest = parts[1:]
    if rest[:1] != ["temporary"]:
        raise StudioError(404, "not_found", "unknown endpoint")
    rest = rest[1:]
    if rest == ["codes"]:
        if method == "POST":
            _create_code(handler, ctx, svc)
        elif method == "GET":
            _list_codes(handler, ctx, svc)
        else:
            raise StudioError(405, "method_not_allowed", "use GET or POST")
    elif len(rest) == 3 and rest[0] == "codes" and rest[2] == "revoke":
        if method != "POST":
            raise StudioError(405, "method_not_allowed", "use POST")
        _revoke_code(handler, ctx, svc, rest[1])
    elif rest == ["grants"]:
        if method != "GET":
            raise StudioError(405, "method_not_allowed", "use GET")
        _list_grants(handler, ctx, svc)
    elif len(rest) == 3 and rest[0] == "grants" and rest[2] == "revoke":
        if method != "POST":
            raise StudioError(405, "method_not_allowed", "use POST")
        _revoke_grant(handler, ctx, svc, rest[1])
    else:
        raise StudioError(404, "not_found", "unknown endpoint")
