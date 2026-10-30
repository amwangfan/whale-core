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
    kw.setdefault("sandbox", "disabled")
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
    ex = ShellExecutor(PERSONAL, env=env, sandbox="disabled")
    result = ex.run("env", is_owner=True)
    assert result.status == "success"
    assert "should-not-leak" not in result.stdout
    assert "CUSTOM_TOKEN" not in result.stdout


def test_scrub_env_allowlist():
    cleaned = _scrub_env({"PATH": "/bin", "HOME": "/root", "SECRET_X": "s", "OTHER": "x"})
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

@pytest.mark.skipif(os.geteuid() != 0 or shutil.which("id") is None,
                    reason="需要 root 演示降权")
def test_drop_to_nobody():
    result = _executor(drop_to_user="nobody").run("id -un", is_owner=True)
    if result.status == "error":
        # Root in a restricted container may lack CAP_SETUID/SETGID.
        assert result.verdict_reason == "credential transition unavailable"
        assert not result.stdout
    else:
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


def test_default_sandbox_fails_closed_without_bwrap(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    result = ShellExecutor(PERSONAL).run("echo should-not-run", is_owner=True)
    assert result.status == "error"
    assert "sandbox unavailable" in result.verdict_reason
    assert result.stdout == ""


def test_root_sandbox_needs_unprivileged_account(monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/" + name)
    result = ShellExecutor(PERSONAL).run("echo should-not-run", is_owner=True)
    assert result.status == "error"
    assert "non-root" in result.verdict_reason


def test_sandbox_launcher_exposes_only_explicit_workspace(monkeypatch, tmp_path):
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/" + name)
    ex = ShellExecutor(PERSONAL, allowed_cwds=(str(tmp_path),))
    argv = ex._argv("echo safe", str(tmp_path))
    assert "--unshare-all" in argv and "--cap-drop" in argv
    assert "--as=536870912:536870912" in argv
    assert "--bind" in argv and argv[argv.index("--bind") + 1] == str(tmp_path)
    assert "/etc" not in argv and "/root" not in argv
    assert argv[-3:] == ["/bin/sh", "-c", "echo safe"]


def test_unbounded_output_is_killed_with_bounded_capture():
    result = _executor(output_chars=100, max_output_bytes=2048).run(
        "while :; do echo xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx; done", is_owner=True)
    assert result.status == "output_limit"
    assert len(result.stdout) <= 100
    assert result.duration_s < 3


def test_no_cwd_uses_unique_private_directory():
    result = _executor().run("pwd; stat -c %a .", is_owner=True)
    assert result.status == "success"
    assert "whale-shell-" in result.stdout and "700" in result.stdout
    assert not os.path.exists(result.stdout.splitlines()[0])


def test_invalid_execution_limits_are_rejected():
    for kwargs in ({"timeout_s": 0}, {"timeout_s": float("nan")},
                   {"output_chars": 0}, {"max_output_bytes": -1}, {"sandbox": "auto"}):
        with pytest.raises(ValueError):
            ShellExecutor(PERSONAL, **kwargs)


def test_real_namespace_support_is_explicit():
    # This CI container may forbid user namespaces; both outcomes must be honest.
    result = ShellExecutor(PERSONAL).run("echo isolated-whale", is_owner=True)
    if result.status == "success":
        assert result.stdout.strip() == "isolated-whale"
    else:
        assert result.status in ("error", "failed")
        assert "sandbox" in result.verdict_reason
        assert "isolated-whale" not in result.stdout


def test_process_limit_is_installed_inside_required_user_namespace(monkeypatch, tmp_path):
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/" + name)
    ex = ShellExecutor(PERSONAL, max_processes=7)
    argv = ex._argv("echo isolated", str(tmp_path))
    bwrap = argv.index("/usr/bin/bwrap")
    sandbox_command = argv.index("--", bwrap)
    assert "--unshare-user" in argv[bwrap:sandbox_command]
    assert "--disable-userns" in argv[bwrap:sandbox_command]
    assert not any(arg.startswith("--nproc=") for arg in argv[:sandbox_command])
    assert argv[sandbox_command + 1:] == ["/usr/bin/prlimit", "--nproc=7:7", "--",
                                          "/bin/sh", "-c", "echo isolated"]
    # CPU, memory, file and descriptor limits still protect bwrap itself.
    assert "--cpu=30:30" in argv[:bwrap]
    assert "--as=536870912:536870912" in argv[:bwrap]
    assert "--fsize=16777216:16777216" in argv[:bwrap]
    assert "--nofile=64:64" in argv[:bwrap]


def test_trusted_host_shell_never_sets_a_shared_uid_process_limit():
    ex = _executor(max_processes=1)
    argv = ex._argv("sleep 0.01; echo trusted-child", "/tmp")
    assert not any(arg.startswith("--nproc=") for arg in argv)
    # On an ordinary CI account the runner already has multiple same-UID tasks.
    # The former outer --nproc=1 made this real child command fail to fork.
    result = ex.run("sleep 0.01; echo trusted-child", is_owner=True)
    assert result.status == "success", result.as_dict()
    assert result.stderr == ""
    assert result.stdout.strip() == "trusted-child"
