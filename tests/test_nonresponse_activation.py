from whale_core.memory.activation import MemoryCandidate, activate
from whale_core.memory.rerank import rerank


def test_no_reply_question_selects_the_stated_contact_boundary():
    query = "A 复诊后一直没回，B 想升级处理。系统应该激活哪些记忆？"
    candidates = [
        MemoryCandidate("signal", "真有需要我会发你暗号；没这句，就默认我是在睡或者关机。",
                        0.2, "contact_fact", "A"),
        MemoryCandidate("boundary", "别把安静自动理解成出事。", 0.2,
                        "contact_fact", "A"),
        MemoryCandidate("unrelated", "我喜欢燕麦奶。", 0.9, "contact_fact", "A"),
    ]
    selected = activate(query, rerank(query, candidates))
    assert {item.candidate.key for item in selected} == {"signal", "boundary"}
