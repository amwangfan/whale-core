"""RPC methods: the complete management and interaction surface.

Every method is reachable over HTTP and over the Link WebSocket with the
same parameters and results.
"""

from __future__ import annotations

import asyncio
import fnmatch
import logging
import platform
import time
from typing import Any, Literal

from pydantic import BaseModel, Field

from ..agent.persona import PersonaCard, from_card_v2
from ..agent.runtime import RunRequest
from ..kernel.errors import WhaleError
from ..kernel.ids import new_id
from ..link.protocol import PROTOCOL, Client, NoParams

STARTED = time.time()

log = logging.getLogger("whale.server.api")


class Id(BaseModel):
    id: str


class Page(BaseModel):
    limit: int = Field(50, ge=1, le=500)


class EventsQuery(BaseModel):
    pattern: str = "*"
    limit: int = Field(100, ge=1, le=1000)
    before: str | None = None

class ConfigPatch(BaseModel):
    patch: dict[str, Any]

class SecretSet(BaseModel):
    key: str
    value: str | None = None

class ProviderUpsert(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9_-]{1,32}$")
    base_url: str
    label: str = ""
    api_key: str | None = None

class Role(BaseModel):
    role: Literal["chat", "fast", "vision", "embedding"] = "chat"

class ModelList(BaseModel):
    provider: str

class ChatSend(BaseModel):
    text: str = Field(min_length=1, max_length=20000)
    conversation_id: str | None = None
    images: list[str] = Field(default_factory=list, max_length=4)
    persona_id: str | None = None
    wait: bool = True
    # 轻量档（眼镜 quick 语义）：fast 角色模型 + remember=false 时不召回不回写记忆
    model_role: Literal["chat", "fast"] | None = None
    remember: bool = True

class RunId(BaseModel):
    run_id: str

class ConvList(BaseModel):
    limit: int = 50
    channel: str | None = None

class ConvMessages(BaseModel):
    id: str
    limit: int = 100
    before: int | None = None

class ConvUpdate(BaseModel):
    id: str
    title: str | None = None
    persona_id: str | None = None

class RunList(BaseModel):
    limit: int = 50
    mode: str | None = None
    status: str | None = None
    task_id: str | None = None

class MemList(BaseModel):
    kind: str | None = None
    status: str | None = "active"
    query: str = ""
    limit: int = Field(50, ge=1, le=500)
    offset: int = 0
    order: str = "updated_at DESC"
    scope: str | None = Field(None, description="owner | shared | group | person | an exact scope such as group:qq:1")

class MemSearch(BaseModel):
    query: str
    limit: int = 10

class MemAdd(BaseModel):
    content: str = Field(min_length=1, max_length=4000)
    kind: str = "fact"
    importance: float = Field(0.6, ge=0, le=1)
    tags: list[str] = Field(default_factory=list)

class MemEdit(BaseModel):
    id: str
    content: str | None = None
    kind: str | None = None
    importance: float | None = None
    tags: list[str] | None = None
    status: Literal["active", "candidate", "archived", "superseded"] | None = None

class Graph(BaseModel):
    limit: int = Field(300, ge=1, le=2000)
    kind: str | None = None
    scope: str | None = None
    sources: bool = False

class PolicySet(BaseModel):
    pattern: str
    verdict: Literal["allow", "ask", "deny"] | None = None

class Invoke(BaseModel):
    name: str
    args: dict[str, Any] = Field(default_factory=dict)

class ApprovalList(BaseModel):
    status: str | None = "pending"

class Resolve(BaseModel):
    id: str
    approve: bool
    note: str = ""
    remember: bool = False   # always allow this capability from now on

class TaskCreate(BaseModel):
    goal: str = Field(min_length=1, max_length=8000)
    title: str = ""
    schedule: dict[str, Any] | None = None
    start: bool = True

class TaskList(BaseModel):
    status: str | None = None
    limit: int = 100

