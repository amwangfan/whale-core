"""whale CLI 测试：setup / doctor / run / plugins / memory（全离线）。

用 tmp_path 作 --config-dir，capsys 抓输出，monkeypatch 控环境变量与 stdin。
覆盖冻结契约：setup 三档与覆盖语义、doctor 退出码与前缀、plugins 全子命令、
memory 导出导入往返、run REPL（替身模型）历史累积、argparse SystemExit。
"""

from __future__ import annotations

import io
import json

import pytest

from whale_core.cli.main import main
from whale_core.memory.history import load_history
from whale_core.memory.sqlite_store import SqliteMemoryStore
from whale_core.core.deepseek import DeepSeekCompanion
from whale_core.memory.store import MemoryKind, MemoryRecord

# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------

ECHO_MANIFEST = {
    "plugin_id": "echo",
    "name": "回声插件（测试）",
    "version": "0.1.0",
    "actions": ["ping"],
    "action_risk": {"ping": "read_only"},
    "config_schema": {
        "properties": {
            "api_token": {"type": "string", "format": "secret"},
            "endpoint": {"type": "string"},
        }
    },
}


def _write_manifest(config_dir, data, filename=None):
    manifests = config_dir / "manifests"
    manifests.mkdir(parents=True, exist_ok=True)
    pid = data.get("plugin_id", "plugin") if isinstance(data, dict) else "bad"
    file = manifests / (filename or f"{pid}.json")
    if isinstance(data, str):
        file.write_text(data, encoding="utf-8")
    else:
        file.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return file


