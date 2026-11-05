#!/usr/bin/env python3
"""冒烟：生成一张简单结果图 → ResultDelivery 投递 owner 私聊（或离线图库 stub）。

用法（N100）：
  PYTHONPATH=src:plugins python3 scripts/smoke_result_delivery.py \\
    --secrets /opt/whale/secrets/qq_workflow.env
"""
from __future__ import annotations

import argparse
import os
import struct
import sys
import time
import zlib
from pathlib import Path


def _load_env(path: Path) -> dict:
    out = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def _png_rgb(w: int, h: int, rgb=(32, 128, 220), label_rows=8) -> bytes:
    """无第三方依赖的最小真彩 PNG（纯色 + 顶部深色条，便于肉眼辨认）。"""
    rows = []
    for y in range(h):
        row = bytearray([0])  # filter none
        for x in range(w):
            if y < label_rows:
                row.extend((20, 20, 20))
            else:
                row.extend(rgb)
        rows.append(bytes(row))
    raw = b"".join(rows)
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xffffffff))
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--secrets", required=True)
    ap.add_argument("--force-offline", action="store_true",
                    help="强制走手机图库 stub（不发 QQ）")
    args = ap.parse_args(argv)
    env = _load_env(Path(args.secrets))
    for k, v in env.items():
        os.environ.setdefault(k, v)

    sys.path[:0] = ["src", "plugins"]
    from weq_channel.full_mode_window import FullModeWindow, WindowedSendPort
    from weq_channel.weq_send import WeQWebSendPort
    from whale_core.life.result_delivery import DeliveryItem, ResultDelivery, summarize_receipts

    base = env.get("WEQ_WEB_URL") or "http://127.0.0.1:7690"
    token = env["WEQ_WEB_TOKEN"]
    owner_uid = env["OWNER_UID"]
    self_uin = env.get("QQ_SELF_UIN") or None

    port = WeQWebSendPort(base, token, target_uid=owner_uid,
                          expected_account_uin=self_uin)
    if os.environ.get("QQ_FULLMODE_ONDEMAND", "").strip() == "1":
        window = FullModeWindow(
            base, token,
            state_file=os.environ.get("QQ_FULLMODE_STATE",
                                      "/tmp/whale-fullmode-send.ts"))
        port = WindowedSendPort(port, window)

    stamp = time.strftime("%Y-%m-%d %H:%M:%S Asia/Shanghai")
    png = _png_rgb(320, 180)
    summary = f"whale ResultDelivery 冒烟 {stamp}"
    items = [
        DeliveryItem(kind="text", text=summary),
        DeliveryItem(kind="image", image_bytes=png,
                     filename=f"smoke-{int(time.time())}.png"),
    ]
    probe = (lambda: False) if args.force_offline else None
    delivery = ResultDelivery(port, owner_uid=owner_uid, online_probe=probe)
    receipts = delivery.deliver(items, summary=summary)
    result = summarize_receipts(receipts)
    print(result)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
