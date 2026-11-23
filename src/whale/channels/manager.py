"""Channels 管理器：config.json ``channels`` 段驱动的渠道生命周期。

配置形态（渠道密钥在 secrets.json 的 ``channel.<id>.<key>`` 下，永不入 config）::

    {"channels": {"qq": {"kind": "onebot", "enabled": true,
                          "api_base": "http://127.0.0.1:3000", "owner_qq": "10001", ...}}}

注意：内置 QQ 插件托管的是同一套 OneBot 实现；同一个 QQ 账号不要同时经
``plugins.qq`` 与 ``channels.<id>`` 启用（能力名 ``qq.send_*`` 会互相覆盖）。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from ..kernel.errors import WhaleError
from ..link.protocol import Client, NoParams
from .base import Channel, CoreSink
from .onebot import OneBotChannel
from .qq import QQChannel
from .weq import WeQChannel

log = logging.getLogger("whale.channels")

# kind 缺省回退到渠道 id。kind="qq" 由 config.backend 选实现（onebot 默认 | weq）；
# "onebot"/"weq" 作为 kind 直写等价于固定 backend 的 qq。
KINDS: dict[str, type[Channel]] = {"onebot": OneBotChannel, "qq": QQChannel, "weq": WeQChannel}
BACKENDS: dict[str, type[Channel]] = {"onebot": OneBotChannel, "weq": WeQChannel}


def resolve_class(cid: str, cfg: dict[str, Any]) -> type[Channel] | None:
    kind = str(cfg.get("kind") or cid)
    if kind == "qq":
        return BACKENDS.get(str(cfg.get("backend") or "onebot"))
    return KINDS.get(kind)


class Channels:
    def __init__(self, app: Any) -> None:
        self.app = app
        self.channels: dict[str, Channel] = {}
        self._sinks: dict[str, CoreSink] = {}
        self._errors: dict[str, str] = {}
        app.config.on_change(self._on_config)
        m = app.rpc.method

        @m("channels.list", NoParams, "IO 渠道（QQ 等）的配置与运行状态")
        async def channels_list(p: NoParams, c: Client | None) -> Any:
            return {"channels": self.list(), "kinds": sorted(KINDS)}

    # -- lifecycle ---------------------------------------------------------------
    async def start(self) -> None:
        for cid, cfg in self.app.config.value.channels.items():
            if cfg.get("enabled"):
                await self._open(cid, cfg)

    async def stop(self) -> None:
        for cid in list(self.channels):
            await self._close(cid)

    def _on_config(self, patch: dict[str, Any]) -> None:
        if "channels" not in patch or not self.app.started:
            return
        try:
            asyncio.get_running_loop().create_task(self.resync())
        except RuntimeError:
            pass

    async def resync(self) -> None:
        """让运行中的渠道集合与 config.channels 一致（增/改/停）。"""
        want = {cid: cfg for cid, cfg in self.app.config.value.channels.items() if cfg.get("enabled")}
        for cid in list(self.channels):
            if cid not in want:
                await self._close(cid)
        for cid, cfg in want.items():
            await self._close(cid)          # 配置可能变了：一律重建，语义最简单也最诚实
            await self._open(cid, cfg)

    async def _open(self, cid: str, cfg: dict[str, Any]) -> None:
        kind = str(cfg.get("kind") or cid)
        cls = resolve_class(cid, cfg)
        if cls is None:
            self._errors[cid] = f"unknown channel kind: {kind}" + (
                f" backend: {cfg.get('backend')}" if kind == "qq" else "")
            log.warning("channel %s: %s", cid, self._errors[cid])
            return
        sink = CoreSink(self.app, cid)
        try:
            channel = cls(self.app, cid, {k: v for k, v in cfg.items() if k not in ("kind", "enabled")},
                          token=self.app.config.secret(f"channel.{cid}.access_token"),
                          web_token=self.app.config.secret(f"channel.{cid}.web_token"), sink=sink)
            await channel.open()
        except Exception as exc:
            self._errors[cid] = (f"{exc.code}: {exc.message}" if isinstance(exc, WhaleError)
                                 else repr(exc))[:300]
            log.warning("channel %s failed to open: %s", cid, self._errors[cid])
            await sink.close()
            return
        self._errors.pop(cid, None)
        self.channels[cid] = channel
        self._sinks[cid] = sink
        await self.app.bus.emit("channel.opened", {"id": cid, "kind": kind})

    async def _close(self, cid: str) -> None:
        channel = self.channels.pop(cid, None)
        sink = self._sinks.pop(cid, None)
        if channel is not None:
            try:
                await channel.close()
            except Exception:
                log.exception("channel %s close failed", cid)
        if sink is not None:
            await sink.close()
        if channel is not None:
            await self.app.bus.emit("channel.closed", {"id": cid})

    # -- views ----------------------------------------------------------------------
    def get(self, cid: str) -> Channel:
        channel = self.channels.get(cid)
        if channel is None:
            raise WhaleError("not_found", f"channel not running: {cid}")
        return channel

    def list(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for cid, cfg in self.app.config.value.channels.items():
            channel = self.channels.get(cid)
            if channel is not None:
                out.append({**channel.status(), "enabled": True})
            else:
                out.append({"id": cid, "kind": str(cfg.get("kind") or cid), "enabled": bool(cfg.get("enabled")),
                            "connected": False, "last_error": self._errors.get(cid, "")})
        return out