def _read_config(config_dir):
    return json.loads((config_dir / "config.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# setup
# ---------------------------------------------------------------------------


def test_setup_local_writes_config(tmp_path, capsys):
    rc = main(["setup", "--profile", "local", "--config-dir", str(tmp_path)])
    assert rc == 0
    config = _read_config(tmp_path)
    assert config["profile"] == "local"
    assert config["model"]["base_url"]
    assert (tmp_path / "memory.sqlite3").is_file()
    readme = tmp_path / "manifests" / "README.txt"
    assert readme.is_file()
    assert "plugin_id" in readme.read_text(encoding="utf-8")
    out = capsys.readouterr().out
    assert "local" in out


def test_setup_repeat_does_not_overwrite(tmp_path, capsys):
    assert main(["setup", "--profile", "local", "--config-dir", str(tmp_path)]) == 0
    config_path = tmp_path / "config.json"
    config_path.write_text('{"profile": "local", "sentinel": true}', encoding="utf-8")
    rc = main(
        [
            "setup",
            "--profile",
            "local",
            "--config-dir",
            str(tmp_path),
            "--non-interactive",
        ]
    )
    assert rc == 0
    assert _read_config(tmp_path).get("sentinel") is True
    assert "--force" in capsys.readouterr().out


def test_setup_force_overwrites(tmp_path):
    assert main(["setup", "--profile", "local", "--config-dir", str(tmp_path)]) == 0
    (tmp_path / "config.json").write_text('{"profile": "cloud"}', encoding="utf-8")
    rc = main(["setup", "--profile", "local", "--config-dir", str(tmp_path), "--force"])
    assert rc == 0
    config = _read_config(tmp_path)
    assert config["profile"] == "local"
    assert "sentinel" not in config


def test_setup_cloud_without_env_key_fails(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    rc = main(["setup", "--profile", "cloud", "--config-dir", str(tmp_path)])
    assert rc == 2
    assert not (tmp_path / "config.json").exists()
    assert "DEEPSEEK_API_KEY" in capsys.readouterr().err


def test_setup_cloud_with_key_stores_env_reference_not_key(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-live-secret-123")
    rc = main(["setup", "--profile", "cloud", "--config-dir", str(tmp_path)])
    assert rc == 0
    raw = (tmp_path / "config.json").read_text(encoding="utf-8")
    config = json.loads(raw)
    assert config["profile"] == "cloud"
    assert config["model"]["api_key_env"] == "DEEPSEEK_API_KEY"
    assert "sk-live-secret-123" not in raw  # 密钥本体绝不落盘


def test_setup_non_interactive_defaults_to_local(tmp_path):
    rc = main(["setup", "--config-dir", str(tmp_path), "--non-interactive"])
    assert rc == 0
    assert _read_config(tmp_path)["profile"] == "local"


def test_setup_local_profile(tmp_path):
    rc = main(["setup", "--profile", "local", "--config-dir", str(tmp_path)])
    assert rc == 0
    config = _read_config(tmp_path)
    assert config["profile"] == "local"
    assert config["model"]["base_url"]


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------


def test_doctor_all_ok_after_local_setup(tmp_path, capsys):
    assert main(["setup", "--profile", "local", "--config-dir", str(tmp_path)]) == 0
    capsys.readouterr()
    rc = main(["doctor", "--config-dir", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "[ok]" in out
    assert "[fail]" not in out


def test_doctor_missing_config_fails(tmp_path, capsys):
    rc = main(["doctor", "--config-dir", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "[fail] config.json" in out


def test_doctor_cloud_missing_key_fails(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-x")
    assert main(["setup", "--profile", "cloud", "--config-dir", str(tmp_path)]) == 0
    monkeypatch.delenv("DEEPSEEK_API_KEY")
    rc = main(["doctor", "--config-dir", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "DEEPSEEK_API_KEY" in out  # 提示 env 引用名
    assert "key 未配置" in out


def test_doctor_cloud_with_key_ok_and_no_echo(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-live-secret-123")
    assert main(["setup", "--profile", "cloud", "--config-dir", str(tmp_path)]) == 0
    rc = main(["doctor", "--config-dir", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "key 已配置" in out
    assert "sk-live-secret-123" not in out  # 不回显 key 本体


# ---------------------------------------------------------------------------
# plugins
# ---------------------------------------------------------------------------


def test_plugins_list_builtin_catalog(tmp_path, capsys):
    rc = main(["plugins", "list", "--config-dir", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "ID" in out and "STATUS" in out
    assert "local_model" in out
    assert "speech_asr" in out
    assert "web_fetch" in out


def test_plugins_list_tolerates_bad_manifest(tmp_path, capsys):
    _write_manifest(tmp_path, ECHO_MANIFEST)
    _write_manifest(tmp_path, "{not valid json", filename="broken.json")
    rc = main(["plugins", "list", "--config-dir", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 0  # 坏清单记 errors，不炸
    assert "[warn]" in out and "broken.json" in out
    assert "echo" in out and "loaded" in out


def test_plugins_enable_disable_flow(tmp_path, capsys):
    _write_manifest(tmp_path, ECHO_MANIFEST)
    rc = main(["plugins", "enable", "echo", "--config-dir", str(tmp_path)])
    assert rc == 0
    assert "enabled" in capsys.readouterr().out
    # 状态持久化：新进程语义（重新装配）仍 enabled
    rc = main(["plugins", "list", "--config-dir", str(tmp_path)])
    assert rc == 0
    assert "enabled" in capsys.readouterr().out
    rc = main(["plugins", "disable", "echo", "--config-dir", str(tmp_path)])
    assert rc == 0
    assert "disabled" in capsys.readouterr().out
    rc = main(["plugins", "list", "--config-dir", str(tmp_path)])
    assert "disabled" in capsys.readouterr().out


def test_plugins_info_masks_secrets(tmp_path, capsys):
    _write_manifest(tmp_path, ECHO_MANIFEST)
    store = SqliteMemoryStore(tmp_path / "memory.sqlite3")
    store.put(
        "plugin:config:echo",
        {"api_token": "sk-plugin-secret", "endpoint": "https://api.example.com"},
    )
    store.close()
    rc = main(["plugins", "info", "echo", "--config-dir", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "回声插件" in out
    assert "***" in out
    assert "sk-plugin-secret" not in out  # 脱敏视图
    assert "https://api.example.com" in out


def test_plugins_info_unknown_plugin(tmp_path, capsys):
    rc = main(["plugins", "info", "ghost", "--config-dir", str(tmp_path)])
    assert rc == 1
    assert "ghost" in capsys.readouterr().err


def test_plugins_enable_unknown_plugin(tmp_path):
    assert main(["plugins", "enable", "ghost", "--config-dir", str(tmp_path)]) == 1


def test_plugins_doctor_healthcheck(tmp_path, capsys):
    _write_manifest(tmp_path, ECHO_MANIFEST)
    assert main(["plugins", "enable", "echo", "--config-dir", str(tmp_path)]) == 0
    rc = main(["plugins", "doctor", "echo", "--config-dir", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "echo" in out and "unknown" in out  # NullPluginExecutor 无 healthcheck


def test_plugins_doctor_missing_plugin(tmp_path):
    assert main(["plugins", "doctor", "ghost", "--config-dir", str(tmp_path)]) == 1


# ---------------------------------------------------------------------------
# memory export / import
# ---------------------------------------------------------------------------


def _seed_records(config_dir):
    store = SqliteMemoryStore(config_dir / "memory.sqlite3")
    for i in range(3):
        store.put_record(
            MemoryRecord(
                kind=MemoryKind.EPISODE,
                key=f"ep:{i}",
                content={"summary": f"记忆 {i}"},
                tags=("chat",),
            )
        )
    store.close()


def test_memory_export_import_roundtrip(tmp_path, capsys):
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.mkdir()
    _seed_records(src)
    export_file = tmp_path / "backup.jsonl"
    rc = main(["memory", "export", str(export_file), "--config-dir", str(src)])
    assert rc == 0
    assert "exported 3 records" in capsys.readouterr().out
    rc = main(["memory", "import", str(export_file), "--config-dir", str(dst)])
    assert rc == 0
    assert "imported 3, skipped 0" in capsys.readouterr().out
    store = SqliteMemoryStore(dst / "memory.sqlite3")
    assert store.get_record("ep:1").content["summary"] == "记忆 1"
    store.close()


def test_memory_import_skip_then_overwrite(tmp_path, capsys):
    src = tmp_path / "src"
    src.mkdir()
    _seed_records(src)
    export_file = tmp_path / "backup.jsonl"
    assert main(["memory", "export", str(export_file), "--config-dir", str(src)]) == 0
    capsys.readouterr()
    dst = tmp_path / "dst"
    assert main(["memory", "import", str(export_file), "--config-dir", str(dst)]) == 0
    # 重复导入：默认跳过
    rc = main(["memory", "import", str(export_file), "--config-dir", str(dst)])
    assert rc == 0
    assert "imported 0, skipped 3" in capsys.readouterr().out
    # --overwrite：同 key 覆盖写入
    rc = main(
        ["memory", "import", str(export_file), "--overwrite", "--config-dir", str(dst)]
    )
    assert rc == 0
    assert "imported 3, skipped 0" in capsys.readouterr().out


def test_memory_import_missing_file(tmp_path, capsys):
    rc = main(
        [
            "memory",
            "import",
            str(tmp_path / "nope.jsonl"),
            "--config-dir",
            str(tmp_path / "cfg"),
        ]
    )
    assert rc == 1
    assert "不存在" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# run（demo REPL）
# ---------------------------------------------------------------------------


def _fake_model_reply(self, text, *, persona="", memories=(), history=()):
    return "[double] 鲸鱼收到：" + text[:50]  # 测试替身（非产品 mock 模式）


def test_run_repl_and_history(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("你好鲸鱼\n今天天气不错\n/quit\n"))
    monkeypatch.setattr(DeepSeekCompanion, "reply", _fake_model_reply)
    rc = main(["run", "--config-dir", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert out.count("whale> [double]") == 2
    assert "bye" in out
    store = SqliteMemoryStore(tmp_path / "memory.sqlite3")
    history = load_history(store, "cli")
    store.close()
    assert len(history) == 4  # 两轮：user+assistant 各两条
    assert history[0] == {"role": "user", "content": "你好鲸鱼"}
    assert history[1]["role"] == "assistant"
    assert history[1]["content"].startswith("[double]")


def test_run_repl_eof_exits(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("只说一句\n"))  # 无 /quit，靠 EOF
    monkeypatch.setattr(DeepSeekCompanion, "reply", _fake_model_reply)
    rc = main(["run", "--config-dir", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "[double]" in out


# ---------------------------------------------------------------------------
# argparse 行为
# ---------------------------------------------------------------------------


def test_unknown_command_system_exit(tmp_path):
    with pytest.raises(SystemExit) as exc_info:
        main(["bogus-command"])
    assert exc_info.value.code != 0


def test_missing_plugin_id_system_exit(tmp_path):
    with pytest.raises(SystemExit) as exc_info:
        main(["plugins", "info"])
    assert exc_info.value.code != 0


def test_memory_export_missing_path_system_exit(tmp_path):
    with pytest.raises(SystemExit) as exc_info:
        main(["memory", "export"])
    assert exc_info.value.code != 0


# ---------- whale api（PR-13 收尾） ----------


def test_api_token_resolution(tmp_path, monkeypatch):
    from whale_core.cli.main import _resolve_api_token

    monkeypatch.delenv("WHALE_API_TOKEN", raising=False)
    token, source = _resolve_api_token(tmp_path, env={})
    assert token and "新生成" in source
    import stat

    mode = stat.S_IMODE((tmp_path / "api_token").stat().st_mode)
    assert mode == 0o600
    # 第二次复用同一 token
    token2, source2 = _resolve_api_token(tmp_path, env={})
    assert token2 == token and "新生成" not in source2
    # env 优先
    token3, source3 = _resolve_api_token(tmp_path, env={"WHALE_API_TOKEN": "envtok"})
    assert token3 == "envtok"


def test_build_api_server_local(tmp_path, monkeypatch, capsys):
    from types import SimpleNamespace
    from whale_core.cli.main import _build_api_server

    monkeypatch.delenv("WHALE_API_TOKEN", raising=False)
    (tmp_path / "config.json").write_text('{"profile": "local"}', encoding="utf-8")
    args = SimpleNamespace(config_dir=str(tmp_path), host="127.0.0.1", port=0)
    server, token, source = _build_api_server(args)
    try:
        assert server.port != 0  # 绑定临时端口
        assert token
        out = capsys.readouterr().out
        assert "local 档" in out  # local 档 notes
    finally:
        server.stop()


def test_api_subcommand_registered():
    from whale_core.cli.main import build_parser

    parser = build_parser()
    # api 子命令存在且带 host/port
    actions = [a for a in parser._subparsers._group_actions[0].choices]  # noqa: SLF001
    assert "api" in actions
