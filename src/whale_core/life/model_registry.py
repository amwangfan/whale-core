"""模型注册表（2026-11-21 主人令）：kv ``models.registry`` 驱动运行时装配。

注册表形态：[{name, provider, model, effort}]——运行时装配按 ``name`` 取。
env（WHALE_REPLY_MODEL / WHALE_VISION_MODEL 等）值为注册表 ``name`` 时按
注册表解析；未命中 = 裸模型 id（向后兼容旧行为）。

初始三条（agentscope 供应商；凭据走 env 回落零复制）：
- v4-pro-0813：直接回复脑（快档）
- qwen4-max：视觉脑
- kimi-k3：意图/任务规划脑（慢档思考）
"""
from __future__ import annotations

REGISTRY_KEY = "models.registry"

DEFAULT_REGISTRY = [
    {"name": "v4-pro-0813", "provider": "agentscope",
     "model": "deepseek-v4-pro-0813", "effort": ""},
    {"name": "qwen4-max", "provider": "agentscope",
     "model": "qwen4-max", "effort": ""},
    {"name": "kimi-k3", "provider": "agentscope",
     "model": "kimi-k3", "effort": "high"},
]

_PROVIDER_ENV = {
    # provider → (base_url env 链, api_key env 链)——零密钥复制：只引用键名
    "agentscope": (("WHALE_ASR_BASE_URL", "QQ_INTENT_BASE_URL"),
                   ("WHALE_ASR_API_KEY", "QQ_INTENT_API_KEY")),
    "deepseek": (("DEEPSEEK_BASE_URL",), ("DEEPSEEK_API_KEY",)),
}


def seed_registry(store) -> list:
    """注册表缺省播种（已有则不动）。"""
    existing = store.get(REGISTRY_KEY)
    if isinstance(existing, list) and existing:
        return existing
    store.put(REGISTRY_KEY, list(DEFAULT_REGISTRY))
    return list(DEFAULT_REGISTRY)


def registry_entries(store) -> list:
    seed_registry(store)
    entries = store.get(REGISTRY_KEY)
    return entries if isinstance(entries, list) else []


def _first_env(env, names) -> str:
    for n in names:
        v = (env.get(n) or "").strip()
        if v:
            return v
    return ""


def resolve_brain(name_or_model: str, *, env, store, timeout: float = 35.0,
                  max_tokens: int | None = None):
    """按注册表名/裸模型 id 装配脑。未配置凭据返回 None（调用方回落）。"""
    from whale_core.core.deepseek import DeepSeekCompanion

    raw = (name_or_model or "").strip()
    if not raw:
        return None
    for entry in registry_entries(store):
        if not isinstance(entry, dict) or entry.get("name") != raw:
            continue
        provider = str(entry.get("provider") or "agentscope").strip()
        base_chain, key_chain = _PROVIDER_ENV.get(provider, _PROVIDER_ENV["agentscope"])
        base = _first_env(env, base_chain) or str(entry.get("base_url") or "").strip()
        key = _first_env(env, key_chain)
        if not key:
            # 管理面供应商实体密钥槽（providers 面按 name/id 匹配）——密钥不出域
            key = _provider_key_from_store(store, provider)
        if not key:
            return None
        base = base or str(entry.get("base_url") or "").strip() \
            or _provider_base_from_store(store, provider) \
            or "https://platform.agentscope.io/v1"
        return DeepSeekCompanion(
            model=str(entry.get("model") or raw), base_url=base, api_key=key,
            timeout=timeout, max_tokens=max_tokens,
            reasoning_effort=str(entry.get("effort") or "").strip() or None)
    # 未命中注册表：裸模型 id（向后兼容）——走 agentscope 端点回落
    base = _first_env(env, ("WHALE_REPLY_BASE_URL", "QQ_INTENT_BASE_URL",
                            "DEEPSEEK_BASE_URL"))
    key = _first_env(env, ("WHALE_REPLY_API_KEY", "QQ_INTENT_API_KEY",
                           "DEEPSEEK_API_KEY"))
    if not key:
        return None
    return DeepSeekCompanion(model=raw, base_url=base or None, api_key=key,
                             timeout=timeout, max_tokens=max_tokens)


# ---- 两档回复档位（kv conversation_id→tier；默认快速） ---------------------

TIER_PREFIX = "replytier:"
TIERS = ("fast", "slow")


def get_tier(store, conversation_id: str) -> str:
    v = store.get(TIER_PREFIX + str(conversation_id))
    return str(v) if str(v) in TIERS else "fast"


def set_tier(store, conversation_id: str, tier: str) -> bool:
    tier = str(tier or "").strip()
    if tier not in TIERS:
        return False
    store.put(TIER_PREFIX + str(conversation_id), tier)
    return True

# ---- 模型角色映射（2026-11-22 控制台重构 · 裁决②唯一事实源） -------------
# kv models.roles：{role: registry 条目 name}。角色全集固定五档：
#   reply_fast  回复（快档·日常）
#   reply_slow  回复慢档（深度思考，两档回复的 slow 腿）
#   vision      视觉
#   intent      意图（规划+发送前审核同角色）
#   executor    任务执行（多轮工具循环）
ROLES_KEY = "models.roles"
ROLE_NAMES = ("reply_fast", "reply_slow", "vision", "intent", "executor")

# 首启播种：从既有 env 推导角色→注册表名（只绑定注册表里真实存在的条目）
_ROLE_ENV_CHAIN = {
    "reply_fast": ("WHALE_REPLY_MODEL",),
    "reply_slow": ("WHALE_SLOW_MODEL",),          # 无则留空（两档回复 slow 腿另行接线）
    "vision": ("WHALE_VISION_MODEL",),
    "intent": ("WHALE_INTENT_MODEL", "QQ_INTENT_MODEL"),
    "executor": ("WHALE_EXECUTOR_MODEL",),
}


