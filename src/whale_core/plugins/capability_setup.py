"""One composition point for optional speech and media capabilities.

Providers are registered only for explicitly configured endpoints. No canned results.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping
import base64
import json

from whale_core.core.runtime import ToolSpec
from whale_core.plugins.artifacts import ArtifactStore
from whale_core.plugins.media_image import OpenAIImagesProvider, register_image_gen
from whale_core.plugins.media_video import DashScopeWanVideoProvider, VideoTaskManager, register_video_gen
from whale_core.plugins.speech import OpenAICompatibleASR, OpenAICompatibleTTS, register_speech
from whale_core.plugins.realtime_voice import OpenAICompatibleRealtime, register_realtime_voice


@dataclass
class OptionalCapabilities:
    artifacts: ArtifactStore | None = None
    video_manager: VideoTaskManager | None = None
    notes: list[str] = field(default_factory=list)


def configure_optional_capabilities(registry, tools, store, *, env: Mapping[str,str],
                                    artifact_dir: str | Path | None = None) -> OptionalCapabilities:
    out = OptionalCapabilities(artifacts=ArtifactStore(artifact_dir, store) if artifact_dir else None)
    if out.artifacts:
        out.artifacts.cleanup()

    def require(pid):
        provider = registry.provider_of(pid)
        if provider is None:
            raise ValueError("provider disabled or unavailable")
        return provider

    def save(reference: str) -> str:
        return out.artifacts.save_reference(reference).url if out.artifacts else reference

    image_base = env.get("WHALE_IMAGE_BASE_URL", "").strip()
    if image_base:
        register_image_gen(registry, provider=OpenAIImagesProvider(base_url=image_base,
            api_key=env.get("WHALE_IMAGE_API_KEY", ""), model=env.get("WHALE_IMAGE_MODEL") or "dall-e-3"))
        def generate(args):
            result = require("media_image").generate(str(args.get("prompt") or args.get("query") or args.get("goal") or ""),
                size=str(args.get("size") or "1024x1024"), n=int(args.get("n") or 1))
            return json.dumps({"task_id":result.task_id, "status":result.status,
                               "images":[save(r) for r in result.images]}, ensure_ascii=False)
        tools.register(ToolSpec(name="generate_image", capability="media.image.generate", plugin_id="media_image",
                                description="按提示生成图片，返回已保存的图片链接", handler=generate))
        out.notes.append("图像生成已配置")

    speech_base = env.get("WHALE_SPEECH_BASE_URL", "").strip()
    if speech_base:
        common = {"base_url":speech_base, "api_key":env.get("WHALE_SPEECH_API_KEY", "")}
        register_speech(registry,
            asr=OpenAICompatibleASR(**common, model=env.get("WHALE_ASR_MODEL") or "whisper-1"),
            tts=OpenAICompatibleTTS(**common, model=env.get("WHALE_TTS_MODEL") or "tts-1", voice=env.get("WHALE_TTS_VOICE") or "alloy"))
        def transcribe(args):
            encoded = str(args.get("audio_base64") or "")
            if not encoded or len(encoded)>35*1024*1024:
                raise ValueError("audio_base64 required or exceeds limit")
            return require("speech_asr").transcribe(base64.b64decode(encoded, validate=True), lang=str(args.get("lang") or "zh"), format=str(args.get("format") or "wav"))
        def speak(args):
            audio = require("speech_tts").synthesize(str(args.get("text") or ""), voice=str(args.get("voice") or ""), rate=float(args.get("rate") or 1))
            if out.artifacts is None:
                raise ValueError("artifact storage required for speech output")
            return json.dumps(out.artifacts.save_bytes(audio, "audio/mpeg").as_dict())
        tools.register(ToolSpec(name="transcribe_audio", capability="asr.transcribe", plugin_id="speech_asr", handler=transcribe))
        tools.register(ToolSpec(name="synthesize_speech", capability="tts.synthesize", plugin_id="speech_tts", handler=speak))
        out.notes.append("语音识别与合成已配置")

    video_base = env.get("WHALE_VIDEO_BASE_URL", "").strip()
    if video_base:
        provider = DashScopeWanVideoProvider(base_url=video_base,
            api_key=env.get("WHALE_VIDEO_API_KEY") or env.get("DASHSCOPE_API_KEY", ""),
            model=env.get("WHALE_VIDEO_MODEL") or "wan2.6-t2v")
        register_video_gen(registry, provider=provider)
        out.video_manager = VideoTaskManager(store, provider, artifact_store=out.artifacts, min_poll_interval=15)
        def video(args):
            require("media_video")
            task = out.video_manager.submit(str(args.get("prompt") or args.get("query") or ""),
                duration_s=int(args.get("duration_s") or 5), size=str(args.get("size") or "1280x720"))
            return json.dumps(task.as_dict(), ensure_ascii=False)
        tools.register(ToolSpec(name="generate_video", capability="media.video.generate", plugin_id="media_video", handler=video))
        out.notes.append("异步视频生成已配置（DashScope Wan）")

    realtime_url = env.get("WHALE_REALTIME_URL", "").strip()
    if realtime_url:
        register_realtime_voice(registry, OpenAICompatibleRealtime(url=realtime_url,
            api_key=env.get("WHALE_REALTIME_API_KEY", ""), model=env.get("WHALE_REALTIME_MODEL", ""),
            voice=env.get("WHALE_REALTIME_VOICE") or "alloy",
            protocol=env.get("WHALE_REALTIME_PROTOCOL") or "ga"))
        out.notes.append("原生实时语音已配置；音频客户端独立连接，不经过文本模型")
    embedding_base = env.get("WHALE_EMBEDDING_BASE_URL", "").strip()
    if embedding_base:
        from whale_core.plugins.embedding import OpenAICompatibleEmbedding, register_embedding
        register_embedding(registry, OpenAICompatibleEmbedding(base_url=embedding_base,
            api_key=env.get("WHALE_EMBEDDING_API_KEY", ""),
            model=env.get("WHALE_EMBEDDING_MODEL") or "text-embedding-3-small"))
        out.notes.append("语义记忆嵌入已配置；故障时回退关键词召回")
    return out
