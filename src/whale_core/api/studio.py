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
)
from whale_core.life.temporary_auth import (
    GRANTABLE_SCOPES,
    TemporaryAuthError,
    TemporaryAuthService,
)
from whale_core.memory.bindings import BindingError, BindingStore
from whale_core.memory.fork import ForkError, ForkService
from whale_core.memory.spaces import (
    FORKABLE_TARGET_TYPES,
    WRITABLE_TYPES,
    SpaceStore,
)
from whale_core.memory.store import MemoryKind, MemoryStore
from whale_core.ui.persona_cards import (
    CARD_KEY_PREFIX,
    PersonaCard,
    PersonaCardLibrary,
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

    @classmethod
    def from_store(cls, store: MemoryStore) -> "StudioServices":
        spaces = SpaceStore(store)
        return cls(
            spaces=spaces,
            bindings=BindingStore(store, spaces),
            forks=ForkService(store, spaces),
            auth=TemporaryAuthService(store),
            personas=PersonaCardLibrary(),
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
    """可见会话 = 有任何绑定的 key ∪ 已知渠道的历史会话。"""
    keys: set[str] = set()
    for store_key in ctx.store.keys():
        for prefix in ("bind:persona:", "bind:mem:", _CAPS_PREFIX):
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
    # MEM-HARD-01：vault 直接 403，且绝不落边（INV-01）。
    if space_ref is not None and space_ref.type is MemorySpaceType.PROTECTED_ORIGINAL:
        _audit(ctx, "memory.unbind" if remove else "memory.bind",
               outcome="denied", reason="vault_not_bindable",
               conversation_key=ref.as_key(), space_id=space_id)
        raise StudioError(403, "vault_not_bindable",
                          "个人原始库不能直接连接任何会话；"
                          "请先创建受控副本（Fork）")
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
    elif _path_known(rest):
        # 路径模板已知但方法不符（PUT 子资源已在上面拦截，这里只剩 GET 集合）
        raise StudioError(405, "method_not_allowed", "method not allowed")
    else:
        raise StudioError(404, "not_found", "unknown endpoint")


def _path_known(rest: list[str]) -> bool:
    """路径模板已知但方法不符 → 405（否则 404）。"""
    if rest in (["conversations"], ["personas"], ["memory-spaces"], ["graph"],
                ["forks"], ["forks", "preview"], ["vault", "records"]):
        return True
    return len(rest) == 3 and rest[0] == "conversations" \
        and rest[2] in ("persona", "memories", "capabilities")


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
