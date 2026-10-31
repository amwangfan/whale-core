"""Frozen command routing is tested without running a GUI or freezing per test."""
import importlib
import json
from types import SimpleNamespace

from whale_core.cli import frozen_entry


def test_version_and_help(capsys):
    assert frozen_entry.main(["--version"]) == 0
    assert frozen_entry.__version__ in capsys.readouterr().out
    assert frozen_entry.main(["--help"]) == 0
    assert "No separately installed Python" in capsys.readouterr().out


def test_no_arguments_uses_browser_desktop(monkeypatch):
    desktop = importlib.import_module("whale_core.cli.desktop")
    observed = {}
    monkeypatch.setattr(desktop, "run_desktop", lambda **kwargs: observed.update(kwargs) or 0)
    assert frozen_entry.main([]) == 0
    assert observed == {"browser_only": True}


def test_regular_commands_delegate(monkeypatch):
    cli = importlib.import_module("whale_core.cli.main")
    observed = []
    monkeypatch.setattr(cli, "main", lambda args: observed.extend(args) or 12)
    assert frozen_entry.main(["doctor", "--help"]) == 12
    assert observed == ["doctor", "--help"]


def test_loopback_serve_lifecycle_and_no_token_output(monkeypatch, tmp_path, capsys):
    cli = importlib.import_module("whale_core.cli.main")
    observed = {"started": False, "stopped": False, "closed": False}
    server = SimpleNamespace(url="http://127.0.0.1:45678/api/v1")
    server.start = lambda: observed.update(started=True)
    server.stop = lambda: observed.update(stopped=True)
    server.ctx = SimpleNamespace(store=SimpleNamespace(close=lambda: observed.update(closed=True)))
    def create(args):
        assert args.host == "127.0.0.1" and args.port == 0
        assert args.config_dir == str(tmp_path)
        return server, "secret-should-not-print", "test"
    monkeypatch.setattr(cli, "_build_api_server", create)
    def interrupt():
        raise KeyboardInterrupt
    monkeypatch.setattr(frozen_entry.threading, "Event", lambda: SimpleNamespace(wait=interrupt))
    assert frozen_entry.main(["--serve", "--config-dir", str(tmp_path)]) == 0
    assert all(observed.values())
    output = capsys.readouterr().out
    assert "secret-should-not-print" not in output
    assert json.loads(output)["url"] == server.url


def test_frozen_linux_launcher_survives_spaces_and_metacharacters(tmp_path):
    import os
    from pathlib import Path
    import runpy
    import subprocess
    build = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts" / "freeze_desktop.py"))
    output = tmp_path / 'runtime $literal `not-a-command` % "quoted"'
    executable = output / "whale-core" / "whale-core"
    executable.parent.mkdir(parents=True)
    executable.write_text('#!/bin/sh\nprintf "executable %s\\n" "$1"\n')
    executable.chmod(0o755)
    build["_linux_wrapper"](output, executable)
    result = subprocess.check_output([str(output / "launch-whale-core.sh"), "--version"], text=True)
    assert result.strip() == "executable --version"
    environment = {**os.environ, "XDG_DATA_HOME": str(tmp_path / "user-apps")}
    subprocess.run([str(output / "install-launcher.sh")], env=environment, check=True)
    entry = (tmp_path / "user-apps" / "applications" / "whale-core-frozen.desktop").read_text()
    assert "Terminal=true" in entry
    assert "%%" in entry and r'\$literal' in entry and r'\"quoted\"' in entry
    assert "python" not in entry.lower()


def test_frozen_macos_app_carries_runtime_without_python(tmp_path):
    from pathlib import Path
    import plistlib
    import runpy
    build = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts" / "freeze_desktop.py"))
    executable = tmp_path / "whale-core" / "whale-core"
    executable.parent.mkdir()
    executable.write_text("binary-placeholder")
    (executable.parent / "_internal").mkdir()
    (executable.parent / "_internal" / "module.dat").write_text("bundled-runtime")
    app = build["_mac_wrapper"](tmp_path, executable)
    runtime = app / "Contents" / "Resources" / "runtime"
    assert (runtime / "_internal" / "module.dat").read_text() == "bundled-runtime"
    script = (app / "Contents" / "MacOS" / "WhaleCore").read_text()
    assert "python" not in script.lower()
    assert "quoted form of executablePath" in script
    with (app / "Contents" / "Info.plist").open("rb") as stream:
        assert plistlib.load(stream)["CFBundleExecutable"] == "WhaleCore"
