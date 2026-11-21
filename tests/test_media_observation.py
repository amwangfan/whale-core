"""#96 配套：观察负载 media 段的解析/渲染/验证（core 侧）。

锚点：whale-mobile PR#63（main 2314f31）——观察负载附加 media 段
（package/state/playing/position_ms/duration_ms/title≤40 字）；
媒体键 media_play_pause/next/previous 经 device.key 一步直控。
"""
from __future__ import annotations

from whale_core.devices.gui_agent.decision import LlmJsonDecision
from whale_core.devices.gui_agent.verify import verify_action
from whale_core.devices.observation_codec import media_state, observation
from whale_core.devices.transports.base import (
    MediaState,
    Observation,
    ObserveRequest,
    now_ms,
)


def _payload(media=None):
    p = {
        "observation_id": "obs_x", "captured_at": now_ms(),
        "foreground_package": "tv.danmaku.bili", "activity": ".Player",
        "screen": {"w": 1080, "h": 2400}, "truncated": False,
        "elements": [{"element_id": 1, "class_name": "android.widget.Button",
                      "text": "暂停", "resource_id": "", "content_desc": "",
                      "bounds": [0, 0, 10, 10], "clickable": True,
                      "fingerprint": "ab" * 8}],
    }
    if media is not None:
        p["media"] = media
    return p


def _obs(media: MediaState | None) -> Observation:
    return Observation(
        observation_id="obs_t", device_id="d", taken_at=now_ms(),
        foreground_package="tv.danmaku.bili", activity=".Player",
        elements=(), ui_signature="sig", screenshot_ref=None,
        screenshot_signature=None, screen_width=1080, screen_height=2400,
        media=media)


class TestMediaParsing:
    def test_media_section_parsed(self):
        obs = observation("d", _payload(media={
            "package": "tv.danmaku.bili", "state": "playing",
            "playing": True, "position_ms": 61000, "duration_ms": 198000,
            "title": "某视频标题"}), ObserveRequest())
        assert obs.media is not None
        assert obs.media.playing is True
        assert obs.media.position_ms == 61000
        assert obs.media.title == "某视频标题"

    def test_no_media_section_is_none(self):
        obs = observation("d", _payload(), ObserveRequest())
        assert obs.media is None

    def test_malformed_media_tolerated(self):
        # 非法类型字段不炸（宽容未知/畸形键）
        obs = observation("d", _payload(media={
            "playing": "yes", "position_ms": "abc", "title": None}),
            ObserveRequest())
        assert obs.media is not None
        assert obs.media.playing is False
        assert obs.media.position_ms == 0

    def test_media_state_helper_none(self):
        assert media_state({}) is None
        assert media_state({"media": "nope"}) is None


class TestMediaRendering:
    def test_decision_renders_media_line(self):
        captured = {}

        async def complete(prompt: str) -> str:
            captured["prompt"] = prompt
            return '{"action":"finish","done":true,"success":true}'

        import asyncio
        dec = LlmJsonDecision(complete)
        obs = _obs(MediaState(package="tv.danmaku.bili", state="playing",
                              playing=True, position_ms=61000,
                              duration_ms=198000, title="测试视频"))
        asyncio.run(dec.decide(goal="暂停播放", observation=obs,
                               history=[], stuck_hint=""))
        prompt = captured["prompt"]
        assert "媒体：tv.danmaku.bili" in prompt
        assert "播放中=是" in prompt
        assert "61000/198000ms" in prompt

    def test_decision_omits_media_line_when_absent(self):
        captured = {}

        async def complete(prompt: str) -> str:
            captured["prompt"] = prompt
            return '{"action":"finish","done":true,"success":true}'

        import asyncio
        dec = LlmJsonDecision(complete)
        asyncio.run(dec.decide(goal="随便", observation=_obs(None),
                               history=[], stuck_hint=""))
        # 规则文本本身提到「媒体：」段；无 media 段时不得渲染状态行
        assert "播放中=" not in captured["prompt"]


class TestMediaVerify:
    def test_play_pause_flip_passed(self):
        before = _obs(MediaState(playing=True))
        after = _obs(MediaState(playing=False))
        v = verify_action(kind="device.key",
                          arguments={"key": "media_play_pause"},
                          before=before, after=after)
        assert v.status.value == "passed"

    def test_play_pause_no_flip_failed(self):
        before = _obs(MediaState(playing=True))
        after = _obs(MediaState(playing=True))
        v = verify_action(kind="device.key",
                          arguments={"key": "media_play_pause"},
                          before=before, after=after)
        assert v.status.value == "failed"

    def test_no_media_section_skipped(self):
        before = _obs(None)
        after = _obs(None)
        v = verify_action(kind="device.key",
                          arguments={"key": "media_play_pause"},
                          before=before, after=after)
        assert v.status.value == "skipped"  # 绝不伪报

    def test_next_title_change_passed(self):
        before = _obs(MediaState(title="视频A", position_ms=90000))
        after = _obs(MediaState(title="视频B", position_ms=1000))
        v = verify_action(kind="device.key",
                          arguments={"key": "media_next"},
                          before=before, after=after)
        assert v.status.value == "passed"

    def test_back_still_works(self):
        before = _obs(None)
        after = _obs(None)
        # back 无确定性信号变化 → 维持旧口径
        v = verify_action(kind="device.key", arguments={"key": "back"},
                          before=before, after=after)
        assert v.status.value in ("passed", "failed", "skipped")
