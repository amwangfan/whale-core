"""群成员昵称目录（2026-11-01）：uin/uid → 群名片/昵称，kv 缓存。

背景（owner 生产实证）：群时间线渲染用「群友<hash>」代号，机器人想称呼
群友只会念出一串 ID（它自己吐槽"像乱码"）。本目录用 weq-web 的
memberBriefs 补丁端点（apply-memberbriefs.py）把 uin 解析成群名片
（昵称兜底），供时间线渲染替换代号。

设计纪律：
- 缓存：kv ``namecache:g<群号>:<uin>``，正缓存 24h / 负缓存 10min；
- 每次渲染最多一次批量 HTTP（窗口内未命中者一次查完）；
- 一切异常/失败回退空串（调用方回落代号标签），绝不阻断主链路；
- 昵称只做**展示标签**，永不作为身份键/权限依据（issue #15 纪律不变）。
"""
from __future__ import annotations

import re
import time
from typing import Iterable, Mapping

_POS_TTL_S = 24 * 3600
_NEG_TTL_S = 600
_MAX_BATCH = 20
_BAD_CHARS_RE = re.compile(r"[\[\]|\\\x00-\x1f]")


def _clean_name(raw: object) -> str:
    """昵称净化为安全的展示标签：去空白/方括号/竖线（时间线行格式
    ``[msg|label]`` 的结构字符），截断 16 字。"""
    name = _BAD_CHARS_RE.sub("", str(raw or "")).strip()
    name = " ".join(name.split())
    return name[:16]


class WeqNameDirectory:
    """uin → 群名片（昵称兜底）的缓存解析器。

    ``fetch`` 可注入以便离线测试；默认用 WeQWebSendPort.member_briefs。
    """

    def __init__(self, store, port=None, *, fetch=None,
                 pos_ttl: float = _POS_TTL_S, neg_ttl: float = _NEG_TTL_S):
        self.store = store
        self.pos_ttl = pos_ttl
        self.neg_ttl = neg_ttl
        if fetch is not None:
            self._fetch = fetch
        elif port is not None and hasattr(port, "member_briefs"):
            self._fetch = lambda gc, uins, uids: port.member_briefs(
                gc, uins=uins, uids=uids)
        else:
            self._fetch = None

    def _cache_key(self, group_code: str, uin: str) -> str:
        return f"namecache:g{group_code}:{uin}"

    def _cached(self, group_code: str, uin: str) -> str | None:
        """命中返回名字（可能为空串=负缓存）；未命中/过期返回 None。"""
        try:
            rec = self.store.get(self._cache_key(group_code, uin))
        except Exception:
            return None
        if not isinstance(rec, Mapping):
            return None
        try:
            at = float(rec.get("at") or 0)
        except (TypeError, ValueError):
            return None
        name = str(rec.get("name") or "")
        ttl = self.pos_ttl if name else self.neg_ttl
        if time.time() - at > ttl:
            return None
        return name

    def _cache_put(self, group_code: str, uin: str, name: str) -> None:
        try:
            self.store.put(self._cache_key(group_code, uin),
                           {"name": name, "at": time.time()})
        except Exception:
            pass

    def resolve_group(self, group_code: str,
                      members: Iterable[tuple[str, str]]) -> dict[str, str]:
        """批量解析：members 为 (sender_id=uin, sender_uid) 对，
        返回 {uin: 展示名}（只含解析到非空名字的）。"""
        group_code = str(group_code or "").strip()
        if not group_code:
            return {}
        uniq: list[str] = []
        seen: set[str] = set()
        for sender_id, _uid in members or ():
            uin = str(sender_id or "").strip()
            if uin and uin.isdigit() and uin not in seen:
                seen.add(uin)
                uniq.append(uin)
        out: dict[str, str] = {}
        missing: list[str] = []
        for uin in uniq:
            hit = self._cached(group_code, uin)
            if hit is None:
                missing.append(uin)
            elif hit:
                out[uin] = hit
        if missing and self._fetch is not None:
            batch = missing[:_MAX_BATCH]
            try:
                rows = self._fetch(group_code, batch, [])
            except Exception:
                rows = []
            found: dict[str, str] = {}
            for row in rows or ():
                if not isinstance(row, Mapping):
                    continue
                uin = str(row.get("uin") or "").strip()
                name = _clean_name(row.get("card")) or _clean_name(row.get("nick"))
                if uin and name:
                    found[uin] = name
            now_names = {u: found.get(u, "") for u in batch}
            for uin, name in now_names.items():
                self._cache_put(group_code, uin, name)
                if name:
                    out[uin] = name
        return out

    def resolver(self):
        """返回 GroupTimelineStore.name_resolver 契约的可调用：
        (conversation_id, [(sender_id, sender_uid), ...]) -> {sender_id: 名字}
        """
        def _resolve(conversation_id: str,
                     members: Iterable[tuple[str, str]]) -> dict[str, str]:
            conv = str(conversation_id or "")
            if not conv.startswith("group:"):
                return {}
            try:
                return self.resolve_group(conv.split(":", 1)[1], members)
            except Exception:
                return {}
        return _resolve
