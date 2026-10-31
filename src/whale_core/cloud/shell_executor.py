"""Policy-gated shell execution with fail-closed Linux namespace isolation.

The default ``bubblewrap`` mode requires a usable bwrap/prlimit installation and
an unprivileged service account. It never silently falls back to host execution.
Only explicit ``sandbox='disabled'`` runs trusted commands on the host. Shell
policy is a guardrail, not the sandbox boundary. Output is drained incrementally
and capped before allocation; timeouts kill the complete process group.
"""
from __future__ import annotations

import math
import os
import re
import selectors
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from whale_core.cloud.shell_policy import ShellPolicy, ShellVerdict

DEFAULT_TIMEOUT_S = 30.0
DEFAULT_OUTPUT_CHARS = 4000
_ENV_ALLOW = ("PATH", "HOME", "LANG", "LC_ALL", "TERM", "TZ", "TMPDIR")
_ENV_SECRET_RE = re.compile(r"(?:_KEY|_TOKEN|_SECRET|_PASSWORD|_CREDENTIAL|API_?KEY)", re.IGNORECASE)


@dataclass(frozen=True)
class ExecutionResult:
    status: str  # success | failed | timeout | output_limit | denied | error
    exit_code: int | None
    stdout: str
    stderr: str
    duration_s: float
    verdict_reason: str = ""

    def as_dict(self) -> dict:
        return {"status": self.status, "exit_code": self.exit_code,
                "stdout": self.stdout, "stderr": self.stderr,
                "duration_s": round(self.duration_s, 3), "verdict_reason": self.verdict_reason}


def _scrub_env(env: dict[str, str] | None) -> dict[str, str]:
    source = dict(os.environ if env is None else env)
    return {k: v for k, v in source.items()
            if k in _ENV_ALLOW and not _ENV_SECRET_RE.search(k)}


