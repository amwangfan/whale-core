#!/usr/bin/env python3
"""SshTransport 真机冒烟（leaf5 + nova）。

用法：PYTHONPATH=src python3 scripts/ssh_smoke.py

覆盖任务书要求的五项：
1. device.shell `echo hello && id`（leaf5 应 uid=0）
2. device.shell `getprop ro.product.model`（nova 验证 env_prefix/PATH 处理）
3. device.file.write + read 回环（/data/local/tmp/whale_ssh_test.txt）
4. device.shell 超时（sleep 60 timeout 3s → TIMEOUT）
5. device.file.list /data/local/tmp

主机密钥从 ssh-keyscan 结果 pinning（ed25519），绝不 accept-any。
"""

import asyncio
import base64
import json
import sys

from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from whale_core.devices.transports.base import ActionRequest, ReceiptStatus  # noqa: E402
from whale_core.devices.transports.ssh import SshDeviceTarget, SshTransport  # noqa: E402

# ssh-keyscan 实测 pinning（2026-11，leaf5 dropbear / nova OpenSSH chroot）
TARGETS = {
    "leaf5": SshDeviceTarget(
        host="100.92.91.85", port=2222, username="root",
        key_path="/root/.ssh/android",
        expected_host_key="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIMyUyplbqOQSF"
                          "+Kt0pMXMSEq5zoB3hXOxsuE9QUDbKSk"),
    "nova": SshDeviceTarget(
        host="100.102.110.11", port=22, username="root",
        key_path="/root/.ssh/android",
        expected_host_key="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAqOwYxWJS1Zt"
                          "y6pHcRlNmMHgXy574r2VSzBXPKcbsO0",
        # NetHunter chroot：Android 命令需要显式 PATH
        env_prefix="export PATH=/system/bin:/system/xbin:$PATH"),
}

_seq = 0


def act(capability, arguments, timeout_ms=15_000):
    global _seq
    _seq += 1
    return ActionRequest(action_id=f"act_SMOKE{_seq:02d}", capability=capability,
                         arguments=arguments, idempotency_key=f"smoke-{_seq}",
                         timeout_ms=timeout_ms)


async def smoke_device(t: SshTransport, device_id: str):
    print(f"\n{'='*64}\n[{device_id}] 冒烟开始\n{'='*64}")
    await t.connect(device_id)
    h = await t.health(device_id)
    print(f"health: connected={h.connected} detail={h.detail!r}")
    print(f"capabilities: {list(await t.capabilities(device_id))}")

    # 1. device.shell echo hello && id
    r = await t.execute(device_id, act("device.shell",
                                       {"command": "echo hello && id"}))
    print(f"\n[1] shell 'echo hello && id' -> {r.status.value}")
    print(f"    summary={r.summary}")
    print(f"    provider_ref={r.provider_ref}")

    # 2. getprop（nova 走 env_prefix 的 PATH；leaf5 原生 Android shell）
    r = await t.execute(device_id, act(
        "device.shell", {"command": "getprop ro.product.model"}))
    print(f"\n[2] getprop ro.product.model -> {r.status.value}")
    print(f"    summary={r.summary}")

    # 3. file.write + read 回环
    payload = f"whale-ssh-smoke:{device_id}\n".encode()
    w = await t.execute(device_id, act("device.file.write", {
        "path": "/data/local/tmp/whale_ssh_test.txt",
        "data_b64": base64.b64encode(payload).decode()}))
    print(f"\n[3] file.write -> {w.status.value} summary={w.summary}")
    rd = await t.execute(device_id, act("device.file.read", {
        "path": "/data/local/tmp/whale_ssh_test.txt"}))
    got, _ = await t.read_output(device_id, rd.output_ref, max_bytes=1 << 20)
    ok = got == payload
    print(f"    file.read  -> {rd.status.value} 回环一致={ok} "
          f"summary={rd.summary}")

    # 4. 超时：sleep 60 timeout 3s → TIMEOUT（不抛异常）
    r = await t.execute(device_id, act(
        "device.shell", {"command": "sleep 60", "timeout_ms": 3000},
        timeout_ms=3000))
    print(f"\n[4] sleep 60 timeout=3s -> {r.status.value} "
          f"error_code={r.error_code} summary={r.summary!r}")
    if r.status is ReceiptStatus.TIMEOUT:
        print(f"    （符合预期：TIMEOUT，未抛异常）")
    else:
        print(f"    !! 非预期状态")

    # 5. file.list /data/local/tmp（条目多时全量走 output_ref）
    r = await t.execute(device_id, act("device.file.list",
                                       {"path": "/data/local/tmp"}))
    s = json.loads(r.summary)
    if r.output_ref:
        blob, _ = await t.read_output(device_id, r.output_ref,
                                      max_bytes=1 << 22)
        entries = json.loads(blob.decode())["entries"]
    else:
        entries = s.get("entries", [])
    names = [e["name"] for e in entries][:10]
    print(f"\n[5] file.list /data/local/tmp -> {r.status.value} "
          f"count={s.get('count')} output_ref={r.output_ref} 前10项={names}")

    await t.disconnect(device_id)
    print(f"\n[{device_id}] 冒烟结束")


async def main():
    t = SshTransport(TARGETS)
    failed = False
    for device_id in ("leaf5", "nova"):
        try:
            await smoke_device(t, device_id)
        except Exception as exc:
            failed = True
            print(f"\n[{device_id}] !! 冒烟异常: {exc}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    asyncio.run(main())
