"""相关历史情节检索（召回断层修复）+ 通道语气固定层移除的测试。

覆盖（owner 实证 2026-11-11「30 轮后旧经历无法回忆」）：

- 30 轮无关闲聊后，旧 chat 情节里的宠物名字/生日仍能按相关性回到 prompt；
- curated 召回占满 5 条时，相关旧情节仍进 model（related 排在无关 recent 前，
  不被 memories[:8] 截掉）；curated 优先级不变；
- 多会话/多空间隔离：别空间问不到、同空间别会话也问不到（跨会话知识仍走
  curated，原始情节不串会话）；
- 无关旧话题不注入（词项相关性 + IDF 加权门槛，无关键词白名单）；
- 重复内容去重（检索层与注入层都只算一条）；
- 既有语义保持：superseded / expires_at 到期排除；curated 记录不由本通道
  双注入；空间 ACL（空元组 fail closed、vault 硬排除、include_protected
  仅主人私聊路径）；paused 只停写、不影响已存记忆召回；
- 只读不晋升：召回过的原始情节不变成 stable/curated 事实；
- 注入片段限长（MAX_SNIPPET_CHARS）、总条数仍受 memories[:8] 约束；
- companion 的宿主固定「通道语气」层已删除；人格卡表达与关系事实 /
  InnerState 提示不受影响。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from whale_core.core.companion import WhaleCompanion
from whale_core.memory.continuity import remember_episode
from whale_core.memory.curation import MemoryCurator
from whale_core.memory.episode_retrieval import (
    MAX_SNIPPET_CHARS,
    retrieve_relevant_episodes,
)
from whale_core.memory.spaces import SpaceStore
from whale_core.memory.store import (
    InMemoryStore,
    MemoryKind,
    MemoryRecord,
)
from whale_core.memory.types import MemorySpaceRef, MemorySpaceType


class FakeBrain:
    """捕获 reply() 实际收到的 persona/memories/history 的测试替身。"""

    def __init__(self, reply_text: str = "好呀"):
        self.calls: list[dict] = []
        self.reply_text = reply_text

    def reply(self, text, *, persona="", memories=(), history=()):
        self.calls.append({
            "text": text,
            "persona": persona,
            "memories": list(memories),
            "history": list(history),
        })
        return self.reply_text


#: 30 条互不相关、且与测试查询无多字词项重叠的闲聊填充。
FILLERS = [
    "昨晚看了一部科幻电影，特效很震撼",
    "中午吃了牛肉面，味道还不错",
    "周末打算去湖边散步，天气预报说晴朗",
    "最近在学吉他，手指都磨出茧了",
    "地铁上人多得挤不上去",
    "新买的耳机音质挺好",
    "下午开了个会，讨论季度计划",
    "冰箱里的牛奶快过期了",
    "晚上想煮个汤，放点玉米",
    "阳台上的绿萝长出新叶子",
    "同事介绍了一家火锅店",
    "手机相册整理了一晚上",
    "明天要交一份报告，还得加班",
    "路上的梧桐树叶都黄了",
    "买了袋橙子，挺甜的",
    "健身房续了年卡，得督促自己去",
    "邻居家的装修声太吵了",
    "追的剧更新到第十二集，剧情反转了",
    "快递放到了驿站，晚点去拿",
    "今天风大，帽子差点被吹跑",
    "晚饭煮了粥，配咸菜",
    "老同学发消息说下个月结婚",
    "暖气片该放水了，屋里不热",
    "超市打折囤了两提纸巾",
    "看完一本小说，结局有点意外",
    "早上堵车，迟到了十分钟",
    "窗台上的多肉又长侧芽了",
    "想换个手机壳，选不好颜色",
    "楼下新开了家面包房，香味飘过来",
    "整理衣柜，翻出旧围巾",
]

PET_TURN = "我家狗狗叫旺财，生日是3月12号"
PET_QUERY = "旺财的生日是哪天来着？"


def _companion(store, brain=None, curator=None, **kwargs):
    brain = brain if brain is not None else FakeBrain()
    curator = MemoryCurator(store) if curator is None else curator
    return WhaleCompanion(store, brain, curator=curator, **kwargs), brain


def _chat_episode(store, key, summary, *, conversation_id="c1", tags=("chat",),
                  space_id="", created_at=None, expires_at=None):
    content = {"summary": summary, "conversation_id": conversation_id,
               "layer": "episode"}
    if expires_at is not None:
        content["expires_at"] = expires_at
    record = MemoryRecord(
        kind=MemoryKind.EPISODE, key=key, content=content, tags=tags,
        space_id=space_id,
        created_at=created_at or datetime.now(timezone.utc))
    store.put_record(record)
    return record


# ---------------------------------------------------------------------------
# 30 轮后旧经历召回（owner 实证场景）
# ---------------------------------------------------------------------------


def test_pet_name_and_birthday_recalled_after_30_turns():
    store = InMemoryStore()
    companion, brain = _companion(store)
    companion.chat(PET_TURN, conversation_id="c-pet")
    for filler in FILLERS:  # 30 轮无关闲聊把宠物情节挤出近期时间窗
        companion.chat(filler, conversation_id="c-pet")
    companion.chat(PET_QUERY, conversation_id="c-pet")
    memories = brain.calls[-1]["memories"]
    assert any("旺财" in m and "3月12号" in m for m in memories), memories
    assert len(memories) <= 8  # 总条数预算不变
    # 只读不晋升：召回过的情节仍是原始 chat 情节，没有变成 curated/stable，
    # 也没有凭空多出 preference 记录。
    episodes = store.list_by_kind(MemoryKind.EPISODE)
    pet = [r for r in episodes if "旺财" in str(r.content.get("summary", ""))
           and "3月12号" in str(r.content.get("summary", ""))]
    assert pet, "宠物情节应仍在磁盘上"
    assert all("curated" not in r.tags and "stable" not in r.tags
               and not r.content.get("stable_fact") for r in pet)
    assert store.list_by_kind(MemoryKind.PREFERENCE) == []


def test_related_episode_survives_full_curated_budget():
    """curated 5 条占满前排预算时，相关旧情节仍要进 model（related 排在
    无关 recent 之前；否则 curated5+recent3 会在 [:8] 处把它截掉）。"""
    store = InMemoryStore()
    curator = MemoryCurator(store)
    curated_summaries = [
        "旺财是只柯基犬",
        "旺财喜欢吃鸡肉",
        "旺财每年3月要驱虫",
        "旺财的疫苗本在抽屉里",
        "旺财怕打雷",
    ]
    for i, summary in enumerate(curated_summaries):
        curator.remember(summary, source="test", evidence_id=f"t:{i}",
                         importance=0.9, consent=True)
    companion, brain = _companion(store, curator=curator)
    companion.chat("旺财的生日派对定在3月12号晚上", conversation_id="c-cur")
    for filler in FILLERS[:8]:
        companion.chat(filler, conversation_id="c-cur")
    companion.chat(PET_QUERY, conversation_id="c-cur")
    memories = brain.calls[-1]["memories"]
    assert any("生日派对" in m for m in memories), memories
    # curated 优先不变：第一条仍是 curated 召回
    assert memories[0] in curated_summaries
    assert len(memories) <= 8


def test_old_episode_not_vetoed_by_age():
    """时间语义：衰减只影响排序，120 天前的相关情节照样召回；无关的不来。"""
    store = InMemoryStore()
    now = datetime.now(timezone.utc)
    _chat_episode(store, "e-old", f"用户：{PET_TURN}\nEnxi：好呀",
                  created_at=now - timedelta(days=120))
    for i, filler in enumerate(FILLERS[:6]):
        _chat_episode(store, f"e-f{i}", f"用户：{filler}\nEnxi：好呀",
                      created_at=now - timedelta(days=i))
    hits = retrieve_relevant_episodes(store, PET_QUERY, conversation_id="c1")
    assert [h.record.key for h in hits] == ["e-old"]
    assert hits[0].relevance >= 0.15
    assert "距今120天" in hits[0].reasons
    assert "原始情节" in hits[0].reasons[-1]  # 诚实标注：不是已确认事实


def test_limit_and_exclude_keys_respected():
    store = InMemoryStore()
    _chat_episode(store, "e-1", "用户：旺财的生日是3月12号\nEnxi：好呀")
    _chat_episode(store, "e-2", "用户：旺财的生日礼物买好了\nEnxi：好呀")
    hits = retrieve_relevant_episodes(store, "旺财的生日是哪天", conversation_id="c1")
    assert len(hits) == 2
    assert len(retrieve_relevant_episodes(
        store, "旺财的生日是哪天", conversation_id="c1", limit=1)) == 1
    keys = {h.record.key for h in retrieve_relevant_episodes(
        store, "旺财的生日是哪天", conversation_id="c1", exclude_keys=("e-1",))}
    assert keys == {"e-2"}


# ---------------------------------------------------------------------------
# 多会话 / 多空间隔离
# ---------------------------------------------------------------------------


def test_multi_conversation_space_isolation():
    store = InMemoryStore()
    spaces = SpaceStore(store)
    spaces.create(MemorySpaceType.GROUP_SPACE, name="a", space_id="sp-a")
    spaces.create(MemorySpaceType.GROUP_SPACE, name="b", space_id="sp-b")
    ref_a = MemorySpaceRef("sp-a", MemorySpaceType.GROUP_SPACE, 1, can_write=True)
    ref_b = MemorySpaceRef("sp-b", MemorySpaceType.GROUP_SPACE, 1, can_write=True)
    companion, brain = _companion(store)
    # 会话 A（空间 A）：猫的名字，随后 6 轮闲聊挤出近期窗
    companion.chat("我家猫咪叫雪球，生日是6月1号",
                   conversation_id="group:aaa", allowed_spaces=(ref_a,))
    for filler in FILLERS[:6]:
        companion.chat(filler, conversation_id="group:aaa", allowed_spaces=(ref_a,))
    # 本会话本空间：正常召回
    companion.chat("我家猫咪叫什么来着",
                   conversation_id="group:aaa", allowed_spaces=(ref_a,))
    assert any("雪球" in m for m in brain.calls[-1]["memories"])
    # 会话 B（空间 B）：跨空间问同样的话 → 一个字都不泄漏
    companion.chat("我家猫咪叫什么来着",
                   conversation_id="group:bbb", allowed_spaces=(ref_b,))
    assert not any("雪球" in m for m in brain.calls[-1]["memories"])
    # 会话 C（同空间 A、不同会话）：原始情节不跨会话（跨会话知识走 curated）
    companion.chat("我家猫咪叫什么来着",
                   conversation_id="group:ccc", allowed_spaces=(ref_a,))
    assert not any("雪球" in m for m in brain.calls[-1]["memories"])
    # 写入侧空间归属正确：A 的情节在 sp-a；sp-a 里没有 B 会话的情节
    in_a = store.list_by_kind(MemoryKind.EPISODE, space_ids=("sp-a",))
    assert any("雪球" in str(r.content.get("summary", "")) for r in in_a)
    assert all(str(r.content.get("conversation_id")) != "group:bbb" for r in in_a)


def test_retriever_space_acl():
    """空间语义：显式限域、空元组 fail closed、vault 硬排除、
    include_protected 仅主人私聊召回路径可达（与 MemoryCurator 同口径）。"""
    store = InMemoryStore()
    summary = "用户：旺财的生日是3月12号\nEnxi：好呀"
    _chat_episode(store, "e-a", summary, space_id="sp-a")
    ref_a = MemorySpaceRef("sp-a", MemorySpaceType.GROUP_SPACE, 1)
    ref_b = MemorySpaceRef("sp-b", MemorySpaceType.GROUP_SPACE, 1)
    ref_v = MemorySpaceRef("vault-x", MemorySpaceType.PROTECTED_ORIGINAL, 1)
    q = "旺财的生日是哪天"
    assert retrieve_relevant_episodes(store, q, conversation_id="c1",
                                      allowed_spaces=(ref_a,))
    assert retrieve_relevant_episodes(store, q, conversation_id="c1",
                                      allowed_spaces=(ref_b,)) == ()
    assert retrieve_relevant_episodes(store, q, conversation_id="c1",
                                      allowed_spaces=()) == ()  # fail closed
    store.put("space:vault-x", {
        "space_id": "vault-x", "type": "protected_original", "name": "v",
        "owner": "owner", "version": 1, "created_at": "t", "retired_at": None})
    _chat_episode(store, "e-v", summary, space_id="vault-x")
    scoped = retrieve_relevant_episodes(store, q, conversation_id="c1",
                                        allowed_spaces=(ref_a, ref_v))
    assert all(h.record.key != "e-v" for h in scoped)  # vault 默认硬排除
    unlocked = retrieve_relevant_episodes(store, q, conversation_id="c1",
                                          allowed_spaces=(ref_a, ref_v),
                                          include_protected=True)
    assert any(h.record.key == "e-v" for h in unlocked)


def test_retriever_session_scoped_and_empty_query_safe():
    store = InMemoryStore()
    _chat_episode(store, "e-1", "用户：旺财的生日是3月12号\nEnxi：好呀",
                  conversation_id="c1")
    q = "旺财的生日是哪天"
    # 会话隔离：别会话（哪怕同空间/兼容期未分区）问不到
    assert retrieve_relevant_episodes(store, q, conversation_id="c2") == ()
    # 空查询不倾倒历史（没有相关性信号就不注入）
    assert retrieve_relevant_episodes(store, "", conversation_id="c1") == ()
    assert retrieve_relevant_episodes(store, "？？？", conversation_id="c1") == ()


# ---------------------------------------------------------------------------
# 无关旧话题不注入 / 去重
# ---------------------------------------------------------------------------


def test_unrelated_old_topic_not_injected():
    store = InMemoryStore()
    companion, brain = _companion(store)
    companion.chat("上周末去爬山了，山顶风景特别好", conversation_id="c-m")
    for filler in FILLERS[:10]:
        companion.chat(filler, conversation_id="c-m")
    companion.chat("帮我推荐几首欢快的歌", conversation_id="c-m")
    memories = brain.calls[-1]["memories"]
    assert not any("爬山" in m or "山顶" in m for m in memories), memories
    assert len(memories) <= 8


def test_duplicate_episodes_injected_once():
    store = InMemoryStore()
    for i in range(2):  # 同内容重复落盘两条（不同 key）
        remember_episode(store, summary="用户：我养了只仓鼠叫团子\nEnxi：好可爱",
                         key=f"ep:dup:{i}", tags=("chat",),
                         extra={"conversation_id": "c-dup", "layer": "episode"})
    for i, filler in enumerate(FILLERS[:6]):
        remember_episode(store, summary=f"用户：{filler}\nEnxi：好呀",
                         key=f"ep:fill:{i}", tags=("chat",),
                         extra={"conversation_id": "c-dup", "layer": "episode"})
    hits = retrieve_relevant_episodes(store, "我养的仓鼠叫什么名字",
                                      conversation_id="c-dup")
    assert len(hits) == 1  # 检索层按规范化内容去重，limit 不被重复占位
    companion, brain = _companion(store)
    companion.chat("我养的仓鼠叫什么名字", conversation_id="c-dup")
    memories = brain.calls[-1]["memories"]
    assert sum(1 for m in memories if "团子" in m) == 1


def test_related_piece_truncated():
    store = InMemoryStore()
    companion, brain = _companion(store)
    long_text = "旺财的生日是3月12号，" + "我们还准备了蛋糕气球和彩带" * 30
    companion.chat(long_text, conversation_id="c-t")
    for filler in FILLERS[:6]:
        companion.chat(filler, conversation_id="c-t")
    companion.chat(PET_QUERY, conversation_id="c-t")
    piece = next(m for m in brain.calls[-1]["memories"] if "旺财" in m)
    assert len(piece) <= MAX_SNIPPET_CHARS
    assert "3月12号" in piece  # 关键信息在开头，不因截断丢失


# ---------------------------------------------------------------------------
# 既有排除语义保持（superseded / expired / curated / paused）
# ---------------------------------------------------------------------------


def test_superseded_expired_curated_excluded():
    store = InMemoryStore()
    now = datetime.now(timezone.utc)
    _chat_episode(store, "e-base", "用户：旺财的生日是3月12号\nEnxi：好呀")
    _chat_episode(store, "e-super", "用户：旺财的生日是3月13号旧说法\nEnxi：好呀",
                  tags=("chat", "superseded"))
    _chat_episode(store, "e-exp", "用户：旺财的生日派对提醒\nEnxi：好呀",
                  expires_at=(now - timedelta(hours=1)).isoformat())
    _chat_episode(store, "e-cur", "旺财的生日是3月12号（主人确认）",
                  tags=("curated", "stable"))
    hits = retrieve_relevant_episodes(store, "旺财的生日是哪天",
                                      conversation_id="c1")
    assert {h.record.key for h in hits} == {"e-base"}


def test_paused_space_stops_writes_but_not_recall():
    """paused=停止继续记录（写入侧闸）；已存记忆不因暂停而「被忘记」。"""
    store = InMemoryStore()
    spaces = SpaceStore(store)
    spaces.create(MemorySpaceType.OWNER_WORKING_COPY, name="w", space_id="sp-w")
    ref_w = MemorySpaceRef("sp-w", MemorySpaceType.OWNER_WORKING_COPY, 1,
                           can_write=True)
    companion, brain = _companion(store)
    companion.chat(PET_TURN, conversation_id="c-p", allowed_spaces=(ref_w,))
    for filler in FILLERS[:6]:
        companion.chat(filler, conversation_id="c-p", allowed_spaces=(ref_w,))
    store.put("space:paused:sp-w", {"paused": True, "at": "t", "by": "owner"})
    before = len(store.list_by_kind(MemoryKind.EPISODE, space_ids=("sp-w",)))
    companion.chat(PET_QUERY, conversation_id="c-p", allowed_spaces=(ref_w,))
    memories = brain.calls[-1]["memories"]
    assert any("旺财" in m and "3月12号" in m for m in memories)  # 召回不受影响
    after = len(store.list_by_kind(MemoryKind.EPISODE, space_ids=("sp-w",)))
    assert after == before  # 暂停期间本轮不再沉淀新情节


# ---------------------------------------------------------------------------
# 通道语气固定层移除（owner 2026-11-11）：人格卡表达不被宿主话术覆盖
# ---------------------------------------------------------------------------


def test_channel_tone_layer_removed_and_card_expression_kept():
    from whale_core.life.context_resolver import ConversationContextResolver
    from whale_core.life.state import InnerStateStore
    from whale_core.memory.bindings import BindingStore
    from whale_core.memory.consent import MemberConsentStore, MemberMemoryDirectory
    from whale_core.memory.timeline import GroupTimelineStore
    from whale_core.ui.persona_cards import import_external_card, switch_card

    store = InMemoryStore()
    spaces = SpaceStore(store)
    directory = MemberMemoryDirectory(BindingStore(store, spaces), spaces,
                                      MemberConsentStore(store))
    timeline = GroupTimelineStore(store)
    resolver = ConversationContextResolver(member_memory=directory)
    import_external_card(store, card_id="wuxia", title="武侠卡",
                         prompt_text="你是鲸侠客，说话像武侠小说，多用江湖口吻。")
    switch_card(store, "wuxia")  # 用户显式激活人格卡（只写 persona:* 键）
    InnerStateStore(store).update(add_interest="模拟电路")
    ctx = resolver.resolve(channel="qq", sender_id="10002",
                           conversation_id="group:1124744261")
    brain = FakeBrain()
    WhaleCompanion(store, brain, timeline=timeline).chat(
        "在吗", conversation_id="group:1124744261", context=ctx, episodic=False)
    persona = brain.calls[0]["persona"]
    assert "通道语气" not in persona  # 宿主固定通道语气层已删除
    assert "武侠" in persona  # 人格卡表达完整进入上下文，未被固定话术覆盖
    assert "关系：" in persona  # 关系事实层保留（不动）
    assert "你最近感兴趣：模拟电路" in persona  # InnerState 层保留（不动）
