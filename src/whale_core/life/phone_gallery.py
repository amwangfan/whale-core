"""手机图库投递（Magic8 等）。

现状：whale-mobile / 设备 agent 尚无「写入系统图库」现成能力面。
本模块提供：
1. stub_save_to_gallery — 落盘到 N100 状态目录并记日志（可审计、可事后人工导入）；
2. probe_adb_mediastore — 若本机 adb 能看到设备，尝试 content insert / media scan
   （能做就做；失败诚实返回，不假装成功）。
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
from pathlib import Path

log = logging.getLogger("whale_core.phone_gallery")

DEFAULT_STAGING = Path(os.environ.get(
    "WHALE_PHONE_GALLERY_STAGING", "/opt/whale/state/phone-gallery-staging"))


def stub_save_to_gallery(image_bytes: bytes, filename: str = "result.png",
                         *, staging_dir: Path | None = None) -> tuple[bool, str]:
    """无图库 API 时的诚实 stub：写入 staging 目录，返回 stub: 前缀明细。"""
    if not image_bytes:
        return False, "stub:empty-bytes"
    root = Path(staging_dir or DEFAULT_STAGING)
    try:
        root.mkdir(parents=True, exist_ok=True)
        safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in (filename or "result.png"))
        safe = safe[:80] or "result.png"
        stamp = time.strftime("%Y%m%d-%H%M%S")
        path = root / f"{stamp}-{safe}"
        path.write_bytes(image_bytes)
        log.info("phone_gallery stub saved %s (%d bytes)", path, len(image_bytes))
        # 顺带尝试 ADB；成功则通道升级为真实图库
        adb_ok, adb_detail = probe_adb_mediastore(path)
        if adb_ok:
            return True, adb_detail
        return True, f"stub:staged:{path.name}; adb={adb_detail}"
    except OSError as exc:
        return False, f"stub:io:{type(exc).__name__}"


def probe_adb_mediastore(local_path: Path) -> tuple[bool, str]:
    """探测 adb 设备并把图片推进 MediaStore（尽力而为）。

    需要：PATH 上有 adb、至少一台 device、对端允许文件推送。
    非 root Magic8 通常可 `adb push` 到 `/sdcard/Pictures/Whale/` 再
    `am broadcast` media scanner。
    """
    adb = shutil.which("adb")
    if not adb:
        return False, "adb-missing"
    try:
        devices = subprocess.check_output(
            [adb, "devices"], text=True, timeout=5, stderr=subprocess.DEVNULL)
    except Exception as exc:
        return False, f"adb-devices:{type(exc).__name__}"
    serials = []
    for line in devices.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            serials.append(parts[0])
    if not serials:
        return False, "adb-no-device"
    serial = serials[0]
    remote_dir = "/sdcard/Pictures/Whale"
    remote = f"{remote_dir}/{local_path.name}"
    try:
        subprocess.check_call(
            [adb, "-s", serial, "shell", "mkdir", "-p", remote_dir],
            timeout=10, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.check_call(
            [adb, "-s", serial, "push", str(local_path), remote],
            timeout=60, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # 触发媒体扫描（Android 版本差异大，失败不掩盖 push 成功）
        scan = subprocess.run(
            [adb, "-s", serial, "shell", "am", "broadcast",
             "-a", "android.intent.action.MEDIA_SCANNER_SCAN_FILE",
             "-d", f"file://{remote}"],
            timeout=15, capture_output=True, text=True)
        scan_ok = scan.returncode == 0
        detail = f"adb-push:{serial}:{remote};scan={'ok' if scan_ok else 'skip'}"
        log.info("phone_gallery %s", detail)
        return True, detail
    except Exception as exc:
        return False, f"adb-push-failed:{type(exc).__name__}"
