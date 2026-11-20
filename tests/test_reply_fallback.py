"""回复脑回落测试（2026-11-20 紧急修）：主脑故障 → 回落脑兜底。

覆盖：可重试故障回落 + 打点计数、不可重试故障透传（不吞）、正常走主脑、
性格性 skip（正常返回空/沉默文本）不触发回落。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

from whale_core.core.deepseek import ModelError
from whale_core.memory.store import InMemoryStore


def _load_runner():
    p = __import__("pathlib").Path(__file__).resolve().parent.parent / "examples" / "qq_live_workflow.py"
    spec = importlib.util.spec_from_file_location("qq_live_workflow", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Brain:
    def __init__(self, out="主脑答", exc=None):
        self.out, self.exc, self.calls = out, exc, []

    def reply(self, text, **kw):
        self.calls.append(text)
        if self.exc:
            raise self.exc
        return self.out


def test_retryable_network_error_falls_back():
    mod = _load_runner()
    store = InMemoryStore()
    primary = _Brain(exc=ModelError("network", "timed out", retryable=True))
    fallback = _Brain(out="回落答")
    brain = mod._FallbackBrain(primary, fallback, store)
    assert brain.reply("hi") == "回落答"
    assert primary.calls == ["hi"] and fallback.calls == ["hi"]
    assert int(store.get("budget:reply_fallback_count")) == 1


def test_non_retryable_error_propagates():
    mod = _load_runner()
    primary = _Brain(exc=ModelError("auth", "bad key", retryable=False))
    brain = mod._FallbackBrain(primary, _Brain())
    try:
        brain.reply("hi")
    except ModelError as e:
        assert e.kind == "auth"
    else:
        raise AssertionError("non-retryable ModelError must propagate")


def test_normal_uses_primary():
    mod = _load_runner()
    brain = mod._FallbackBrain(_Brain(out="主脑答"), _Brain(), InMemoryStore())
    assert brain.reply("hi") == "主脑答"


def test_personality_silence_not_falling_back():
    """主脑正常返回「不接话」类文本 = 性格决策，不触发回落。"""
    mod = _load_runner()
    store = InMemoryStore()
    brain = mod._FallbackBrain(_Brain(out=""), _Brain(), store)
    assert brain.reply("hi") == ""
    assert store.get("budget:reply_fallback_count") is None
