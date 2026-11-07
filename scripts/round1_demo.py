#!/usr/bin/env python3
"""第一轮闭环演示（开发计划 §14）：

    IM(QQ 模拟) 发起任务 → Core(GUI Agent) → ADB 操作真机 → 结果回 IM(记录不发送)
    → scrcpy 录屏 + RunTrace JSONL → 导出 MP4

用法：
    WHALE_ADB=/path/to/adb WHALE_DEVICE_ID=<paired-device-id> \
    WHALE_ADB_SERIAL=<adb-serial> python3 scripts/round1_demo.py \
        --goal "打开设置，找到电池相关页面，告诉我你看到了什么"

QQ 通道为模拟模式（RecordingOneBotTransport），全程零真实发送。
需要 DEEPSEEK_API_KEY（从 /opt/whale/secrets/qq_workflow.env 读取，或环境变量）。
"""
from __future__ import annotations

import argparse
import asyncio
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
from whale_core.devices.recording._types import RecordingOptions  # noqa: E402
from whale_core.devices.recording.run_recorder import RunRecorder  # noqa: E402
from whale_core.devices.recording.store import RecordingStore      # noqa: E402
from whale_core.devices.transports.adb import AdbTransport         # noqa: E402
from whale_core.devices.transports.base import new_id, now_ms      # noqa: E402
from whale_core.integrations.im.gateway import ImGateway           # noqa: E402
from whale_core.integrations.im.providers.qq_dryrun import (       # noqa: E402
    QqDryRunProvider,
)
from whale_core.memory import InMemoryStore                        # noqa: E402
from whale_core.notifications.gateway import NotificationGateway   # noqa: E402
from whale_core.notifications.providers.whale_wss import (          # noqa: E402
    WhaleWssProvider,
)
from whale_core.notifications.services.notification_service import (  # noqa: E402
    NotificationService,
)


class RecordingWssPort:
    """演示用 WSS 端口：记录 notification.event payload，不真实发送。

    生产通知由 Core EmbeddedWssGateway 与 Mobile NodeRuntimeV2 接线。
    """

    def __init__(self) -> None:
        self.sent: list[dict] = []

    def is_online(self, device_id: str) -> bool:
        return True

    async def send_notification_event(self, device_id: str, payload: dict) -> None:
        self.sent.append({"device_id": device_id, "payload": payload})

# 设备身份与 ADB 地址由运行环境提供，不把个人 tailnet 拓扑或设备 ID 提交到仓库。


class RestTransport:
    """经 whale-core 管理 REST API 驱动已在线设备的 V2 embedded WSS 动作。

    observe 暂不支持（REST 观察返回 output_ref，需二次拉取——本演示用
    ADB/SSH 做观察，REST 做动作）；execute 走 Core 的真实动作与回执链路。
    """

    kind = "embedded_wss"

    def __init__(self, api_base: str, token: str) -> None:
        self._base = api_base.rstrip("/")
        self._token = token

    async def connect(self, device_id: str) -> None: ...
    async def disconnect(self, device_id: str) -> None: ...

    async def capabilities(self, device_id: str):
        return ["device.observe", "device.tap", "device.long_press",
                "device.swipe", "device.input_text", "device.key",
                "device.wait", "device.app.launch", "device.shell"]

    async def health(self, device_id: str):
        from whale_core.devices.transports.base import TransportHealth, now_ms
        return TransportHealth(connected=True, detail="rest", checked_at=now_ms())

    async def observe(self, device_id, request):
        raise RuntimeError("unavailable: REST transport 不支持 observe")

    async def execute(self, device_id, action):
        import urllib.request
        from whale_core.devices.transports.base import (
            ActionReceipt, ReceiptStatus, now_ms)
        body = {
            "capability": action.capability,
            "args": action.arguments,
            "idempotency_key": action.idempotency_key,
            "authorized_scope": [action.capability],
            "timeout": max(1, action.timeout_ms // 1000),
        }
        req = urllib.request.Request(
            f"{self._base}/api/v1/devices/{device_id}/action",
            data=json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {self._token}",
                     "Content-Type": "application/json"})
        started = now_ms()
        try:
            with urllib.request.urlopen(req, timeout=action.timeout_ms / 1000 + 60) as resp:
                r = json.loads(resp.read())["result"]["receipt"]
            return ActionReceipt(
                action_id=action.action_id,
                status=ReceiptStatus(r.get("status", "failed")),
                summary=str((r.get("result") or {}).get("summary", "")),
                error_code=r.get("error_code"),
                started_at=started, completed_at=now_ms())
        except Exception as exc:  # noqa: BLE001
            return ActionReceipt(
                action_id=action.action_id, status=ReceiptStatus.FAILED,
                summary=f"rest: {type(exc).__name__}", error_code="unavailable",
                started_at=started, completed_at=now_ms())

    async def read_output(self, device_id, output_ref, *, offset=0, max_bytes=65536):
        return b"", False


