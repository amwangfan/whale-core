"""录制模块内部 ADB 子进程助手（不依赖 transports/，自包含）。

约定与 transports/adb.py 一致：直接 argv 不经宿主 shell、有界输出、
超时即杀进程。这里只提供录制模块需要的最小集合。
"""

from __future__ import annotations

import asyncio
import hashlib
import os

from dataclasses import dataclass

DEFAULT_ADB_PATH = os.environ.get("WHALE_ADB", "adb")
DEFAULT_ADB_TIMEOUT_S = 15.0
MAX_OUTPUT_BYTES = 1 * 1024 * 1024  # 单次 adb 命令输出硬上限


class AdbError(Exception):
    """adb 命令失败（非零退出 / 超时 / 输出超限）。"""


@dataclass(frozen=True)
class AdbResult:
    returncode: int
    stdout: bytes
    stderr: bytes


async def _bounded_read(stream: asyncio.StreamReader | None, limit: int) -> bytes:
    """有界读取子进程流：超过 limit 抛 AdbError。"""
    if stream is None:
        return b""
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await stream.read(65536)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise AdbError(f"adb 输出超过上限 {limit} 字节")
        chunks.append(chunk)
    return b"".join(chunks)


async def adb(
    serial: str,
    *args: str,
    timeout_s: float = DEFAULT_ADB_TIMEOUT_S,
    adb_path: str = DEFAULT_ADB_PATH,
) -> AdbResult:
    """执行 ``adb -s <serial> <args...>``，非零退出抛 AdbError。"""
    proc = await asyncio.create_subprocess_exec(
        adb_path, "-s", serial, *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            _communicate_bounded(proc), timeout=timeout_s
        )
    except (asyncio.TimeoutError, AdbError):
        await _kill_quietly(proc)
        raise
    result = AdbResult(proc.returncode or 0, stdout, stderr)
    if result.returncode != 0:
        raise AdbError(
            f"adb {' '.join(args)} 失败（rc={result.returncode}）: "
            f"{result.stderr.decode(errors='replace')[:500]}"
        )
    return result


async def _communicate_bounded(
    proc: asyncio.subprocess.Process,
) -> tuple[bytes, bytes]:
    return (
        await _bounded_read(proc.stdout, MAX_OUTPUT_BYTES),
        await _bounded_read(proc.stderr, MAX_OUTPUT_BYTES),
    )


async def _kill_quietly(proc: asyncio.subprocess.Process) -> None:
    try:
        proc.kill()
    except ProcessLookupError:
        pass
    try:
        await asyncio.wait_for(proc.wait(), timeout=5)
    except asyncio.TimeoutError:
        pass


async def adb_shell_text(
    serial: str,
    command: str,
    *,
    timeout_s: float = DEFAULT_ADB_TIMEOUT_S,
    adb_path: str = DEFAULT_ADB_PATH,
) -> str:
    """执行远端 shell 命令并返回 stdout 文本（strip 后）。"""
    result = await adb(serial, "shell", command, timeout_s=timeout_s, adb_path=adb_path)
    return result.stdout.decode(errors="replace").strip()


async def remote_sha256(
    serial: str,
    remote_path: str,
    *,
    adb_path: str = DEFAULT_ADB_PATH,
) -> str | None:
    """读取远端文件 sha256（设备无 sha256sum 时返回 None）。"""
    try:
        out = await adb_shell_text(
            serial, f"sha256sum {remote_path}", adb_path=adb_path
        )
    except AdbError:
        return None
    token = out.split()[0] if out.split() else ""
    return token.lower() if len(token) == 64 else None


def local_sha256(path: str) -> str:
    """本地文件 sha256（流式，文件可能较大）。"""
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
