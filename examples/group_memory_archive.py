"""Enable a local group archive or stage exported messages for manual processing in Studio."""
import argparse
from contextlib import closing
import json
from pathlib import Path
from whale_core.memory.group_refresh import enable, export_rows, stage
from whale_core.memory.sqlite_store import SqliteMemoryStore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config-dir", type=Path, required=True)
    parser.add_argument("--group-id", required=True)
    parser.add_argument("--roster", type=Path, required=True)
    parser.add_argument("exports", nargs="*", type=Path)
    args = parser.parse_args()
    if not (args.config_dir/"memory.sqlite3").is_file():
        parser.error("请使用已初始化的实例目录")
    roster = json.loads(args.roster.read_text(encoding="utf-8-sig"))
    archive = args.config_dir/"group_archive.sqlite3"
    with closing(SqliteMemoryStore(args.config_dir/"memory.sqlite3")) as store:
        enable(store, archive, args.group_id, roster)
        added = sum(stage(store,archive,args.group_id,export_rows(p,args.group_id)) for p in args.exports)
    print(f"{added} 条新增发言等待手动更新")


if __name__ == "__main__":
    main()
