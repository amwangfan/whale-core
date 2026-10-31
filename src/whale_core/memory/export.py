"""记忆 JSONL 导出 / 导入（格式隐含版本 v1）。

用途：备份、迁移、恢复演练——把 MemoryStore 里的记忆记录落成可 diff、
可传输的文本行，必要时再灌回任意 MemoryStore 后端。

格式（v1）：每行一个 JSON 对象，键固定为
``{"key", "kind", "record_id", "created_at", "tags", "content"}``；
``created_at`` 用 ISO 8601 字符串，``tags`` 为字符串数组，
``content`` 为 JSON 对象；整体 ``ensure_ascii=False``，非 ASCII 原文保留。

边界：只导出 / 导入记忆记录（MemoryRecord，三种 MemoryKind）。
store 的 kv（draft / ledger / quota 等账本数据）不属于记忆，不导出。

导入幂等语义：默认 ``overwrite=False``，目标库已存在同 key 记录时跳过，
重复执行同一导入不会放大已有数据；``overwrite=True`` 时同 key 覆盖写入。

两个函数只依赖 MemoryStore 协议：不 import sqlite3、不碰文件路径，
文件对象（文本模式、utf-8 编码）由调用方负责打开与关闭。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Mapping, TextIO
from uuid import uuid4

from whale_core.memory.store import MemoryKind, MemoryRecord, MemoryStore

__all__ = ["export_jsonl", "import_jsonl"]


def export_jsonl(store: MemoryStore, file_obj: TextIO) -> int:
    """把 store 中全部三类记忆记录按 JSONL 写入 file_obj，返回导出条数。

    kv 数据（draft / ledger / quota 等账本）不属于记忆，不在导出范围。
    """
    count = 0
    for kind in MemoryKind:
        for record in store.list_by_kind(kind):
            line = {
                "key": record.key,
                "kind": record.kind.value,
                "record_id": record.record_id,
                "created_at": record.created_at.isoformat(),
                "tags": list(record.tags),
                "content": dict(record.content),
            }
            file_obj.write(json.dumps(line, ensure_ascii=False) + "\n")
            count += 1
    return count


def import_jsonl(
    store: MemoryStore, file_obj: TextIO, *, overwrite: bool = False
) -> tuple[int, int]:
    """从 JSONL 逐行导入记忆记录，返回 (imported, skipped)。

    空行跳过（不计数）；JSON 畸形、缺 key/kind、kind 不在 MemoryKind、
    content 非 dict 的行跳过并计数（不抛异常）；其余行正常入库。
    目标 key 已存在时：overwrite=False 跳过并计数，overwrite=True 覆盖写入。
    """
    imported = 0
    skipped = 0
    for raw_line in file_obj:
        line = raw_line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            skipped += 1
            continue
        if not isinstance(obj, dict):
            skipped += 1
            continue
        key = obj.get("key")
        kind_value = obj.get("kind")
        content = obj.get("content")
        if not isinstance(key, str) or not key:
            skipped += 1
            continue
        if not isinstance(kind_value, str):
            skipped += 1
            continue
        try:
            kind = MemoryKind(kind_value)
        except ValueError:
            skipped += 1
            continue
        if not isinstance(content, dict):
            skipped += 1
            continue
        if not overwrite and store.get_record(key) is not None:
            skipped += 1
            continue
        store.put_record(_record_from_payload(obj, key=key, kind=kind, content=content))
        imported += 1
    return imported, skipped


def _record_from_payload(
    payload: Mapping[str, Any], *, key: str, kind: MemoryKind, content: dict
) -> MemoryRecord:
    """还原 MemoryRecord；可选字段 record_id / created_at / tags 缺失或畸形时取默认值。"""
    record_id = payload.get("record_id")
    if not isinstance(record_id, str) or not record_id:
        record_id = uuid4().hex
    created_raw = payload.get("created_at")
    created_at: datetime | None = None
    if isinstance(created_raw, str):
        try:
            created_at = datetime.fromisoformat(created_raw)
        except ValueError:
            created_at = None
    if created_at is None:
        created_at = datetime.now(timezone.utc)
    elif created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    tags_raw = payload.get("tags")
    if isinstance(tags_raw, (list, tuple)) and all(isinstance(t, str) for t in tags_raw):
        tags = tuple(tags_raw)
    else:
        tags = ()
    return MemoryRecord(
        kind=kind, key=key, content=content,
        record_id=record_id, created_at=created_at, tags=tags,
    )
