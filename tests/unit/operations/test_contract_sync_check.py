"""issue #46：跨仓契约漂移校验脚本（scripts/contract_sync_check.py）离线单测。"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPT = (
    Path(__file__).resolve().parents[3]
    / "scripts" / "contract_sync_check.py"
)


def _load():
    spec = importlib.util.spec_from_file_location("contract_sync_check", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_tree(root: Path, files: dict) -> None:
    for rel, content in files.items():
        p = root / "contracts" / "v1" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")


BASE = {
    "PROTOCOL.md": "# Protocol\n",
    "messages/sensor.event.schema.json": '{"type": "object"}',
    "examples/sensor.event.json": '{"type": "sensor.event"}',
}


class TestContractSyncCheck:
    def test_in_sync(self, tmp_path, capsys):
        mod = _load()
        _make_tree(tmp_path / "a", BASE)
        _make_tree(tmp_path / "b", BASE)
        assert mod.main(["--a", str(tmp_path / "a"), "--b", str(tmp_path / "b")]) == 0
        assert "一致" in capsys.readouterr().out

    def test_content_drift_detected(self, tmp_path, capsys):
        mod = _load()
        _make_tree(tmp_path / "a", BASE)
        changed = dict(BASE, **{"PROTOCOL.md": "# Protocol v2\n"})
        _make_tree(tmp_path / "b", changed)
        rc = mod.main(["--a", str(tmp_path / "a"), "--b", str(tmp_path / "b")])
        assert rc == 1
        assert "DIFFERS    PROTOCOL.md" in capsys.readouterr().out

    def test_missing_file_detected(self, tmp_path, capsys):
        mod = _load()
        _make_tree(tmp_path / "a", BASE)
        _make_tree(tmp_path / "b", {k: v for k, v in BASE.items() if "examples" not in k})
        rc = mod.main(["--a", str(tmp_path / "a"), "--b", str(tmp_path / "b")])
        assert rc == 1
        assert "ONLY-IN-A" in capsys.readouterr().out

    def test_out_of_scope_files_ignored(self, tmp_path):
        mod = _load()
        _make_tree(tmp_path / "a", {**BASE, "README.md": "meta A\n"})
        _make_tree(tmp_path / "b", {**BASE, "README.md": "meta B\n"})
        assert mod.main(["--a", str(tmp_path / "a"), "--b", str(tmp_path / "b")]) == 0

    def test_missing_contracts_dir_is_error(self, tmp_path, capsys):
        mod = _load()
        _make_tree(tmp_path / "a", BASE)
        (tmp_path / "b").mkdir()
        rc = mod.main(["--a", str(tmp_path / "a"), "--b", str(tmp_path / "b")])
        assert rc == 2
        assert "缺少 contracts/v1" in capsys.readouterr().err

    def test_requires_pair(self, tmp_path):
        mod = _load()
        try:
            mod.main(["--a", str(tmp_path)])
        except SystemExit as exc:
            assert exc.code == 2
        else:  # pragma: no cover
            raise AssertionError("expected SystemExit")
