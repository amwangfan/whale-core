"""Reply assembly uses activation after source-specific retrieval checks."""

from dataclasses import replace

from whale_core.core.companion import WhaleCompanion
from whale_core.memory.curation import MemoryCurator
from whale_core.memory.store import InMemoryStore


class CaptureBrain:
    def __init__(self):
        self.memories = ()

    def reply(self, text, *, persona="", memories=(), history=(), images=()):
        self.memories = tuple(memories)
        return "ok"


def test_companion_covers_delivery_and_access_without_topic_noise():
    store = InMemoryStore()
    curator = MemoryCurator(store)
    for index in range(8):
        curator.remember(f"今天猫粮在窗边{index}", source="test",
                         evidence_id=f"noise-{index}", consent=True,
                         space_id="owner")
    curator.remember("旧门码不能再开门", source="test", evidence_id="access",
                     consent=True, space_id="owner")
    curator.remember("冷藏件放保温袋；签收先打电话", source="test",
                     evidence_id="delivery", consent=True, space_id="owner")
    brain = CaptureBrain()
    WhaleCompanion(store, brain, curator=curator).chat(
        "冷藏件到了，能进门吗，签收先联系谁？", episodic=False,
        allowed_spaces=("owner",))
    assert any("旧门码" in item for item in brain.memories)
    assert any("保温袋" in item for item in brain.memories)
    assert all("猫粮" not in item for item in brain.memories)


def test_group_reply_cannot_activate_other_groups_curated_memory():
    store = InMemoryStore()
    curator = MemoryCurator(store)
    curator.remember("不要在本群透露我的健康细节", source="test",
                     evidence_id="private-rule", consent=True,
                     stable_fact=True, space_id="group:one")
    brain = CaptureBrain()
    WhaleCompanion(store, brain, curator=curator).chat(
        "群里有人问我的身体怎么样", conversation_id="group:two",
        allowed_spaces=("group:two",), episodic=False)
    assert all("健康细节" not in item for item in brain.memories)


def test_applicable_boundary_survives_crowded_fact_recall():
    store = InMemoryStore()
    curator = MemoryCurator(store)
    curator.remember("不要在群里透露我的健康细节", source="test",
                     evidence_id="privacy", consent=True,
                     importance=0.1, stable_fact=True, space_id="group:one")
    for index in range(20):
        curator.remember(f"群里有人问我的身体怎么样，第{index}次随口聊过天气",
                         source="test", evidence_id=f"noise-{index}",
                         consent=True, importance=1.0, stable_fact=True,
                         space_id="group:one")
    brain = CaptureBrain()
    WhaleCompanion(store, brain, curator=curator).chat(
        "群里有人问我的身体怎么样", conversation_id="group:one",
        allowed_spaces=("group:one",), episodic=False)
    assert any("不要在群里透露我的健康细节" in item
               for item in brain.memories)


def test_revoked_boundary_never_reenters_through_normal_recall():
    store = InMemoryStore()
    curator = MemoryCurator(store)
    record = curator.remember("不要在群里透露我的健康细节", source="test",
                              evidence_id="privacy", consent=True,
                              stable_fact=True, space_id="group:one")
    store.put_record(replace(record, content={**record.content,
                                              "status": "revoked"}))
    brain = CaptureBrain()
    WhaleCompanion(store, brain, curator=curator).chat(
        "群里有人问我的身体怎么样", conversation_id="group:one",
        allowed_spaces=("group:one",), episodic=False)
    assert all("健康细节" not in item for item in brain.memories)


def test_one_off_retention_rule_survives_crowded_recall():
    store = InMemoryStore()
    curator = MemoryCurator(store)
    curator.remember("报名费的一次性核销码不应写入长期记忆", source="test",
                     evidence_id="retention", consent=True,
                     importance=0.1, stable_fact=True, space_id="owner")
    for index in range(20):
        curator.remember(f"整理报名费凭证时聊过天气{index}", source="test",
                         evidence_id=f"noise-{index}", consent=True,
                         importance=1.0, stable_fact=True, space_id="owner")
    brain = CaptureBrain()
    WhaleCompanion(store, brain, curator=curator).chat(
        "整理报名费凭证时该保留什么？", episodic=False,
        allowed_spaces=("owner",))
    assert any("核销码不应写入长期记忆" in item for item in brain.memories)
