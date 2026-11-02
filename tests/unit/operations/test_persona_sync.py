"""角色卡双向同步（cli/persona_sync.py）离线测试。"""

from __future__ import annotations

import json

from whale_core.cli.persona_sync import main
from whale_core.memory.sqlite_store import SqliteMemoryStore
from whale_core.ui.persona_cards import CARD_KEY_PREFIX, PersonaCard


def _seed_store(path):
    store = SqliteMemoryStore(str(path))
    store.put(f"{CARD_KEY_PREFIX}c1", PersonaCard(
        card_id="c1", title="卡一", name="小鲸", summary="设定甲").as_dict())
    store.put(f"{CARD_KEY_PREFIX}c2", PersonaCard(
        card_id="c2", title="卡二", name="老鲸").as_dict())
    store.close()


class TestPersonaSync:
    def test_export_import_roundtrip(self, tmp_path):
        src = tmp_path / "src.db"
        _seed_store(src)
        cards_dir = tmp_path / "personas"
        assert main(["export", "--store", str(src), "--dir",
                     str(cards_dir)]) == 0
        assert (cards_dir / "c1.json").is_file()
        assert (cards_dir / "c2.json").is_file()

        dst = tmp_path / "dst.db"
        dst.touch()
        assert main(["import", "--store", str(dst), "--dir",
                     str(cards_dir)]) == 0
        store = SqliteMemoryStore(str(dst))
        try:
            c1 = store.get(f"{CARD_KEY_PREFIX}c1")
            assert c1["name"] == "小鲸" and c1["summary"] == "设定甲"
            assert store.get(f"{CARD_KEY_PREFIX}c2")["title"] == "卡二"
        finally:
            store.close()

    def test_import_rejects_bad_card(self, tmp_path, capsys):
        cards_dir = tmp_path / "personas"
        cards_dir.mkdir()
        (cards_dir / "broken.json").write_text(
            json.dumps({"card_id": "broken", "title": ""}), encoding="utf-8")
        db = tmp_path / "d.db"
        db.touch()
        rc = main(["import", "--store", str(db), "--dir", str(cards_dir)])
        assert rc == 1
        out = capsys.readouterr().out
        assert "失败" in out and "broken.json" in out

    def test_import_set_active(self, tmp_path):
        src = tmp_path / "s.db"
        _seed_store(src)
        cards_dir = tmp_path / "personas"
        main(["export", "--store", str(src), "--dir", str(cards_dir)])
        dst = tmp_path / "d.db"
        dst.touch()
        assert main(["import", "--store", str(dst), "--dir", str(cards_dir),
                     "--set-active", "c1"]) == 0
        store = SqliteMemoryStore(str(dst))
        try:
            assert store.get("persona:active_card")["card_id"] == "c1"
        finally:
            store.close()

    def test_import_missing_dir(self, tmp_path, capsys):
        rc = main(["import", "--store", str(tmp_path / "x.db"),
                   "--dir", str(tmp_path / "nope")])
        assert rc == 2
