"""MiniMax provider（视频/TTS）+ 多模型路由（2026-11-01 owner 决策：允许指定模型）。

全部离线：HTTP 走 provider_http.open_request 替身。
"""
from __future__ import annotations

import io
import json

import pytest

from whale_core.memory.store import InMemoryStore
from whale_core.plugins import provider_http
from whale_core.plugins.media_video import VideoError, VideoTaskManager
from whale_core.plugins.minimax_media import (
    MiniMaxError,
    MiniMaxTTSProvider,
    MiniMaxVideoProvider,
)


def _fake_transport(responses, calls):
    def transport(req, **kw):
        calls.append(req)
        return io.BytesIO(json.dumps(responses.pop(0)).encode())
    return transport


def test_minimax_video_submit_poll_happy_path(monkeypatch):
    calls = []
    responses = [
        {"task_id": "mm-1", "base_resp": {"status_code": 0, "status_msg": "success"}},
        {"task_id": "mm-1", "status": "success", "file_id": "f-1",
         "base_resp": {"status_code": 0, "status_msg": "success"}},
        {"file": {"file_id": "f-1", "download_url": "https://cdn.example/v.mp4"},
         "base_resp": {"status_code": 0, "status_msg": "success"}},
    ]
    monkeypatch.setattr(provider_http, "open_request", _fake_transport(responses, calls))
    p = MiniMaxVideoProvider(api_key="k", model="MiniMax-Hailuo-2.3")
    assert p.submit("一只小鲸鱼在窗台晒太阳", duration_s=5, size="1280x720") == "mm-1"
    body = json.loads(calls[0].data)
    assert body["model"] == "MiniMax-Hailuo-2.3"
    assert body["duration"] == 6            # Hailuo 只支持 6/10，5 归一到 6
    assert body["resolution"] == "768P"     # Hailuo 系无 720P
    assert "/v1/video_generation" in calls[0].full_url
    status, url, err = p.poll("mm-1")
    assert status == "succeeded" and url.endswith("v.mp4") and err == ""
    assert "query/video_generation" in calls[1].full_url
    assert "files/retrieve" in calls[2].full_url
    assert p.cancel("mm-1") is False        # MiniMax 无取消接口，如实 False


def test_minimax_video_poll_processing_and_failure(monkeypatch):
    calls = []
    monkeypatch.setattr(provider_http, "open_request", _fake_transport(
        [{"status": "processing", "base_resp": {"status_code": 0}}], calls))
    p = MiniMaxVideoProvider(api_key="k", model="T2V-01")
    assert p.poll("mm-2")[0] == "running"
    monkeypatch.setattr(provider_http, "open_request", _fake_transport(
        [{"status": "failed", "base_resp": {"status_code": 0}}], calls))
    assert p.poll("mm-2")[0] == "failed"


def test_minimax_base_resp_error_mapping(monkeypatch):
    calls = []
    monkeypatch.setattr(provider_http, "open_request", _fake_transport(
        [{"base_resp": {"status_code": 1008, "status_msg": "balance"}}], calls))
    p = MiniMaxVideoProvider(api_key="k")
    with pytest.raises(MiniMaxError) as ei:
        p.submit("x")
    assert ei.value.kind == "quota"


def test_minimax_tts_hex_decode_and_params(monkeypatch):
    calls = []
    wav = b"RIFF" + b"\x00" * 40
    monkeypatch.setattr(provider_http, "open_request", _fake_transport(
        [{"data": {"audio": wav.hex(), "status": 2},
          "base_resp": {"status_code": 0}}], calls))
    p = MiniMaxTTSProvider(api_key="k", model="speech-02-hd", voice="female-tianmei")
    assert p.synthesize("你好", rate=1.2) == wav
    body = json.loads(calls[0].data)
    assert body["voice_setting"]["voice_id"] == "female-tianmei"
    assert body["voice_setting"]["speed"] == 1.2
    assert body["audio_setting"]["format"] == "wav"
    assert body["stream"] is False and body["output_format"] == "hex"


def test_minimax_model_validation():
    with pytest.raises(ValueError):
        MiniMaxVideoProvider(api_key="k", model="no-such-model")
    with pytest.raises(ValueError):
        MiniMaxTTSProvider(api_key="k", model="no-such-model")
    with pytest.raises(ValueError):
        MiniMaxVideoProvider(api_key="k", base_url="http://evil.example.com")


# ---- VideoTaskManager 多模型路由 ----

class _ScriptProvider:
    def __init__(self, tag):
        self.tag = tag
        self.submits = []
    def submit(self, prompt, *, duration_s=5, size="1280x720"):
        self.submits.append(prompt)
        return f"{self.tag}-job"
    def poll(self, task_id):
        return ("succeeded", f"https://cdn.example/{self.tag}.mp4", "")
    def cancel(self, task_id):
        return True


def test_manager_model_routing_and_unknown_model():
    store = InMemoryStore()
    a, b = _ScriptProvider("a"), _ScriptProvider("b")
    m = VideoTaskManager(store, a, providers={"model-a": a, "model-b": b},
                         default_model="model-a")
    t1 = m.submit("默认模型")
    t2 = m.submit("指定 B", model="model-b")
    assert t1.model == "" and t2.model == "model-b"
    assert a.submits and b.submits
    assert m.poll(t2.task_id).url.endswith("/b.mp4")   # poll 按任务模型路由
    with pytest.raises(VideoError) as ei:
        m.submit("未知模型", model="model-x")
    assert ei.value.kind == "config" and "model-a" in str(ei.value)
    # 重启恢复：task.model 持久化，新 manager 同路由
    m2 = VideoTaskManager(store, a, providers={"model-a": a, "model-b": b},
                          default_model="model-a")
    assert m2.poll(t2.task_id).url.endswith("/b.mp4")