def seed_roles(store, env) -> dict:
    """角色映射缺省播种（已有则不动）。只绑定注册表内真实存在的条目名。"""
    existing = store.get(ROLES_KEY)
    if isinstance(existing, dict) and existing:
        return dict(existing)
    names = {e.get("name") for e in registry_entries(store) if isinstance(e, dict)}
    roles: dict[str, str] = {}
    for role, chain in _ROLE_ENV_CHAIN.items():
        for var in chain:
            v = (env.get(var) or "").strip()
            if v and v in names:
                roles[role] = v
                break
    store.put(ROLES_KEY, roles)
    return dict(roles)


def get_roles(store, env=None) -> dict:
    """读角色映射（缺省时按 env 播种）。env=None 时只读已存。"""
    existing = store.get(ROLES_KEY)
    if isinstance(existing, dict) and existing:
        return dict(existing)
    if env is None:
        return {}
    return seed_roles(store, env)


def set_role(store, role: str, name: str | None) -> bool:
    """绑定/解绑角色→注册表条目。name=None 解绑；name 须在注册表内。"""
    role = str(role or "").strip()
    if role not in ROLE_NAMES:
        return False
    roles = store.get(ROLES_KEY)
    roles = dict(roles) if isinstance(roles, dict) else {}
    if name is None:
        roles.pop(role, None)
    else:
        names = {e.get("name") for e in registry_entries(store) if isinstance(e, dict)}
        if str(name) not in names:
            return False
        roles[role] = str(name)
    store.put(ROLES_KEY, roles)
    return True


def _provider_key_from_store(store, provider: str) -> str:
    """管理面供应商实体（providers:registry）按 name/id 匹配取密钥槽（只读）。"""
    if store is None:
        return ""
    try:
        raw = store.get("providers:registry")
        if isinstance(raw, list):
            for p in raw:
                if isinstance(p, dict) and provider in (p.get("name"), p.get("id")):
                    key = store.get(f"provider:key:{p['id']}")
                    return str(key) if key else ""
    except Exception:  # noqa: BLE001 — 密钥槽读取失败按未配置处理
        return ""
    return ""


def _provider_base_from_store(store, provider: str) -> str:
    """供应商实体的 base_url（按 name/id 匹配）。"""
    if store is None:
        return ""
    try:
        raw = store.get("providers:registry")
        if isinstance(raw, list):
            for p in raw:
                if isinstance(p, dict) and provider in (p.get("name"), p.get("id")):
                    return str(p.get("base_url") or "")
    except Exception:  # noqa: BLE001
        return ""
    return ""

# ---- 角色视图与条目写入（裁决② read-through/write-through 的支撑） ----

def upsert_entry(store, entry: dict) -> dict:
    """按 name upsert 注册表条目（不存在则追加）。返回落库后的条目。"""
    name = str(entry.get("name") or "").strip()
    if not name:
        raise ValueError("entry.name 必填")
    entries = registry_entries(store)
    clean = {
        "name": name,
        "provider": str(entry.get("provider") or "agentscope").strip() or "agentscope",
        "model": str(entry.get("model") or name).strip(),
        "effort": str(entry.get("effort") or "").strip(),
    }
    if entry.get("base_url"):
        clean["base_url"] = str(entry["base_url"]).strip()
    for i, e in enumerate(entries):
        if isinstance(e, dict) and e.get("name") == name:
            entries[i] = clean
            store.put(REGISTRY_KEY, entries)
            return clean
    entries.append(clean)
    store.put(REGISTRY_KEY, entries)
    return clean


def role_view(store, env, role: str) -> dict | None:
    """角色当前生效视图（读穿注册表）：{name, model, provider, base_url, effort,
    api_key_configured}；角色未绑定或注册表无此条目 → None（调用方回落旧径）。"""
    roles = get_roles(store, env)
    name = roles.get(role)
    if not name:
        return None
    for entry in registry_entries(store):
        if not isinstance(entry, dict) or entry.get("name") != name:
            continue
        provider = str(entry.get("provider") or "agentscope").strip()
        base_chain, key_chain = _PROVIDER_ENV.get(provider, _PROVIDER_ENV["agentscope"])
        base = (_first_env(env, base_chain) or str(entry.get("base_url") or "").strip()
                or _provider_base_from_store(store, provider))
        key = _first_env(env, key_chain) or _provider_key_from_store(store, provider)
        return {
            "name": name,
            "model": str(entry.get("model") or name),
            "provider": provider,
            "base_url": base,
            "effort": str(entry.get("effort") or ""),
            "api_key_configured": bool(key),
        }
    return None

def put_provider_credential(store, provider_name: str, base_url: str,
                            api_key: str | None) -> str:
    """写穿支撑：按名找/建供应商条目，写入密钥槽（只进不出）。返回供应商 id。"""
    name = str(provider_name or "").strip() or "custom"
    raw = store.get("providers:registry")
    providers = [p for p in raw if isinstance(p, dict)] if isinstance(raw, list) else []
    target = next((p for p in providers if p.get("name") == name), None)
    import time as _time
    if target is None:
        import uuid as _uuid
        now = int(_time.time())
        target = {"id": _uuid.uuid4().hex[:12], "name": name,
                  "base_url": str(base_url or ""), "enabled": True,
                  "created_at": now, "updated_at": now}
        providers.append(target)
    else:
        if base_url:
            target["base_url"] = str(base_url)
        target["updated_at"] = int(_time.time())
    store.put("providers:registry", providers)
    if api_key is not None:
        if api_key == "":
            store.delete(f"provider:key:{target['id']}")
        else:
            store.put(f"provider:key:{target['id']}", api_key)
    return str(target["id"])
