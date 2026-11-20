"""GUI 任务生产入口（#58）：把 observe→decide→act→verify 自主循环接到任务面。

纪律：
- 任务记录落既有任务 kv 面（``task:{task_id}`` + ``tasks:recent`` 索引），
  Studio 任务页/设备详情可见——不另建一套任务系统；
- 设备动作经 DeviceGatewayV2 统一选路（GatewayV2Transport），scope/吊销
  门控逐动作生效；停任务 = 协程取消 + best-effort device.stop（急停）；
- 决策模型来自环境配置（WHALE_GUI_AGENT_MODEL → QQ_INTENT_* → DEEPSEEK_*），
  未配置时 start 明确拒绝（503 语义由 API 层映射），绝不静默空转；
- 高风险动作无审批端口即拒（GuiAgentLoop 既有纪律：approval_denied 计失败）。
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
import time
import urllib.request
import uuid
from typing import Any, Callable, Mapping

from whale_core.devices.gateway_v2 import DeviceGatewayV2, GatewayV2Transport
from whale_core.devices.gui_agent.decision import LlmJsonDecision
from whale_core.devices.gui_agent.loop import GuiAgentLoop, GuiRunResult
from whale_core.devices.gui_agent.trace import RunTraceWriter
from whale_core.devices.transports.base import new_id

#: 任务记录键域（复用既有任务面：tasks:recent 索引 + task:<id> 本体）
_RECENT_KEY = "tasks:recent"
_TASK_PREFIX = "task:"
_INDEX_PREFIX = "gui_task:index:"        # device_id → [task_id, ...]
_TRACE_DIR = "/opt/whale/state/gui_traces"

DEFAULT_MAX_STEPS = 50
DEFAULT_TASK_TIMEOUT_S = 600
MAX_TASK_TIMEOUT_S = 3600
DEFAULT_ACTION_TIMEOUT_S = 10
DEFAULT_OBSERVE_TIMEOUT_S = 45


class GuiTaskError(ValueError):
    """任务入口拒绝（code 对齐管理面错误码）。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _new_task_id() -> str:
    """任务 ID 用 v2 协议形态（task_+ULID）——GUI 序列动作经 contracts/v2
    下发时 task_id 有 schema pattern（^task_[0-9A-HJKMNP-TV-Z]{26}$），
    纯 hex 会被协议预检 invalid_message（nova 真机实测踩中）。"""
    return new_id("task")


