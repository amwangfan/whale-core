"""issue #57：反应图片/表情素材库 + [[表情:标签]] 规划标记测试（离线）。"""
from __future__ import annotations

import pytest

from whale_core.life.reaction_media import ReactionMediaStore
from whale_core.life.response_planner import plan_reply
from whale_core.memory.store import InMemoryStore


class _FakeArtifacts:
    def __init__(self):
        self._blobs = {}

    def save(self, aid, data):
        self._blobs[aid] = data

    def get(self, aid):
        if aid not in self._blobs:
            raise KeyError("artifact not found")
        return {"artifact_id": aid}

    def read(self, aid):
        if aid not in self._blobs:
            raise KeyError("artifact not found")
        return ({"artifact_id": aid}, self._blobs[aid])


def _store():
    artifacts = _FakeArtifacts()
    artifacts.save("art-1", b"img1")
    artifacts.save("art-2", b"img2")
    artifacts.save("art-3", b"img3")
    return ReactionMediaStore(InMemoryStore(), artifacts), artifacts


class TestRegistry:
    def test_add_list_remove(self):
        ms, _ = _store()
        m = ms.add("art-1", ("开心", "庆祝"), added_by="owner")
        assert m.media_id and m.tags == ("开心", "庆祝")
        assert [x.media_id for x in ms.list()] == [m.media_id]
        assert ms.remove(m.media_id) is True
        assert ms.remove(m.media_id) is False
        assert ms.list() == ()

    def test_add_validates(self):
        ms, _ = _store()
        with pytest.raises(ValueError):
            ms.add("art-1", ())           # 无标签
        with pytest.raises(KeyError):
            ms.add("art-999", ("开心",))  # 产物不存在（如实失败）
        with pytest.raises(ValueError):
            ms.add("", ("开心",))

    def test_persona_scoping(self):
        ms, _ = _store()
        g = ms.add("art-1", ("开心",))                       # 全局
        p = ms.add("art-2", ("开心",), persona_id="card-a")  # 角色 A 专属
        listed = {m.media_id for m in ms.list(persona_id="card-a")}
        assert listed == {g.media_id, p.media_id}
        listed_b = {m.media_id for m in ms.list(persona_id="card-b")}
        assert listed_b == {g.media_id}  # 别的角色只见全局素材


class TestPick:
    def test_tag_match_and_no_match(self):
        ms, _ = _store()
        ms.add("art-1", ("开心",))
        assert ms.pick(("开心",), conversation_id="c1") is not None
        assert ms.pick(("委屈",), conversation_id="c1") is None  # 无匹配退回文字

    def test_no_recent_repeat(self):
        ms, _ = _store()
        a = ms.add("art-1", ("开心",))
        b = ms.add("art-2", ("开心",))
        first = ms.pick(("开心",), conversation_id="c", min_interval_s=0)
        second = ms.pick(("开心",), conversation_id="c", min_interval_s=0)
        assert {first.media_id, second.media_id} == {a.media_id, b.media_id}
        # 两张都进近期窗口后 → 没有可挑的，退回文字
        assert ms.pick(("开心",), conversation_id="c", min_interval_s=0) is None

    def test_rate_limit(self):
        ms, _ = _store()
        ms.add("art-1", ("开心",))
        ms.add("art-2", ("开心",))
        assert ms.pick(("开心",), conversation_id="c",
                       min_interval_s=600, now=1000.0) is not None
        # 10 分钟内同会话不再发
        assert ms.pick(("开心",), conversation_id="c",
                       min_interval_s=600, now=1001.0) is None
        # 别的会话不受限
        assert ms.pick(("开心",), conversation_id="c2",
                       min_interval_s=600, now=1001.0) is not None

    def test_read_bytes_fail_soft(self):
        ms, _ = _store()
        m = ms.add("art-1", ("开心",))
        assert ms.read_bytes(m) == b"img1"
        ms.artifacts._blobs.clear()
        assert ms.read_bytes(m) is None  # 产物丢了 → None（调用方退回文字）


class TestPlannerMarker:
    def test_reaction_tag_extracted_and_stripped(self):
        plan = plan_reply("太棒了！[[表情:开心]]", is_group=False, max_segments=3)
        assert plan.reaction_tag == "开心"
        assert all("[[" not in b for b in plan.bubbles)
        assert plan.bubbles == ("太棒了！",)

    def test_only_marker_is_image_only_turn(self):
        plan = plan_reply("[[表情:庆祝]]", is_group=False, max_segments=3)
        assert plan.silent is False
        assert plan.bubbles == ()
        assert plan.reaction_tag == "庆祝"

    def test_marker_inside_code_fence_untouched(self):
        text = "看这个：\n```\n[[表情:开心]]\n```"
        plan = plan_reply(text, is_group=False, max_segments=3)
        assert plan.reaction_tag == ""
        assert any("[[表情:开心]]" in b for b in plan.bubbles)

    def test_no_marker_default_empty(self):
        plan = plan_reply("普通回复", is_group=False, max_segments=3)
        assert plan.reaction_tag == ""


def test_artifact_retention_override(tmp_path):
    """issue #57 配套：长期素材可覆盖产物默认 7 天保留期。"""
    from whale_core.plugins.artifacts import ArtifactStore
    store = InMemoryStore()
    arts = ArtifactStore(tmp_path, store)
    short = arts.save_bytes(b"x" * 100, "image/png")
    long = arts.save_bytes(b"y" * 100, "image/gif", retention_s=365 * 86400)
    assert long.expires_at - long.created_at == 365 * 86400
    assert short.expires_at - short.created_at == 7 * 86400
    import pytest as _pt
    with _pt.raises(ValueError):
        arts.save_bytes(b"z" * 10, "image/png", retention_s=-1)


def test_has_sha256_dedup():
    """自动收编查重：同内容哈希不再入库（2026-11-03 owner 授权场景）。"""
    import hashlib
    ms, arts = _store()
    ms.add("art-1", ("开心",))
    # 模拟 ArtifactStore 的 kv 元数据（fake artifacts 不写 kv，这里补登记）
    ms.store.put("artifact:art-1", {"sha256": hashlib.sha256(b"img1").hexdigest()})
    sha1 = hashlib.sha256(b"img1").hexdigest()
    assert ms.has_sha256(sha1) is True
    assert ms.has_sha256(hashlib.sha256(b"other").hexdigest()) is False
    assert ms.has_sha256("") is False
