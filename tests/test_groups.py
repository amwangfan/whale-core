import json
import unittest

from starlette.testclient import TestClient

from whale.agent.runtime import RunRequest
from whale.groups import analysis
from whale.kernel.errors import WhaleError
from whale.server.http import create_server

from helpers import make_app, scripted

GID = "7654321"
SID = "qq:" + GID
DAY = 86_400_000
T0 = 1_795_000_000_000  # a fixed past instant

ROSTER = {
    "members": [{"name": "阿杰", "accounts": ["10001", "10009"], "aliases": ["杰哥"]},
                {"name": "小雨", "accounts": ["10002"], "aliases": []},
                {"name": "老王", "accounts": ["10003"], "aliases": []}],
    "bots": [{"name": "骰娘", "accounts": ["20000"], "aliases": ["bot"]}],
}


def conversation(now_ms):
    """Three days of synthetic chat: catchphrases, topics, preferences, a bot command."""
    start = now_ms - 3 * DAY
    lines = []
    for day in range(3):
        base = start + day * DAY
        lines += [
            ("10001", "阿杰", "确实，周末的跑团模组挺好玩"),
            ("10002", "小雨", "有没有人想周六开团？"),
            ("10001", "阿杰", "笑死，上次骰子全是大失败"),
            ("10003", "老王", "我倒觉得克苏鲁的模组更有意思"),
            ("10009", "杰哥", "确实确实"),
            ("20000", "骰娘", ".r 1d100 结果 42"),
            ("10002", "小雨", "要不这周继续跑团吧"),
            ("10001", "阿杰", "笑死，守秘人又在放线索"),
        ]
        if day == 0:
            lines.append(("10002", "小雨", "我最喜欢克苏鲁的模组"))
        if day == 2:
            lines.append(("10003", "老王", "我比较喜欢周末跑团，平时太累了"))
    rows = []
    for k, (uid, name, text) in enumerate(lines):
        day = k // 9
        rows.append({"message_id": f"m{k:03d}", "sender_id": uid, "sender_name": name, "text": text,
                     "sent_at": start + day * DAY + (k % 9) * 60_000})
    return rows


class GroupSpaceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.app = make_app()
        self.g = self.app.groups
        self.space = await self.g.enable("qq", GID, "跑团群", "周末跑团", ROSTER["members"], ROSTER["bots"])
        from whale.kernel.ids import now_ms

        self.now = now_ms()
        self.rows = conversation(self.now)

    async def test_roster_validation(self):
        with self.assertRaises(WhaleError):
            await self.g.enable("qq", "111111", "x", "", [{"name": "a", "accounts": ["1"]}, {"name": "a", "accounts": ["2"]}], [])
        with self.assertRaises(WhaleError):
            await self.g.enable("qq", "111112", "x", "", [{"name": "a", "accounts": ["abc"]}], [])
        with self.assertRaises(WhaleError):
            await self.g.enable("qq", GID, "dup", "", ROSTER["members"], [])

    async def test_capture_refresh_builds_digest_portraits_and_preferences(self):
        added, skipped = self.g.stage(SID, self.rows)
        self.assertEqual(added, len(self.rows))
        self.assertEqual(self.g.stage(SID, self.rows)[0], 0)  # idempotent by message id
        self.assertEqual(self.g.view(self.g.require(SID))["pending"], len(self.rows))
        res = await self.g.refresh(SID)
        self.assertEqual(res["processed"], len(self.rows))
        self.assertEqual(res["pending"], 0)
        self.assertGreaterEqual(res["counts"]["profiles_added"], 2)
        self.assertEqual(res["counts"]["preferences_added"], 2)

        digest = self.g.digest(SID)
        self.assertEqual(len(digest["segments"]), 3)                    # one per day (gap ≥ 20 min)
        self.assertTrue(digest["keywords"])
        self.assertTrue(any("跑团" in k["word"] for k in digest["keywords"]))
        jie = next(m for m in digest["members"] if m["name"] == "阿杰")
        self.assertEqual(jie["count"], 12)                              # both accounts merged (3 + 1 per day)
        self.assertIn("杰哥", jie["aliases"])
        traits = {t["label"] for t in jie["portrait"]["traits"] if t["kind"] == "expressions"}
        self.assertTrue({"确实", "笑死"} & traits)
        self.assertTrue(all(e.startswith("qq:group:" + GID + ":") for t in jie["portrait"]["traits"]
                            for e in t["evidence_ids"]))
        self.assertFalse(any(m["name"] == "骰娘" for m in digest["members"]))   # bots never portrayed
        yu = next(m for m in digest["members"] if m["name"] == "小雨")
        self.assertEqual([c["quote"] for c in yu["claims"]], ["我最喜欢克苏鲁的模组"])
        self.assertTrue(yu["claims"][0]["current"])

        portrait = self.app.memory.store.get(jie["portrait"]["memory_id"])
        self.assertEqual((portrait.kind, portrait.scope, portrait.subject), ("person", "group:" + SID, "qq:10001"))
        again = await self.g.refresh(SID)
        self.assertEqual(again["processed"], 0)

    async def test_new_messages_update_preference_and_portrait(self):
        self.g.stage(SID, self.rows)
        await self.g.refresh(SID)
        later = {"message_id": "m900", "sender_id": "10002", "sender_name": "小雨",
                 "text": "我其实更喜欢克苏鲁的模组", "sent_at": self.now - 60_000}
        self.g.stage(SID, [later])
        res = await self.g.refresh(SID)
        self.assertEqual(res["processed"], 1)
        self.assertEqual(res["counts"]["preferences_updated"], 1)
        yu = next(m for m in self.g.digest(SID)["members"] if m["name"] == "小雨")
        current = [c for c in yu["claims"] if c["current"]]
        self.assertEqual([c["quote"] for c in current], ["我其实更喜欢克苏鲁的模组"])

    async def test_evidence_is_verified_against_the_archive(self):
        self.g.stage(SID, self.rows)
        await self.g.refresh(SID)
        jie = next(m for m in self.g.digest(SID)["members"] if m["name"] == "阿杰")
        mid = jie["portrait"]["memory_id"]
        ref = jie["portrait"]["traits"][0]["evidence_ids"][0]
        ev = self.g.evidence(mid, ref)
        self.assertTrue(ev["available"])
        self.assertIn(ev["sender_id"], ("10001", "10009"))
        self.assertEqual(ev["display_name"], "阿杰")
        with self.assertRaises(WhaleError):                               # not cited by this memory
            self.g.evidence(mid, "qq:group:" + GID + ":m005")
        self.app.db.execute("DELETE FROM group_messages WHERE message_id=?", (ref.rsplit(":", 1)[1],))
        self.assertEqual(self.g.evidence(mid, ref)["reason"], "source_missing")

    async def test_group_memories_follow_the_one_way_rule(self):
        self.g.stage(SID, self.rows)
        await self.g.refresh(SID)
        owner = [h.item.content for h in await self.app.memory.retrieve("小雨 克苏鲁", scope="owner")]
        self.assertTrue(any("小雨" in c for c in owner))                 # group → owner
        group = [h.item.content for h in await self.app.memory.retrieve("小雨 克苏鲁", scope="group:" + SID)]
        self.assertTrue(any("小雨" in c for c in group))                  # the group itself
        other = await self.app.memory.retrieve("小雨 克苏鲁", scope="group:qq:999999")
        self.assertEqual(other, [])                                       # other groups never
        await self.app.memory.remember("主人的生日是三月", importance=0.9)
        seen = [h.item.content for h in await self.app.memory.retrieve("主人生日", scope="group:" + SID)]
        self.assertNotIn("主人的生日是三月", seen)                        # owner → group never
        self.assertIn("主人的生日是三月", [h.item.content for h in await self.app.memory.retrieve("主人生日")])

    async def test_opt_out_and_pause(self):
        self.g.stage(SID, self.rows)
        await self.g.refresh(SID)
        yu = next(m for m in self.g.digest(SID)["members"] if m["name"] == "小雨")
        await self.g.set_opt_out(SID, "10002", True)
        self.assertEqual(self.app.memory.store.get(yu["portrait"]["memory_id"]).status, "forgotten")
        self.assertEqual(self.g.messages(SID, "10002", None, None, 0)["items"], [])
        self.assertEqual(self.g.stage(SID, [{**self.rows[1], "message_id": "n1"}])[0], 0)
        self.assertTrue(next(m for m in self.g.digest(SID)["members"] if m["name"] == "小雨")["opted_out"])
        await self.g.set_paused(SID, True)
        self.assertFalse(await self.g.capture("qq", GID, "n2", "10001", "阿杰", "还在吗"))
        with self.assertRaises(WhaleError):
            await self.g.refresh(SID)

    async def test_import_chat_exporter_json(self):
        export = {"chatInfo": {"type": "group", "peerUid": GID, "name": "跑团群"}, "messages": [
            {"id": "e1", "seq": "1", "type": "text", "timestamp": self.now - 5000,
             "sender": {"uin": "10001", "name": "阿杰"}, "content": {"elements": [{"type": "text", "data": {"text": "确实"}}]}},
            {"id": "e2", "seq": "2", "type": "reply", "timestamp": self.now - 4000, "sender": {"uin": "10002"},
             "content": {"elements": [{"type": "reply", "data": {"referencedMessageId": "e1"}},
                                      {"type": "text", "data": {"text": "同意"}}]}},
            {"id": "e3", "type": "text", "recalled": True, "timestamp": self.now, "sender": {"uin": "10002"},
             "content": {"text": "撤回了"}},
            {"id": "e4", "type": "image", "timestamp": self.now, "sender": {"uin": "10002"}, "content": {}}]}
        res = await self.g.import_export(SID, json.dumps(export, ensure_ascii=False))
        self.assertEqual(res, {"added": 2, "skipped": 0})
        wrong = dict(export, chatInfo={"type": "group", "peerUid": "1"})
        with self.assertRaises(WhaleError):
            await self.g.import_export(SID, json.dumps(wrong))

    async def test_delete_wipes_archive_and_memories(self):
        self.g.stage(SID, self.rows)
        await self.g.refresh(SID)
        res = await self.g.delete(SID, True)
        self.assertGreaterEqual(res["memories_forgotten"], 3)
        self.assertEqual(self.g.list(), [])
        items, _ = self.app.memory.store.list(scope="group:" + SID)
        self.assertEqual(items, [])

    async def test_qq_channel_archives_every_group_message(self):
        from whale.channels.qq import QQChannel, QQEvent

        ch = QQChannel(self.app, "qq", {"owner_qq": "99999", "group_mode": "mention", "allowed_groups": [GID]})
        res = await ch.ingest(QQEvent(kind="group", message_id="live1", uin="10003", nick="老王",
                                      text="今晚开团吗", group_id=GID))
        self.assertEqual(res, "skip:not_mentioned")                    # not answered, still archived
        msgs = self.g.messages(SID, None, None, None, 0)["items"]
        self.assertEqual([m["text"] for m in msgs], ["今晚开团吗"])