def make_llm_decision(env: Mapping[str, str] | None = None) -> LlmJsonDecision:
    """生产决策端：env 配置的 OpenAI 兼容 chat 端点（kimi 系 thinking 关闭）。

    解析顺序与真机验收口径一致：WHALE_GUI_AGENT_* → QQ_INTENT_* → DEEPSEEK_*。
    密钥只经环境变量读取，绝不落日志/落库。
    """
    env = os.environ if env is None else env
    model = (env.get("WHALE_GUI_AGENT_MODEL") or env.get("QQ_INTENT_MODEL")
             or env.get("DEEPSEEK_MODEL") or "").strip()
    base = (env.get("WHALE_GUI_AGENT_BASE_URL") or env.get("QQ_INTENT_BASE_URL")
            or env.get("DEEPSEEK_BASE_URL") or "").strip().rstrip("/")
    key = (env.get("WHALE_GUI_AGENT_API_KEY") or env.get("QQ_INTENT_API_KEY")
           or env.get("DEEPSEEK_API_KEY") or "").strip()
    if not (model and base and key):
        raise GuiTaskError(
            "unavailable", "GUI 决策模型未配置（需 WHALE_GUI_AGENT_* 或 "
            "QQ_INTENT_*/DEEPSEEK_* 环境）")
    if not base.endswith("/v1"):
        base += "/v1"

    async def complete(prompt: str) -> str:
        body = json.dumps({
            "model": model, "thinking": {"type": "disabled"},
            "messages": [{"role": "user", "content": prompt}],
        }).encode()

        def _call() -> str:
            req = urllib.request.Request(
                f"{base}/chat/completions", data=body,
                headers={"Authorization": f"Bearer {key}",
                         "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=90) as resp:
                payload = json.loads(resp.read())
            return payload["choices"][0]["message"]["content"]

        return await asyncio.to_thread(_call)

    return LlmJsonDecision(complete)


class GuiTaskService:
    """GUI 任务的创建/查询/停止（whale-api 进程内单例，挂 ctx）。

    decision_factory：测试注入点（ScriptedDecision）；生产默认
    ``make_llm_decision``（env 配置）。
    """

    def __init__(self, store, gateway_v2: DeviceGatewayV2, *,
                 decision_factory: Callable[[], Any] | None = None,
                 trace_dir: str = _TRACE_DIR) -> None:
        self._store = store
        self._gw2 = gateway_v2
        self._decision_factory = decision_factory or make_llm_decision
        self._trace_dir = trace_dir
        self._lock = threading.Lock()
        self._futures: dict[str, Any] = {}  # task_id → concurrent.futures.Future

    # ---- 记录读写 ----

    def _put_record(self, record: dict) -> None:
        self._store.put(f"{_TASK_PREFIX}{record['task_id']}", record)

    def _event(self, record: dict, kind: str, **extra) -> None:
        record["events"].append({"at": time.time(), "kind": kind,
                                 **{k: v for k, v in extra.items()
                                    if v is not None}})
        record["at"] = time.time()

    def get(self, task_id: str) -> dict | None:
        record = self._store.get(f"{_TASK_PREFIX}{task_id}")
        return dict(record) if isinstance(record, dict) else None

    def list_for_device(self, device_id: str, *, limit: int = 20) -> list[dict]:
        ids = self._store.get(f"{_INDEX_PREFIX}{device_id}") or []
        out = []
        for task_id in list(ids)[-limit:]:
            record = self.get(task_id)
            if record is not None:
                out.append(record)
        out.reverse()  # 新→旧
        return out

    # ---- 生命周期 ----

    def start(self, device_id: str, goal: str, *,
              max_steps: int = DEFAULT_MAX_STEPS,
              task_timeout_s: int = DEFAULT_TASK_TIMEOUT_S,
              action_timeout_s: int = DEFAULT_ACTION_TIMEOUT_S,
              observe_timeout_s: int = DEFAULT_OBSERVE_TIMEOUT_S,
              screenshot: bool = False,
              approval=None) -> dict:
        goal = str(goal or "").strip()
        if not 1 <= len(goal) <= 500:
            raise GuiTaskError("bad_request", "goal 须为 1..500 字符")
        max_steps = max(1, min(int(max_steps), 100))
        if isinstance(task_timeout_s, bool):
            raise GuiTaskError("bad_request", "task_timeout_s 非法")
        task_timeout_s = max(10, min(int(task_timeout_s), MAX_TASK_TIMEOUT_S))
        # 慢机（高负载/GSI）需要更宽的动作与观察窗口；设上下界防失控
        action_timeout_s = max(1, min(int(action_timeout_s), 120))
        observe_timeout_s = max(5, min(int(observe_timeout_s), 300))
        # 截图默认关（UI 树驱动决策；部分设备无媒体投影授权，强求截图整轮失败）
        screenshot = bool(screenshot)
        # 门控前置：设备状态/基本 scope 在编排层逐动作还会再查，这里先给
        # 调用方一个诚实的 409，而不是跑起来才撞墙。
        registry = self._gw2.registry
        if registry is not None:
            status = registry.status_of(device_id)
            if status != "active":
                raise GuiTaskError(
                    "device_revoked" if status == "revoked" else "unavailable",
                    f"设备状态 {status}")
            granted = set(registry.scopes_of(device_id))
            missing = {"device.observe", "device.tap"} - granted
            if missing:
                raise GuiTaskError(
                    "scope_denied", f"设备未授予 GUI 基础能力: {sorted(missing)}")
        decision = self._decision_factory()  # 未配置模型在此抛 GuiTaskError

        task_id = _new_task_id()
        run_id = new_id("run")
        record: dict[str, Any] = {
            "task_id": task_id, "at": time.time(), "goal": goal,
            "phase": "pending", "status": "pending",
            "capabilities": ["gui.agent"],
            "receipts": [], "owner_id": "owner",
            "kind": "gui_agent", "device_id": device_id, "run_id": run_id,
            "max_steps": max_steps, "task_timeout_s": task_timeout_s,
            "action_timeout_s": action_timeout_s,
            "observe_timeout_s": observe_timeout_s,
            "screenshot": screenshot,
            "events": [{"at": time.time(), "kind": "pending"}],
        }
        self._put_record(record)
        index = list(self._store.get(f"{_INDEX_PREFIX}{device_id}") or [])
        index.append(task_id)
        self._store.put(f"{_INDEX_PREFIX}{device_id}", index[-200:])
        recent = list(self._store.get(_RECENT_KEY) or [])
        recent.append(task_id)
        self._store.put(_RECENT_KEY, recent[-200:])

        from whale_core.devices.sync_bridge import gateway_loop
        loop = gateway_loop()
        future = asyncio.run_coroutine_threadsafe(
            self._run(record, decision, approval), loop)
        with self._lock:
            self._futures[task_id] = future
        return record

    def stop(self, task_id: str) -> dict:
        record = self.get(task_id)
        if record is None:
            raise GuiTaskError("not_found", f"未知任务 {task_id}")
        if record.get("status") in ("succeeded", "failed", "stopped"):
            return record  # 幂等：终态任务再 stop 直接回现状
        record["stop_requested"] = True
        self._event(record, "stop_requested")
        self._put_record(record)
        with self._lock:
            future = self._futures.get(task_id)
        if future is not None:
            future.cancel()
        # 急停 best-effort：取消设备侧在飞动作（scope 未授 device.stop 则跳过）
        registry = self._gw2.registry
        if registry is None or "device.stop" in set(
                registry.scopes_of(record["device_id"])):
            from whale_core.devices.sync_bridge import gateway_loop
            asyncio.run_coroutine_threadsafe(
                self._gw2.execute(
                    record["device_id"], "device.stop", {},
                    idempotency_key=f"gui-stop:{task_id}",
                    timeout_ms=5000),
                gateway_loop())
        return self.get(task_id) or record

    async def _run(self, record: dict, decision, approval) -> None:
        task_id = record["task_id"]
        device_id = record["device_id"]
        run_id = record["run_id"]
        trace = RunTraceWriter(run_id, sink_dir=self._trace_dir)
        record["trace_path"] = trace.path
        record["phase"] = record["status"] = "running"
        self._event(record, "running")
        self._put_record(record)
        from whale_core.devices.transports.base import ObserveRequest
        loop = GuiAgentLoop(
            GatewayV2Transport(self._gw2),
            max_steps=record["max_steps"],
            task_timeout_ms=record["task_timeout_s"] * 1000,
            action_timeout_ms=record["action_timeout_s"] * 1000,
            observe_timeout_ms=record["observe_timeout_s"] * 1000,
            observe_request=ObserveRequest(
                screenshot=bool(record.get("screenshot", False)),
                ui_tree=True))
        try:
            result: GuiRunResult = await loop.run(
                device_id, goal=record["goal"], decision=decision,
                approval=approval, trace=trace, task_id=task_id)
        except asyncio.CancelledError:
            record["phase"] = record["status"] = "stopped"
            record["reason"] = "stop requested"
            self._event(record, "stopped")
            self._finish(record, result=None)
            return
        except Exception as exc:  # noqa: BLE001 — 循环异常如实记 failed
            record["phase"] = record["status"] = "failed"
            record["reason"] = f"loop-error:{type(exc).__name__}"
            self._event(record, "failed", reason=record["reason"])
            self._finish(record, result=None)
            return
        status = {"succeeded": "succeeded", "stopped": "stopped"}.get(
            result.status, "failed")
        record["phase"] = record["status"] = status
        record["reason"] = result.reason
        record["steps"] = [
            {"step": s.step, "action": s.action_kind, "outcome": s.outcome,
             "summary": s.summary[:200], "verification": s.verification}
            for s in result.steps]
        self._event(record, status, detail=result.reason[:200])
        self._finish(record, result=result)

    def _finish(self, record: dict, result: GuiRunResult | None) -> None:
        self._put_record(record)
        with self._lock:
            self._futures.pop(record["task_id"], None)

    def shutdown(self) -> None:
        """进程退出时取消在飞任务（记录标 stopped，跨重启可对账）。"""
        with self._lock:
            futures = list(self._futures.items())
            self._futures.clear()
        for task_id, future in futures:
            future.cancel()
            record = self.get(task_id)
            if record and record.get("status") not in (
                    "succeeded", "failed", "stopped"):
                record["phase"] = record["status"] = "stopped"
                record["reason"] = "service shutdown"
                self._event(record, "stopped", detail="service shutdown")
                self._put_record(record)
