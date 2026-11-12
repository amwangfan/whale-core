"""WhaleApiServer × settings.bundle 的 owner_fact 宿主接线（PR#88 发布前核查）。

生产 launcher（repo 外）的真实构造顺序：``ApiContext(store, manager,
orchestrator, curator, optional)`` → ``ctx.settings = SettingsController(
config_dir, bundle)``，**不显式传** owner_facts / owner_fact_authority /
owner_fact_worker。修复前 WhaleApiServer.__init__ 只注册
``settings.on_apply``，首启管理路径 409、worker 丢失 stop 钩子（要等
settings 热重载才接线）。

本测试模拟该真实接线：SettingsController 用真实类、bundle 挂真实
``build_owner_fact_stack`` 产物；模型回复用确定性罐装 Fake——不启动真实
模型、不触网、不读 secret/用户库/设备。断言：

1. settings.bundle 驱动时三字段在 server 构造后即补齐（首启即接线），
   且**不**整调 ``ctx.apply_bundle``（profile 等既有字段不被 bundle 改写）；
2. 宿主显式注入（非 None）的字段一律保留：全部显式不覆盖，部分显式只补
   缺失（None）字段；
3. 无 settings、或 settings.bundle 为 None / bundle 无该三属性时，字段保持
   None（不凭空造能力）；
4. ``server.stop()`` 确实停止继承来的 owner_fact_worker（线程退出）。
"""
from __future__ import annotations

from types import SimpleNamespace

from whale_core.api.server import ApiContext, WhaleApiServer
from whale_core.configuration import SettingsController
from whale_core.core.companion import WhaleCompanion
from whale_core.core.owner_fact_host import build_owner_fact_stack
from whale_core.core.runtime import ConversationOrchestrator
from whale_core.memory.curation import MemoryCurator
from whale_core.memory.store import InMemoryStore
from whale_core.plugins.manager import PluginManager
from whale_core.plugins.registry import PluginRegistry

TOKEN = "owner-fact-host-wiring-fake-token"  # 假 token，仅回环鉴权用


class FakeModelReply:
    """确定性提炼替身：罐装空候选；无 job 入队时永不被调用。"""

    def complete(self, prompt: str, *, timeout: float) -> str:
        return '{"candidates": []}'


class FakeBrain:
    def reply(self, text, *, persona="", memories=(), history=()):
        return f"[fake] {text}"


class FakeWorker:
    """显式注入替身：记录 stop 调用，验证 server 不覆盖宿主注入。"""

    def __init__(self):
        self.stop_calls: list[dict] = []

    def stop(self, *, drain: bool = False, timeout: float = 5.0) -> None:
        self.stop_calls.append({"drain": drain, "timeout": timeout})


def _host_ctx() -> ApiContext:
    """模拟生产 launcher 的 ApiContext 构造：只传 store/manager/
    orchestrator/curator/optional，三 owner_fact 字段缺省 None。"""
    store = InMemoryStore()
    manager = PluginManager(PluginRegistry(), store)
    curator = MemoryCurator(store)
    companion = WhaleCompanion(store, FakeBrain(), curator=curator)
    orch = ConversationOrchestrator(store, companion)
    return ApiContext(store=store, manager=manager, orchestrator=orch,
                      curator=curator, optional=SimpleNamespace())


def _stack(store, *, start_worker: bool):
    stack = build_owner_fact_stack(store, FakeModelReply(), env={},
                                   start_worker=start_worker, model_timeout=5.0)
    assert stack.enabled, stack.note  # 装配必须真实成功（不许静默）
    return stack


def _bundle(stack, **extra):
    return SimpleNamespace(owner_facts=stack.service,
                           owner_fact_authority=stack.authority,
                           owner_fact_worker=stack.worker, **extra)


def test_settings_bundle_backfills_owner_fact_fields_on_init(tmp_path):
    """settings.bundle 驱动：构造 server 后三字段立即补齐（不等热重载）。"""
    ctx = _host_ctx()
    stack = _stack(ctx.store, start_worker=True)
    try:
        assert ctx.owner_facts is None
        assert ctx.owner_fact_authority is None
        assert ctx.owner_fact_worker is None
        # bundle.profile 与 ctx 不同：若误整调 apply_bundle 会改写它。
        ctx.settings = SettingsController(
            tmp_path, _bundle(stack, profile="cloud"), env={})
        server = WhaleApiServer(ctx, port=0, token=TOKEN)
        try:
            assert ctx.owner_facts is stack.service
            assert ctx.owner_fact_authority is stack.authority
            assert ctx.owner_fact_worker is stack.worker
            # 热重载路径仍注册（settings 热更新照旧生效）。
            assert ctx.settings.on_apply == ctx.apply_bundle
            # 刻意不调 apply_bundle：profile 等既有字段不被 bundle 改写。
            assert ctx.profile == "local"
        finally:
            server.stop()
    finally:
        stack.shutdown()


