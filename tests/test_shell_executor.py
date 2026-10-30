"""cloud/shell_executor.py 受控执行器测试（真实子进程，本机回环；不触网）。"""

from __future__ import annotations

import os
import shutil

import pytest

from whale_core.cloud.shell_executor import ShellExecutor, _scrub_env
from whale_core.cloud.shell_policy import ShellPolicy

PERSONAL = ShellPolicy(tier="personal")


def _executor(**kw) -> ShellExecutor:
    kw.setdefault("timeout_s", 5.0)
    return ShellExecutor(PERSONAL, **kw)


# ---------- 策略前置 ----------


def test_policy_denied_before_spawn():
    result = _executor().run("rm -rf /", is_owner=True)
    assert result.status == "denied"
    assert "destructive_rm" in result.verdict_reason
    assert result.exit_code is None


def test_owner_gate():
    result = _executor().run("echo hi", is_owner=False)
    assert result.status == "denied"
    assert "owner" in result.verdict_reason


def test_managed_tier_denies_free_command():
    ex = ShellExecutor(ShellPolicy(tier="managed"))
    result = ex.run("echo hi", is_owner=True)
    assert result.status == "denied"


# ---------- 真实执行 ----------


def test_echo_success():
    result = _executor().run("echo hello-whale", is_owner=True)
    assert result.status == "success"
    assert result.exit_code == 0
    assert result.stdout.strip() == "hello-whale"
    assert result.duration_s < 5.0


def test_nonzero_exit_is_failed():
    result = _executor().run("exit 3", is_owner=True)
    assert result.status == "failed"
    assert result.exit_code == 3


def test_stderr_captured():
    result = _executor().run("echo oops >&2", is_owner=True)
    assert "oops" in result.stderr


def test_timeout_kills_process_group():
    ex = _executor(timeout_s=0.5)
    result = ex.run("sleep 30", is_owner=True)
    assert result.status == "timeout"
    assert result.duration_s < 5.0  # 强杀生效，没等满 30s


def test_output_truncated():
    ex = _executor(output_chars=200)
    result = ex.run("seq 1 100000", is_owner=True)
    assert len(result.stdout) == 200


# ---------- 环境擦洗 ----------


def test_env_scrubbed_of_secrets():
    env = dict(os.environ)
    env["MY_API_KEY"] = "should-not-leak"
    env["CUSTOM_TOKEN"] = "nope"
    ex = ShellExecutor(PERSONAL, env=env)
    result = ex.run("env", is_owner=True)
    assert result.status == "success"
    assert "should-not-leak" not in result.stdout
    assert "CUSTOM_TOKEN" not in result.stdout


def test_scrub_env_allowlist():
    cleaned = _scrub_env(
        {"PATH": "/bin", "HOME": "/root", "SECRET_X": "s", "OTHER": "x"}
    )
    assert cleaned == {"PATH": "/bin", "HOME": "/root"}


# ---------- cwd 白名单 ----------


def test_cwd_whitelist(tmp_path):
    ex = _executor(allowed_cwds=(str(tmp_path),))
    ok = ex.run("pwd", is_owner=True, cwd=str(tmp_path))
    assert ok.status == "success" and str(tmp_path) in ok.stdout
    bad = ex.run("pwd", is_owner=True, cwd="/etc")
    assert bad.status == "denied"


def test_default_cwd_is_tmpdir():
    result = _executor().run("pwd", is_owner=True)
    assert result.status == "success"


# ---------- 降权 ----------


@pytest.mark.skipif(
    os.geteuid() != 0 or shutil.which("id") is None, reason="需要 root 演示降权"
)
def test_drop_to_nobody():
    # Container UID 0 may lack SETUID/SETGID; being root alone is not proof
    # of permission. Exercise this runtime's failure path separately below.
    cap_path = __import__("pathlib").Path("/proc/self/status")
    if cap_path.exists():
        cap = next(
            (
                line.split()[1]
                for line in cap_path.read_text().splitlines()
                if line.startswith("CapEff:")
            ),
            "0",
        )
        if int(cap, 16) & 0xC0 != 0xC0:
            pytest.skip("host lacks CAP_SETUID/CAP_SETGID")
    result = _executor(drop_to_user="nobody").run("id -un", is_owner=True)
    assert result.status == "success"
    assert result.stdout.strip() == "nobody"


def test_drop_to_unknown_user_errors():
    result = _executor(drop_to_user="no_such_user_xyz").run("id", is_owner=True)
    assert result.status == "error"
    assert "unknown drop user" in result.verdict_reason


def test_invalid_bytes_in_output_do_not_escape():
    """安全审查回归：非法字节输出经 errors=replace 不抛 UnicodeDecodeError。"""
    result = _executor().run("printf '\\377\\376abc'", is_owner=True)
    assert result.status == "success"
    assert "abc" in result.stdout


def test_drop_permission_failure_is_classified(monkeypatch):
    import subprocess

    def denied(*args, **kwargs):
        raise PermissionError("private details")

    monkeypatch.setattr(subprocess, "Popen", denied)
    result = _executor(drop_to_user="nobody").run("id", is_owner=True)
    assert result.status == "error"
    assert result.verdict_reason == "PermissionError"
    assert "private details" not in result.stderr
