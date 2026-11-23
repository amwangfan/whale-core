"""Composition root: builds every component and owns the lifecycle."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from . import __version__
from .agent.persona import Personas
from .agent.records import Conversations, RunLedger
from .agent.runtime import AgentRuntime
from .autonomy import Explorer, Proactive
from .capabilities import builtin
from .channels import Channels
from .capabilities.policy import Approvals, Policy
from .capabilities.spec import CapabilityRegistry
from .kernel.artifacts import Artifacts
from .kernel.config import Config
from .kernel.db import Database
from .kernel.events import EventBus, EventLog
from .link.protocol import LinkHub, RpcRegistry
from .memory.service import MemoryService
from .memory.store import MemoryStore
from .models.router import ModelRouter
from .plugins.manager import PluginManager
from .tasks import engine as task_engine
from .tasks.engine import TaskEngine
from .tasks.scheduler import Scheduler

log = logging.getLogger("whale")


class App:
    def __init__(self, data_dir: str | Path | None = None, config: dict[str, Any] | None = None) -> None:
        from .devices import DeviceService  # registers migrations
        from .server import api

        self.version = __version__
        self.data_dir = Path(data_dir) if data_dir else None
        self.config = Config(self.data_dir, config)
        self.db = Database(self.data_dir / "whale.db" if self.data_dir else ":memory:")
        self.db.migrate()
        self.bus = EventBus(EventLog(self.db))
        self.artifacts = Artifacts(self.db, self.data_dir)
        # Plugin webhooks: (plugin_id, path) -> async handler(request) -> Response, served at
        # /api/hooks/{plugin_id}/{path}. Unauthenticated by the core: the plugin verifies its caller.
        self.hooks: dict[tuple[str, str], Any] = {}
        # 渠道上下文注入与回合后置钩子（channel → callable）；眼镜面在此挂快照/日程抽取。
        self.context_providers: dict[str, Any] = {}
        self.turn_hooks: dict[str, Any] = {}
        # Optional httpx transport injected by tests/embedders; plugins build clients with it.
        self.http_transport: Any = None
        self.models = ModelRouter(self.config, self.db, self.bus)
        self.models.app = self   # 让模型面共享 app.http_transport（测试注入假传输）
        self.capabilities = CapabilityRegistry(self.bus)
        self.policy = Policy(self.config)
        self.approvals = Approvals(self.db, self.bus)
        self.memory = MemoryService(MemoryStore(self.db), self.models, self.bus, self.config)
        self.personas = Personas(self.db, self.bus)
        self.conversations = Conversations(self.db, self.bus)
        self.runs = RunLedger(self.db, self.bus)
        self.agent = AgentRuntime(self)
        self.scheduler = Scheduler(self.db, self.bus, self.config.value.agent.timezone)
        self.tasks = TaskEngine(self)
        self.proactive = Proactive(self)
        self.explorer = Explorer(self)
        self.hub = LinkHub(self)
        self.rpc = RpcRegistry()
        self.plugins = PluginManager(self)
        self.devices = DeviceService(self)
        self.channels = Channels(self)
        from .groups import GroupService

        self.groups = GroupService(self)
        builtin.register(self)
        task_engine.register(self)
        api.register(self)
        from .glasses import register as register_glasses

        register_glasses(self)
        self._register_jobs()
        self.config.on_change(self._on_config)
        self.started = False

    def _register_jobs(self) -> None:
        sch = self.scheduler
        sch.handler("memory.maintain", lambda args: self.memory.maintain())
        sch.handler("autonomy.explore", lambda args: self.explorer.explore(args.get("topic")))
        self.glasses.sync_remind_job()

    def _sync_system_jobs(self) -> None:
        cfg = self.config.value
        self.scheduler.ensure_system("sys_memory_maintain", "记忆整理与遗忘", "memory.maintain",
                                     {"every_min": max(5, cfg.memory.consolidation_interval_min)}, True)
        self.scheduler.ensure_system("sys_explore", "自主探索", "autonomy.explore",
                                     {"every_min": max(15, cfg.autonomy.explore_interval_min)},
                                     cfg.autonomy.explore)

    def _on_config(self, patch: dict[str, Any]) -> None:
        if "agent" in patch:
            self.scheduler.tz = self.config.value.agent.timezone
        if self.started and ("autonomy" in patch or "memory" in patch):
            self._sync_system_jobs()
        if self.started and "devices" in patch:
            self.scheduler.ensure_system("sys_phone_digest", "手机经历整理成记忆", "phone.digest",
                                         {"every_min": max(5, self.config.value.devices.digest_interval_min)}, True)
        if self.started and "glasses" in patch:
            self.glasses.sync_remind_job()
        self.bus.publish_event("config.changed", {"sections": sorted(patch.keys())})

    async def start(self) -> None:
        interrupted = self.runs.recover()
        self.approvals.expire_orphans()
        await self.tasks.recover()
        if interrupted:
            log.info("marked %d runs interrupted after restart", interrupted)
        self.memory.start()
        self._sync_system_jobs()
        self.scheduler.start()
        await self.plugins.start()
        await self.devices.start()
        await self.channels.start()
        self.started = True
        await self.bus.emit("system.started", {"version": self.version})

    async def stop(self) -> None:
        await self.channels.stop()
        await self.devices.stop()
        await self.plugins.stop()
        await self.scheduler.stop()
        await self.memory.stop()
        self.started = False
        self.db.close()
