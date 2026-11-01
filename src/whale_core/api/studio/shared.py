"""S5 Studio API 的共享件：错误类型、响应封装、审计、请求校验与常量。

本模块是 ``whale_core.api.studio`` 包内各域模块（personas/memory/
capabilities_rhythm/auth/members/conversations/graph/router）的唯一共享
实现点——权限/校验/审计逻辑在此保持单一实现，不在域模块间复制。
"""

from __future__ import annotations

import json
import re
import time
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
    SCOPE_CHAT_BASIC,
)

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

#: 会话 key 的字符白名单（URL decode 后校验）；conversation_id 内部允许冒号。
_KEY_RE = re.compile(r"^[A-Za-z0-9_.:\-]+$")
_KEY_MIN, _KEY_MAX = 3, 220

#: code/grant/人物卡 id 的路径段白名单（oc-/tg- 前缀 + hex）。
_ID_RE = re.compile(r"^[A-Za-z0-9\-]{1,64}$")

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


def _reject_unknown_fields(body: dict, allowed: set[str]) -> None:
    unknown = sorted(set(body) - allowed)
    if unknown:
        raise StudioError(422, "invalid_request",
                          f"未知字段: {unknown!r}（允许: {sorted(allowed)!r}）")


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
