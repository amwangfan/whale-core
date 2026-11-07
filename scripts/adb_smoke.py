"""ADB Transport 真机冒烟（手动运行，不进 CI）。

用法：
    python3 scripts/adb_smoke.py                 # 默认 leaf5 + nova
    python3 scripts/adb_smoke.py 100.92.91.85:5555

流程（对应 contracts/v2 §7/§8 语义）：
    connect → health → observe（前台包名+元素数+截图 ref）
    → device.wait 500ms → 再 observe（ui_signature/截图签名对比，验证屏稳检测可行）
    → device.app.launch com.android.settings → observe 验证前台切换
退出码：全部通过 0，任一失败 1。
"""
from __future__ import annotations

import asyncio
import json
import sys
import time

from pathlib import Path

# 仓库未安装为包时也能直接跑（pythonpath 约定与 pyproject 一致）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from whale_core.devices.transports.adb import (  # noqa: E402
    AdbTransport,
    screenshot_difference,
)
from whale_core.devices.transports.base import (  # noqa: E402
    ActionRequest,
    ObserveRequest,
    ReceiptStatus,
    new_id,
)

ADB_PATH = "/opt/android-sdk/platform-tools/adb"
DEFAULT_DEVICES = {
    "leaf5": "100.92.91.85:5555",   # Android 13, Magisk root
    "nova": "100.102.110.11:5555",  # Android 9 GSI, root
}


async def smoke_device(transport: AdbTransport, device_id: str) -> dict:
    """对单台设备跑完整冒烟；返回结构化报告（ok=全部检查通过）。"""
    report: dict = {"device_id": device_id, "ok": True, "checks": {}}
    checks: dict[str, dict] = {}

    def record(name: str, ok: bool, **detail) -> None:
        checks[name] = {"ok": ok, **detail}
        if not ok:
            report["ok"] = False

    try:
        await transport.connect(device_id)
        record("connect", True)
    except Exception as exc:  # noqa: BLE001 - 冒烟要报告而非中断
        record("connect", False, error=repr(exc))
        report["checks"] = checks
        return report

    health = await transport.health(device_id)
    record("health", health.connected, detail=health.detail)

    obs1 = await transport.observe(device_id, ObserveRequest())
    shot1_size = transport._outputs.size_of(obs1.screenshot_ref or "")  # noqa: SLF001
    record("observe_1", bool(obs1.elements),
           foreground=obs1.foreground_package, activity=obs1.activity,
           elements=len(obs1.elements), truncated=obs1.truncated,
           screen=f"{obs1.screen_width}x{obs1.screen_height}",
           screenshot_ref=obs1.screenshot_ref, screenshot_bytes=shot1_size,
           locked=obs1.locked, keyboard=obs1.keyboard_shown,
           ui_signature=obs1.ui_signature[:16])

    wait_receipt = await transport.execute(device_id, ActionRequest(
        action_id=new_id("act"), capability="device.wait",
        arguments={"ms": 500}, idempotency_key=f"smoke:wait:{device_id}"))
    record("wait_500ms", wait_receipt.status is ReceiptStatus.SUCCEEDED,
           status=wait_receipt.status.value, summary=wait_receipt.summary)

    obs2 = await transport.observe(device_id, ObserveRequest())
    same_ui = obs1.ui_signature == obs2.ui_signature
    shot_diff = screenshot_difference(
        obs1.screenshot_signature or "", obs2.screenshot_signature or "")
    record("observe_2_stable_screen", True,  # 屏稳只观测不硬断言（e-ink 残影等）
           ui_signature_equal=same_ui,
           screenshot_difference=round(shot_diff, 6),
           elements=len(obs2.elements))

    launch = await transport.execute(device_id, ActionRequest(
        action_id=new_id("act"), capability="device.app.launch",
        arguments={"package": "com.android.settings"},
        idempotency_key=f"smoke:launch:{device_id}"))
    record("launch_settings", launch.status is ReceiptStatus.SUCCEEDED,
           status=launch.status.value, summary=launch.summary)

    await asyncio.sleep(1.5)  # 等 Activity 切换
    obs3 = await transport.observe(device_id, ObserveRequest(screenshot=False))
    fg_switched = obs3.foreground_package == "com.android.settings"
    record("observe_3_foreground_switched", fg_switched,
           foreground=obs3.foreground_package, activity=obs3.activity,
           elements=len(obs3.elements),
           ui_changed=obs3.ui_signature != obs2.ui_signature)

    report["checks"] = checks
    return report


async def _main(argv: list[str]) -> int:
    serials = argv[1:]
    devices = ({f"dev{i}": s for i, s in enumerate(serials)}
               if serials else DEFAULT_DEVICES)
    transport = AdbTransport(devices, adb_path=ADB_PATH)
    started = time.time()
    reports = []
    for device_id in devices:
        print(f"[smoke] {device_id} ({devices[device_id]}) ...", flush=True)
        reports.append(await smoke_device(transport, device_id))
    elapsed = round(time.time() - started, 1)
    print(json.dumps({"elapsed_s": elapsed, "reports": reports},
                     ensure_ascii=False, indent=2))
    ok = all(r["ok"] for r in reports)
    print(f"[smoke] {'全部通过' if ok else '存在失败'}（{elapsed}s）")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main(sys.argv)))