def test_explicitly_injected_owner_fact_fields_are_preserved(tmp_path):
    """宿主显式注入的字段一律保留：全部显式不覆盖，部分显式只补缺失。"""
    # 子情形 A：三字段全部显式注入 → 一个都不覆盖。
    ctx = _host_ctx()
    explicit_facts, explicit_authority = object(), object()
    explicit_worker = FakeWorker()
    ctx.owner_facts = explicit_facts
    ctx.owner_fact_authority = explicit_authority
    ctx.owner_fact_worker = explicit_worker
    stack = _stack(ctx.store, start_worker=True)
    try:
        ctx.settings = SettingsController(tmp_path, _bundle(stack), env={})
        server = WhaleApiServer(ctx, port=0, token=TOKEN)
        try:
            assert ctx.owner_facts is explicit_facts
            assert ctx.owner_fact_authority is explicit_authority
            assert ctx.owner_fact_worker is explicit_worker
        finally:
            server.stop()
        # server.stop 关闭的是宿主显式注入的 worker，而非 bundle 里的。
        assert len(explicit_worker.stop_calls) == 1
    finally:
        stack.shutdown()

    # 子情形 B：只显式注入 owner_facts → 保留它，另两个仍从 bundle 补齐。
    ctx = _host_ctx()
    ctx.owner_facts = explicit_facts
    stack = _stack(ctx.store, start_worker=False)
    try:
        ctx.settings = SettingsController(tmp_path, _bundle(stack), env={})
        server = WhaleApiServer(ctx, port=0, token=TOKEN)
        try:
            assert ctx.owner_facts is explicit_facts
            assert ctx.owner_fact_authority is stack.authority
            assert ctx.owner_fact_worker is stack.worker
        finally:
            server.stop()
    finally:
        stack.shutdown()


def test_no_settings_or_bundle_keeps_owner_fact_fields_none(tmp_path):
    """无 settings / bundle 为 None / bundle 无该三属性：字段保持 None。"""
    # 子情形 A：不挂 settings（纯 ApiContext）。
    ctx = _host_ctx()
    server = WhaleApiServer(ctx, port=0, token=TOKEN)
    try:
        assert ctx.owner_facts is None
        assert ctx.owner_fact_authority is None
        assert ctx.owner_fact_worker is None
    finally:
        server.stop()

    # 子情形 B：settings 存在但 bundle 为 None。
    ctx = _host_ctx()
    ctx.settings = SettingsController(tmp_path, None, env={})
    server = WhaleApiServer(ctx, port=0, token=TOKEN)
    try:
        assert ctx.owner_facts is None
        assert ctx.owner_fact_authority is None
        assert ctx.owner_fact_worker is None
        assert ctx.settings.on_apply == ctx.apply_bundle
    finally:
        server.stop()

    # 子情形 C：bundle 存在但无 owner_fact 属性（旧形态 bundle，不炸）。
    ctx = _host_ctx()
    ctx.settings = SettingsController(
        tmp_path, SimpleNamespace(profile="cloud"), env={})
    server = WhaleApiServer(ctx, port=0, token=TOKEN)
    try:
        assert ctx.owner_facts is None
        assert ctx.owner_fact_authority is None
        assert ctx.owner_fact_worker is None
    finally:
        server.stop()


def test_server_stop_stops_inherited_owner_fact_worker(tmp_path):
    """继承自 settings.bundle 的 worker 随 server.stop 真实停止（线程退出）。"""
    ctx = _host_ctx()
    stack = _stack(ctx.store, start_worker=True)
    thread = stack.worker._thread
    assert thread is not None and thread.is_alive()
    ctx.settings = SettingsController(tmp_path, _bundle(stack), env={})
    server = WhaleApiServer(ctx, port=0, token=TOKEN)
    server.start()
    try:
        assert ctx.owner_fact_worker is stack.worker
        assert thread.is_alive()
    finally:
        server.stop()
    assert stack.worker._stop.is_set()
    assert not thread.is_alive()
