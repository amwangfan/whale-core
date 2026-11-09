"""Decode byte pages and assemble output without losing UTF-8 or looping forever."""
from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from typing import Awaitable, Callable

MAX_PAGE_BYTES = 48 * 1024


@dataclass(frozen=True)
class OutputPage:
    data: bytes
    truncated: bool
    next_offset: int | None
    total_bytes: int | None
    source_truncated: bool = False


def decode_page(result: dict, offset: int, limit: int) -> OutputPage:
    data = result.get("data")
    if not isinstance(data, list) or not data or not isinstance(data[0], dict):
        raise ValueError("device.output.read 缺少输出对象")
    item = data[0]
    if isinstance(item.get("text"), str):
        chunk = item["text"].encode("utf-8")
    else:
        encoded = item.get("data_base64", item.get("bytes_b64"))
        if not isinstance(encoded, str):
            raise ValueError("device.output.read 未声明输出编码")
        try:
            chunk = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("device.output.read base64 无效") from exc
    truncated = bool(result.get("truncated") or item.get("truncated"))
    next_offset, total = item.get("next_offset"), item.get("total_bytes")
    if next_offset is not None or total is not None:
        start = item.get("offset")
        if any(type(n) is not int or n < 0 for n in (start, next_offset, total)):
            raise ValueError("输出分页元数据无效")
        eof = not chunk and not truncated and start == total and offset >= total
        if (not eof and start != offset) or next_offset != start + len(chunk):
            raise ValueError("输出分页偏移不连续")
        if len(chunk) > limit or next_offset > total or truncated != (next_offset < total):
            raise ValueError("输出分页长度或截断标记无效")
    return OutputPage(chunk, truncated, next_offset, total, item.get("source_truncated") is True)


async def collect_pages(fetch: Callable[[int, int], Awaitable[OutputPage]], *,
                        max_bytes: int = 1024 * 1024) -> bytes:
    if type(max_bytes) is not int or max_bytes <= 0:
        raise ValueError("max_bytes 必须为正整数")
    output = bytearray()
    expected_total = None
    while True:
        page = await fetch(len(output), min(MAX_PAGE_BYTES, max_bytes - len(output)))
        if page.source_truncated:
            raise ValueError("输出源已截断，无法组装完整输出")
        if len(output) + len(page.data) > max_bytes:
            raise ValueError("输出超过读取预算")
        if expected_total is not None and page.total_bytes != expected_total:
            raise ValueError("读取期间输出长度发生变化")
        expected_total = page.total_bytes
        output.extend(page.data)
        if not page.truncated:
            return bytes(output)
        if not page.data or page.next_offset != len(output):
            raise ValueError("输出分页没有前进或数据源不支持分页")
        if len(output) >= max_bytes:
            raise ValueError("输出超过读取预算")
