"""插件六分类 + 内部处理型（意图委托 / 上下文隔离）测试，全部离线。

覆盖：kind 推断与一致性校验、provider 注册与能力解析、ProcessorRouter
门控与摘要截断、DelegatingIntentProcessor 工具循环与「原始输出不回流」不变量。
"""

from __future__ import annotations

import pytest

from whale_core.plugins import (
    DelegatingIntentProcessor,
    NullIntentProcessor,
    PluginKind,
    PluginManifest,
    PluginRegistry,
    ProcessingRequest,
    ProcessingResult,
    ProcessorRouter,
)

# ---------------------------------------------------------------------------
# 六分类：推断与一致性
# ---------------------------------------------------------------------------

FG_MONITOR = PluginManifest(  # 输入型：手机实时返回在看什么应用
    plugin_id="fg_monitor", name="前台监测", version="1.0", inputs=("fg.app_changed",)
)
DESKTOP_NOTIFY = PluginManifest(  # 输出型：给电脑弹提示
    plugin_id="desktop_notify", name="桌面弹提示", version="1.0",
    actions=("notify",), action_risk={"notify": "communicate"},
)
QQ_CHANNEL = PluginManifest(  # 多端共用型：QQ
    plugin_id="qq", name="QQ 渠道", version="1.0",
    inputs=("qq.message",), actions=("send_message",),
    action_risk={"send_message": "communicate"},
)
QWEN_EXECUTOR = PluginManifest(  # 内部处理型：意图执行委托（qwen 执行器模型）
    plugin_id="qwen_executor", name="qwen 操作执行器", version="1.0",
    kind="processor", provides=("operation_execution",),
)
QWEN_MODEL = PluginManifest(  # 模型供给型
    plugin_id="qwen_endpoint", name="qwen 模型端点", version="1.0",
    kind="model", provides=("chat", "asr"),
)
VECTOR_MEMORY = PluginManifest(  # 记忆/存储型
    plugin_id="vector_recall", name="混合召回后端", version="1.0",
    kind="memory", provides=("recall", "store"),
)


def test_kind_inference_backward_compatible() -> None:
    assert FG_MONITOR.resolved_kind is PluginKind.INPUT
    assert DESKTOP_NOTIFY.resolved_kind is PluginKind.OUTPUT
    assert QQ_CHANNEL.resolved_kind is PluginKind.CHANNEL


def test_provider_kinds_explicit() -> None:
    assert QWEN_EXECUTOR.resolved_kind is PluginKind.PROCESSOR
    assert QWEN_MODEL.resolved_kind is PluginKind.MODEL
    assert VECTOR_MEMORY.resolved_kind is PluginKind.MEMORY


def test_provider_kind_requires_provides_and_forbids_io() -> None:
    with pytest.raises(ValueError, match="provides"):
        PluginManifest(plugin_id="p1", name="x", version="1", kind="processor")
    with pytest.raises(ValueError, match="no inputs/actions"):
        PluginManifest(plugin_id="p2", name="x", version="1", kind="model",
                       provides=("chat",), inputs=("m.event",))
    with pytest.raises(ValueError, match="no inputs/actions"):
        PluginManifest(plugin_id="p3", name="x", version="1", kind="memory",
                       provides=("store",), actions=("write",))


def test_explicit_kind_must_match_capabilities() -> None:
    with pytest.raises(ValueError, match="inputs only"):
        PluginManifest(plugin_id="m1", name="x", version="1", kind="input",
                       inputs=("a.b",), actions=("act",))
    with pytest.raises(ValueError, match="both inputs and actions"):
        PluginManifest(plugin_id="m2", name="x", version="1", kind="channel",
                       inputs=("a.b",))
    with pytest.raises(ValueError, match="invalid plugin kind"):
        PluginManifest(plugin_id="m3", name="x", version="1", kind="magic",
                       inputs=("a.b",))
    with pytest.raises(ValueError, match="provider-kind"):
        PluginManifest(plugin_id="m4", name="x", version="1", inputs=("a.b",),
                       actions=("act",), action_risk={"act": "read_only"},
                       provides=("leftover",))


def test_no_capabilities_without_provider_kind_rejected() -> None:
    with pytest.raises(ValueError, match="no capabilities"):
        PluginManifest(plugin_id="empty", name="x", version="1")


# ---------------------------------------------------------------------------
# 注册表：provider 挂载与能力解析
# ---------------------------------------------------------------------------


def test_registry_provider_registration_and_lookup() -> None:
    reg = PluginRegistry()
    proc = NullIntentProcessor()
    reg.register(QWEN_EXECUTOR, provider=proc)
    reg.register(QWEN_MODEL)  # 未接线：provider=None
    reg.register(VECTOR_MEMORY, provider=object())

    assert reg.processors_for("operation_execution") == [("qwen_executor", proc)]
    assert reg.processors_for("chat") == []  # model 能力不算 processor
    assert reg.providers_for("chat", kind=PluginKind.MODEL) == [("qwen_endpoint", None)]
    assert reg.providers_for("recall")[0][0] == "vector_recall"
    assert reg.kind_of("qq") is None  # 未注册
    assert reg.kind_of("qwen_executor") is PluginKind.PROCESSOR


