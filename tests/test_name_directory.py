"""2026-11-01：群成员昵称目录（uin → 群名片/昵称，kv 缓存）。"""

from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "plugins"))

from weq_channel.name_directory import WeqNameDirectory, _clean_name

from whale_core.memory.store import InMemoryStore


def _fetch(rows):
    def _f(group_code, uins, uids):
        return [r for r in rows if r.get("uin") in set(uins)]
    return _f


def test_resolve_group_card_preferred_then_nick():
    store = InMemoryStore()
    d = WeqNameDirectory(store, fetch=_fetch([
        {"uid": "u_a", "uin": "10001", "card": "游荡者", "nick": "枕竹听"},
        {"uid": "u_b", "uin": "10002", "card": "", "nick": "阿白"},
    ]))
    out = d.resolve_group("1124744261", [("10001", "u_a"), ("10002", "u_b")])
    assert out == {"10001": "游荡者", "10002": "阿白"}


def test_resolve_caches_and_avoids_refetch():
    calls = []
    def fetch(group_code, uins, uids):
        calls.append(tuple(uins))
        return [{"uid": "u_a", "uin": "10001", "card": "游荡者", "nick": ""}]
    d = WeqNameDirectory(InMemoryStore(), fetch=fetch)
    d.resolve_group("g1", [("10001", "")])
    d.resolve_group("g1", [("10001", "")])
    assert calls == [("10001",)]  # 第二次走缓存


def test_negative_cache_short_ttl():
    calls = []
    def fetch(group_code, uins, uids):
        calls.append(tuple(uins))
        return []
    d = WeqNameDirectory(InMemoryStore(), fetch=fetch, neg_ttl=3600)
    assert d.resolve_group("g1", [("10009", "")]) == {}
    d.resolve_group("g1", [("10009", "")])
    d.resolve_group("g1", [("10009", "")])  # 负缓存：不重复打
    assert calls == [("10009",)]


def test_fetch_failure_returns_empty_and_does_not_raise():
    def boom(group_code, uins, uids):
        raise RuntimeError("weq down")
    d = WeqNameDirectory(InMemoryStore(), fetch=boom)
    assert d.resolve_group("g1", [("10001", "")]) == {}
    assert d.resolver()("group:g1", [("10001", "")]) == {}


def test_resolver_ignores_non_group_conversations():
    d = WeqNameDirectory(InMemoryStore(), fetch=_fetch([]))
    assert d.resolver()("private:u_x", [("10001", "u_x")]) == {}


def test_name_sanitized_for_label_format():
    assert _clean_name("  阿[白]|豪 \n") == "阿白豪"
    assert _clean_name("x" * 40) == "x" * 16
    assert _clean_name("") == ""


def test_dirty_names_cleaned_on_resolve():
    d = WeqNameDirectory(InMemoryStore(), fetch=_fetch([
        {"uid": "u_a", "uin": "10001", "card": "[内鬼]|甲", "nick": ""},
    ]))
    out = d.resolve_group("g1", [("10001", "")])
    assert out == {"10001": "内鬼甲"}
