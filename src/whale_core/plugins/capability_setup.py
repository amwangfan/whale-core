"""One composition point for optional speech and media capabilities.

分层：内置 provider 实现（宿主装配点），非插件框架契约（builtin-provider，issue #31）。

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


# DashScope 媒体协议兼容宿主（供应商适配矩阵，P1）：
# 回复模型端点只有确属 DashScope 兼容协议时才被媒体能力继承——否则
# 「显示已配置但实际 API 不存在」的假就绪比「未配置」更有害。
_DASHSCOPE_COMPAT_HOSTS = (
    "dashscope.aliyuncs.com",
    "dashscope.aliyuncs-inc.com",
    "agentscope.io",
)


def _host_dashscope_compatible(base_url: str) -> bool:
    from urllib.parse import urlsplit

    try:
        host = (urlsplit(base_url).hostname or "").lower()
    except ValueError:
        return False
    return any(host == h or host.endswith("." + h) for h in _DASHSCOPE_COMPAT_HOSTS)


def _resolve_capability_env(env: Mapping[str, str], cap: str) -> tuple[str, str, str]:
    """能力端点/凭据/供应商解析，三级回退（带供应商适配矩阵）：

    1. 专属覆盖：``WHALE_<CAP>_BASE_URL`` / ``WHALE_<CAP>_API_KEY``
    2. 共享媒体平台：``WHALE_MEDIA_BASE_URL`` / ``WHALE_MEDIA_API_KEY``
    3. 回复模型同款：``DEEPSEEK_BASE_URL`` / ``DEEPSEEK_API_KEY``——
       **仅当端点与媒体协议兼容**（DashScope 兼容宿主矩阵）或用户显式声明
       ``WHALE_<CAP>_PROVIDER`` / ``WHALE_MEDIA_PROVIDER`` 时才继承；
       不兼容则返回空（=未配置，诚实显示，不假装就绪）。

    返回 (base_url, api_key, provider_kind)。provider_kind ∈ {"dashscope", "openai"}；
    dashscope = DashScope 兼容协议（阿里云百炼 / AgentScope 平台同路径）。
    """
    kind = (env.get(f"WHALE_{cap}_PROVIDER") or env.get("WHALE_MEDIA_PROVIDER") or "dashscope").strip().lower()
    if kind not in ("dashscope", "openai"):
        kind = "dashscope"
    base = (env.get(f"WHALE_{cap}_BASE_URL") or env.get("WHALE_MEDIA_BASE_URL") or "").strip()
    key = (env.get(f"WHALE_{cap}_API_KEY") or env.get("WHALE_MEDIA_API_KEY") or "").strip()
    if not base:
        # 第 3 级：回复模型同款——协议兼容或用户显式声明供应商才继承
        declared = bool(env.get(f"WHALE_{cap}_PROVIDER") or env.get("WHALE_MEDIA_PROVIDER"))
        reply_base = (env.get("DEEPSEEK_BASE_URL") or "").strip()
        if reply_base and (declared or _host_dashscope_compatible(reply_base)):
            base = reply_base
            key = key or (env.get("DEEPSEEK_API_KEY") or "").strip()
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

    # MiniMax（图像 + 视频 + 语音）：WHALE_MINIMAX_API_KEY 配了才注册
    # （key 由 owner 自填）。位置必须在 image/speech/video 装配之前：
    # 三者的可选模型清单与默认模型都要并入 MiniMax 模型。
    minimax_key = (env.get("WHALE_MINIMAX_API_KEY") or "").strip()
    minimax_base = (env.get("WHALE_MINIMAX_BASE_URL") or "").strip() or "https://api.minimaxi.com"
    minimax = None
    if minimax_key:
        from whale_core.plugins.minimax_media import (
            MiniMaxImageProvider, MiniMaxTTSProvider, MiniMaxVideoProvider)
        minimax = (MiniMaxVideoProvider, MiniMaxTTSProvider, MiniMaxImageProvider)
        out.notes.append("MiniMax 已配置（图像 + 视频 + 语音模型可选）")

    # 2026-11-01 owner 决策：生图/视频默认二次元动漫画风（群友实测反馈
    # 写实向出图违和）。对方提示词里已带风格词（写实/真人/油画…）时以其为准。
    media_style = (env.get("WHALE_MEDIA_STYLE") or "二次元动漫风格").strip()
    import re as _re
    _STYLE_CUES_RE = _re.compile(
        r"写实|真人|照片|photoreal|realistic|cosplay|油画|水彩|素描|像素|"
        r"二次元|动漫|漫画|插画|古风|国风|赛博|3[dD]|风格", _re.I)

    def _with_media_style(prompt: str) -> str:
        p = (prompt or "").strip()
        if not p or not media_style or _STYLE_CUES_RE.search(p):
            return p
        return p + "，" + media_style

    image_base, image_key, image_kind = _resolve_capability_env(env, "IMAGE")
    if image_base or minimax is not None:
        mm_image_name = "minimax-" + (env.get("WHALE_MINIMAX_IMAGE_MODEL") or "image-01").strip()
        # 默认模型：显式 WHALE_IMAGE_MODEL 优先；其次 MiniMax（owner 决策：
        # 画图模型切成 minimax）；再回退供应商默认。
        vendor_default = "qwen-image-3.0" if image_kind == "dashscope" else "dall-e-3"
        image_default = (env.get("WHALE_IMAGE_MODEL") or "").strip() \
            or (mm_image_name if minimax is not None else vendor_default)
        # 候选清单：默认 + 显式列表 + 供应商默认（默认被切去 MiniMax 时
        # qwen 等仍保留为可选项，不悄悄消失）
        seen_models: set[str] = set()
        image_models = []
        for m in [image_default] + [
                x.strip() for x in (env.get("WHALE_IMAGE_MODELS") or "").split(",")
            ] + ([vendor_default] if image_base else []):
            if m and m not in seen_models:
                seen_models.add(m)
                image_models.append(m)
        image_providers = {}
        for m in image_models:
            if m == mm_image_name or m.startswith("minimax-"):
                continue  # MiniMax 模型在下方单独装配
            if not image_base:
                continue
            if image_kind == "dashscope":
                from whale_core.plugins.dashscope_media import DashScopeImageProvider
                image_providers[m] = DashScopeImageProvider(base_url=image_base, api_key=image_key, model=m)
            else:
                image_providers[m] = OpenAIImagesProvider(base_url=image_base, api_key=image_key, model=m)
        if minimax is not None:
            image_providers[mm_image_name] = minimax[2](
                api_key=minimax_key, base_url=minimax_base,
                model=mm_image_name.removeprefix("minimax-"))
        if image_default not in image_providers:
            # 显式指定的默认模型不可用（如 key 未配）→ 回退到任一可用模型
            if not image_providers:
                image_default = ""
            else:
                out.notes.append(f"图像默认模型 {image_default} 不可用，回退 "
                                 + sorted(image_providers)[0])
                image_default = sorted(image_providers)[0]
        if image_default:
            image_provider = image_providers[image_default]
            register_image_gen(registry, provider=image_provider)
            default_size = "1328*1328" if image_kind == "dashscope" else "1024x1024"
            def generate(args):
                require("media_image")  # 注册被撤销即拒绝（保 disable 语义）
                # 2026-11-01：允许指定生图模型（owner 决策）；未知模型诚实报错并列可用
                model = str(args.get("model") or "").strip()
                provider = image_provider
                if model:
                    provider = image_providers.get(model)
                    if provider is None:
                        raise ValueError(
                            f"未知生图模型 {model!r}（可用：{', '.join(sorted(image_providers))}）")
                result = provider.generate(
                    _with_media_style(str(args.get("prompt") or args.get("query") or args.get("goal") or "")),
                    size=str(args.get("size") or default_size), n=int(args.get("n") or 1))
                # 只回本地工件引用（2026-11-01「恶意链接」事件：裸签名 URL 被
                # 模型贴进 QQ 正文触发安全警告且外部打不开）。图片由系统作为
                # 真实图片消息发出，模型不应在正文里贴任何链接/路径。
                return json.dumps({"task_id":result.task_id, "status":result.status,
                                   "model": model or image_default,
                                   "images":[save(r) for r in result.images],
                                   "note":"图片会由系统自动发送给对方；回复正文里"
                                          "不要包含任何图片链接或文件路径。"},
                                  ensure_ascii=False)
            tools.register(ToolSpec(name="generate_image", capability="media.image.generate", plugin_id="media_image",
                                    description="按提示生成图片（默认二次元动漫画风，对方要求别的风格时以其为准）；"
                                    "可用 model 参数指定模型（"
                                    + "、".join(sorted(image_providers)) + "）", handler=generate))
            out.notes.append("图像生成已配置（可选模型：" + "、".join(sorted(image_providers)) + "）")

    speech_base, speech_key, speech_kind = _resolve_capability_env(env, "SPEECH")
    tts_providers: dict = {}
    tts_default = ""
    # 2026-11-01 owner 决策「qwen/ds 不走代理」：MiniMax TTS 不再依赖 dashscope
    # 基座存在——无 WHALE_MEDIA/SPEECH_BASE_URL 时也能独立挂载（仅 TTS；
    # MiniMax 无 ASR，语音识别随 dashscope 基座缺席而缺席，诚实不挂）。
    if speech_base or minimax is not None:
        mm_tts_model = (env.get("WHALE_MINIMAX_TTS_MODEL") or "speech-02-hd").strip()
        mm_tts_voice = (env.get("WHALE_MINIMAX_TTS_VOICE") or "female-tianmei").strip()
        if speech_base:
            common = {"base_url": speech_base, "api_key": speech_key}
            tts_default = env.get("WHALE_TTS_MODEL") or ("qwen-audio-3.0-tts-flash" if speech_kind == "dashscope" else "tts-1")
            if speech_kind == "dashscope":
                from whale_core.plugins.dashscope_media import DashScopeASRProvider, DashScopeTTSProvider
                asr = DashScopeASRProvider(**common, model=env.get("WHALE_ASR_MODEL") or "fun-asr-flash-2026-06-15")
                # 默认 + WHALE_TTS_MODELS 追加（音色不跨模型，每个模型各记自己的音色）
                tts_models = [tts_default] + [
                    m.strip() for m in (env.get("WHALE_TTS_MODELS") or "").split(",")
                    if m.strip() and m.strip() != tts_default]
                for m in tts_models:
                    voice = (env.get("WHALE_TTS_VOICE") or "longanlingxin") if m == tts_default else ""
                    tts_providers[m] = DashScopeTTSProvider(**common, model=m, voice=voice)
            else:
                asr = OpenAICompatibleASR(**common, model=env.get("WHALE_ASR_MODEL") or "whisper-1")
                tts_providers[tts_default] = OpenAICompatibleTTS(**common, model=tts_default, voice=env.get("WHALE_TTS_VOICE") or "alloy")
            register_speech(registry, asr=asr, tts=tts_providers[tts_default])
            def transcribe(args):
                encoded = str(args.get("audio_base64") or "")
                if not encoded or len(encoded)>35*1024*1024:
                    raise ValueError("audio_base64 required or exceeds limit")
                return require("speech_asr").transcribe(base64.b64decode(encoded, validate=True), lang=str(args.get("lang") or "zh"), format=str(args.get("format") or "wav"))
            tools.register(ToolSpec(name="transcribe_audio", capability="asr.transcribe", plugin_id="speech_asr", handler=transcribe))
        # MiniMax 语音并入可选模型（音色 id 是 MiniMax 平台体系，不跨 dashscope）
        if minimax is not None:
            tts_providers[mm_tts_model] = minimax[1](
                api_key=minimax_key, base_url=minimax_base,
                model=mm_tts_model, voice=mm_tts_voice)
        if not tts_default:
            tts_default = mm_tts_model if minimax is not None else ""
        elif tts_default not in tts_providers:
            # 显式默认不可用（如基座已撤）→ 回退任一可用模型
            out.notes.append(f"语音默认模型 {tts_default} 不可用，回退 "
                             + sorted(tts_providers)[0])
            tts_default = sorted(tts_providers)[0]
        if not speech_base and tts_default:
            # 仅 MiniMax：只挂 TTS 插件（无 ASR 可挂，诚实缺席）
            from whale_core.plugins.registry import PluginManifest
            from whale_core.plugins.speech import TTS_SYNTHESIZE
            registry.register(
                PluginManifest(
                    plugin_id="speech_tts", name="TTS（语音合成）", version="1.0.0",
                    kind="model", provides=(TTS_SYNTHESIZE,),
                    description="MiniMax t2a_v2 语音合成端点",
                ),
                provider=tts_providers[tts_default])
        if tts_default:
            def speak(args):
                # 2026-11-01：允许指定语音模型/音色（owner 决策）；未知模型诚实报错
                model = str(args.get("model") or "").strip()
                provider = tts_providers[tts_default]
                if model:
                    provider = tts_providers.get(model)
                    if provider is None:
                        raise ValueError(
                            f"未知语音模型 {model!r}（可用：{', '.join(sorted(tts_providers))}）")
                audio = provider.synthesize(str(args.get("text") or ""), voice=str(args.get("voice") or ""), rate=float(args.get("rate") or 1))
                if out.artifacts is None:
                    raise ValueError("artifact storage required for speech output")
                return json.dumps(out.artifacts.save_bytes(audio, "audio/mpeg").as_dict())
            tools.register(ToolSpec(name="synthesize_speech", capability="tts.synthesize", plugin_id="speech_tts",
                                    description="文本转语音；可用 model 参数指定模型（"
                                    + "、".join(sorted(tts_providers)) + "）", handler=speak))
            out.notes.append(
                ("语音识别与合成已配置" if speech_base
                 else "语音合成已配置（仅 MiniMax；无 ASR 基座，识别未挂载）")
                + "（语音可选模型：" + "、".join(sorted(tts_providers)) + "）")

    video_base, video_key, _video_kind = _resolve_capability_env(env, "VIDEO")
    # 2026-11-01 owner 决策「不走代理」：MiniMax 视频不再依赖 dashscope 基座——
    # 无 WHALE_MEDIA/VIDEO_BASE_URL 时独立挂载，happyhorse/wan 随基座缺席而缺席。
    if video_base or minimax is not None:
        mm_video_model = (env.get("WHALE_MINIMAX_VIDEO_MODEL") or "MiniMax-Hailuo-2.3").strip()
        video_model = (env.get("WHALE_VIDEO_MODEL") or "").strip() \
            or (mm_video_model if (minimax is not None and not video_base)
                else "happyhorse-1.1-t2v")
        video_models = [video_model] + [
            m.strip() for m in (env.get("WHALE_VIDEO_MODELS") or "").split(",")
            if m.strip() and m.strip() != video_model]
        video_providers = {}
        for m in video_models:
            if m in ("MiniMax-Hailuo-2.3", "MiniMax-Hailuo-02", "T2V-01",
                     "T2V-01-Director"):
                continue  # MiniMax 模型在下方单独装配
            if not video_base:
                continue
            if "happyhorse" in m:
                from whale_core.plugins.media_video import DashScopeHappyHorseVideoProvider
                video_providers[m] = DashScopeHappyHorseVideoProvider(base_url=video_base,
                    api_key=video_key or env.get("DASHSCOPE_API_KEY", ""), model=m)
            else:
                video_providers[m] = DashScopeWanVideoProvider(base_url=video_base,
                    api_key=video_key or env.get("DASHSCOPE_API_KEY", ""), model=m)
        if minimax is not None:
            video_providers[mm_video_model] = minimax[0](
                api_key=minimax_key, base_url=minimax_base, model=mm_video_model)
        if video_model not in video_providers:
            # 显式默认不可用（如基座已撤）→ 回退任一可用模型
            out.notes.append(f"视频默认模型 {video_model} 不可用，回退 "
                             + sorted(video_providers)[0])
            video_model = sorted(video_providers)[0]
        provider = video_providers[video_model]
        register_video_gen(registry, provider=provider)
        out.video_manager = VideoTaskManager(store, provider, artifact_store=out.artifacts,
                                             min_poll_interval=15,
                                             providers=video_providers,
                                             default_model=video_model)
        def video(args):
            require("media_video")
            # 2026-11-01：允许指定视频模型（owner 决策）；未知模型诚实报错并列可用
            model = str(args.get("model") or "").strip()
            task = out.video_manager.submit(
                _with_media_style(str(args.get("prompt") or args.get("query") or "")),
                duration_s=int(args.get("duration_s") or 5), size=str(args.get("size") or "1280x720"),
                model=model)
            return json.dumps(task.as_dict(), ensure_ascii=False)
        tools.register(ToolSpec(name="generate_video", capability="media.video.generate", plugin_id="media_video",
                                description="按提示生成视频（异步任务；默认二次元动漫画风，"
                                "对方要求别的风格时以其为准）；可用 model 参数指定模型（"
                                + "、".join(sorted(video_providers)) + "）", handler=video))
        out.notes.append("异步视频生成已配置（可选模型：" + "、".join(sorted(video_providers)) + "）")

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
        out.notes.append("whale-io PCM 语音桥已配置（浏览器低延迟通话；协议镜像层已验证，真机联调待安排）")
    return out
