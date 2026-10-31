"""issue #28：``whale qq`` CLI 入口（转发 examples 运行器 + 单实例 flock）。

全离线：``--store :memory: --inbox none``；``--secrets`` 指向不存在路径，
防止误载开发者本机的 secrets/qq_workflow.env；锁路径 monkeypatch 到
tmp_path，不碰生产 /tmp/whale-qq-runner.lock。
"""

from __future__ import annotations

import os

import pytest

from whale_core.cli import qq as qq_cli
from whale_core.cli.main import main as cli_main

ENV_KEYS = (
    "WEQ_SSE_TOKEN", "QQ_LIVE_AUTOSEND", "QQ_SELF_UIN", "QQ_SELF_UID",
    "OWNER_QQ", "WEQ_WEB_TOKEN", "WEQ_WEB_URL", "OWNER_UID",
    "WHALE_QQ_GROUP_ALLOW", "WQ_PUSH_PORT", "QQ_FULLMODE_ONDEMAND",
    "WHALE_QQ_STRANGER_CHAT",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ENV_KEYS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def tmp_lock_path(monkeypatch, tmp_path):
    monkeypatch.setattr(qq_cli, "LOCK_PATH", tmp_path / "whale-qq-runner.lock")


def _store_args(tmp_path) -> list[str]:
    return ["--store", ":memory:", "--secrets", str(tmp_path / "no-such.env")]


def _run_args(tmp_path, *extra: str) -> list[str]:
    return ["run", *_store_args(tmp_path), "--inbox", "none", *extra]


# ── 入口转发 ────────────────────────────────────────────────


def test_qq_run_help_available(capsys):
    """whale qq run --help 可用：转发到运行器的 run 子命令帮助，退出码 0；
    求助路径不取单实例锁。"""
    with pytest.raises(SystemExit) as exc:
        cli_main(["qq", "run", "--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "--live-auto-send" in out and "--auto-approve" in out
    assert not qq_cli.LOCK_PATH.exists()  # --help 未触发 flock


def test_qq_top_level_help(capsys):
    with pytest.raises(SystemExit) as exc:
        cli_main(["qq", "--help"])
    assert exc.value.code == 0


def test_qq_forwards_show_subcommand(tmp_path, capsys):
    """非 run 子命令原样转发且不加锁：show 在空存储上列出空草稿。"""
    rc = cli_main(["qq", "show", *_store_args(tmp_path)])
    assert rc == 0
    assert "（没有草稿）" in capsys.readouterr().out
    assert not qq_cli.LOCK_PATH.exists()


# ── 双开关纪律（issue #12 经 CLI 入口不变）─────────────────


def test_live_auto_send_refused_without_env_switch(tmp_path, capsys, monkeypatch):
    """只有 --live-auto-send、缺 QQ_LIVE_AUTOSEND=1：fail closed，退出码 2。"""
    monkeypatch.setenv("WEQ_SSE_TOKEN", "synthetic-token")
    rc = cli_main(["qq", *_run_args(tmp_path, "--live-auto-send")])
    assert rc == 2
    assert "QQ_LIVE_AUTOSEND=1" in capsys.readouterr().out


def test_live_auto_send_refused_without_weq_web_trio(tmp_path, capsys, monkeypatch):
    """双开关齐全但 WEQ_WEB_* 未配齐：仍 fail closed，退出码 2。"""
    monkeypatch.setenv("WEQ_SSE_TOKEN", "synthetic-token")
    monkeypatch.setenv("QQ_LIVE_AUTOSEND", "1")
    rc = cli_main(["qq", *_run_args(tmp_path, "--live-auto-send")])
    assert rc == 2
    assert "WEQ_WEB_TOKEN/WEQ_WEB_URL/OWNER_UID" in capsys.readouterr().out


def test_default_run_is_not_live(tmp_path, capsys, monkeypatch):
    """默认（无任何开关）不会因环境变量配齐而隐式自动发送——
    运行器只在 --live-auto-send 显式给出后才检查双开关。此处验证
    互斥参数组合直接拒绝：--auto-approve 与 --live-auto-send 同给退出码 2。"""
    monkeypatch.setenv("WEQ_SSE_TOKEN", "synthetic-token")
    monkeypatch.setenv("QQ_LIVE_AUTOSEND", "1")
    rc = cli_main(["qq", *_run_args(tmp_path, "--auto-approve", "--live-auto-send")])
    assert rc == 2
    assert "互斥" in capsys.readouterr().out


# ── 单实例锁 ────────────────────────────────────────────────


def test_run_lock_conflict_exit_3(tmp_path, capsys):
    """已有实例持有 flock：冲突即退出码 3 + 明确错误（不进入运行器）。"""
    fcntl = pytest.importorskip("fcntl")
    fd = os.open(str(qq_cli.LOCK_PATH), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        rc = cli_main(["qq", *_run_args(tmp_path)])
    finally:
        os.close(fd)
    assert rc == qq_cli.LOCK_CONFLICT_EXIT == 3
    assert "锁" in capsys.readouterr().err


def test_run_acquires_lock_before_runner_checks(tmp_path, monkeypatch):
    """run 取得锁后才进入运行器（即便随后因缺 WEQ_SSE_TOKEN 失败退出）。"""
    rc = cli_main(["qq", *_run_args(tmp_path)])
    assert rc == 2  # 运行器 fail closed：WEQ_SSE_TOKEN 未配置
    assert qq_cli.LOCK_PATH.exists()  # 锁文件已由 CLI 入口创建
