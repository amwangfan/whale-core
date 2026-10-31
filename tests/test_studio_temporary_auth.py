"""S4 一次性口令 + sudo 租约测试（Whale Studio）。

对应 goal-chain S4 验收与 F2 负向矩阵（N05/N06/N15/N16/N17）。
"""

from __future__ import annotations

import threading
import time

import pytest

from whale_core.life.context_resolver import ConversationContextResolver
from whale_core.life.conversation_context import ConversationKind
from whale_core.life.temporary_auth import (
    FAIL_LIMIT,
    TemporaryAuthError,
    TemporaryAuthService,
    estimate_strength,
    maybe_intercept_sudo,
)
from whale_core.memory.sqlite_store import SqliteMemoryStore
from whale_core.memory.store import InMemoryStore

PRIV = ConversationKind.QQ_PRIVATE


@pytest.fixture(params=["memory", "sqlite"])
def store(request, tmp_path):
    if request.param == "memory":
        yield InMemoryStore()
    else:
        s = SqliteMemoryStore(tmp_path / "t.sqlite3")
        yield s
        s.close()


@pytest.fixture
def auth(store):
    return TemporaryAuthService(store)


def _verify(auth, code, sender="u_new", conv="private:u_new", kind=PRIV):
    return auth.verify(code, channel="qq", sender_id=sender,
                       conversation_kind=kind, conversation_id=conv)


# ---------------------------------------------------------------------------
# 单次核销与租约分账（INV-05 / T05 / N15）
# ---------------------------------------------------------------------------


def test_code_works_once_then_never_again(store, auth):
    code = auth.create_code(allowed_scopes=("task.read:self",), grant_max_uses=2)
    grant = _verify(auth, code)
    assert grant is not None and grant.max_uses == 2
    # 同一口令第二次验证：即使同账号同私聊也拒绝
    assert _verify(auth, code) is None
    # 原租约仍按自己的 TTL/次数工作（口令核销不影响已发租约）
    grants = auth.active_grants("anon:qq:u_new", "qq:qq_private:private:u_new",
                                now=time.time())
    assert len(grants) == 1 and grants[0].grant_id == grant.grant_id


