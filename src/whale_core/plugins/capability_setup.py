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


def _resolve_capability_env(env: Mapping[str, str], cap: str) -> tuple[str, str, str]:
    """能力端点/凭据/供应商解析，三级回退：

    1. 专属覆盖：``WHALE_<CAP>_BASE_URL`` / ``WHALE_<CAP>_API_KEY``
    2. 共享媒体平台：``WHALE_MEDIA_BASE_URL`` / ``WHALE_MEDIA_API_KEY``
    3. 回复模型同款：``DEEPSEEK_BASE_URL`` / ``DEEPSEEK_API_KEY``（owner 决策：
       媒体默认吃回复模型同一端点与 key，不额外配置）

    返回 (base_url, api_key, provider_kind)。provider_kind ∈ {"dashscope", "openai"}；
    dashscope = DashScope 兼容协议（阿里云百炼 / AgentScope 平台同路径）。
    """
    base = (env.get(f"WHALE_{cap}_BASE_URL") or env.get("WHALE_MEDIA_BASE_URL")
            or env.get("DEEPSEEK_BASE_URL") or "").strip()
    key = (env.get(f"WHALE_{cap}_API_KEY") or env.get("WHALE_MEDIA_API_KEY")
           or env.get("DEEPSEEK_API_KEY") or "").strip()
    kind = (env.get(f"WHALE_{cap}_PROVIDER") or env.get("WHALE_MEDIA_PROVIDER") or "dashscope").strip().lower()
    if kind not in ("dashscope", "openai"):
        kind = "dashscope"
    return base, key, kind


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

    image_base, image_key, image_kind = _resolve_capability_env(env, "IMAGE")
    if image_base:
        if image_kind == "dashscope":
            from whale_core.plugins.dashscope_media import DashScopeImageProvider
            image_provider = DashScopeImageProvider(base_url=image_base, api_key=image_key,
                model=env.get("WHALE_IMAGE_MODEL") or "qwen-image-3.0")
        else:
            image_provider = OpenAIImagesProvider(base_url=image_base, api_key=image_key,
                model=env.get("WHALE_IMAGE_MODEL") or "dall-e-3")
        register_image_gen(registry, provider=image_provider)
        default_size = "1328*1328" if image_kind == "dashscope" else "1024x1024"
        def generate(args):
            result = require("media_image").generate(str(args.get("prompt") or args.get("query") or args.get("goal") or ""),
                size=str(args.get("size") or default_size), n=int(args.get("n") or 1))
            # 同时回原始 URL（签名临时链接，QQ 等外部渠道可直接打开）
            # 与本地工件引用（控制台内可管理）；工件复制实现持久保存。
            return json.dumps({"task_id":result.task_id, "status":result.status,
                               "images":[save(r) for r in result.images],
                               "urls":[r for r in result.images]}, ensure_ascii=False)
        tools.register(ToolSpec(name="generate_image", capability="media.image.generate", plugin_id="media_image",
                                description="按提示生成图片，返回已保存的图片链接", handler=generate))
        out.notes.append("图像生成已配置")

    speech_base, speech_key, speech_kind = _resolve_capability_env(env, "SPEECH")
    if speech_base:
        common = {"base_url": speech_base, "api_key": speech_key}
        if speech_kind == "dashscope":
            from whale_core.plugins.dashscope_media import DashScopeASRProvider, DashScopeTTSProvider
            asr = DashScopeASRProvider(**common, model=env.get("WHALE_ASR_MODEL") or "fun-asr-flash-2026-06-15")
            tts = DashScopeTTSProvider(**common, model=env.get("WHALE_TTS_MODEL") or "qwen-audio-3.0-tts-flash",
                                       voice=env.get("WHALE_TTS_VOICE") or "alloy")
        else:
            asr = OpenAICompatibleASR(**common, model=env.get("WHALE_ASR_MODEL") or "whisper-1")
            tts = OpenAICompatibleTTS(**common, model=env.get("WHALE_TTS_MODEL") or "tts-1", voice=env.get("WHALE_TTS_VOICE") or "alloy")
        register_speech(registry, asr=asr, tts=tts)
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

    video_base, video_key, _video_kind = _resolve_capability_env(env, "VIDEO")
    if video_base:
        video_model = env.get("WHALE_VIDEO_MODEL") or "happyhorse-1.1-t2v"
        if "happyhorse" in video_model:
            from whale_core.plugins.media_video import DashScopeHappyHorseVideoProvider
            provider = DashScopeHappyHorseVideoProvider(base_url=video_base,
                api_key=video_key or env.get("DASHSCOPE_API_KEY", ""), model=video_model)
        else:
            provider = DashScopeWanVideoProvider(base_url=video_base,
                api_key=video_key or env.get("DASHSCOPE_API_KEY", ""), model=video_model)
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

    # whale-io PCM 语音桥（PR#4 移植）：浏览器低延迟通话的独立传输选项，
    # 与上面的 realtime chunk/poll 会话并存。显式配置 WHALE_PCM_BRIDGE_HOST 才注册。
    pcm_host = env.get("WHALE_PCM_BRIDGE_HOST", "").strip()
    if pcm_host:
        from whale_core.plugins.pcm_voice import PCMVoiceProvider, register_pcm_voice
        try:
            pcm_port = int(env.get("WHALE_PCM_BRIDGE_PORT") or "18799")
            pcm_timeout = float(env.get("WHALE_PCM_BRIDGE_TIMEOUT") or "15")
        except ValueError:
            raise ValueError("WHALE_PCM_BRIDGE_PORT/TIMEOUT 必须是数字") from None
        register_pcm_voice(registry, PCMVoiceProvider(
            host=pcm_host, port=pcm_port,
            bridge_token=env.get("WHALE_PCM_BRIDGE_TOKEN", ""),
            tls=(env.get("WHALE_PCM_BRIDGE_TLS") or "") == "1",
            public_ws_url=env.get("WHALE_PCM_BRIDGE_PUBLIC_WS_URL", "").strip(),
            timeout=pcm_timeout))
        out.notes.append("whale-io PCM 语音桥已配置（浏览器低延迟通话；协议按 PR#4 契约，未与真实桥联调）")
    return out
