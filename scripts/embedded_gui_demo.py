#!/usr/bin/env python3
"""GUI Agent 经 embedded WSS（生产通道）跑真机任务——第一轮闭环的正式形态。

observe/act 全部经 dev core REST → embedded WSS → 设备 agent2 执行器，
不经 ADB。录屏由 RunRecorder 经 ADB scrcpy 侧车（录制不改控制通道）。

用法：python3 scripts/embedded_gui_demo.py --device-id <hex32> --goal "..."
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from whale_core.devices.gui_agent import (                        # noqa: E402
    GuiAgentLoop,
    LlmJsonDecision,
    RunTraceWriter,
)
from whale_core.devices.transports.base import (                  # noqa: E402
    ActionReceipt,
    Observation,
    ObserveRequest,
    ReceiptStatus,
    UiElement,
    new_id,
    now_ms,
)

BASE = os.environ.get("WHALE_API", "http://127.0.0.1:8190")
TOKEN = (os.environ.get("WHALE_API_TOKEN")
         or open("/var/lib/whale-dev/api_token").read().strip())


def _call(method: str, path: str, body: dict | None, timeout: int = 120) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Authorization": f"Bearer {TOKEN}",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


class EmbeddedRestTransport:
    """embedded WSS 通道的 REST 门面 transport（DeviceTransport 语义）。"""

    kind = "embedded_wss"

    def __init__(self, device_id: str) -> None:
        self._did = device_id

    async def connect(self, device_id: str) -> None: ...
    async def disconnect(self, device_id: str) -> None: ...

    async def capabilities(self, device_id: str):
        return ["device.observe", "device.tap", "device.long_press",
                "device.swipe", "device.input_text", "device.key",
                "device.wait", "device.app.launch", "device.shell"]

    async def health(self, device_id: str):
        from whale_core.devices.transports.base import TransportHealth
        return TransportHealth(connected=True, detail="rest", checked_at=now_ms())

    async def observe(self, device_id: str, request: ObserveRequest) -> Observation:
        receipt = await self.execute_raw(device_id, "device.observe", {},
                                         timeout_s=90)
        if receipt.status is not ReceiptStatus.SUCCEEDED or not receipt.output_ref:
            raise RuntimeError(f"observe 失败: {receipt.status} {receipt.summary}")
        # 拉取观察负载
        q = await asyncio.to_thread(_call, "POST",
            f"/api/v1/devices/{self._did}/query",
            {"operation": "device.output.read",
             "filters": {"output_ref": receipt.output_ref},
             "max_items": 1, "timeout": 60}, 90)
        result = q["result"]["result"] if "result" in q.get("result", {}) else q["result"]
        items = result.get("data") or []
        if not items:
            raise RuntimeError("observe output_ref 拉取为空")
        doc = json.loads(items[0]["text"])
        return self._to_observation(doc)

    @staticmethod
    def _to_observation(doc: dict) -> Observation:
        import json as _json
        elements = []
        for e in doc.get("elements", []):
            b = e.get("bounds")
            if isinstance(b, str):
                # uiautomator 文本形态 "[l,t][r,b]"
                import re as _re
                nums = [int(x) for x in _re.findall(r"-?\d+", b)]
                b = nums[:4] if len(nums) >= 4 else [0, 0, 0, 0]
            if not isinstance(b, (list, tuple)):
                b = [0, 0, 0, 0]
            elements.append(UiElement(
                element_id=int(e.get("element_id", 0)),
                class_name=str(e.get("class_name") or ""),
                text=str(e.get("text") or ""),
                resource_id=str(e.get("resource_id") or ""),
                content_desc=str(e.get("content_desc") or ""),
                bounds=(int(b[0]), int(b[1]), int(b[2]), int(b[3])),
                clickable=bool(e.get("clickable")),
                long_clickable=bool(e.get("long_clickable", False)),
                scrollable=bool(e.get("scrollable", False)),
                editable=bool(e.get("editable", False)),
                enabled=bool(e.get("enabled", True)),
                fingerprint=str(e.get("fingerprint") or ""),
            ))
        ui_sig = hashlib.sha256(_json.dumps(
            [{k: v for k, v in el.__dict__.items() if k != "element_id"}
             for el in elements], sort_keys=True, default=str).encode()
        ).hexdigest()[:16]
        screen = doc.get("screen") or {}
        return Observation(
            observation_id=str(doc.get("observation_id") or new_id("obs")),
            device_id=str(doc.get("device_id") or ""),
            taken_at=int(doc.get("captured_at") or now_ms()),
            foreground_package=str(doc.get("foreground_package") or ""),
            activity=doc.get("activity"),
            elements=tuple(elements),
            ui_signature=ui_sig,
            screenshot_ref=doc.get("screenshot_ref"),
            screenshot_signature=None,
            screen_width=int(screen.get("w") or 0),
            screen_height=int(screen.get("h") or 0),
        )

    async def execute(self, device_id: str, action) -> ActionReceipt:
        return await self.execute_raw(
            device_id, action.capability, action.arguments,
            idem=action.idempotency_key,
            observation_id=action.observation_id,
            timeout_s=max(1, action.timeout_ms // 1000))

    async def execute_raw(self, device_id: str, capability: str,
                          arguments: dict, *, idem: str | None = None,
                          observation_id: str | None = None,
                          timeout_s: int = 30) -> ActionReceipt:
        body = {"capability": capability, "args": dict(arguments),
                "idempotency_key": idem or f"embedded-gui-{now_ms()}",
                "authorized_scope": [capability], "timeout": timeout_s}
        if observation_id:
            body["observation_id"] = observation_id
        started = now_ms()
        try:
            r = await asyncio.to_thread(
                _call, "POST", f"/api/v1/devices/{self._did}/action", body,
                timeout_s + 60)
            rec = r["result"]["receipt"]
            result = rec.get("result") or {}
            return ActionReceipt(
                action_id=str(rec.get("action_id") or ""),
                status=ReceiptStatus(rec.get("status", "failed")),
                summary=str(result.get("summary") or ""),
                output_ref=result.get("output_ref"),
                error_code=rec.get("error_code"),
                started_at=started, completed_at=now_ms())
        except Exception as exc:  # noqa: BLE001
            return ActionReceipt(
                action_id=idem or "", status=ReceiptStatus.FAILED,
                summary=f"rest: {type(exc).__name__}", error_code="unavailable",
                started_at=started, completed_at=now_ms())

    async def read_output(self, device_id, output_ref, *, offset=0, max_bytes=65536):
        return b"", False


def _load_secrets() -> dict[str, str]:
    out: dict[str, str] = {}
    path = "/opt/whale/secrets/qq_workflow.env"
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k] = v
    for k in ("DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL", "DEEPSEEK_MODEL"):
        if os.environ.get(k):
            out[k] = os.environ[k]
    return out


def make_complete(secrets):
    """决策模型：kimi-k3 经 AgentScope 平台（QQ_INTENT_* 凭据），thinking 关闭。

    用户指定：GUI 决策与实际使用都用 platform 的 kimi 模型、思考关/medium。
    """
    key = secrets.get("QQ_INTENT_API_KEY") or secrets.get("DEEPSEEK_API_KEY", "")
    base = (secrets.get("QQ_INTENT_BASE_URL")
            or secrets.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")).rstrip("/")
    model = secrets.get("QQ_INTENT_MODEL", "kimi-k3")
    if not base.endswith("/v1"):
        base += "/v1"

    async def complete(prompt: str) -> str:
        # kimi-k3 不接受 temperature 参数（platform 侧 400）；thinking 关闭
        body = json.dumps({"model": model,
                           "thinking": {"type": "disabled"},
                           "messages": [{"role": "user", "content": prompt}]}).encode()

        def _c() -> str:
            req = urllib.request.Request(
                f"{base}/chat/completions", data=body,
                headers={"Authorization": f"Bearer {key}",
                         "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=90) as resp:
                return json.loads(resp.read())["choices"][0]["message"]["content"]
        return await asyncio.to_thread(_c)

    return complete


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--device-id", required=True)
    ap.add_argument("--goal", required=True)
    ap.add_argument("--max-steps", type=int, default=12)
    ap.add_argument("--out", default="/tmp/whale-embedded-gui")
    args = ap.parse_args()

    transport = EmbeddedRestTransport(args.device_id)
    trace = RunTraceWriter(new_id("run"), sink_dir=args.out)
    loop = GuiAgentLoop(transport, max_steps=args.max_steps,
                        action_timeout_ms=60_000, observe_timeout_ms=120_000,
                        task_timeout_ms=1_200_000)
    result = await loop.run(args.device_id, goal=args.goal,
                            decision=LlmJsonDecision(make_complete(_load_secrets())),
                            trace=trace, task_id=new_id("task"))
    print(json.dumps({
        "status": result.status, "reason": result.reason,
        "steps": [{"step": s.step, "action": s.action_kind,
                   "outcome": s.outcome, "summary": s.summary,
                   "verification": s.verification} for s in result.steps],
        "trace": trace.path,
    }, ensure_ascii=False, indent=2))
    return 0 if result.status == "succeeded" else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