def test_registry_rejects_provider_object_on_io_plugin() -> None:
    reg = PluginRegistry()
    with pytest.raises(ValueError, match="provider-kind"):
        reg.register(DESKTOP_NOTIFY, provider=object())


def test_registry_enforces_io_and_provider_registration_boundaries() -> None:
    reg = PluginRegistry()
    with pytest.raises(ValueError, match="provider-kind plugins cannot register I/O"):
        reg.register(QWEN_MODEL, input_adapter=object())
    with pytest.raises(ValueError, match="provider-kind plugins cannot register I/O"):
        reg.register(QWEN_MODEL, executor=object())
    with pytest.raises(ValueError, match="input_adapter requires"):
        reg.register(DESKTOP_NOTIFY, input_adapter=object())
    with pytest.raises(ValueError, match="executor requires"):
        reg.register(FG_MONITOR, executor=object())


# ---------------------------------------------------------------------------
# ProcessorRouter：门控、截断与审计
# ---------------------------------------------------------------------------


class EchoProcessor:
    def __init__(self, result: ProcessingResult | None = None, raise_exc: bool = False):
        self.result = result or ProcessingResult("done", "ok")
        self.raise_exc = raise_exc
        self.seen: list[ProcessingRequest] = []

    def process(self, request: ProcessingRequest) -> ProcessingResult:
        if self.raise_exc:
            raise RuntimeError("boom")
        self.seen.append(request)
        return self.result


def make_router(processor=None, **kwargs) -> tuple[PluginRegistry, ProcessorRouter]:
    reg = PluginRegistry()
    reg.register(QWEN_EXECUTOR, provider=processor)
    return reg, ProcessorRouter(reg, **kwargs)


def test_router_unknown_capability_denied() -> None:
    _, router = make_router(EchoProcessor())
    result = router.route(ProcessingRequest(capability="teleport", task="x"))
    assert result.status == "denied" and "teleport" in result.summary


def test_router_unwired_provider_fails_closed() -> None:
    _, router = make_router(None)
    result = router.route(ProcessingRequest(capability="operation_execution", task="x"))
    assert result.status == "failed" and "not connected" in result.summary


def test_router_exception_and_invalid_status() -> None:
    _, router = make_router(EchoProcessor(raise_exc=True))
    assert router.route(ProcessingRequest(capability="operation_execution", task="x")).status == "failed"

    _, router2 = make_router(EchoProcessor(ProcessingResult("weird-status", "s")))
    assert router2.route(ProcessingRequest(capability="operation_execution", task="x")).status == "failed"


def test_router_truncates_summary_and_receipts() -> None:
    """上下文隔离最后防线：processor 违约返回超长内容也被截断。"""
    huge = ProcessingResult(
        "done", "摘要" * 5000, receipts=tuple(f"tool-{i}:" + "x" * 300 for i in range(50))
    )
    _, router = make_router(EchoProcessor(huge), max_summary_chars=200)
    result = router.route(ProcessingRequest(capability="operation_execution", task="x"))
    assert len(result.summary) == 200
    assert len(result.receipts) == 20
    assert all(len(r) <= 120 for r in result.receipts)


def test_router_audit_minimizes_privacy_surface() -> None:
    proc = EchoProcessor()
    _, router = make_router(proc)
    request = ProcessingRequest(
        capability="operation_execution",
        task="敏感任务全文" * 100,
        constraints={"allowed_tools": ("what_time",), "secret_cfg": "S3CRET"},
        source="qq:owner",
    )
    result = router.route(request)
    record = router.audit(request, result, "qwen_executor")
    assert record["status"] == "done" and record["processor"] == "qwen_executor"
    assert len(record["task_digest"]) == 64
    assert "敏感任务全文" not in str(record)
    dumped = str(record)
    assert "S3CRET" not in dumped and "secret_cfg" not in dumped


# ---------------------------------------------------------------------------
# DelegatingIntentProcessor：DS→qwen 委托模式的执行器循环
# ---------------------------------------------------------------------------


class ScriptedModel:
    def __init__(self, responses: list[str]):
        self.responses = list(responses)
        self.seen: list[list[dict]] = []

    def complete(self, messages) -> str:
        self.seen.append([dict(m) for m in messages])
        if not self.responses:
            return '{"final": "（脚本耗尽）"}'
        return self.responses.pop(0)


class SpyRunner:
    def __init__(self, script: dict[str, tuple[bool, str]] | None = None, raise_on: str = ""):
        self.script = script or {}
        self.raise_on = raise_on
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, tool: str, args) -> tuple[bool, str]:
        if tool == self.raise_on:
            raise RuntimeError("runner down")
        self.calls.append((tool, dict(args)))
        return self.script.get(tool, (True, "fine"))


def make_request(**kwargs) -> ProcessingRequest:
    base = dict(capability="operation_execution", task="查明天上海天气并发给老板", source="test")
    base.update(kwargs)
    return ProcessingRequest(**base)