def _load_secrets() -> dict[str, str]:
    path = "/opt/whale/secrets/qq_workflow.env"
    out: dict[str, str] = {}
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


def make_deepseek_complete(secrets: dict[str, str]):
    key = secrets.get("DEEPSEEK_API_KEY", "")
    base = secrets.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    model = secrets.get("DEEPSEEK_MODEL", "deepseek-chat")
    if not key:
        return None

    async def complete(prompt: str) -> str:
        body = json.dumps({
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.2,
        }).encode()

        def _call() -> str:
            root = base.rstrip("/")
            url = (root if root.endswith("/v1") else root + "/v1") + "/chat/completions"
            req = urllib.request.Request(
                url, data=body,
                headers={"Authorization": f"Bearer {key}",
                         "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=90) as resp:
                data = json.loads(resp.read())
            return data["choices"][0]["message"]["content"]

        return await asyncio.to_thread(_call)

    return complete


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--device-id", default=os.environ.get("WHALE_DEVICE_ID"))
    ap.add_argument("--serial", default=os.environ.get("WHALE_ADB_SERIAL"))
    ap.add_argument("--goal",
                    default="打开设置，找到「电池」页面，总结你看到的选项")
    ap.add_argument("--out", default="/tmp/whale-round1-demo")
    ap.add_argument("--max-steps", type=int, default=12)
    ap.add_argument("--action-timeout-ms", type=int, default=30000,
                    help="单动作超时（慢设备如 nova 需要 30s 级）")
    args = ap.parse_args()

    device_id, serial = args.device_id, args.serial
    if not device_id or not serial:
        print("FATAL: 请通过 WHALE_DEVICE_ID 与 WHALE_ADB_SERIAL，或 --device-id / --serial 指定设备")
        return 2
    out_dir = os.path.join(args.out, str(now_ms()))
    os.makedirs(out_dir, exist_ok=True)

    secrets = _load_secrets()
    complete = make_deepseek_complete(secrets)
    if complete is None:
        print("FATAL: 无 DEEPSEEK_API_KEY（secrets 与环境变量都没有）")
        return 2

    # ---- 通道装配 ----
    adb = AdbTransport(device_serials={device_id: serial})
    await adb.connect(device_id)

    # 唤醒 + 解屏前置（nova 等会 Doze 的设备观察会超时；与 record_smoke 同款处理）
    wake = await adb.execute(
        device_id,
        __import__("whale_core.devices.transports.base", fromlist=["ActionRequest"])
        .ActionRequest(
            action_id=new_id("act"), capability="device.shell",
            arguments={"command": "input keyevent KEYCODE_WAKEUP; sleep 1; "
                                  "wm dismiss-keyguard; "
                                  "input swipe 540 2000 540 400 150; true"},
            idempotency_key=f"demo-wake:{now_ms()}", timeout_ms=8000))
    print(f"[demo] 设备唤醒: {wake.status.value}")

    # ---- IM（QQ 模拟：零真实发送）----
    provider = QqDryRunProvider(bot_id="whale-demo")
    task_results: list[dict] = []
    pending: list[asyncio.Task] = []

    # ---- 原生通知（修正方案 §13：WSS 首选；演示用 RecordingWssPort 记录）----
    wss_port = RecordingWssPort()
    notif_gateway = NotificationGateway(
        InMemoryStore(), providers=[WhaleWssProvider(wss_port)])
    notify_service = NotificationService(notif_gateway)

    async def run_task(goal_text: str, target_id: str) -> None:
        """收到 IM 任务 → GUI Agent 真机执行 → 结果投递回 IM（记录不发送）。"""
        run_id = new_id("run")
        trace = RunTraceWriter(run_id,
                               sink_dir=os.path.join(out_dir, "traces"))
        store = RecordingStore(root=os.path.join(out_dir, "recordings"))
        recorder = RunRecorder(store=store)
        rec_started = False
        try:
            await recorder.start(
                serial, run_id=run_id, device_id=device_id,
                options=RecordingOptions(profile="phone_only"))
            rec_started = True
        except Exception as exc:  # noqa: BLE001 — 录制失败不阻塞任务（诚实降级）
            print(f"[demo] 录屏启动失败（不阻塞任务）: {type(exc).__name__}: {exc}")

        loop = GuiAgentLoop(adb, max_steps=args.max_steps,
                            action_timeout_ms=args.action_timeout_ms)
        decision = LlmJsonDecision(complete)
        result = await loop.run(device_id, goal=goal_text, decision=decision,
                                trace=trace, task_id=new_id("task"))

        reply = (f"任务{'完成' if result.status == 'succeeded' else '结束'}"
                 f"（{result.status}）：{result.reason}\n"
                 f"共 {len(result.steps)} 步。轨迹与录屏已存证。")
        im.send_text(target_id, reply)

        # 任务事件 → 原生通知链（notification.event，走 WSS 首选通道）
        notify_kwargs = dict(
            title=("任务已完成" if result.status == "succeeded" else "任务失败"),
            body=f"{result.reason}（{len(result.steps)} 步）",
        )
        if result.status == "succeeded":
            await notify_service.task_completed(device_id, task_id=run_id,
                                                **notify_kwargs)
        else:
            await notify_service.task_failed(device_id, task_id=run_id,
                                             **notify_kwargs)

        export_path = ""
        if rec_started:
            try:
                await recorder.stop()
                export_path = str(await recorder.export(title=goal_text[:30]))
            except Exception as exc:  # noqa: BLE001
                print(f"[demo] 录屏收尾失败: {type(exc).__name__}: {exc}")
        task_results.append({
            "run_id": run_id, "status": result.status, "reason": result.reason,
            "steps": [
                {"step": s.step, "action": s.action_kind, "outcome": s.outcome,
                 "summary": s.summary, "verification": s.verification}
                for s in result.steps
            ],
            "trace": trace.path, "export": export_path,
        })

    def on_message(msg, conversation_id: str) -> None:
        pending.append(asyncio.create_task(run_task(msg.text, msg.target_id)))

    im = ImGateway(message_handler=on_message)
    im.register_provider(provider)

    # 模拟主人从 QQ 私聊发任务（OneBot v11 私聊消息事件）
    provider.feed({
        "post_type": "message", "message_type": "private",
        "user_id": "10000", "message_id": "demo-0001",
        "message": args.goal, "time": now_ms() // 1000,
    })
    provider.pump()
    if pending:
        await asyncio.gather(*pending)

    # ---- 报告 ----
    sent = provider.drain_sent()
    report = {
        "device": args.device_id, "goal": args.goal,
        "task_results": task_results,
        "im_outbound_recorded_count": len(sent),
        "im_outbound_preview": [s.get("params", {}) for s in sent][:3],
        "notification_events_recorded": [
            {"device_id": s["device_id"],
             "category": s["payload"].get("category"),
             "title": s["payload"].get("title"),
             "notification_id": s["payload"].get("notification_id")}
            for s in wss_port.sent
        ],
        "out_dir": out_dir,
    }
    report_path = os.path.join(out_dir, "demo_report.json")
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"\n演示产物目录: {out_dir}")
    return 0 if task_results and task_results[0]["status"] == "succeeded" else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
