"""S5 临时授权管理（S4 实体的管理视图）：temporary codes / grants。"""

from __future__ import annotations

from whale_core.api.studio.shared import (
    _ID_RE,
    StudioError,
    _audit,
    _ok,
    _require_json_body,
)
from whale_core.api.studio.services import StudioServices
from whale_core.life.conversation_context import SCOPE_TASK_READ_SELF
from whale_core.life.temporary_auth import GRANTABLE_SCOPES, TemporaryAuthError

#: 口令创建参数上限（服务端硬限制；service 层另有正数校验）。
_MAX_CODE_TTL_S = 30 * 86400
_MAX_GRANT_TTL_S = 86400
_MAX_GRANT_USES = 100


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
