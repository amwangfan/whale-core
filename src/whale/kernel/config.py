"""Configuration: one typed tree, editable at runtime, secrets kept apart.

``config.json`` holds everything visible in the console. ``secrets.json``
holds API keys and tokens; the API only ever reports whether a secret is set.
Every change goes through ``Config.update`` which validates, persists and
emits ``config.changed`` so components reconfigure without a restart.
"""

from __future__ import annotations

import json
import os
import secrets as pysecrets
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from .errors import WhaleError


class ProviderConfig(BaseModel):
    id: str
    kind: Literal["openai", "scripted"] = "openai"
    base_url: str = "http://127.0.0.1:11434/v1"
    label: str = ""
    # api key lives in secrets under "provider.<id>"


class ModelRoute(BaseModel):
    provider: str = ""
    model: str = ""
    temperature: float | None = 0.7   # null=不下发该参数（kimi-k3 只收默认温度，实测 0.7 被 400 拒）
    max_tokens: int = 1024
    reasoning_effort: str = ""   # 非空即透传（如 kimi-k3 的 "high"）；空=不下发该参数


class ModelsConfig(BaseModel):
    providers: list[ProviderConfig] = Field(default_factory=list)
    chat: ModelRoute = Field(default_factory=ModelRoute)
    fast: ModelRoute = Field(default_factory=lambda: ModelRoute(temperature=0.2, max_tokens=800))
    vision: ModelRoute = Field(default_factory=ModelRoute)
    embedding: ModelRoute = Field(default_factory=ModelRoute)
    daily_request_budget: int = 2000


class AgentConfig(BaseModel):
    name: str = "鲸鱼"
    persona_id: str = "default"
    timezone: str = "Asia/Shanghai"
    language: str = "zh-CN"
    max_steps: int = 12
    history_turns: int = 16
    context_budget_chars: int = 24000
    # 回复档位：fast=边想边流式输出；slow=完整缓冲想完一次性发（对齐 v2 两档慢档语义：
    # 渠道回复本就完成后投递，slow 档额外关闭 run.delta 流式增量）
    reply_gear: Literal["fast", "slow"] = "fast"


class MemoryConfig(BaseModel):
    extraction: bool = True
    consolidation_interval_min: int = 30
    half_life_days: float = 30.0
    recall_limit: int = 8


class AutonomyConfig(BaseModel):
    explore: bool = False
    explore_interval_min: int = 180
    interests: list[str] = Field(default_factory=list)
    proactive: bool = False
    quiet_hours: tuple[int, int] = (23, 8)
    proactive_daily_limit: int = 3


class ServerConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8080
    cors_origins: list[str] = Field(default_factory=list)


class CallBridgeConfig(BaseModel):
    """东京呼叫信令桥（whale-io call-bridge ring_hub）：url 构造期校验在 devices/callbridge.py。
    token 不入 config——放 secrets 的 devices.call_bridge.token。"""

    url: str = ""            # 如 http://100.x.y.z:18799（tailnet 内网）或 https 反代
    device_id: str = ""      # 桥侧设备名（addon 轮询用同一个，如手机在桥上的注册名）


class DevicesConfig(BaseModel):
    heartbeat_interval_s: int = 30
    heartbeat_timeout_s: int = 90
    challenge_ttl_s: int = 30
    receipt_grace_s: float = 3.0
    pair_ttl_s: int = 600
    digest_interval_min: int = 30
    call_bridge: CallBridgeConfig = Field(default_factory=CallBridgeConfig)


class PolicyConfig(BaseModel):
    # capability name or glob -> allow | ask | deny
    rules: dict[str, Literal["allow", "ask", "deny"]] = Field(default_factory=dict)


class GlassesConfig(BaseModel):
    # 眼镜面：日程提醒循环（默认关，与 v2 WHALE_SCHEDULE_REMIND 同纪律；静默时段沿用 autonomy.quiet_hours）
    schedule_remind: bool = False
    remind_interval_min: int = 5


class WhaleConfig(BaseModel):
    server: ServerConfig = Field(default_factory=ServerConfig)
    models: ModelsConfig = Field(default_factory=ModelsConfig)
    agent: AgentConfig = Field(default_factory=AgentConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    autonomy: AutonomyConfig = Field(default_factory=AutonomyConfig)
    policy: PolicyConfig = Field(default_factory=PolicyConfig)
    devices: DevicesConfig = Field(default_factory=DevicesConfig)
    glasses: GlassesConfig = Field(default_factory=GlassesConfig)
    plugins: dict[str, dict[str, Any]] = Field(default_factory=dict)  # plugin_id -> {"enabled":bool, ...}
    channels: dict[str, dict[str, Any]] = Field(default_factory=dict)  # channel_id -> {"kind": "onebot", "enabled": bool, ...}


def _deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _atomic_write(path: Path, data: dict[str, Any], private: bool = False) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    if private:
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
    os.replace(tmp, path)


class Config:
    def __init__(self, data_dir: str | Path | None, initial: dict[str, Any] | None = None) -> None:
        self.data_dir = Path(data_dir) if data_dir else None
        self._listeners: list[Any] = []
        raw: dict[str, Any] = {}
        self._secrets: dict[str, str] = {}
        if self.data_dir:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            if self.config_path.exists():
                raw = json.loads(self.config_path.read_text(encoding="utf-8"))
            if self.secrets_path.exists():
                self._secrets = json.loads(self.secrets_path.read_text(encoding="utf-8"))
        if initial:
            raw = _deep_merge(raw, initial)
        self.value = WhaleConfig.model_validate(raw)
        if "api_token" not in self._secrets:
            self._secrets["api_token"] = pysecrets.token_urlsafe(24)
            self._save_secrets()

    @property
    def config_path(self) -> Path:
        assert self.data_dir
        return self.data_dir / "config.json"

    @property
    def secrets_path(self) -> Path:
        assert self.data_dir
        return self.data_dir / "secrets.json"

    # -- read ---------------------------------------------------------------
    def public(self) -> dict[str, Any]:
        data = self.value.model_dump(mode="json")
        data["secrets"] = {k: True for k, v in self._secrets.items() if v and k != "api_token"}
        return data

    def secret(self, key: str) -> str:
        return self._secrets.get(key, "")

    @property
    def api_token(self) -> str:
        return self._secrets["api_token"]

    # -- write --------------------------------------------------------------
    def update(self, patch: dict[str, Any]) -> WhaleConfig:
        merged = _deep_merge(self.value.model_dump(mode="json"), patch)
        try:
            value = WhaleConfig.model_validate(merged)
        except ValidationError as exc:
            raise WhaleError(
                "invalid_params", "invalid configuration",
                details={"errors": json.loads(exc.json(include_url=False))},
            ) from exc
        self.value = value
        if self.data_dir:
            _atomic_write(self.config_path, value.model_dump(mode="json"))
        for listener in list(self._listeners):
            listener(patch)
        return value

    def set_secret(self, key: str, value: str | None) -> None:
        if key == "api_token" and not value:
            raise WhaleError("invalid_params", "api token cannot be empty")
        if value:
            self._secrets[key] = value
        else:
            self._secrets.pop(key, None)
        self._save_secrets()
        for listener in list(self._listeners):
            listener({"secrets": {key: bool(value)}})

    def on_change(self, listener: Any) -> None:
        self._listeners.append(listener)

    def _save_secrets(self) -> None:
        if self.data_dir:
            _atomic_write(self.secrets_path, self._secrets, private=True)