class ShellExecutor:
    """Constructing an executor does not execute a command.

    ``allowed_cwds`` grants filesystem write access only to the selected directory
    in sandbox mode. No home directory, config, tokens or network are mounted.
    Install Linux ``bubblewrap`` and ``util-linux``; run Whale as a non-root user.
    Namespace restrictions imposed by a container are reported as unavailable.
    """

    def __init__(self, policy: ShellPolicy | None = None, *,
                 timeout_s: float = DEFAULT_TIMEOUT_S,
                 output_chars: int = DEFAULT_OUTPUT_CHARS,
                 allowed_cwds: tuple[str, ...] = (),
                 drop_to_user: str | None = None,
                 env: dict[str, str] | None = None,
                 sandbox: str = "bubblewrap", max_output_bytes: int = 1024 * 1024,
                 memory_bytes: int = 512 * 1024 * 1024,
                 file_bytes: int = 16 * 1024 * 1024, max_processes: int = 32) -> None:
        if sandbox not in ("bubblewrap", "disabled"):
            raise ValueError("sandbox must be bubblewrap or disabled")
        if not math.isfinite(timeout_s) or timeout_s <= 0:
            raise ValueError("timeout_s must be finite and positive")
        limits = (output_chars, max_output_bytes, memory_bytes, file_bytes, max_processes)
        if any(not isinstance(n, int) or isinstance(n, bool) or n <= 0 for n in limits):
            raise ValueError("execution limits must be positive integers")
        self.policy = policy or ShellPolicy.from_env()
        self.timeout_s, self.output_chars = timeout_s, output_chars
        self.allowed_cwds = tuple(str(Path(p).resolve()) for p in allowed_cwds)
        self.drop_to_user, self.env, self.sandbox = drop_to_user, env, sandbox
        self.max_output_bytes, self.memory_bytes = max_output_bytes, memory_bytes
        self.file_bytes, self.max_processes = file_bytes, max_processes

    def verdict(self, command: str, *, is_owner: bool) -> ShellVerdict:
        return self.policy.command_verdict(command, is_owner=is_owner)

    def _sandbox_argv(self, command: str, cwd: str) -> list[str]:
        bwrap = shutil.which("bwrap")
        if not sys.platform.startswith("linux") or not bwrap:
            raise RuntimeError("sandbox unavailable: Linux bubblewrap is required")
        if os.geteuid() == 0 and not self.drop_to_user:
            raise RuntimeError("sandbox unavailable: run Whale as a non-root user or configure drop_to_user")
        prlimit = shutil.which("prlimit")
        if not prlimit:
            raise RuntimeError("execution unavailable: util-linux prlimit is required")
        argv = [bwrap, "--unshare-all", "--unshare-user", "--disable-userns",
                "--die-with-parent", "--new-session",
                "--cap-drop", "ALL", "--proc", "/proc", "--dev", "/dev",
                "--tmpfs", "/tmp", "--dir", "/home", "--dir", "/home/whale"]
        # Runtime libraries only; never bind /, /etc, host /proc, /home or sockets.
        for path in ("/usr", "/bin", "/sbin", "/lib", "/lib64"):
            if Path(path).exists():
                argv.extend(["--ro-bind", path, path])
        # Only the explicitly granted task workspace is writable.
        argv.extend(["--bind", cwd, "/workspace", "--chdir", "/workspace",
                     "--setenv", "HOME", "/home/whale", "--setenv", "TMPDIR", "/tmp",
                     # RLIMIT_NPROC counts tasks per real UID, not per process.
                     # Install it only after bwrap has created its private user/PID
                     # namespaces; an outer limit counts the host service/runner's
                     # existing processes and can prevent even the first fork.
                     "--", prlimit, f"--nproc={self.max_processes}:{self.max_processes}",
                     "--", "/bin/sh", "-c", command])
        return argv

    def _argv(self, command: str, cwd: str) -> list[str]:
        argv = (self._sandbox_argv(command, cwd) if self.sandbox == "bubblewrap"
                else ["/bin/sh", "-c", command])
        # Per-process hard limits can safely wrap the namespace launcher. The
        # per-UID process limit belongs inside _sandbox_argv, never around bwrap
        # or a trusted host shell. Avoid Python preexec_fn in the threaded API.
        if sys.platform.startswith("linux"):
            prlimit = shutil.which("prlimit")
            if not prlimit:
                raise RuntimeError("execution unavailable: util-linux prlimit is required")
            cpu = max(1, math.ceil(self.timeout_s))
            argv = [prlimit, f"--cpu={cpu}:{cpu}",
                    f"--as={self.memory_bytes}:{self.memory_bytes}",
                    f"--fsize={self.file_bytes}:{self.file_bytes}",
                    "--nofile=64:64", "--", *argv]
        return argv

    def run(self, command: str, *, is_owner: bool, cwd: str | None = None) -> ExecutionResult:
        started = time.monotonic()
        verdict = self.verdict(command, is_owner=is_owner)
        if not verdict.allowed:
            return ExecutionResult("denied", None, "", "", time.monotonic() - started, verdict.reason)
        resolved = self._resolve_cwd(cwd)
        if cwd is not None and resolved is None:
            return ExecutionResult("denied", None, "", "", time.monotonic() - started,
                                   "cwd not in whitelist")
        # Each invocation owns a private temporary workspace; no shared /tmp cwd.
        with tempfile.TemporaryDirectory(prefix="whale-shell-") as task_tmp:
            work = resolved or task_tmp
            try:
                kwargs: dict = {}
                if self.drop_to_user:
                    import pwd
                    try:
                        user = pwd.getpwnam(self.drop_to_user)
                    except KeyError:
                        raise RuntimeError("unknown drop user") from None
                    if self.sandbox == "bubblewrap" and user.pw_uid == 0:
                        raise RuntimeError("sandbox unavailable: drop user must be non-root")
                    if os.geteuid() != user.pw_uid:
                        kwargs.update(user=user.pw_uid, group=user.pw_gid, extra_groups=[])
                        if resolved is None and os.geteuid() == 0:
                            os.chown(task_tmp, user.pw_uid, user.pw_gid)
                argv = self._argv(command, work)
                proc = subprocess.Popen(argv, cwd=work, env=_scrub_env(self.env),
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        start_new_session=True, **kwargs)
            except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
                reason = (str(exc) if isinstance(exc, RuntimeError) else
                          "credential transition unavailable" if kwargs else type(exc).__name__)
                return ExecutionResult("error", None, "", "", time.monotonic() - started, reason)
            return self._collect(proc, started)

    @staticmethod
    def _kill(proc: subprocess.Popen) -> None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

    def _collect(self, proc: subprocess.Popen, started: float) -> ExecutionResult:
        buffers = {"stdout": bytearray(), "stderr": bytearray()}
        total, reason, status = 0, "", ""
        # Keep at most four UTF-8 bytes per requested output character per pipe.
        capture_bytes = min(self.output_chars * 4, self.max_output_bytes)
        with selectors.DefaultSelector() as selector:
            for name in buffers:
                stream = getattr(proc, name)
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, name)
            try:
                while selector.get_map() or proc.poll() is None:
                    remaining = self.timeout_s - (time.monotonic() - started)
                    if remaining <= 0:
                        status, reason = "timeout", f"killed after {self.timeout_s}s"
                        break
                    for key, _ in selector.select(min(remaining, .1)):
                        chunk = os.read(key.fileobj.fileno(), 65536)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        total += len(chunk)
                        target = buffers[key.data]
                        target.extend(chunk[:max(0, capture_bytes - len(target))])
                        if total > self.max_output_bytes:
                            status, reason = "output_limit", "killed after output byte limit"
                            break
                    if status:
                        break
            finally:
                # A successful shell may leave detached/background children;
                # terminate its group as well so tasks do not outlive their lease.
                self._kill(proc)
                for name in buffers:
                    getattr(proc, name).close()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=2)
        if not status:
            status = "success" if proc.returncode == 0 else "failed"
            if self.sandbox == "bubblewrap" and proc.returncode != 0:
                reason = "sandbox command failed; check bubblewrap/user namespace support"
        text = {k: bytes(v).decode("utf-8", errors="replace")[:self.output_chars]
                for k, v in buffers.items()}
        return ExecutionResult(status, proc.returncode if status in ("success", "failed") else None,
                               text["stdout"], text["stderr"], time.monotonic() - started, reason)

    def _resolve_cwd(self, cwd: str | None) -> str | None:
        if cwd is None:
            return None
        resolved = str(Path(cwd).resolve())
        if not Path(resolved).is_dir():
            return None
        for base in self.allowed_cwds:
            if resolved == base or resolved.startswith(base + os.sep):
                return resolved
        return None
