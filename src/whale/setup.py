"""First-run setup: model presets, one-step model connection, network for phones.

The goal is that a non-developer can go from "installed" to "talking to an AI
that remembers me" by picking a provider and pasting one key. Preset model
names are defaults only: after creating the provider we read its live
``/models`` list and pick the best available match per role, so presets keep
working when vendors rename models.
"""

from __future__ import annotations

import asyncio
import re
import socket
from typing import Any

import httpx
from pydantic import BaseModel, Field

from .kernel.errors import WhaleError
from .link.protocol import Client, NoParams

PRESETS: list[dict[str, Any]] = [
    {"id": "deepseek", "name": "DeepSeek 深度求索", "base_url": "https://api.deepseek.com/v1",
     "chat_model": "deepseek-flash", "fast_model": "deepseek-flash",
     "prefer": {"chat": [r"^deepseek-flash$", r"^deepseek-v\d.*pro", r"^deepseek-chat$"],
                "fast": [r"^deepseek-flash$", r"flash", r"^deepseek-chat$"]},
     "key_url": "https://platform.deepseek.com/api_keys", "note": "国内直连，便宜好用，适合日常聊天"},
    {"id": "qwen", "name": "通义千问（阿里云百炼）", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
     "chat_model": "qwen-plus", "fast_model": "qwen-turbo", "vision_model": "qwen-vl-plus",
     "embedding_model": "text-embedding-v4",
     "prefer": {"chat": [r"^qwen-plus$", r"^qwen3[\w.-]*-plus$", r"^qwen-max$"],
                "fast": [r"^qwen-flash$", r"^qwen-turbo$", r"^qwen-plus$"],
                "vision": [r"^qwen-vl-plus$", r"^qwen3?-vl"]},
     "key_url": "https://bailian.console.aliyun.com/?apiKey=1", "note": "一个 Key 同时支持聊天、看图和语义记忆"},
    {"id": "kimi", "name": "Kimi（月之暗面）", "base_url": "https://api.moonshot.cn/v1",
     "chat_model": "kimi-k3", "fast_model": "kimi-k3", "temperature": None,
     "prefer": {"chat": [r"^kimi-k\d+$", r"^kimi-"], "fast": [r"turbo", r"^kimi-k\d+$"]},
     "key_url": "https://platform.moonshot.cn/console/api-keys", "note": "长上下文，原生看图"},
    {"id": "zhipu", "name": "智谱 GLM", "base_url": "https://open.bigmodel.cn/api/paas/v4",
     "chat_model": "glm-5.1", "fast_model": "glm-4.5-flash", "vision_model": "glm-4.5v",
     "embedding_model": "embedding-3",
     "prefer": {"chat": [r"^glm-5[\d.]*$", r"^glm-4\.\d$"], "fast": [r"flash", r"air"], "vision": [r"v$", r"vision"]},
     "key_url": "https://open.bigmodel.cn/usercenter/proj-mgmt/apikeys", "note": "有免费的 flash 模型"},
    {"id": "siliconflow", "name": "硅基流动 SiliconFlow", "base_url": "https://api.siliconflow.cn/v1",
     "chat_model": "deepseek-ai/DeepSeek-V3", "fast_model": "Qwen/Qwen3-8B", "embedding_model": "BAAI/bge-m3",
     "prefer": {"chat": [r"deepseek-ai/DeepSeek-V\d", r"Qwen/Qwen3.*Instruct", r"moonshotai/Kimi"],
                "fast": [r"Qwen/Qwen3-8B", r"Qwen/Qwen3-14B", r"Qwen/Qwen2\.5-7B"]},
     "key_url": "https://cloud.siliconflow.cn/account/ak", "note": "一个平台汇集多家开源模型，含免费模型"},
    {"id": "openai", "name": "OpenAI", "base_url": "https://api.openai.com/v1",
     "chat_model": "gpt-5-mini", "fast_model": "gpt-5-nano", "vision_model": "gpt-5-mini",
     "embedding_model": "text-embedding-3-small",
     "prefer": {"chat": [r"^gpt-5(\.\d+)?-mini$", r"^gpt-5(\.\d+)?$", r"^gpt-4o-mini$"],
                "fast": [r"^gpt-5(\.\d+)?-nano$", r"^gpt-5(\.\d+)?-mini$", r"^gpt-4o-mini$"]},
     "key_url": "https://platform.openai.com/api-keys", "note": "需要能访问 OpenAI 的网络"},
    {"id": "openrouter", "name": "OpenRouter", "base_url": "https://openrouter.ai/api/v1",
     "chat_model": "deepseek/deepseek-chat", "fast_model": "deepseek/deepseek-chat",
     "prefer": {"chat": [r"^deepseek/", r"^qwen/", r"^openai/gpt-5"], "fast": [r"flash", r"mini", r"^deepseek/"]},
     "key_url": "https://openrouter.ai/keys", "note": "一个 Key 用遍海外主流模型"},
    {"id": "ollama", "name": "Ollama（本机模型）", "base_url": "http://127.0.0.1:11434/v1", "local": True,
     "chat_model": "", "fast_model": "",
     "prefer": {"chat": [r"qwen", r"llama", r"deepseek", r"glm", r"."], "fast": [r"qwen", r"llama", r"."]},
     "key_url": "https://ollama.com/download", "note": "完全离线、数据不出电脑；需要先安装 Ollama 并下载一个模型"},
]