class JobToggle(BaseModel):
    id: str
    enabled: bool

class PersonaImport(BaseModel):
    card: dict[str, Any]

class PluginToggle(BaseModel):
    id: str
    enabled: bool

class PluginConfig(BaseModel):
    id: str
    config: dict[str, Any] = Field(default_factory=dict)
    secrets: dict[str, str] = Field(default_factory=dict)

class Explore(BaseModel):
    topic: str | None = None

class Subscribe(BaseModel):
    patterns: list[str]


def register(app: Any) -> None:  # noqa: C901 - a flat list of small handlers reads best
    rpc = app.rpc
    m = rpc.method

    # -- system -------------------------------------------------------------------
    @m("system.status", summary="Overall agent status for the home screen", scope="client")
    async def system_status(p: NoParams, c: Client | None) -> Any:
        mem = app.memory.store.stats()
        tasks = app.tasks.list(limit=200)
        return {
            "version": app.version, "uptime_s": round(time.time() - STARTED), "python": platform.python_version(),
            "agent": {"name": app.config.value.agent.name, "persona": app.personas.resolve(
                app.config.value.agent.persona_id).model_dump(include={"id", "name", "avatar", "tagline", "color"}),
                **app.agent.state()},
            "models": app.models.status(),
            "memory": mem | {"queue_processed": app.memory.processed},
            "capabilities": {"total": len(app.capabilities.list()),
                             "by_provider": _count(s.provider.split(":")[0] for s in app.capabilities.list())},
            "tasks": {"total": len(tasks), "active": sum(t["status"] in ("running", "waiting") for t in tasks),
                      "scheduled": sum(t["status"] == "scheduled" for t in tasks)},
            "approvals": {"pending": len(app.approvals.list("pending"))},
            "devices": {"total": len(app.devices.list()), "online": sum(d.get("online", False) for d in app.devices.list())},
            "clients": [cl.to_dict() for cl in app.hub.clients.values()],
            "autonomy": {"explore": app.config.value.autonomy.explore, "proactive": app.config.value.autonomy.proactive},
        }

    @m("system.schema", summary="All RPC methods with parameter schemas", scope="client")
    async def system_schema(p: NoParams, c: Client | None) -> Any:
        return {"protocol": PROTOCOL, "methods": rpc.schema()}

    @m("events.query", EventsQuery, "Persisted event log (activity timeline)")
    async def events_query(p: EventsQuery, c: Client | None) -> Any:
        return {"events": app.bus._log.query(p.pattern, p.limit, p.before) if app.bus._log else []}

    # -- config ---------------------------------------------------------------------
    @m("config.get", summary="Current configuration (secrets reported as set/unset)")
    async def config_get(p: NoParams, c: Client | None) -> Any:
        return app.config.public()

    @m("config.update", ConfigPatch, "Deep-merge a configuration patch")
    async def config_update(p: ConfigPatch, c: Client | None) -> Any:
        app.config.update(p.patch)
        return app.config.public()

    @m("config.set_secret", SecretSet, "Set or clear a secret (API key)")
    async def config_secret(p: SecretSet, c: Client | None) -> Any:
        if p.key == "api_token":
            raise WhaleError("forbidden", "use the CLI to rotate the API token")
        app.config.set_secret(p.key, p.value)
        return {"key": p.key, "set": bool(p.value)}

    @m("models.upsert_provider", ProviderUpsert, "Add or update a model provider")
    async def provider_upsert(p: ProviderUpsert, c: Client | None) -> Any:
        providers = [x.model_dump() for x in app.config.value.models.providers if x.id != p.id]
        providers.append({"id": p.id, "kind": "openai", "base_url": p.base_url, "label": p.label})
        app.config.update({"models": {"providers": providers}})
        if p.api_key is not None:
            app.config.set_secret(f"provider.{p.id}", p.api_key)
        return app.models.status()

    @m("models.remove_provider", Id, "Remove a provider")
    async def provider_remove(p: Id, c: Client | None) -> Any:
        providers = [x.model_dump() for x in app.config.value.models.providers if x.id != p.id]
        app.config.update({"models": {"providers": providers}})
        app.config.set_secret(f"provider.{p.id}", None)
        return app.models.status()

    @m("models.status", summary="Model roles, health and budget", scope="client")
    async def models_status(p: NoParams, c: Client | None) -> Any:
        return app.models.status()

    @m("models.test", Role, "Send a probe request to a model role")
    async def models_test(p: Role, c: Client | None) -> Any:
        return await app.models.test(p.role)

    @m("models.list_remote", ModelList, "List models offered by a provider (/models)")
    async def models_list(p: ModelList, c: Client | None) -> Any:
        import httpx

        prov = next((x for x in app.config.value.models.providers if x.id == p.provider), None)
        if not prov:
            raise WhaleError("not_found", f"provider {p.provider}")
        key = app.config.secret(f"provider.{prov.id}")
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.get(prov.base_url.rstrip("/") + "/models",
                                     headers={"Authorization": f"Bearer {key}"} if key else {})
                r.raise_for_status()
                return {"models": sorted(x.get("id", "") for x in r.json().get("data", []))}
        except Exception as exc:
            raise WhaleError("unavailable", f"cannot list models: {exc}") from exc

    # -- chat -------------------------------------------------------------------------
    @m("chat.send", ChatSend, "Send a message; streams run.* events; returns the reply", scope="client")
    async def chat_send(p: ChatSend, c: Client | None) -> Any:
        channel = c.role if c else "http"
        conv = app.conversations.ensure(p.conversation_id, channel=channel, persona_id=p.persona_id)
        # 渠道上下文注入（眼镜日历/位置/天气快照等）：由服务端注册的 provider 供给，
        # 客户端不能直接传 system_extra（防提示词注入面扩大）。
        system_extra = ""
        provider = app.context_providers.get(channel)
        if provider is not None:
            try:
                system_extra = str(provider() or "")
            except Exception:
                system_extra = ""
        req = RunRequest(text=p.text, conversation_id=conv["id"], channel=channel, images=p.images,
                         persona_id=p.persona_id, model_role=p.model_role, remember=p.remember,
                         system_extra=system_extra, run_id=new_id("run"))

        async def run_then_hook() -> Any:
            out = await app.agent.run(req)
            # 渠道后置钩子（眼镜日程候选抽取等；fail-soft，不回溯影响回复）
            hook = app.turn_hooks.get(channel)
            if hook is not None and out.status == "completed":
                try:
                    hook(p.text, conv["id"])
                except Exception:
                    log.exception("turn hook for channel %s failed", channel)
            return out

        if not p.wait:
            asyncio.create_task(run_then_hook())
            return {"conversation_id": conv["id"], "run_id": req.run_id, "accepted": True}
        out = await run_then_hook()
        return {"conversation_id": conv["id"], "run_id": out.run["id"], "status": out.status, "text": out.text,
                "error": out.run.get("error", ""), "memories": [
                    {"id": x["id"], "content": x["content"], "kind": x["kind"], "why": x["why"]} for x in out.memories],
                "tools": out.tools}

    @m("chat.cancel", RunId, "Cancel a running run", scope="client")
    async def chat_cancel(p: RunId, c: Client | None) -> Any:
        return {"cancelled": await app.agent.cancel(p.run_id)}

    @m("conversations.list", ConvList, scope="client")
    async def conv_list(p: ConvList, c: Client | None) -> Any:
        return {"conversations": app.conversations.list(p.limit, p.channel)}

    @m("conversations.messages", ConvMessages, scope="client")
    async def conv_messages(p: ConvMessages, c: Client | None) -> Any:
        return {"conversation": app.conversations.get(p.id),
                "messages": app.conversations.messages(p.id, p.limit, p.before)}

    @m("conversations.update", ConvUpdate)
    async def conv_update(p: ConvUpdate, c: Client | None) -> Any:
        return app.conversations.update(p.id, **p.model_dump(exclude_none=True, exclude={"id"}))

    @m("conversations.delete", Id)
    async def conv_delete(p: Id, c: Client | None) -> Any:
        app.conversations.delete(p.id)
        return {"deleted": p.id}

    # -- runs (observability) ------------------------------------------------------------
    @m("runs.list", RunList, "Recent runs")
    async def runs_list(p: RunList, c: Client | None) -> Any:
        return {"runs": app.runs.list(limit=p.limit, mode=p.mode, status=p.status, task_id=p.task_id)}

    @m("runs.get", Id, "A run with all steps and child runs", scope="client")
    async def runs_get(p: Id, c: Client | None) -> Any:
        return {"run": app.runs.get(p.id), "steps": app.runs.steps(p.id),
                "children": app.runs.list(parent_id=p.id, limit=50)}

    # -- memory ---------------------------------------------------------------------------
    @m("memory.list", MemList)
    async def mem_list(p: MemList, c: Client | None) -> Any:
        items, total = app.memory.store.list(kind=p.kind, status=p.status or None, query=p.query, limit=p.limit,
                                             offset=p.offset, order=p.order, scope=p.scope)
        return {"items": [i.to_dict(app.memory.half_life) for i in items], "total": total}

    @m("memory.get", Id)
    async def mem_get(p: Id, c: Client | None) -> Any:
        item = app.memory.store.get(p.id)
        links = app.memory.store.links([p.id])
        other = [l["dst"] if l["src"] == p.id else l["src"] for l in links]
        return {"item": item.to_dict(app.memory.half_life), "links": links,
                "related": [x.to_dict(app.memory.half_life) for x in app.memory.store.many(other)]}

    @m("memory.search", MemSearch, "Hybrid retrieval with explanations")
    async def mem_search(p: MemSearch, c: Client | None) -> Any:
        hits = await app.memory.retrieve(p.query, limit=p.limit, touch=False, scope=None)
        return {"hits": [h.to_dict(app.memory.half_life) for h in hits]}

    @m("memory.remember", MemAdd)
    async def mem_add(p: MemAdd, c: Client | None) -> Any:
        return await app.memory.remember(p.content, kind=p.kind, importance=p.importance, tags=p.tags,
                                         source={"manual": True})

    @m("memory.update", MemEdit)
    async def mem_edit(p: MemEdit, c: Client | None) -> Any:
        fields = p.model_dump(exclude_none=True, exclude={"id"})
        return await app.memory.edit(p.id, **fields)

    @m("memory.forget", Id)
    async def mem_forget(p: Id, c: Client | None) -> Any:
        await app.memory.forget(p.id)
        return {"forgotten": p.id}

    @m("memory.graph", Graph, "Nodes and relation edges for the graph view")
    async def mem_graph(p: Graph, c: Client | None) -> Any:
        return app.memory.graph(p.limit, p.kind, p.scope, p.sources)

    @m("memory.stats")
    async def mem_stats(p: NoParams, c: Client | None) -> Any:
        return app.memory.store.stats() | {"queue_processed": app.memory.processed}

    @m("memory.maintain", summary="Run decay/archival now")
    async def mem_maintain(p: NoParams, c: Client | None) -> Any:
        return await app.memory.maintain()

    # -- capabilities & policy --------------------------------------------------------------
    @m("capabilities.list", summary="Every capability with its effective policy and stats", scope="client")
    async def caps_list(p: NoParams, c: Client | None) -> Any:
        out = []
        for spec in app.capabilities.list():
            d = spec.to_dict()
            d["policy"] = app.policy.decide(spec)
            d["stats"] = app.capabilities.stats.get(spec.name, {"calls": 0, "errors": 0, "total_ms": 0})
            out.append(d)
        return {"capabilities": out, "rules": app.config.value.policy.rules}

    @m("policy.set", PolicySet, "Set (or clear with null) a policy rule for a capability glob")
    async def policy_set(p: PolicySet, c: Client | None) -> Any:
        rules = dict(app.config.value.policy.rules)
        if p.verdict is None:
            rules.pop(p.pattern, None)
        else:
            rules[p.pattern] = p.verdict
        # replace (not merge) the rules map
        app.config.value.policy.rules = {}
        app.config.update({"policy": {"rules": rules}})
        return {"rules": rules}

    @m("capabilities.invoke", Invoke, "Invoke a capability directly as the owner (console quick actions)")
    async def caps_invoke(p: Invoke, c: Client | None) -> Any:
        from ..capabilities.spec import ToolContext

        spec = app.capabilities.get(p.name)
        if app.policy.decide(spec) == "deny":
            raise WhaleError("forbidden", "capability disabled by policy")
        res = await app.capabilities.invoke(p.name, p.args, ToolContext(run_id="manual", mode="chat", app=app))
        return res.to_dict()

    # -- approvals --------------------------------------------------------------------------
    @m("approvals.list", ApprovalList, scope="client")
    async def approvals_list(p: ApprovalList, c: Client | None) -> Any:
        return {"approvals": app.approvals.list(p.status)}

    @m("approvals.resolve", Resolve)
    async def approvals_resolve(p: Resolve, c: Client | None) -> Any:
        row = await app.approvals.resolve(p.id, p.approve, by=c.name if c else "http", note=p.note)
        if p.remember and p.approve:
            rules = dict(app.config.value.policy.rules)
            rules[row["capability"]] = "allow"
            app.config.update({"policy": {"rules": rules}})
        return row

    # -- tasks & schedule ---------------------------------------------------------------------
    @m("tasks.create", TaskCreate)
    async def tasks_create(p: TaskCreate, c: Client | None) -> Any:
        return await app.tasks.create(p.goal, title=p.title, schedule=p.schedule, start=p.start)

    @m("tasks.list", TaskList, scope="client")
    async def tasks_list(p: TaskList, c: Client | None) -> Any:
        return {"tasks": app.tasks.list(p.status, p.limit)}

    @m("tasks.get", Id, scope="client")
    async def tasks_get(p: Id, c: Client | None) -> Any:
        task = app.tasks.get(p.id)
        runs = app.runs.list(task_id=p.id, limit=20)
        latest = None
        if runs:
            latest = {"run": runs[0], "steps": app.runs.steps(runs[0]["id"]),
                      "children": app.runs.list(parent_id=runs[0]["id"])}
        return {"task": task, "runs": runs, "latest": latest}

    @m("tasks.start", Id)
    async def tasks_start(p: Id, c: Client | None) -> Any:
        app.tasks.start(p.id)
        return app.tasks.get(p.id)

    @m("tasks.cancel", Id)
    async def tasks_cancel(p: Id, c: Client | None) -> Any:
        return {"cancelled": await app.tasks.cancel(p.id)}

    @m("tasks.delete", Id)
    async def tasks_delete(p: Id, c: Client | None) -> Any:
        await app.tasks.delete(p.id)
        return {"deleted": p.id}

    @m("schedule.list")
    async def schedule_list(p: NoParams, c: Client | None) -> Any:
        return {"jobs": app.scheduler.list()}

    @m("schedule.set_enabled", JobToggle)
    async def schedule_toggle(p: JobToggle, c: Client | None) -> Any:
        if p.id == "sys_explore":
            app.config.update({"autonomy": {"explore": p.enabled}})
            return app.scheduler.get(p.id)
        return app.scheduler.set_enabled(p.id, p.enabled)

    @m("schedule.run_now", Id)
    async def schedule_run(p: Id, c: Client | None) -> Any:
        asyncio.create_task(app.scheduler.run_now(p.id))
        return {"started": p.id}

    @m("schedule.delete", Id)
    async def schedule_delete(p: Id, c: Client | None) -> Any:
        job = app.scheduler.get(p.id)
        if job["system"]:
            raise WhaleError("forbidden", "system jobs can be disabled, not deleted")
        app.scheduler.remove(p.id)
        return {"deleted": p.id}

    # -- personas -------------------------------------------------------------------------------
    @m("personas.list", scope="client")
    async def personas_list(p: NoParams, c: Client | None) -> Any:
        return {"personas": [x.model_dump() for x in app.personas.list()],
                "active": app.config.value.agent.persona_id}

    @m("personas.save", PersonaCard)
    async def personas_save(p: PersonaCard, c: Client | None) -> Any:
        if p.id == "default" and p.builtin:
            p.builtin = False
        return (await app.personas.save(p)).model_dump()

    @m("personas.import", PersonaImport, "Import a Character Card V2 / SillyTavern JSON")
    async def personas_import(p: PersonaImport, c: Client | None) -> Any:
        card = from_card_v2(p.card, new_id("persona"))
        return (await app.personas.save(card)).model_dump()

    @m("personas.delete", Id)
    async def personas_delete(p: Id, c: Client | None) -> Any:
        await app.personas.delete(p.id)
        if app.config.value.agent.persona_id == p.id:
            app.config.update({"agent": {"persona_id": "default"}})
        return {"deleted": p.id}

    @m("personas.activate", Id)
    async def personas_activate(p: Id, c: Client | None) -> Any:
        app.personas.get(p.id)
        app.config.update({"agent": {"persona_id": p.id}})
        await app.bus.emit("persona.activated", {"id": p.id})
        return {"active": p.id}

    # -- plugins ----------------------------------------------------------------------------------
    @m("plugins.list")
    async def plugins_list(p: NoParams, c: Client | None) -> Any:
        return {"plugins": app.plugins.list()}

    @m("plugins.set_enabled", PluginToggle)
    async def plugins_toggle(p: PluginToggle, c: Client | None) -> Any:
        return await (app.plugins.enable(p.id) if p.enabled else app.plugins.disable(p.id))

    @m("plugins.configure", PluginConfig)
    async def plugins_configure(p: PluginConfig, c: Client | None) -> Any:
        return await app.plugins.configure(p.id, p.config, p.secrets)

    # -- autonomy -------------------------------------------------------------------------------------
    @m("autonomy.explore", Explore, "Start an exploration run now")
    async def autonomy_explore(p: Explore, c: Client | None) -> Any:
        asyncio.create_task(app.explorer.explore(p.topic))
        return {"started": True}

    @m("autonomy.status")
    async def autonomy_status(p: NoParams, c: Client | None) -> Any:
        ok, why = app.proactive.verdict()
        return {"config": app.config.value.autonomy.model_dump(), "proactive_ready": ok, "proactive_reason": why,
                "topics": app.explorer.topics()[:20],
                "recent": [e.to_dict() for e in app.bus.recent("autonomy.*", 30)]}

    # -- link ------------------------------------------------------------------------------------------
    @m("link.subscribe", Subscribe, "Replace this connection's event subscriptions", scope="client")
    async def link_subscribe(p: Subscribe, c: Client | None) -> Any:
        if c is None:
            raise WhaleError("invalid_params", "subscriptions need a Link connection")
        c.subscriptions = p.patterns[:50]
        return {"subscriptions": c.subscriptions}

    @m("link.clients", summary="Connected clients")
    async def link_clients(p: NoParams, c: Client | None) -> Any:
        return {"clients": [x.to_dict() for x in app.hub.clients.values()]}

    _ = fnmatch


def _count(items: Any) -> dict[str, int]:
    out: dict[str, int] = {}
    for x in items:
        out[x] = out.get(x, 0) + 1
    return out