class AnalysisTests(unittest.TestCase):
    def test_ngram_keywords_without_jieba(self):
        name, terms = analysis.extract_terms(["今晚一起跑团吧，守秘人准备了新模组", "《克苏鲁的呼唤》太好看了"])
        self.assertIn(name, ("jieba", "ngram"))
        self.assertTrue(any("跑团" in t for t in terms[0]))
        self.assertIn("克苏鲁的呼唤", terms[1])
        self.assertEqual(terms[1]["克苏鲁的呼唤"], "作品名")

    def test_preference_clauses(self):
        self.assertEqual(list(analysis.preference_clauses("我最喜欢克苏鲁的模组")),
                         [("我最喜欢克苏鲁的模组", "interest:克苏鲁的模组")])
        self.assertEqual(list(analysis.preference_clauses("如果我喜欢跑团呢")), [])
        self.assertEqual(list(analysis.preference_clauses("他说我喜欢跑团")), [])
        self.assertEqual(list(analysis.preference_clauses("我喜欢看病")), [])  # health stays out
        self.assertEqual(list(analysis.preference_clauses("我爱看悬疑片")), [("我爱看悬疑片", "interest:看悬疑片")])


class GroupApiTests(unittest.TestCase):
    def setUp(self):
        self.app = make_app()
        self.client = TestClient(create_server(self.app))
        self.client.__enter__()
        self.h = {"Authorization": "Bearer " + self.app.config.api_token}

    def tearDown(self):
        self.client.__exit__(None, None, None)

    def rpc(self, method, params=None, status=200):
        r = self.client.post("/api/rpc/" + method, json=params or {}, headers=self.h)
        self.assertEqual(r.status_code, status, r.text)
        return r.json()

    def test_end_to_end_over_rpc(self):
        g = self.rpc("groups.enable", {"group_id": GID, "name": "跑团群", **ROSTER})["result"]
        self.assertEqual(g["id"], SID)
        from whale.kernel.ids import now_ms

        self.app.groups.stage(SID, conversation(now_ms()))
        res = self.rpc("groups.refresh", {"id": SID})["result"]
        self.assertGreater(res["processed"], 0)
        got = self.rpc("groups.get", {"id": SID})["result"]
        self.assertEqual(got["group"]["roster"]["bots"][0]["name"], "骰娘")
        seg = got["digest"]["segments"][0]
        page = self.rpc("groups.messages", {"id": SID, "segment": seg["id"]})["result"]
        self.assertEqual(len(page["items"]), seg["count"])
        ctx = self.rpc("groups.context", {"id": SID, "message_id": page["items"][1]["message_id"]})["result"]
        self.assertEqual(len(ctx["items"]), 3)
        graph = self.rpc("memory.graph", {"scope": "group", "sources": True})["result"]
        cats = {n["category"] for n in graph["extra_nodes"]}
        self.assertEqual(cats, {"space", "person", "evidence"})
        self.assertTrue(all(n["scope"].startswith("group:") for n in graph["nodes"]))
        member = got["digest"]["members"][0]
        ev = self.rpc("memory.evidence", {"id": member["portrait"]["memory_id"],
                                          "evidence_id": member["portrait"]["traits"][0]["evidence_ids"][0]})["result"]
        self.assertTrue(ev["available"])
        bad = self.rpc("groups.enable", {"group_id": "12a", "name": "x", "members": []}, status=400)
        self.assertEqual(bad["error"]["code"], "invalid_params")
        self.rpc("groups.update", {"id": SID, "auto_refresh_min": 120})
        jobs = [j for j in self.rpc("schedule.list")["result"]["jobs"] if j["handler"] == "groups.refresh"]
        self.assertEqual(len(jobs), 1)
        self.rpc("groups.delete", {"id": SID, "wipe_memories": True})
        self.assertEqual([j for j in self.rpc("schedule.list")["result"]["jobs"] if j["handler"] == "groups.refresh"], [])


if __name__ == "__main__":
    unittest.main()