def test_concurrent_verify_only_one_wins(store, auth):
    code = auth.create_code()
    results = []

    def attempt(i):
        g = _verify(auth, code, sender=f"u{i}", conv=f"private:u{i}")
        if g is not None:
            results.append(g)

    threads = [threading.Thread(target=attempt, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(results) == 1  # N06：并发只一人成功


def test_expired_and_revoked_codes_rejected(store, auth):
    code1 = auth.create_code(ttl_s=0.01)
    time.sleep(0.02)
    assert _verify(auth, code1) is None
    code2 = auth.create_code()
    live = [c for c in auth.list_codes()
            if not c.get("consumed_at") and not c.get("revoked_at")
            and float(c["expires_at"]) > time.time()]
    assert len(live) == 1
    assert auth.revoke_code(live[0]["code_id"]) is True
    assert _verify(auth, code2) is None


def test_wrong_code_never_grants(store, auth):
    auth.create_code()
    assert _verify(auth, "绝对不可能是这个口令") is None


# ---------------------------------------------------------------------------
# 租约绑定与次数（N06/N16）
# ---------------------------------------------------------------------------


def test_grant_bound_to_exact_sender_and_conversation(store, auth):
    code = auth.create_code(allowed_scopes=("task.read:self",))
    grant = _verify(auth, code, sender="u_a", conv="private:u_a")
    assert grant is not None
    # 换账号 / 换私聊：查不到租约
    assert auth.active_grants("anon:qq:u_b", "qq:qq_private:private:u_a",
                              now=time.time()) == ()
    assert auth.active_grants("anon:qq:u_a", "qq:qq_private:private:u_b",
                              now=time.time()) == ()


def test_grant_use_count_atomic_and_exhaustion(store, auth):
    code = auth.create_code(grant_max_uses=2)
    grant = _verify(auth, code)
    assert auth.consume_grant_use(grant.grant_id) is True
    assert auth.consume_grant_use(grant.grant_id) is True
    assert auth.consume_grant_use(grant.grant_id) is False  # 用尽即拒
    assert auth.active_grants("anon:qq:u_new", "qq:qq_private:private:u_new",
                              now=time.time()) == ()  # 用尽后不再算活跃


def test_mode_a_fingerprint_binding(store, auth):
    code = auth.create_code(action_fingerprint="fp-abc")
    grant = _verify(auth, code)
    assert auth.consume_grant_use(grant.grant_id,
                                  action_fingerprint="fp-改过的") is False
    assert auth.consume_grant_use(grant.grant_id,
                                  action_fingerprint="fp-abc") is True


def test_revoke_grant_immediate(store, auth):
    code = auth.create_code()
    grant = _verify(auth, code)
    assert auth.revoke_grant(grant.grant_id) is True
    assert auth.consume_grant_use(grant.grant_id) is False
    assert auth.active_grants("anon:qq:u_new", "qq:qq_private:private:u_new",
                              now=time.time()) == ()


# ---------------------------------------------------------------------------
# 口令强度与群聊限制（N05）
# ---------------------------------------------------------------------------


def test_group_sudo_rejected(store, auth):
    code = auth.create_code()
    assert auth.verify(code, channel="qq", sender_id="u_x",
                       conversation_kind=ConversationKind.QQ_GROUP,
                       conversation_id="group:1") is None


def test_low_entropy_phrase_scope_restricted(store, auth):
    with pytest.raises(TemporaryAuthError):
        auth.create_code(custom_phrase="床前明月光疑是地上霜",
                         allowed_scopes=("outbox.submit",))
    # 低风险 scope 可以
    code = auth.create_code(custom_phrase="床前明月光疑是地上霜",
                            allowed_scopes=("task.read:self",))
    assert _verify(auth, code) is not None


def test_too_short_custom_phrase_rejected(store, auth):
    with pytest.raises(TemporaryAuthError):
        auth.create_code(custom_phrase="短", allowed_scopes=("task.read:self",))


def test_strength_estimator():
    assert estimate_strength("akf9#Q2mz!Lw7%R8&xY1") == "high"
    assert estimate_strength("床前明月光") == "low"
    assert estimate_strength("aaaaaaaaaaaaaaaaaaaaaaaa") == "low"  # 低多样性


# ---------------------------------------------------------------------------
# 存储安全与限流（N17 / 防猜）
# ---------------------------------------------------------------------------


def test_code_plaintext_never_stored(store, auth):
    code = auth.create_code()
    import json
    for key in store.keys():
        value = store.get(key)
        assert code not in json.dumps(value, ensure_ascii=False)


def test_list_codes_hides_verifier(store, auth):
    auth.create_code()
    listed = auth.list_codes()
    assert listed and "verifier" not in listed[0]


def test_rate_limit_lockout(store, auth):
    auth.create_code()
    for _ in range(FAIL_LIMIT):
        assert _verify(auth, "猜错了") is None
    # 达到上限后锁定：即使现在提交**正确**口令也拒绝
    code2 = auth.create_code()
    assert _verify(auth, code2) is None


# ---------------------------------------------------------------------------
# 拦截与 resolver 集成
# ---------------------------------------------------------------------------


def test_sudo_interception():
    assert maybe_intercept_sudo("sudo abc123") == "abc123"
    assert maybe_intercept_sudo(" sudo 床前明月光 ") == "床前明月光"
    assert maybe_intercept_sudo("今天天气如何 sudo abc") is None
    assert maybe_intercept_sudo("普通聊天") is None


def test_resolver_integrates_grants(store, auth):
    code = auth.create_code(allowed_scopes=("task.read:self",))
    grant = _verify(auth, code)
    resolver = ConversationContextResolver(
        pairing_lookup=lambda c, s: None, grants=auth)
    ctx = resolver.resolve(channel="qq", sender_id="u_new",
                           conversation_id="private:u_new")
    assert "task.read:self" in ctx.capability_scopes
    assert [g.grant_id for g in ctx.grants] == [grant.grant_id]
    # 其他会话不受影响
    other = resolver.resolve(channel="qq", sender_id="u_new",
                             conversation_id="private:other")
    assert "task.read:self" not in other.capability_scopes


def test_grant_expiry_auto_demotes(store, auth):
    code = auth.create_code(grant_ttl_s=0.05)
    grant = _verify(auth, code)
    assert grant is not None
    time.sleep(0.06)
    resolver = ConversationContextResolver(
        pairing_lookup=lambda c, s: None, grants=auth)
    ctx = resolver.resolve(channel="qq", sender_id="u_new",
                           conversation_id="private:u_new")
    assert ctx.grants == ()
    assert "task.read:self" not in ctx.capability_scopes
