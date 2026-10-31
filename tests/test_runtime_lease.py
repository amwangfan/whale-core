"""PR #4 reconciliation: cross-process lifetime guard from the alternate runtime."""
from __future__ import annotations

import io
import os

import pytest

from whale_core.cli.main import main
from whale_core.core.lease import RuntimeBusy, RuntimeLease


pytestmark = pytest.mark.skipif(os.name != "posix", reason="fcntl advisory locks require POSIX")


def test_second_owner_refused_then_lock_released(tmp_path):
    with RuntimeLease(tmp_path) as first:
        with pytest.raises(RuntimeBusy):
            RuntimeLease(tmp_path)
        assert first is not None
    with RuntimeLease(tmp_path):
        pass
    assert (tmp_path / ".runtime.lock").stat().st_mode & 0o077 == 0


def test_cli_run_refuses_another_host(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("/quit\n"))
    with RuntimeLease(tmp_path):
        assert main(["run", "--config-dir", str(tmp_path)]) == 2
    err = capsys.readouterr().err
    assert "已有 Whale 服务" in err


def test_cli_api_refuses_another_host_without_starting(tmp_path, capsys):
    with RuntimeLease(tmp_path):
        assert main(["api", "--port", "0", "--config-dir", str(tmp_path)]) == 2
    assert "已有 Whale 服务" in capsys.readouterr().err


def test_live_restore_refused_before_opening_backup(tmp_path, capsys):
    missing = tmp_path / "missing.sqlite"
    with RuntimeLease(tmp_path):
        assert main(["memory", "restore", str(missing), "--config-dir", str(tmp_path)]) == 2
    assert not (tmp_path / "memory.sqlite3").exists()
    assert "恢复前" in capsys.readouterr().err