def test_delegation_happy_path() -> None:
    model = ScriptedModel([
        '{"tool": "web_search", "args": {"q": "上海 明天 天气"}}',
        '{"final": "明天上海小雨 12-18 度；邮件草稿已备好待审批"}',
    ])
    runner = SpyRunner({"web_search": (True, "小雨 12-18C ...（大段原始结果）")})
    proc = DelegatingIntentProcessor(model, tool_runner=runner)
    result = proc.process(make_request(constraints={"allowed_tools": ("web_search", "send_email")}))
    assert result.status == "done"
    assert result.summary == "明天上海小雨 12-18 度；邮件草稿已备好待审批"
    assert result.receipts == ("web_search:ok",)
    assert runner.calls == [("web_search", {"q": "上海 明天 天气"})]


def test_context_isolation_raw_output_never_flows_back() -> None:
    """核心不变量：工具原始输出只喂执行器模型，不进回流摘要/回执。"""
    secret_blob = "RAW_OUTPUT_TOKEN_LEAK_" + "x" * 5000
    model = ScriptedModel([
        '{"tool": "tail_bridge_log", "args": {}}',
        '{"final": "日志正常，无异常"}',
    ])
    runner = SpyRunner({"tail_bridge_log": (True, secret_blob)})
    proc = DelegatingIntentProcessor(model, tool_runner=runner, max_tool_output_chars=500)
    result = proc.process(make_request(constraints={"allowed_tools": ("tail_bridge_log",)}))
    assert result.status == "done"
    assert secret_blob not in result.summary
    assert "RAW_OUTPUT_TOKEN_LEAK_" not in " ".join(result.receipts)
    assert len(result.summary) <= 1200
    # 原始输出被截断后才回喂执行器模型
    fed = [m["content"] for m in model.seen[-1] if m["role"] == "user"]
    assert any(len(c) <= 500 + 64 for c in fed)


def test_delegation_respects_allowed_tools() -> None:
    model = ScriptedModel([
        '{"tool": "purchase", "args": {"sku": "1"}}',
        '{"final": "未执行购买：不在允许清单"}',
    ])
    runner = SpyRunner()
    proc = DelegatingIntentProcessor(model, tool_runner=runner)
    result = proc.process(make_request(constraints={"allowed_tools": ("what_time",)}))
    assert result.status == "done"
    assert runner.calls == []  # 越权工具根本没到 runner
    assert "purchase:denied-not-allowed" in result.receipts


def test_delegation_without_allowlist_fails_closed() -> None:
    model = ScriptedModel([
        '{"tool": "purchase", "args": {"sku": "1"}}',
        '{"final": "没有获准工具，未执行"}',
    ])
    runner = SpyRunner()
    proc = DelegatingIntentProcessor(model, tool_runner=runner)
    result = proc.process(make_request())
    assert result.status == "done"
    assert runner.calls == []
    assert "purchase:denied-not-allowed" in result.receipts


def test_delegation_gated_receipt_becomes_needs_approval() -> None:
    """门控回执（plugin-hold 等）→ 立即 needs_approval，执行器模型不能代批。"""
    model = ScriptedModel(['{"tool": "send_email", "args": {"to": "boss"}}'])
    runner = SpyRunner({"send_email": (False, "plugin-hold:email:send_email")})
    proc = DelegatingIntentProcessor(model, tool_runner=runner)
    result = proc.process(make_request(constraints={"allowed_tools": ("send_email",)}))
    assert result.status == "needs_approval"
    assert "人工审批" in result.summary
    assert result.receipts == ("send_email:fail",)


def test_delegation_runner_exception_and_round_cap() -> None:
    model = ScriptedModel([
        '{"tool": "web_search", "args": {}}',
        '{"tool": "web_search", "args": {}}',
        '{"tool": "web_search", "args": {}}',
        '{"tool": "web_search", "args": {}}',
        '{"tool": "web_search", "args": {}}',
    ])
    runner = SpyRunner(raise_on="web_search")
    proc = DelegatingIntentProcessor(model, tool_runner=runner, max_rounds=3)
    result = proc.process(make_request(constraints={"allowed_tools": ("web_search",)}))
    assert result.status == "failed" and "rounds exhausted" in result.summary
    assert result.receipts == ("web_search:fail",) * 3


def test_delegation_model_error_and_nonjson() -> None:
    class BoomModel:
        def complete(self, messages) -> str:
            raise ConnectionError("endpoint down")

    proc = DelegatingIntentProcessor(BoomModel(), tool_runner=SpyRunner())
    assert proc.process(make_request()).status == "failed"

    model = ScriptedModel(["我不是 JSON", '{"final": "好了"}'])
    proc2 = DelegatingIntentProcessor(model, tool_runner=SpyRunner())
    result = proc2.process(make_request())
    assert result.status == "done" and result.summary == "好了"


def test_delegation_parses_fenced_json() -> None:
    model = ScriptedModel(['```json\n{"final": "带围栏也行"}\n```'])
    proc = DelegatingIntentProcessor(model, tool_runner=SpyRunner())
    assert proc.process(make_request()).summary == "带围栏也行"
