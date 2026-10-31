"""S5 路由入口（server._dispatch 挂载）：dispatch_studio / dispatch_auth。"""

from __future__ import annotations

from whale_core.api.studio.auth import (
    _create_code,
    _list_codes,
    _list_grants,
    _revoke_code,
    _revoke_grant,
)
from whale_core.api.studio.capabilities_rhythm import (
    _get_rhythm,
    _put_capabilities,
    _put_rhythm,
)
from whale_core.api.studio.conversations import _list_conversations
from whale_core.api.studio.graph import _graph
from whale_core.api.studio.members import (
    _member_consent_grant,
    _member_consent_list,
    _member_consent_revoke,
    _member_memory_correct,
    _member_memory_delete,
    _member_memory_list,
)
from whale_core.api.studio.memory import (
    _fork_create,
    _fork_preview,
    _list_memory_spaces,
    _put_memories,
    _vault_records,
)
from whale_core.api.studio.personas import (
    _create_persona,
    _get_persona_detail,
    _list_personas,
    _put_persona,
    _update_persona,
)
from whale_core.api.studio.services import services_for
from whale_core.api.studio.shared import StudioError, _err


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
