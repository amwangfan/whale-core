"""角色卡与 GitHub 双向同步（owner 决策 2026-11-01：卡面大换新）。

- ``whale persona export --store DB --dir DIR``：把库里全部自定义卡
  （kv ``persona:card:*``）写成 ``DIR/<card_id>.json``；目录里多余文件
  不删（Git 负责历史），只覆盖/新增。
- ``whale persona import --store DB --dir DIR``：把 ``DIR/*.json`` 逐张校验
  后写入库（同 id 覆盖）；``--set-active <id>`` 顺带切换活跃卡。
- 纪律：严格字段表复用 PersonaCard.from_dict；非法卡报错不静默跳过；
  不写任何密钥；导入导出都有逐卡清单输出。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from whale_core.memory.sqlite_store import SqliteMemoryStore
from whale_core.ui.persona_cards import (
    ACTIVE_CARD_KEY,
    CARD_KEY_PREFIX,
    PersonaCard,
    _load_stored_card,
)


def _open_store(path: str) -> SqliteMemoryStore:
    return SqliteMemoryStore(path)


def cmd_export(args: argparse.Namespace) -> int:
    store = _open_store(args.store)
    out_dir = Path(args.dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    try:
        for key in sorted(store.keys()):
            if not key.startswith(CARD_KEY_PREFIX):
                continue
            card_id = key[len(CARD_KEY_PREFIX):]
            card = _load_stored_card(store, card_id)
            if card is None:
                print(f"[跳过] {card_id}：卡数据损坏")
                continue
            path = out_dir / f"{card_id}.json"
            path.write_text(
                json.dumps(card.as_dict(), ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8")
            written.append(card_id)
    finally:
        store.close()
    print(f"导出 {len(written)} 张卡到 {out_dir}：{', '.join(written)}")
    return 0


def cmd_import(args: argparse.Namespace) -> int:
    in_dir = Path(args.dir)
    if not in_dir.is_dir():
        print(f"目录不存在：{in_dir}")
        return 2
    files = sorted(in_dir.glob("*.json"))
    if not files:
        print(f"{in_dir} 里没有 .json 卡")
        return 2
    store = _open_store(args.store)
    ok, bad = [], []
    try:
        for path in files:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                card = PersonaCard.from_dict(data)
                if card.card_id != path.stem:
                    raise ValueError(
                        f"文件名 {path.stem} 与 card_id {card.card_id} 不一致")
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                bad.append((path.name, str(exc)[:120]))
                continue
            store.put(f"{CARD_KEY_PREFIX}{card.card_id}", card.as_dict())
            ok.append(card.card_id)
        if args.set_active:
            target = args.set_active
            if _load_stored_card(store, target) is None:
                print(f"[错误] --set-active 的卡不存在：{target}")
                return 1
            store.put(ACTIVE_CARD_KEY, {"card_id": target, "previous": "",
                                        "at": ""})
            print(f"活跃卡已切换：{target}")
    finally:
        store.close()
    print(f"导入 {len(ok)} 张：{', '.join(ok)}")
    for name, err in bad:
        print(f"[失败] {name}：{err}")
    return 1 if bad else 0


def main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(prog="whale persona")
    sub = parser.add_subparsers(dest="persona_command", required=True)
    for name, help_text in (("export", "库 → 目录（Git 入库用）"),
                            ("import", "目录 → 库（Git 检出后应用）")):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--store", required=True, help="sqlite 库路径")
        p.add_argument("--dir", required=True, help="卡目录（如 personas/）")
        if name == "import":
            p.add_argument("--set-active", default="",
                           help="导入后切换活跃卡为该 id")
        p.set_defaults(func=cmd_export if name == "export" else cmd_import)
    args = parser.parse_args(list(argv))
    return int(args.func(args))
