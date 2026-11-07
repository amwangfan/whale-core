#!/usr/bin/env python3
"""v2 端到端真机验收驱动（经运行中 dev core 的 REST，8190）。

序列：
1. GET /devices —— 设备在线状态 + 能力
2. POST /devices/{id}/action  —— device.app.launch 打开设置（embedded_wss 通道）
3. POST /devices/{id}/query   —— device.status 查询
4. POST /devices/{id}/action  —— device.observe（观察回执）
5. 通知：直接调通知服务的 REST？（若无专用端点，用 action phone.notification.post）

用法：python3 scripts/e2e_v2_device.py --device-id <hex32>
环境：WHALE_API_TOKEN 或默认读 /var/lib/whale-dev/api_token。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request

BASE = os.environ.get("WHALE_API", "http://127.0.0.1:8190")
TOKEN = os.environ.get("WHALE_API_TOKEN") or open(
    "/var/lib/whale-dev/api_token").read().strip()


def call(method: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Authorization": f"Bearer {TOKEN}",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return {"_http_error": e.code, "body": e.read().decode()[:400]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--device-id", required=True)
    args = ap.parse_args()
    did = args.device_id

    print("== 1. 设备状态 ==")
    devices = call("GET", "/api/v1/devices")
    for d in devices.get("result", {}).get("devices", []):
        if d["device_id"] == did:
            print(json.dumps({k: d[k] for k in ("label", "online", "status")},
                             ensure_ascii=False))
            caps = d.get("capabilities") or d.get("scopes") or []
            print("capabilities:", len(caps), sorted(caps)[:8], "...")
            break
    else:
        print("设备不在注册表！")

    print("== 2. device.app.launch 设置 ==")
    r = call("POST", f"/api/v1/devices/{did}/action", {
        "capability": "device.app.launch",
        "args": {"package": "com.android.settings"},
        "idempotency_key": "e2e-v2-launch-settings-001",
        "authorized_scope": ["device.app.launch"],
        "timeout": 30,
    })
    print(json.dumps(r, ensure_ascii=False)[:400])

    print("== 3. device.status 查询 ==")
    r = call("POST", f"/api/v1/devices/{did}/query", {
        "operation": "device.status", "filters": {}, "max_items": 5,
        "timeout": 30,
    })
    print(json.dumps(r, ensure_ascii=False)[:400])

    print("== 4. device.observe ==")
    r = call("POST", f"/api/v1/devices/{did}/action", {
        "capability": "device.observe",
        "args": {},
        "idempotency_key": "e2e-v2-observe-001",
        "authorized_scope": ["device.observe"],
        "timeout": 60,
    })
    print(json.dumps(r, ensure_ascii=False)[:400])

    print("== 5. 原生通知（phone.notification.post） ==")
    r = call("POST", f"/api/v1/devices/{did}/action", {
        "capability": "phone.notification.post",
        "args": {"title": "Whale v2 E2E", "body": "端到端验收通知"},
        "idempotency_key": "e2e-v2-notify-001",
        "authorized_scope": ["phone.notification.post"],
        "timeout": 20,
    })
    print(json.dumps(r, ensure_ascii=False)[:400])
    return 0


if __name__ == "__main__":
    sys.exit(main())
