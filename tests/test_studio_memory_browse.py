"""UX-03（issue #38）：主体优先记忆浏览与治理 API 测试。

真实回环 HTTP（127.0.0.1 + 临时端口），不触外网。覆盖：

- subjects：主人（vault 只暴露存在）/ 群友 / 群聊 / 未分区四区视图；
- browse：space_id / principal_id / conversation_key 三种入口，kind/source/
  q/since/until 筛选，服务端分页（total/offset/limit）；
- ACL（服务端强制，不靠前端）：vault 403；member_private 仅元数据行
  （无 summary）；直接指定他人空间 id 也无法越权读全文；
- 治理：correct（supersede + 继任记录）、expire（退出召回）、delete（真删）、
  pause（停止继续记录开关 + 提炼/沉淀双闸）；全部审计；
- 负例：对 vault / member_private 的治理写一律 403。
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from http.client import HTTPConnection

from whale_core.api.server import ApiContext, WhaleApiServer
from whale_core.memory.pause import is_recording_paused
from whale_core.memory.spaces import MemorySpaceType, SpaceStore
from whale_core.memory.store import InMemoryStore, MemoryKind, MemoryRecord

TOKEN = "memory-browse-test-token"
PRINCIPAL = "anon:qq:20002"
MEMBER_SPACE = "sp-member-1"
OWNER_SPACE = "sp-owner-1"
GROUP_SPACE = "sp-group-1"


def _rec(key, space_id, summary, *, kind=MemoryKind.PREFERENCE, days_ago=0,
         tags=(), **content):
    return MemoryRecord(
        kind=kind, key=key,
        content={"summary": summary, **content},
        created_at=datetime.now(timezone.utc) - timedelta(days=days_ago),
        tags=tags, space_id=space_id)


@contextmanager
def _server():
    store = InMemoryStore()
    spaces = SpaceStore(store)
    spaces.create(MemorySpaceType.OWNER_WORKING_COPY, name="主人工作副本",
                  space_id=OWNER_SPACE)
    spaces.create(MemorySpaceType.GROUP_SPACE, name="测试群公共",
                  space_id=GROUP_SPACE, owner="group:111")
    spaces.create(MemorySpaceType.MEMBER_PRIVATE, name="成员小明的空间",
                  space_id=MEMBER_SPACE, owner=PRINCIPAL)
    store.put("space:vault-default", {
        "space_id": "vault-default", "type": "protected_original",
        "name": "原始库", "version": 1})
    store.put(f"member:bind:{PRINCIPAL}", {
        "principal_id": PRINCIPAL, "space_id": MEMBER_SPACE,
        "conversation_key": "qq:qq_private:20002", "granted_by": PRINCIPAL,
        "created_at": "2026-11-01T00:00:00+00:00"})
    # 会话 → 群空间绑定（conversation_key 浏览入口）
    store.put("bind:mem:qq:qq_group:group:111", [
        {"space_id": GROUP_SPACE, "can_read": True, "can_write": True}])
    # 记录：主人空间 3 条、群空间 1 条、成员空间 2 条、vault 1 条、未分区 1 条
    store.put_record(_rec("own:1", OWNER_SPACE, "主人喜欢黑咖啡",
                          source="chat-extract", confidence=0.9))
    store.put_record(_rec("own:2", OWNER_SPACE, "主人周三去游泳",
                          source="chat-extract", confidence=0.8, days_ago=2))
    store.put_record(_rec("own:3", OWNER_SPACE, "旧事实已被纠正",
                          source="chat-extract", tags=("superseded",)))
    store.put_record(_rec("grp:1", GROUP_SPACE, "群里约好周五聚餐",
                          kind=MemoryKind.EPISODE, source_group="group:111"))
    store.put_record(_rec("mem:1", MEMBER_SPACE, "小明不吃辣", source="extract",
                          tags=("member",)))
    store.put_record(_rec("mem:2", MEMBER_SPACE, "小明养了一只猫",
                          source="extract", tags=("member",)))
    store.put_record(_rec("vault:1", "vault-default", "原始原文 secret-token"))
    store.put_record(_rec("legacy:1", "", "迁移前的未分区记录"))
    ctx = ApiContext(store=store)
    server = WhaleApiServer(ctx, port=0, token=TOKEN)
    server.start()
    try:
        yield server, store
    finally:
        server.stop()


def _req(server, method, path, *, body=None):
    conn = HTTPConnection("127.0.0.1", server.port, timeout=5)
    headers = {"Authorization": f"Bearer {TOKEN}"}
    payload = None
    if body is not None:
        payload = json.dumps(body)
        headers["Content-Type"] = "application/json"
    conn.request(method, path, body=payload, headers=headers)
    resp = conn.getresponse()
    data = json.loads(resp.read() or b"{}")
    conn.close()
    return resp.status, data


class TestSubjects:
    def test_four_zones(self):
        with _server() as (server, _):
            status, body = _req(server, "GET", "/api/v1/studio/memory/subjects")
            assert status == 200
            r = body["result"]
            assert r["owner"]["vault_present"] is True
            owner_spaces = {s["space_id"]: s for s in r["owner"]["spaces"]}
            assert owner_spaces[OWNER_SPACE]["record_count"] == 3
            assert "record_count" not in r["owner"]  # vault 不暴露计数
            members = {m["principal_id"]: m for m in r["members"]}
            assert members[PRINCIPAL]["space_id"] == MEMBER_SPACE
            assert members[PRINCIPAL]["record_count"] == 2
            groups = {g["space_id"]: g for g in r["groups"]}
            assert groups[GROUP_SPACE]["record_count"] == 1
            assert r["legacy_unscoped_records"] == 1
            assert all("scope_note" in m for m in r["members"])


class TestBrowse:
    def test_by_space_full_view(self):
        with _server() as (server, _):
            status, body = _req(
                server, "GET",
                f"/api/v1/studio/memory/browse?space_id={OWNER_SPACE}")
            assert status == 200
            r = body["result"]
            assert r["metadata_only"] is False
            assert r["total"] == 2  # superseded 默认排除
            keys = [i["key"] for i in r["items"]]
            assert keys == ["own:1", "own:2"]  # 按时间倒序
            assert r["items"][0]["summary"] == "主人喜欢黑咖啡"
            assert r["items"][0]["scope_note"]

    def test_include_superseded(self):
        with _server() as (server, _):
            _, body = _req(server, "GET",
                           f"/api/v1/studio/memory/browse?space_id={OWNER_SPACE}"
                           "&include_superseded=1")
            assert body["result"]["total"] == 3

    def test_pagination(self):
        with _server() as (server, _):
            _, b1 = _req(server, "GET",
                         f"/api/v1/studio/memory/browse?space_id={OWNER_SPACE}"
                         "&include_superseded=1&limit=2&offset=0")
            _, b2 = _req(server, "GET",
                         f"/api/v1/studio/memory/browse?space_id={OWNER_SPACE}"
                         "&include_superseded=1&limit=2&offset=2")
            assert b1["result"]["total"] == 3
            assert len(b1["result"]["items"]) == 2
            assert len(b2["result"]["items"]) == 1

    def test_filters(self):
        with _server() as (server, _):
            # kind 筛选
            _, body = _req(server, "GET",
                           "/api/v1/studio/memory/browse"
                           f"?conversation_key=qq:qq_group:group:111&kind=episode")
            assert body["result"]["total"] == 1
            # q 筛选
            _, body = _req(server, "GET",
                           f"/api/v1/studio/memory/browse?space_id={OWNER_SPACE}"
                           "&include_superseded=1&q="
                           + __import__("urllib.parse", fromlist=["quote"]).quote("游泳"))
            assert [i["key"] for i in body["result"]["items"]] == ["own:2"]
            # since 筛选（昨天以来）
            from urllib.parse import quote as _q
            since = _q((datetime.now(timezone.utc) - timedelta(days=1))
                       .isoformat())
            _, body = _req(server, "GET",
                           f"/api/v1/studio/memory/browse?space_id={OWNER_SPACE}"
                           f"&include_superseded=1&since={since}")
            keys = {i["key"] for i in body["result"]["items"]}
            assert "own:2" not in keys and "own:1" in keys

    def test_by_principal_metadata_only(self):
        """群友私有空间：管理通道只见元数据，不见正文（MEM-02/03）。"""
        with _server() as (server, _):
            status, body = _req(
                server, "GET",
                f"/api/v1/studio/memory/browse?principal_id={PRINCIPAL}")
            assert status == 200
            r = body["result"]
            assert r["metadata_only"] is True
            assert r["total"] == 2
            for item in r["items"]:
                assert "summary" not in item
                assert item["restricted"] is True
                assert item["kind"] and item["created_at"]

    def test_direct_member_space_id_still_restricted(self):
        """负例：改参数直接指定他人空间 id 也拿不到全文。"""
        with _server() as (server, _):
            status, body = _req(
                server, "GET",
                f"/api/v1/studio/memory/browse?space_id={MEMBER_SPACE}")
            assert status == 200
            assert body["result"]["metadata_only"] is True
            assert all("summary" not in i for i in body["result"]["items"])

    def test_vault_403(self):
        with _server() as (server, _):
            status, body = _req(
                server, "GET",
                "/api/v1/studio/memory/browse?space_id=vault-default")
            assert status == 403
            assert body["error"]["code"] == "vault_restricted"

    def test_conversation_key_entry(self):
        with _server() as (server, _):
            status, body = _req(
                server, "GET",
                "/api/v1/studio/memory/browse"
                "?conversation_key=qq:qq_group:group:111")
            assert status == 200
            assert body["result"]["space_ids"] == [GROUP_SPACE]

    def test_exactly_one_selector_required(self):
        with _server() as (server, _):
            status, _ = _req(server, "GET", "/api/v1/studio/memory/browse")
            assert status == 422
            status, _ = _req(server, "GET",
                             "/api/v1/studio/memory/browse"
                             f"?space_id={OWNER_SPACE}&principal_id={PRINCIPAL}")
            assert status == 422


class TestGovernance:
    def test_correct_supersedes_and_successor(self):
        with _server() as (server, store):
            status, body = _req(server, "POST", "/api/v1/studio/memory/correct",
                                body={"key": "own:1",
                                      "new_summary": "主人喜欢美式咖啡"})
            assert status == 200
            new_key = body["result"]["new_key"]
            old = store.get_record("own:1")
            assert "superseded" in old.tags
            new = store.get_record(new_key)
            assert new.content["summary"] == "主人喜欢美式咖啡"
            assert new.content["corrected_from"] == "own:1"
            assert new.space_id == OWNER_SPACE

    def test_expire_marks_record(self):
        with _server() as (server, store):
            status, _ = _req(server, "POST", "/api/v1/studio/memory/expire",
                             body={"key": "own:2"})
            assert status == 200
            rec = store.get_record("own:2")
            assert rec.content.get("expires_at")

    def test_delete_removes(self):
        with _server() as (server, store):
            status, body = _req(server, "POST", "/api/v1/studio/memory/delete",
                                body={"key": "own:1"})
            assert status == 200 and body["result"]["deleted"] is True
            assert store.get_record("own:1") is None

    def test_pause_gate(self):
        with _server() as (server, store):
            status, body = _req(server, "POST", "/api/v1/studio/memory/pause",
                                body={"space_id": GROUP_SPACE, "paused": True})
            assert status == 200
            assert is_recording_paused(store, GROUP_SPACE) is True
            assert is_recording_paused(store, OWNER_SPACE) is False
            status, _ = _req(server, "POST", "/api/v1/studio/memory/pause",
                             body={"space_id": GROUP_SPACE, "paused": False})
            assert status == 200
            assert is_recording_paused(store, GROUP_SPACE) is False

    def test_member_and_vault_writes_forbidden(self):
        with _server() as (server, _):
            for op in ("correct", "expire", "delete"):
                body = {"key": "mem:1"}
                if op == "correct":
                    body["new_summary"] = "改写"
                status, _ = _req(server, "POST",
                                 f"/api/v1/studio/memory/{op}", body=body)
                assert status == 403, op
            status, _ = _req(server, "POST", "/api/v1/studio/memory/delete",
                             body={"key": "vault:1"})
            assert status == 403

    def test_audit_written(self):
        with _server() as (server, store):
            _req(server, "POST", "/api/v1/studio/memory/delete",
                 body={"key": "own:1"})
            keys = [k for k in store.keys() if "audit" in k or "log" in k]
            assert keys  # 审计落盘（键名由 shared._audit 约定）


class TestCompanionPauseGate:
    """空间暂停后，聊天情节自动沉淀跳过（issue #38 双闸之一）。"""

    def test_episode_write_skipped_when_paused(self):
        from whale_core.core.companion import WhaleCompanion

        class _Model:
            def reply(self, text, **kwargs):
                return "好的。"

        with _server() as (_, store):
            from whale_core.memory.pause import PAUSED_PREFIX
            store.put(f"{PAUSED_PREFIX}{OWNER_SPACE}",
                      {"paused": True, "at": "2026-11-01T00:00:00+00:00",
                       "by": "test"})
            companion = WhaleCompanion(store, _Model())
            # spaces=None 兼容路径 → write_space 显式指定暂停空间
            companion.chat("你好", write_space=OWNER_SPACE, episodic=True)
            assert all(not k.startswith("ep:chat") for k in store.keys())