EMBED_RE = re.compile(r"embed|bge|rerank|whisper|tts|speech|audio|image|dall|moderation", re.I)
VISION_RE = re.compile(r"-vl|vision|ocr|omni|\dv$", re.I)


def public_preset(p: dict[str, Any]) -> dict[str, Any]:
    return {"id": p["id"], "name": p["name"], "base_url": p["base_url"], "chat_model": p["chat_model"],
            "fast_model": p["fast_model"], "vision_model": p.get("vision_model"),
            "embedding_model": p.get("embedding_model"), "key_url": p.get("key_url"),
            "needs_key": not p.get("local"), "local": bool(p.get("local")), "note": p.get("note", "")}


def pick(available: list[str], wanted: str, patterns: list[str], *, vision: bool = False) -> str:
    """Keep ``wanted`` if the provider lists it, else the first id matching a preference pattern.

    Embedding/audio models are never picked for a chat role; vision-only variants only for ``vision``.
    """
    if not available:
        return wanted
    if wanted and wanted in available:
        return wanted
    pool = [m for m in available if not EMBED_RE.search(m) and (vision or not VISION_RE.search(m))]
    for pattern in patterns:
        rx = re.compile(pattern, re.I)
        for m in sorted(pool):
            if rx.search(m):
                return m
    return wanted or (pool[0] if pool else "")


def lan_addresses() -> list[str]:
    found: list[str] = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("10.255.255.255", 1))  # no packet is sent; picks the default-route interface
            found.append(s.getsockname()[0])
        finally:
            s.close()
    except OSError:
        pass
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            if ip not in found:
                found.append(ip)
    except OSError:
        pass
    return [ip for ip in found if not ip.startswith(("127.", "169.254.", "0."))]


class QuickModel(BaseModel):
    preset: str
    api_key: str | None = Field(None, max_length=400)
    base_url: str | None = Field(None, max_length=300)
    chat_model: str | None = Field(None, max_length=120)


class Complete(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=24)


class Lan(BaseModel):
    enabled: bool


