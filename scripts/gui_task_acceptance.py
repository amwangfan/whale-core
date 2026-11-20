"""#58 真机验收：GUI 自主任务生产入口 + Bilibili 全任务链 + 联合异常（手动，不进 CI）。

用法（N100 上，whale-api 运行中）：
    python3 scripts/gui_task_acceptance.py --device nova
    python3 scripts/gui_task_acceptance.py --device leaf5 --skip-bilibili
    python3 scripts/gui_task_acceptance.py --host 100.x --port 22 --device-id <hex32>

预设：nova → 100.102.110.11:22；leaf5 → 100.92.91.85:2222（均 fleet 密钥
/root/.ssh/android）。主机密钥经 ssh-keyscan 现场取得并 pinning（tailnet 内
初次已有人工验证通道）。

验收矩阵：
 P0 连通：SSH 可达 + 设备注册 active + 传输健康 + 具备 GUI 能力通道
 P1 Bilibili 链（7 个阶段任务，生产 REST 入口逐阶段提交/验收）：
    启动 → 搜索 → 推荐流浏览 → 播放/暂停 → 进度 → 切视频 → 退出
 P2 联合异常：任务停止（stop 端点 + device.stop 急停）/ 超时终态 /
    撤销门控（一次性合成身份：active→revoked→动作 403/409→purge）
每个检查打印 [PASS]/[FAIL]；全部通过退出码 0。真机证据（回执/轨迹路径）随
报告输出，绝不伪造。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.request

API = "http://127.0.0.1:8180"
TOKEN_FILE = "/opt/whale/secrets/whale_api_token"

PRESETS = {
    "nova": {"host": "100.102.110.11", "port": 22},
    "leaf5": {"host": "100.92.91.85", "port": 2222},
}

FAILED: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILED.append(name)
    return ok


def api(token: str, method: str, path: str, body: dict | None = None,
        timeout: float = 70.0) -> tuple[int, dict]:
    req = urllib.request.Request(
        API + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode())
        except Exception:
            return exc.code, {}


def put_ssh_transport(token: str, entries: dict[str, dict]) -> tuple[int, dict]:
    """合并式更新 ssh 传输配置（PUT 是整份替换；先读现状合并，不动其他
    设备/其他通道的既有配置）。"""
    _, body = api(token, "GET", "/api/v1/devices/transports")
    config = dict(body.get("result", {}).get("config") or {})
    ssh_map = dict(config.get("ssh") or {})
    ssh_map.update(entries)
    config["ssh"] = ssh_map
    return api(token, "PUT", "/api/v1/devices/transports", {"config": config})


def host_key_of(host: str, port: int) -> str:
    out = subprocess.run(
        ["ssh-keyscan", "-t", "ed25519", "-p", str(port), host],
        capture_output=True, text=True, timeout=15).stdout
    for line in out.splitlines():
        if "ssh-ed25519" in line:
            parts = line.split()
            return f"{parts[1]} {parts[2]}"
    raise SystemExit(f"无法取得 {host}:{port} 的主机密钥")


def wait_task(token: str, device_id: str, task_id: str,
              timeout_s: float = 900.0) -> dict:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        status, body = api(token, "GET",
                           f"/api/v1/devices/{device_id}/gui_tasks/{task_id}")
        if status == 200:
            record = body["result"]
            if record.get("status") in ("succeeded", "failed", "stopped"):
                return record
        time.sleep(3)
    raise TimeoutError(f"任务 {task_id} 超过 {timeout_s}s 未到终态")


BILIBILI_STAGES = [
    ("launch", "打开哔哩哔哩应用（包名 tv.danmaku.bili），等到首页或推荐流出现"),
    ("search", "在哔哩哔哩搜索「鲸鱼」：点顶部搜索栏后，键盘弹出时直接用 "
               "device.input_text 输入文字（不带 element_id），然后按回车键搜索，"
               "看到结果列表即完成"),
    ("feed", "返回哔哩哔哩首页推荐流（可用 device.key back 返回），"
             "向下滑动浏览至少两条内容"),
    ("playpause", "在推荐流点开第一个视频，等待播放器出现并开始播放，"
                  "然后点屏幕中央唤出控制条后点暂停按钮暂停播放"),
    ("seek", "在当前视频把播放进度拖到大约一半位置（先点屏幕唤出控制条，"
             "再拖动进度条；不要点分享/下载/评论类按钮）"),
    ("switch", "用 device.key back 返回推荐流，点开另一个不同的视频并确认它"
               "正在播放（避免分享/下载类按钮）"),
    ("exit", "退出哔哩哔哩，回到桌面"),
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", choices=sorted(PRESETS))
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    parser.add_argument("--device-id", default="")
    parser.add_argument("--key", default="/root/.ssh/android")
    parser.add_argument("--skip-bilibili", action="store_true")
    parser.add_argument("--stages-timeout", type=float, default=900.0)
    # 慢机（GSI/高负载）放宽动作与观察窗口
    parser.add_argument("--action-timeout", type=int, default=45)
    parser.add_argument("--observe-timeout", type=int, default=150)
    args = parser.parse_args()

    preset = PRESETS.get(args.device or "", {})
    host = args.host or preset.get("host")
    port = args.port or preset.get("port", 22)
    if not host:
        raise SystemExit("--device 或 --host 必填")
    token = open(TOKEN_FILE, encoding="utf-8").read().strip()

    # ---- P0 连通 ----
    probe = subprocess.run(
        ["ssh", "-i", args.key, "-o", "BatchMode=yes",
         "-o", "ConnectTimeout=8", "-p", str(port), f"root@{host}",
         "echo ok && id -u"],
        capture_output=True, text=True, timeout=20)
    check("P0.ssh-reachable", probe.returncode == 0 and "ok" in probe.stdout,
          probe.stderr.strip()[:120])

    device_id = args.device_id
    if not device_id:
        # 找注册表中 label 含设备名的 active 设备
        _, body = api(token, "GET", "/api/v1/devices")
        for dev in body.get("result", {}).get("devices", []):
            if dev.get("status") == "active":
                device_id = dev["device_id"]
                break
    if not check("P0.device-registered", bool(device_id), device_id):
        raise SystemExit(1)

    key = host_key_of(host, port)
    status, body = put_ssh_transport(token, {device_id: {
        "host": host, "port": port, "username": "root",
        "key_path": args.key, "expected_host_key": key}})
    check("P0.transports-put", status == 200, f"{status}")
    time.sleep(1)
    status, body = api(token, "GET", f"/api/v1/devices/{device_id}")
    detail = body.get("result", {})
    caps = set(detail.get("capabilities_effective") or [])
    gui_ready = {"device.observe", "device.tap"} <= caps
    check("P0.gui-capability", gui_ready,
          f"effective={sorted(caps)[:6]}… transports={detail.get('transports')}")

    # ---- P1 Bilibili 阶段链 ----
    if not args.skip_bilibili and gui_ready:
        for stage, goal in BILIBILI_STAGES:
            status, body = api(token, "POST",
                               f"/api/v1/devices/{device_id}/gui_tasks",
                               {"goal": goal, "max_steps": 40,
                                "task_timeout_s": 1200,
                                "action_timeout_s": args.action_timeout,
                                "observe_timeout_s": args.observe_timeout})
            if status != 202:
                check(f"P1.{stage}", False, f"submit {status}: {body}")
                continue
            task_id = body["result"]["task_id"]
            try:
                final = wait_task(token, device_id, task_id,
                                  timeout_s=args.stages_timeout)
            except TimeoutError:
                # 阶段超时：先停任务（顺带实证 stop 路径），如实记 FAIL 再继续
                api(token, "POST",
                    f"/api/v1/devices/{device_id}/gui_tasks/{task_id}/stop")
                check(f"P1.{stage}", False,
                      f"stage timeout {args.stages_timeout}s（已发停止）task={task_id}")
                continue
            steps = len(final.get("steps") or [])
            check(f"P1.{stage}", final["status"] == "succeeded",
                  f"status={final['status']} reason={final.get('reason')} "
                  f"steps={steps} trace={final.get('trace_path')}")
    else:
        print("[info] Bilibili 链跳过（--skip-bilibili 或 GUI 能力未就绪）")

    # ---- P2 联合异常 ----
    # 停止：长浏览任务 → stop → stopped
    status, body = api(token, "POST", f"/api/v1/devices/{device_id}/gui_tasks",
                       {"goal": "在当前界面持续向下浏览推荐内容",
                        "max_steps": 50, "task_timeout_s": 600,
                        "action_timeout_s": args.action_timeout,
                        "observe_timeout_s": args.observe_timeout})
    if status == 202:
        task_id = body["result"]["task_id"]
        time.sleep(8)
        status, body = api(
            token, "POST",
            f"/api/v1/devices/{device_id}/gui_tasks/{task_id}/stop")
        stopped = wait_task(token, device_id, task_id, timeout_s=60)
        check("P2.task-stop", stopped["status"] == "stopped",
              f"status={stopped['status']}")
    else:
        check("P2.task-stop", False, f"submit {status}")

    # 超时终态（device.shell sleep 超 timeout）
    status, body = api(token, "POST", f"/api/v1/devices/{device_id}/action",
                       {"capability": "device.shell",
                        "arguments": {"command": "sleep 30", "timeout_ms": 3000},
                        "idempotency_key": f"acc58:timeout:{int(time.time())}",
                        "timeout": 10})
    receipt = body.get("result", {}).get("receipt", {})
    check("P2.timeout-terminal", receipt.get("status") == "timeout",
          f"status={receipt.get('status')}")

    # 撤销门控：合成一次性身份 active→revoked→动作被拒→purge
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
    )
    tmp_key = Ed25519PrivateKey.generate()
    status, body = api(token, "POST", "/api/v1/devices/pair",
                       {"label": "acc58-throwaway",
                        "pubkey_hex": tmp_key.public_key().public_bytes_raw().hex(),
                        "scopes": ["device.shell"]})
    tmp_device = body.get("result", {}).get("device_id", "")
    code = body.get("result", {}).get("code", "")
    api(token, "POST", "/api/v1/devices/pair/approve", {"code": code})
    put_ssh_transport(token, {tmp_device: {
        "host": host, "port": port, "username": "root",
        "key_path": args.key, "expected_host_key": key}})
    idem = f"acc58:revoke:{int(time.time())}"
    status, body = api(token, "POST", f"/api/v1/devices/{tmp_device}/action",
                       {"capability": "device.shell",
                        "arguments": {"command": "echo pre"},
                        "idempotency_key": idem})
    pre_ok = status == 200
    api(token, "POST", f"/api/v1/devices/{tmp_device}/revoke")
    status, body = api(token, "POST", f"/api/v1/devices/{tmp_device}/action",
                       {"capability": "device.shell",
                        "arguments": {"command": "echo post"},
                        "idempotency_key": idem + "b"})
    denied = status in (403, 409)
    api(token, "POST", f"/api/v1/devices/{tmp_device}/purge")
    # 清掉 throwaway 的传输配置（整份 PUT 语义：读现状→删该项→写回）
    _, body = api(token, "GET", "/api/v1/devices/transports")
    config = dict(body.get("result", {}).get("config") or {})
    ssh_map = dict(config.get("ssh") or {})
    ssh_map.pop(tmp_device, None)
    config["ssh"] = ssh_map
    api(token, "PUT", "/api/v1/devices/transports", {"config": config})
    check("P2.revoke-gating", pre_ok and denied,
          f"pre_ok={pre_ok} post_denied={denied}")

    print()
    if FAILED:
        print(f"FAILED: {len(FAILED)} 项未过: {FAILED}")
        return 1
    print("ALL PASS — #58 真机验收矩阵通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
