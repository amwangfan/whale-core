"""S5 会话 / 绑定视图：会话清单与单会话聚合视图（graph 的数据源之一）。"""

from __future__ import annotations

from whale_core.api.studio.memory import _memory_binding_view
from whale_core.api.studio.services import StudioServices
from whale_core.api.studio.shared import (
    _CAPS_PREFIX,
    StudioError,
    _binding_versions,
    _ceiling_for,
    _ok,
    _parse_conversation_key,
)
from whale_core.life.conversation_context import ConversationKind, ConversationRef
from whale_core.life.rhythm import RhythmStore


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