class Setup:
    def __init__(self, app: Any) -> None:
        self.app = app
        # set by the CLI runner: () -> None, restarts the server in-process
        self.restart_hook: Any = None
        self.bound: tuple[str, int] | None = None
        self._register()

    # -- network ----------------------------------------------------------------------------------
    def network(self) -> dict[str, Any]:
        cfg = self.app.config.value.server
        lan = cfg.host in ("0.0.0.0", "::", "")
        port = self.bound[1] if self.bound else cfg.port
        addresses = lan_addresses()
        running_lan = self.bound is not None and self.bound[0] in ("0.0.0.0", "::", "")
        return {"host": cfg.host, "port": port, "lan_enabled": lan, "addresses": addresses,
                "urls": [f"http://{ip}:{port}" for ip in addresses] if (running_lan or (lan and not self.bound)) else [],
                "restart_required": self.bound is not None and (lan != running_lan or self.bound[1] != cfg.port)}

    # -- status -----------------------------------------------------------------------------------
    def status(self) -> dict[str, Any]:
        app = self.app
        models = app.models.status()
        channels = app.config.value.channels
        plugins = app.config.value.plugins
        qq = any(c.get("enabled") and str(c.get("kind") or cid) in ("qq", "onebot", "weq")
                 for cid, c in channels.items()) or bool(plugins.get("qq", {}).get("enabled"))
        phone = any(not d.get("revoked") for d in app.devices.list())
        persona = app.config.value.agent.persona_id != "default" or len(app.personas.list()) > 1
        return {"completed": bool(app.db.kv_get("setup", "completed", False)),
                "steps": {"model": not models["offline"], "persona": persona, "phone": phone, "qq": qq},
                "models_offline": models["offline"], "network": self.network()}

    async def quick_model(self, p: QuickModel) -> dict[str, Any]:
        preset = next((x for x in PRESETS if x["id"] == p.preset), None)
        if preset is None:
            raise WhaleError("invalid_params", f"unknown preset {p.preset}")
        if not preset.get("local") and not (p.api_key or self.app.config.secret(f"provider.{preset['id']}")):
            raise WhaleError("invalid_params", "请填写 API Key")
        base = (p.base_url or preset["base_url"]).rstrip("/")
        providers = [x.model_dump() for x in self.app.config.value.models.providers if x.id != preset["id"]]
        providers.append({"id": preset["id"], "kind": "openai", "base_url": base, "label": preset["name"]})
        self.app.config.update({"models": {"providers": providers}})
        if p.api_key:
            self.app.config.set_secret(f"provider.{preset['id']}", p.api_key.strip())
        available = await self._list_models(base, self.app.config.secret(f"provider.{preset['id']}"))
        if preset.get("local") and not available:
            return {"ok": False, "test": {"ok": False, "error": "没有连上 Ollama，或者还没有下载模型（试试 ollama pull qwen3）"},
                    "status": self.app.models.status()}
        prefer = preset.get("prefer", {})
        chat = p.chat_model or pick(available, preset["chat_model"], prefer.get("chat", []))
        fast = pick(available, preset["fast_model"], prefer.get("fast", [])) or chat
        temp = preset["temperature"] if "temperature" in preset else 0.7
        routes: dict[str, Any] = {
            "chat": {"provider": preset["id"], "model": chat, "temperature": temp},
            "fast": {"provider": preset["id"], "model": fast, "temperature": None if temp is None else 0.2},
        }
        if preset.get("vision_model"):
            routes["vision"] = {"provider": preset["id"],
                                "model": pick(available, preset["vision_model"], prefer.get("vision", []), vision=True),
                                "temperature": temp}
        if preset.get("embedding_model"):
            routes["embedding"] = {"provider": preset["id"], "model": preset["embedding_model"]}
        self.app.config.update({"models": routes})
        test = await self.app.models.test("chat")
        return {"ok": bool(test.get("ok")), "test": test, "chosen": {k: v["model"] for k, v in routes.items()},
                "status": self.app.models.status()}

    async def _list_models(self, base: str, key: str) -> list[str]:
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        try:
            async with httpx.AsyncClient(timeout=8, transport=self.app.http_transport) as client:
                r = await client.get(base + "/models", headers=headers)
                if r.status_code >= 400:
                    return []
                return [str(x.get("id")) for x in (r.json().get("data") or []) if x.get("id")]
        except (httpx.HTTPError, ValueError):
            return []

    # -- RPC --------------------------------------------------------------------------------------
    def _register(self) -> None:
        m = self.app.rpc.method

        @m("setup.status", NoParams, "First-run progress", scope="client")
        async def setup_status(p: NoParams, c: Client | None) -> Any:
            return self.status()

        @m("models.presets", NoParams, "Model provider presets for one-step setup")
        async def models_presets(p: NoParams, c: Client | None) -> Any:
            return {"presets": [public_preset(x) for x in PRESETS]}

        @m("setup.quick_model", QuickModel, "Create a provider from a preset, route all roles, test chat")
        async def setup_quick(p: QuickModel, c: Client | None) -> Any:
            return await self.quick_model(p)

        @m("setup.complete", Complete, "Mark the first-run wizard done")
        async def setup_complete(p: Complete, c: Client | None) -> Any:
            if p.name:
                self.app.config.update({"agent": {"name": p.name.strip()}})
            self.app.db.kv_set("setup", "completed", True)
            await self.app.bus.emit("setup.completed", {})
            return {"completed": True}

        @m("system.network", NoParams, "Addresses phones can use to reach this Whale")
        async def system_network(p: NoParams, c: Client | None) -> Any:
            return self.network()

        @m("system.set_lan", Lan, "Allow (or stop) devices on the local network from connecting")
        async def system_set_lan(p: Lan, c: Client | None) -> Any:
            self.app.config.update({"server": {"host": "0.0.0.0" if p.enabled else "127.0.0.1"}})
            return self.network()

        @m("system.restart", NoParams, "Restart the server (applies network changes)")
        async def system_restart(p: NoParams, c: Client | None) -> Any:
            if self.restart_hook is None:
                raise WhaleError("unavailable", "当前运行方式不支持自动重启，请手动重启 Whale")
            asyncio.get_running_loop().call_later(0.5, self.restart_hook)
            return {"restarting": True}
