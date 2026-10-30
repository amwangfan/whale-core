"""Official plugin composition. Every runtime capability follows manager state.

Factories are trusted code, never imported from an arbitrary manifest. Secrets are
split by PluginManager; disabled providers are resolved afresh at call boundaries.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

from whale_core.core.deepseek import DeepSeekCompanion, ModelError
from whale_core.core.diagnostics import InstrumentedProvider
from whale_core.core.runtime import ToolRegistry, ToolSpec
from whale_core.plugins.manifest import PluginManifest
from whale_core.plugins.registry import PluginRegistry
from whale_core.plugins.model_provider import require_chat_model


def schema(**fields):
    properties = {}
    for name, value in fields.items():
        properties[name] = {
            "type": "boolean"
            if isinstance(value, bool)
            else "number"
            if isinstance(value, (float, int))
            else "string",
            "default": value,
        }
        if any(
            word in name.lower()
            for word in ("key", "token", "secret", "password", "credential", "cookie")
        ):
            properties[name].pop("default")
            properties[name]["format"] = "secret"
    if "provider" in properties:
        properties["provider"]["enum"] = ["tavily", "brave", "duckduckgo"]
    return {"type": "object", "properties": properties}


class ManagedChatModel:
    """Resolve each call; disabling a model cannot leave a live stale reference."""

    def __init__(self, registry, store, manager):
        self.registry, self.store, self.manager = registry, store, manager

    def _provider(self):
        self.manager.refresh()
        try:
            return require_chat_model(self.registry)
        except LookupError as exc:
            raise ModelError(
                "config", "No enabled chat provider; open Settings"
            ) from exc

    def __getattr__(self, name):
        return getattr(self._provider(), name)

    def reply(self, text, *, persona="", memories=(), history=()):
        from whale_core.core.budget import reserve

        reserve(self.store, "model_calls")
        model = self._provider()
        try:
            return InstrumentedProvider("chat", model, self.store).reply(
                text, persona=persona, memories=memories, history=history
            )
        except ModelError as exc:
            if exc.kind not in ("network", "server", "rate_limited"):
                raise
            for _pid, fallback in self.registry.providers_for("llm.chat"):
                if fallback is not None and fallback is not model:
                    reserve(self.store, "model_calls")
                    return InstrumentedProvider("fallback", fallback, self.store).reply(
                        text, persona=persona, memories=memories, history=history
                    )
            raise


class ManagedTools(ToolRegistry):
    def __init__(self, registry, manager):
        super().__init__()
        self.registry, self.manager = registry, manager

    def specs(self):
        self.manager.refresh()
        return [
            t
            for t in super().specs()
            if not t.plugin_id or self.registry.manifest(t.plugin_id)
        ]

    def get(self, name):
        self.manager.refresh()
        t = super().get(name)
        return (
            t
            if t and (not t.plugin_id or self.registry.manifest(t.plugin_id))
            else None
        )

    def for_capability(self, capability):
        return [t for t in self.specs() if t.capability == capability]


def wire_builtins(manager, store, env, profile, legacy_config):
    registry = manager.registry
    tools = ManagedTools(registry, manager)
    notes = []

    def load(
        pid,
        name,
        capabilities,
        defaults,
        factory,
        *,
        enabled=False,
        kind="model",
        action="",
    ):
        saved = store.get(manager._state_key(pid))
        manager.configure_factory(pid, factory, defaults=defaults)
        manifest = PluginManifest(
            plugin_id=pid,
            name=name,
            version="1.0.0",
            kind=kind,
            provides=tuple(capabilities) if kind in ("model", "processor") else (),
            actions=(action,) if action else (),
            action_risk={action: "read_only"} if action else {},
            description="Official configurable provider",
        )
        manager.load_manifest(manifest, config_schema=schema(**defaults))
        if saved is None and enabled:
            manager.enable(pid)

    local = profile == "local"
    pid = "local_model" if local else "deepseek"
    old = legacy_config.get("model") or {}
    model_config = {
        "base_url": old.get("base_url")
        or env.get("LOCAL_MODEL_BASE_URL" if local else "DEEPSEEK_BASE_URL")
        or ("http://127.0.0.1:11434/v1" if local else "https://api.deepseek.com"),
        "model": old.get("model")
        or env.get("LOCAL_MODEL_NAME" if local else "DEEPSEEK_MODEL")
        or ("llama3.2" if local else "deepseek-chat"),
        "api_key": env.get(
            str(
                old.get("api_key_env")
                or ("LOCAL_MODEL_API_KEY" if local else "DEEPSEEK_API_KEY")
            ),
            "",
        ),
        "timeout": 25.0,
    }
    load(
        pid,
        "本地模型" if local else "云端模型",
        ["llm.chat"],
        model_config,
        lambda: {"provider": DeepSeekCompanion(**manager.runtime_config(pid))},
        enabled=True,
    )
    notes.append(profile + " 档：llm.chat 已注册；配置与启停由插件管理器统一管理")
    if (
        not local
        and not model_config["api_key"]
        and not manager.runtime_config(pid).get("api_key")
    ):
        notes.append("[warn] DEEPSEEK_API_KEY 未配置——模型调用会以 config 错误失败")

    load(
        "fallback_model",
        "备用文本模型",
        ["llm.chat"],
        {
            "base_url": "https://api.deepseek.com",
            "api_key": "",
            "model": "deepseek-chat",
            "timeout": 25.0,
        },
        lambda: {
            "provider": DeepSeekCompanion(**manager.runtime_config("fallback_model"))
        },
    )
    load(
        "intent_model",
        "独立意图模型",
        ["llm.intent"],
        {
            "base_url": "https://api.deepseek.com",
            "api_key": "",
            "model": "deepseek-chat",
            "timeout": 15.0,
        },
        lambda: {
            "provider": DeepSeekCompanion(**manager.runtime_config("intent_model"))
        },
    )

    from whale_core.plugins.web_search import register_web_search, select_provider
    from whale_core.plugins.web_fetch import register_web_fetch

    def search_factory():
        c = manager.runtime_config("web_search")
        provider = select_provider(
            {
                "WHALE_SEARCH_PROVIDER": c["provider"],
                "TAVILY_API_KEY": c["api_key"] if c["provider"] == "tavily" else "",
                "BRAVE_API_KEY": c["api_key"] if c["provider"] == "brave" else "",
            }
        )
        tmp = PluginRegistry()
        register_web_search(tmp, provider=provider)
        return {"executor": tmp.executor("web_search")}

    search_name = env.get("WHALE_SEARCH_PROVIDER") or (
        "tavily"
        if env.get("TAVILY_API_KEY")
        else "brave"
        if env.get("BRAVE_API_KEY")
        else "duckduckgo"
    )
    load(
        "web_search",
        "网页搜索",
        [],
        {
            "provider": search_name,
            "api_key": env.get("TAVILY_API_KEY") or env.get("BRAVE_API_KEY") or "",
        },
        search_factory,
        enabled=bool(env.get("TAVILY_API_KEY") or env.get("BRAVE_API_KEY")),
        kind="output",
        action="search",
    )

    def fetch_factory():
        tmp = PluginRegistry()
        register_web_fetch(tmp)
        return {"executor": tmp.executor("web_fetch")}

    load(
        "web_fetch",
        "网页阅读",
        [],
        {},
        fetch_factory,
        enabled=True,
        kind="output",
        action="fetch",
    )

    def search(args):
        executor = registry.executor("web_search")
        hits = executor.provider.search(
            str(args.get("query") or ""), limit=int(args.get("limit", 5))
        )
        return (
            "\n".join(f"{h.title} — {h.snippet}（{h.url}）" for h in hits)
            or "没有找到结果"
        )

    def fetch(args):
        executor = registry.executor("web_fetch")
        result = executor._fetcher.fetch(str(args.get("url") or ""))
        return result.title + "\n" + result.final_url + "\n" + result.text

    tools.register(
        ToolSpec(
            "search_web",
            "web.search",
            "搜索公开网页（query, limit）",
            plugin_id="web_search",
            handler=search,
        )
    )
    tools.register(
        ToolSpec(
            "fetch_url",
            "web.fetch",
            "读取网页（url）",
            plugin_id="web_fetch",
            handler=fetch,
        )
    )

    from whale_core.plugins.speech import OpenAICompatibleASR, OpenAICompatibleTTS
    from whale_core.plugins.media_image import OpenAIImagesProvider
    from whale_core.plugins.media_video import HttpVideoProvider, DashScopeVideoProvider
    from whale_core.plugins.bridge import HttpBridgeProvider
    from whale_core.plugins.embedding import OpenAIEmbeddingProvider

    for plugin, title, capability, cls, default_model in (
        ("speech_asr", "语音识别", "asr.transcribe", OpenAICompatibleASR, "whisper-1"),
        ("speech_tts", "语音合成", "tts.synthesize", OpenAICompatibleTTS, "tts-1"),
        (
            "media_image",
            "图片生成",
            "media.image.generate",
            OpenAIImagesProvider,
            "gpt-image-2",
        ),
        (
            "embedding",
            "语义检索",
            "embedding.embed",
            OpenAIEmbeddingProvider,
            "text-embedding-3-small",
        ),
    ):
        prefix = {
            "speech_asr": "ASR",
            "speech_tts": "TTS",
            "media_image": "IMAGE",
            "media_video": "VIDEO",
            "voice_bridge": "VOICE",
            "embedding": "EMBEDDING",
        }[plugin]
        key = env.get(prefix + "_API_KEY") or (
            env.get("OPENAI_API_KEY", "")
            if plugin not in ("media_video", "voice_bridge")
            else ""
        )
        defaults = {
            "api_key": key,
            "base_url": env.get(prefix + "_BASE_URL")
            or (
                "https://api.openai.com/v1"
                if plugin not in ("media_video", "voice_bridge")
                else "http://127.0.0.1:8090"
            ),
            "model": env.get(prefix + "_MODEL") or default_model,
            "timeout": 60.0,
        }

        def factory(p=plugin, provider_cls=cls):
            return {"provider": provider_cls(**manager.runtime_config(p))}

        load(plugin, title, [capability], defaults, factory, enabled=bool(key))

    def video_factory():
        config = manager.runtime_config("media_video")
        protocol = config.pop("protocol", "dashscope")
        cls = {"dashscope": DashScopeVideoProvider, "bridge": HttpVideoProvider}.get(
            protocol
        )
        if cls is None:
            raise ValueError("unknown video protocol")
        return {"provider": cls(**config)}

    load(
        "media_video",
        "视频生成 · 万相 / 自定义桥",
        ["media.video.generate"],
        {
            "protocol": env.get("VIDEO_PROTOCOL", "dashscope"),
            "base_url": env.get(
                "VIDEO_BASE_URL", "https://dashscope.aliyuncs.com/api/v1"
            ),
            "model": env.get("VIDEO_MODEL", "wan2.6-t2v"),
            "api_key": env.get("VIDEO_API_KEY", ""),
            "timeout": 60.0,
        },
        video_factory,
        enabled=bool(env.get("VIDEO_API_KEY")),
    )

    # Prefer the existing whale-io audio bridge's real TCP PCM protocol.
    # HTTP bridges remain an advanced alternative for other realtime providers.
    from whale_core.plugins.pcm_voice import PCMVoiceProvider

    def pcm_factory():
        return {"provider": PCMVoiceProvider(**manager.runtime_config("voice_bridge"))}

    defaults = {
        "host": env.get("BRIDGE_HOST", "127.0.0.1"),
        "port": int(env.get("BRIDGE_PORT", "18799")),
        "bridge_token": env.get("BRIDGE_TOKEN", ""),
        "tls": False,
        "public_ws_url": env.get("WHALE_VOICE_PUBLIC_URL", ""),
        "timeout": 15.0,
    }
    load(
        "voice_bridge", "实时语音 · whale-io", ["voice.realtime"], defaults, pcm_factory
    )
    # HTTP session negotiation is explicitly separate from the working PCM relay.
    load(
        "voice_http",
        "实时语音 · HTTP 桥",
        ["voice.session.http"],
        {
            "base_url": "http://127.0.0.1:8090",
            "api_key": "",
            "model": "",
            "timeout": 30.0,
        },
        lambda: {
            "provider": HttpBridgeProvider(**manager.runtime_config("voice_http"))
        },
    )

    def channel_factory():
        config = manager.runtime_config("channel_bridge")
        protocol = config.pop("protocol", "whale-io")
        if protocol == "http":
            return {
                "provider": HttpBridgeProvider(
                    **{
                        k: config[k]
                        for k in ("base_url", "api_key", "model", "timeout")
                    }
                )
            }
        if protocol != "whale-io":
            raise ValueError("unknown channel protocol")
        from whale_core.plugins.whale_io import WhaleIOChannelProvider

        names = (
            "dashboard_url",
            "dashboard_token",
            "ring_url",
            "ring_token",
            "weq_url",
            "weq_token",
            "weq_account",
            "owner_uid",
            "timeout",
        )
        return {"provider": WhaleIOChannelProvider(**{k: config[k] for k in names})}

    load(
        "channel_bridge",
        "手机 / 眼镜 / 消息投递",
        ["channel.bridge"],
        {
            "protocol": env.get("CHANNEL_PROTOCOL", "whale-io"),
            "base_url": env.get("CHANNEL_BASE_URL", "http://127.0.0.1:8091"),
            "api_key": env.get("CHANNEL_API_KEY", ""),
            "model": "",
            "timeout": 15.0,
            "target": env.get("CHANNEL_TARGET", ""),
            "dashboard_url": env.get("WHALE_DASHBOARD_URL", ""),
            "dashboard_token": env.get("WHALE_DASHBOARD_TOKEN", ""),
            "ring_url": env.get("WHALE_RING_URL", ""),
            "ring_token": env.get("SIGNAL_TOKEN", ""),
            "weq_url": env.get("WEQ_WEB_URL", ""),
            "weq_token": env.get("WEQ_WEB_TOKEN", ""),
            "weq_account": env.get("QQ_SELF_UIN", ""),
            "owner_uid": env.get("OWNER_UID", ""),
        },
        channel_factory,
        kind="processor",
    )

    # Read-only generation tools are explicit owner-requested calls; outbound sends
    # and shell are handled by the separate approved JobService, never this path.
    def image(args):
        p = registry.provider_of("media_image")
        result = p.generate(
            str(args.get("prompt") or args.get("query") or ""),
            size=str(args.get("size") or "1024x1024"),
            n=int(args.get("n", 1)),
        )
        return json.dumps(asdict(result), ensure_ascii=False)

    tools.register(
        ToolSpec(
            "generate_image",
            "media.image.generate",
            "生成图片（prompt,size,n）",
            plugin_id="media_image",
            handler=image,
        )
    )
    return tools, ManagedChatModel(registry, store, manager), notes
